# LangChain 模型层：chat model 工厂 + 网关 provider 适配器（T1a 换底盘，自研 HTTP 层就此退役）。
"""LangChain model layer: the chat-model factory and the gateway provider adapter.

This module replaces the retired self-built HTTP provider with standard LangChain chat models,
WITHOUT touching the gateway facade — cache, router, retries, telemetry and the resilience
wrappers keep operating on the same provider protocol. Three pieces:

* :func:`build_chat_model` — the ONE configuration point for which vendor integration backs a
  model id ("Version A"): ``ChatDeepSeek`` by default (key from ``DEEPSEEK_API_KEY``), or
  ``ChatOpenAI`` against any OpenAI-compatible endpoint when a custom ``base_url`` is supplied.
  Both are standard packages from the ``live`` extra, imported lazily so offline runs never
  need them.
* :class:`ChatModelProvider` — adapts a LangChain chat model to the gateway's provider
  protocol (``complete`` / ``complete_with_tools`` / ``model`` / ``supports_tools``), with
  behaviour parity to the retired provider: the output-cap guard raises
  :class:`~owcopilot.llm.gateway.OutputTruncatedError` on ``finish_reason == "length"`` (text
  path only — the tools path deliberately stays tolerant), usage comes from the standard
  ``AIMessage.usage_metadata`` (``input_token_details.cache_read`` feeds the provider
  prefix-cache discount), and errors propagate untranslated — the underlying integrations
  raise the ``openai.*`` exception family, which the shared classifier maps without converting
  types (so deterministic errors remain outside retry/failover/breaker policy).
* :func:`build_chat_model_with_resilience` — the AGENT path's model-call policy: an always-on
  whole-chain deadline/attempt budget plus predicate-aware failover and per-provider circuit
  breakers (the latter two env opt-in). It honors dynamic retry headers while a hard-down primary
  opens independently and the secondary keeps serving.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import urlparse

from langchain_core.language_models import LanguageModelInput
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    convert_to_messages,
)
from langchain_core.runnables import Runnable, RunnableConfig
from pydantic import SecretStr

from .circuit import (
    CircuitOpenError,
    _CircuitState,
    circuit_enabled_from_env,
    circuit_state_from_env,
)
from .execution_policy import LLMExecutionPolicy
from .gateway import (
    LLMDeadlineExceededError,
    OutputTruncatedError,
    ToolCall,
    ToolCallResponse,
    ensure_invocation_time,
    invocation_budget_scope,
    prepare_provider_invocation,
    provider_transport_attempt_cap,
    remaining_invocation_time,
    should_count_breaker_failure,
    should_failover,
)

__all__ = [
    "ChatModelProvider",
    "CircuitBreakerChatModel",
    "CircuitOpenError",
    "DeadlineBudgetChatModel",
    "PolicyFailoverChatModel",
    "build_chat_model",
    "build_chat_model_with_resilience",
    "resolve_chat_provider_kind",
]

_LIVE_INSTALL_HINT = (
    "the real LLM mode needs the LangChain model integrations from the 'live' extra. "
    "Install them with: pip install owcopilot[live]"
)

DEFAULT_TRANSPORT_MAX_RETRIES = 2

logger = logging.getLogger(__name__)

ChatProviderKind = Literal["deepseek", "openai_compatible", "anthropic"]


def resolve_chat_provider_kind(
    provider_kind: str | None,
    base_url: str | None,
) -> ChatProviderKind:
    """Resolve an explicit provider, with a safe compatibility fallback for old configs."""

    normalized = (provider_kind or "").strip().lower()
    if normalized:
        if normalized not in {"deepseek", "openai_compatible", "anthropic"}:
            raise ValueError(f"unsupported chat provider kind: {provider_kind}")
        return cast(ChatProviderKind, normalized)
    hostname = (urlparse(base_url).hostname or "").lower() if base_url else ""
    if hostname == "api.anthropic.com" or hostname.endswith(".anthropic.com"):
        # Existing UI configs created before provider_kind was persisted remain valid.
        return "anthropic"
    return "openai_compatible" if base_url else "deepseek"


def build_chat_model(
    *,
    model: str,
    api_key: str | None = None,
    base_url: str | None = None,
    # Parity with the retired provider: temperature is NOT sent unless explicitly configured
    # (None is excluded from the request payload, so the API-side default applies).
    temperature: float | None = None,
    max_retries: int = DEFAULT_TRANSPORT_MAX_RETRIES,
    timeout: float | None = None,
    max_tokens: int | None = None,
    provider_kind: str | None = None,
    execution_policy: LLMExecutionPolicy | None = None,
) -> BaseChatModel:
    """Build the LangChain chat model behind a real model id — the provider configuration point.

    Default ("Version A"): :class:`langchain_deepseek.ChatDeepSeek`, with the key from
    *api_key* or the ``DEEPSEEK_API_KEY`` env var. A custom *base_url* (any OpenAI-compatible
    endpoint — vLLM, a gateway, OpenAI itself) routes to :class:`langchain_openai.ChatOpenAI`
    instead. No self-built HTTP anywhere: retries (*max_retries*), timeouts and connection
    handling are the standard integrations' own.

    Both integrations belong to the ``live`` extra and are imported lazily; a missing package
    raises a guided :class:`ModuleNotFoundError` naming the install command instead of a bare
    import error.

    Kept off ``langchain.chat_models.init_chat_model``: that unified factory was evaluated as a
    replacement for this function's provider dispatch and is behaviourally equivalent on the
    happy path (it is a thin ``cls(**kwargs)`` per provider, verified directly), but its provider
    resolver (``_get_chat_model_creator``) is process-lifetime ``functools.lru_cache``d — once ANY
    caller anywhere in the process successfully builds a given provider, a later "package not
    installed" simulation for that SAME provider silently returns the cached class instead of
    raising. That breaks the guided-missing-dependency contract this function and
    ``app/actions/connection.py``'s BYO-key probe both rely on (verified: after one real build,
    monkeypatching ``sys.modules["langchain_deepseek"] = None`` no longer raises through
    ``init_chat_model`` at all). This function's own lazy `import` statements re-check on every
    call, so the guided error stays reliable forever, not just before the first successful build.
    """
    if execution_policy is not None:
        max_retries = execution_policy.sdk_max_retries
    resolved_provider = resolve_chat_provider_kind(provider_kind, base_url)
    if resolved_provider == "anthropic":
        try:
            from langchain_anthropic import ChatAnthropic  # noqa: PLC0415 — live extra, lazy
        except ModuleNotFoundError as exc:
            raise ModuleNotFoundError(
                f"langchain-anthropic is not installed; {_LIVE_INSTALL_HINT}"
            ) from exc
        anthropic_kwargs: dict[str, Any] = {
            "model_name": model,
            "base_url": base_url or None,
            "temperature": temperature,
            "max_retries": max_retries,
            "timeout": timeout,
            "max_tokens_to_sample": max_tokens,
        }
        # Omitting the field preserves ChatAnthropic's ANTHROPIC_API_KEY lookup.
        if api_key:
            anthropic_kwargs["api_key"] = SecretStr(api_key)
        return cast(BaseChatModel, ChatAnthropic(**anthropic_kwargs))
    if resolved_provider == "openai_compatible":
        try:
            from langchain_openai import ChatOpenAI  # noqa: PLC0415 — live extra, lazy
        except ModuleNotFoundError as exc:
            raise ModuleNotFoundError(
                f"langchain-openai is not installed; {_LIVE_INSTALL_HINT}"
            ) from exc
        return ChatOpenAI(
            model=model,
            base_url=base_url,
            api_key=SecretStr(api_key) if api_key else None,
            temperature=temperature,
            max_retries=max_retries,
            timeout=timeout,
            # ChatOpenAI's output-cap field is aliased to the current API parameter name
            # (it sends `max_completion_tokens`; the old `max_tokens` is deprecated there).
            max_completion_tokens=max_tokens,
        )
    try:
        from langchain_deepseek import ChatDeepSeek  # noqa: PLC0415 — live extra, lazy
    except ModuleNotFoundError as exc:
        raise ModuleNotFoundError(
            f"langchain-deepseek is not installed; {_LIVE_INSTALL_HINT}"
        ) from exc
    return cast(
        BaseChatModel,
        ChatDeepSeek(
            model=model,
            api_key=SecretStr(api_key) if api_key else None,
            temperature=temperature,
            max_retries=max_retries,
            timeout=timeout,
            max_tokens=max_tokens,
        ),
    )


def _usage_of(message: AIMessage) -> tuple[int, int, int]:
    """Map ``AIMessage.usage_metadata`` to ``(input_tokens, output_tokens, cached_input_tokens)``.

    ``input_token_details.cache_read`` is LangChain's standard field for provider-side
    prefix-cache hits (langchain-openai fills it from ``prompt_tokens_details.cached_tokens``,
    which DeepSeek reports alongside its legacy ``prompt_cache_hit_tokens``) — it feeds the
    gateway's cheaper cache-hit pricing. A model that reports no usage (the offline fakes)
    degrades to zeros, exactly like the retired provider's 3-tuple tolerance.
    """
    usage: dict[str, Any] = dict(message.usage_metadata or {})
    details: dict[str, Any] = dict(usage.get("input_token_details") or {})
    return (
        int(usage.get("input_tokens", 0) or 0),
        int(usage.get("output_tokens", 0) or 0),
        int(details.get("cache_read", 0) or 0),
    )


def _response_model_of(message: AIMessage) -> str:
    """The model id the API response body reported (``gen_ai.response.model``), "" if unknown."""
    return str(
        message.response_metadata.get("model_name") or message.response_metadata.get("model") or ""
    )


@contextmanager
def _auth_errors_explained(api_key_env: str) -> Iterator[None]:
    """Attach the credential's provenance to an auth rejection — **without changing its type**.

    A 401 from the vendor says *the key is wrong*; it cannot say *which key we sent, or why that
    one*. Silent shadowing (a shell variable beating ``.env``) is the usual cause, so we add that
    explanation via ``add_note`` (PEP 678) rather than wrapping.

    Wrapping would be wrong here: the failover wrapper classifies an auth failure by its
    ``openai.*`` exception TYPE in order to *not* retry on the secondary (a bad key would fail
    there too, and masking a misconfiguration behind a second provider is worse than failing).
    Re-raising a different class would silently break that passthrough — a contract test pins it.
    """
    try:
        yield
    except Exception as exc:  # 供应商 SDK 的异常类型不统一,按状态码/文本识别
        text = str(exc)
        status = getattr(exc, "status_code", None)
        if status in (401, 403) or "401" in text or "Authentication" in text:
            exc.add_note(f"凭据来源:{credential_source_hint(api_key_env)}")
        raise


def _project_env(name: str) -> str:
    """通用变量名的项目专属版本(``OPENAI_API_KEY`` → ``OWCOPILOT_OPENAI_API_KEY``)。"""
    return f"OWCOPILOT_{name}"


def _dotenv_value(name: str) -> str | None:
    """``.env`` 文件里该变量的字面值 —— 用来和进程环境里的那把作比较。

    刻意不用 ``load_dotenv``:它默认不覆盖已有变量,读不出"被盖住的那一把"是什么。
    """
    path = Path.cwd() / ".env"
    if not path.is_file():
        return None
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key.strip() == name:
                return value.strip().strip('"').strip("'") or None
    except OSError:
        return None
    return None


def credential_source_hint(api_key_env: str = "OPENAI_API_KEY") -> str:
    """Explain WHERE the credential in play came from — the missing half of a bare 401.

    A machine that has another vendor's SDK installed usually already exports the generic
    ``OPENAI_API_KEY`` globally. Because ``load_dotenv`` does not override a variable the shell
    already set, that global key silently shadows the one in ``.env`` — and if ``.env`` points the
    base URL at a different (OpenAI-compatible) vendor, the request goes out with vendor A's key
    against vendor B's endpoint. The server can only answer 401; only we can say why.
    """
    project_var = _project_env(api_key_env)
    if os.getenv(project_var):
        return f"当前用的是 {project_var}(项目专属变量,优先级最高)。"
    shell_key = os.getenv(api_key_env)
    if not shell_key:
        return f"{api_key_env} 未设置。"
    tail = shell_key[-4:] if len(shell_key) >= 4 else "?"
    return (
        f"当前用的是环境变量 {api_key_env}(尾号 {tail})。注意:进程环境里的同名变量"
        f"**优先于 .env 文件**,所以 .env 里的那个没有生效。"
        f"若两者属于不同服务商,就会出现「拿 A 家的密钥敲 B 家的门」。"
        f"解决办法:把本项目的凭据改用项目专属变量 {project_var} / {_project_env('OPENAI_BASE_URL')}"
        f"(它们优先级最高,且不会和你机器上其它工具的通用变量冲突)。"
    )


class ChatModelProvider:
    """Gateway provider backed by a LangChain chat model (the retired HTTP provider's successor).

    Implements the same optional-member surface the resilience wrappers and
    ``provider_supports_tools`` probe (see ``PROVIDER_CAPABILITY_MEMBERS``): ``complete``,
    ``complete_with_tools``, ``model`` and ``supports_tools``. Configuration mirrors the retired
    provider so ``build_real_provider`` and the connection probe swap in without contract
    changes: env-driven base_url/api_key (native Anthropic or a BYO OpenAI-compatible endpoint),
    prompt-gated JSON mode, and the ``OWCOPILOT_MAX_OUTPUT_TOKENS`` runaway-completion cap.

    *chat_model* is injectable so tests drive the adapter with the standard LangChain fakes
    (``scripted_chat_model``) at $0; when omitted, the first call lazily builds the real model
    via :func:`build_chat_model` (lazy so offline processes never import the live extra).
    """

    # Probed by ``provider_supports_tools``: LangChain chat models speak native tool-calling
    # uniformly via ``bind_tools``.
    supports_tools = True
    # The selected LangChain provider SDK owns transport retries. ``LLMGateway`` sees this marker
    # and does not wrap the SDK in another retry loop.
    owns_retry_policy = True
    suppress_gateway_replay = True
    # Compatibility with the first retry-ownership contract.
    handles_transport_retries = True
    # A concrete SDK provider does not orchestrate nested children; its immediate caller reserves
    # the declared transport-attempt ceiling before invoking it.
    manages_invocation_budget = False

    _PROJECT_ENV_PREFIX = "OWCOPILOT_"

    def __init__(
        self,
        model: str,
        *,
        base_url_env: str = "OPENAI_BASE_URL",
        api_key_env: str = "OPENAI_API_KEY",
        json_mode: bool = True,
        timeout: float | None = None,
        max_retries: int = DEFAULT_TRANSPORT_MAX_RETRIES,
        max_output_tokens: int | None = None,
        provider_kind: str | None = None,
        provider_env: str = "LLM_PROVIDER",
        chat_model: BaseChatModel | None = None,
        execution_policy: LLMExecutionPolicy | None = None,
    ):
        self.model = model
        # 项目专属变量优先于通用变量：装过别家 SDK 的机器上,全局 OPENAI_API_KEY 往往已经指向
        # 真正的 OpenAI,而本项目的 .env 配的可能是另一家兼容端点 —— 通用名一撞车,就会拿着
        # A 家的 key 去敲 B 家的门,只报一句 401 而看不出原因。项目前缀让两者互不干扰
        # (与既有的 OWCOPILOT_FALLBACK_* 惯例一致)。
        self.base_url = os.getenv(_project_env(base_url_env)) or os.getenv(base_url_env)
        self.api_key = os.getenv(_project_env(api_key_env)) or os.getenv(api_key_env)
        self.api_key_env = (
            _project_env(api_key_env) if os.getenv(_project_env(api_key_env)) else api_key_env
        )
        configured_provider = (
            provider_kind or os.getenv(_project_env(provider_env)) or os.getenv(provider_env)
        )
        self.provider_kind = resolve_chat_provider_kind(configured_provider, self.base_url)
        self.json_mode = json_mode
        self.timeout = (
            timeout
            if timeout is not None
            else float(os.getenv("OWCOPILOT_PROVIDER_TIMEOUT_SEC", "60"))
        )
        self.max_retries = max(
            0, execution_policy.sdk_max_retries if execution_policy else max_retries
        )
        # Same runaway-completion guard (cost + latency) and calibration as the retired
        # provider: reasoning models spend part of the cap on hidden reasoning, so 6000 fits a
        # live bilingual draft while still catching runaways. A capped reply raises a guided
        # OutputTruncatedError instead of failing later in JSON parsing.
        self.max_output_tokens = (
            max_output_tokens
            if max_output_tokens is not None
            else int(os.getenv("OWCOPILOT_MAX_OUTPUT_TOKENS", "6000"))
        )
        self._chat = chat_model
        self._injected_chat = chat_model is not None

    def _ensure_chat(self) -> BaseChatModel:
        """The backing chat model, built lazily on first use (never at import/construct time)."""
        if self._chat is None:
            # Fail fast with an actionable message instead of a deep SDK error. Key resolution:
            # the configured env (OPENAI_API_KEY by default — the BYO-endpoint contract) or,
            # for the default DeepSeek route, the integration's own DEEPSEEK_API_KEY.
            has_key = bool(self.api_key) or (
                not self.base_url and bool(os.getenv("DEEPSEEK_API_KEY"))
            )
            if not has_key:
                raise RuntimeError(
                    f"real LLM mode needs an API key, but {self.api_key_env} is empty"
                    + ("" if self.base_url else " and DEEPSEEK_API_KEY is not set either")
                    + ". Set it in your environment or .env. Note: an existing shell "
                    f"{self.api_key_env} takes precedence over .env."
                )
            self._warn_if_credential_looks_foreign()
            self._chat = build_chat_model(
                model=self.model,
                api_key=self.api_key,
                base_url=self.base_url,
                max_retries=self.max_retries,
                timeout=self.timeout,
                max_tokens=self.max_output_tokens if self.max_output_tokens > 0 else None,
                provider_kind=self.provider_kind,
            )
        return self._chat

    @property
    def max_transport_attempts(self) -> int:
        """Hard ceiling for HTTP attempts inside one provider call (initial + SDK retries)."""
        return self.max_retries + 1

    def chat_model(self) -> BaseChatModel:
        """Public access to the backing LangChain chat model (built lazily on first use).

        The agent assembly point: hosts wiring ``create_agent`` take the model from here so the
        provider stays the ONE place that knows how the real model is configured (env, key
        resolution, output cap). Offline tests construct the provider with an injected fake and
        get that same instance back.
        """
        return self._ensure_chat()

    def _warn_if_credential_looks_foreign(self) -> None:
        """密钥来自进程环境、而 .env 另有一把不同的 —— 建模型时就提醒,别等 401。

        ``load_dotenv`` 不覆盖进程里已有的同名变量,所以装过别家 SDK 的机器上,全局密钥会静默
        盖掉 ``.env`` 里的那把。等到请求打出去才收到 401 时,现场已经看不出是哪把密钥在用了。
        这里只警告不拦截:两把密钥属于同一家服务商时,这是完全正常的配置。
        """
        if not self.api_key or self.api_key_env.startswith(self._PROJECT_ENV_PREFIX):
            return
        dotenv_key = _dotenv_value(self.api_key_env)
        if dotenv_key and dotenv_key != self.api_key:
            logger.warning(
                "凭据来源提示:%s",
                credential_source_hint(self.api_key_env),
            )

    def _wants_json(self, system: str, user: str) -> bool:
        """OpenAI/DeepSeek reject a json_object response_format unless the prompt itself
        mentions 'json' — same prompt gate as the retired provider, so e.g. the planner task
        (no 'json' in prompt) doesn't 400 on the cheap tier."""
        # Anthropic's Messages API is not OpenAI's `response_format=json_object` contract.
        # JSON remains prompt-constrained and validated by our normal structured parsers.
        return (
            self.json_mode
            and self.provider_kind != "anthropic"
            and ("json" in f"{system}\n{user}".lower())
        )

    def _raise_if_truncated(self, message: AIMessage) -> None:
        """Fail loud-and-guided when the reply was cut at the output cap (text path only)."""
        finish_reason = str(
            message.response_metadata.get("finish_reason")
            or message.response_metadata.get("stop_reason")
            or ""
        )
        if finish_reason not in {"length", "max_tokens"}:
            return
        _, out_tok, _ = _usage_of(message)
        raise OutputTruncatedError(completion_tokens=out_tok, cap=self.max_output_tokens)

    def complete(self, *, system: str, user: str, model: str) -> tuple[str, int, int, int, str]:
        """One text completion. *model* is the tier label (protocol symmetry); the real model id
        is this provider's configured ``self.model``. Returns the gateway's 5-tuple:
        ``(text, input_tokens, output_tokens, cached_input_tokens, response_model)``."""
        chat = self._ensure_chat()
        kwargs: dict[str, Any] = (
            {"response_format": {"type": "json_object"}} if self._wants_json(system, user) else {}
        )
        with _auth_errors_explained(self.api_key_env):
            message = chat.invoke(
                [SystemMessage(content=system), HumanMessage(content=user)], **kwargs
            )
        self._raise_if_truncated(message)
        in_tok, out_tok, cached = _usage_of(message)
        return str(message.text), in_tok, out_tok, cached, _response_model_of(message)

    def complete_with_tools(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        model: str,  # noqa: ARG002 — tier label, kept for protocol symmetry with complete()
    ) -> ToolCallResponse:
        """One native function-calling turn.

        *messages* stays the running OpenAI-format conversation the agent loop maintains
        (system / user / assistant-with-tool_calls / tool) — ``convert_to_messages`` maps it to
        LangChain messages, and ``bind_tools`` takes the OpenAI tool schemas as-is, so the
        gateway-facing contract is unchanged. ``AIMessage.tool_calls`` maps back to the
        gateway's :class:`ToolCall`; a malformed tool call (LangChain's ``invalid_tool_calls``)
        degrades to ``arguments={}`` so the registry reports the missing-arg error as an
        observation and the loop self-corrects — same tolerance as the retired parser. No JSON
        response_format and no truncation raise here, by design (both match the old provider).
        """
        base_chat = self._ensure_chat()
        # The installed LangChain OpenAI serializer omits vendor reasoning_content.
        # Keep its official client/configuration, but send the complete native
        # message history through that SDK for DeepSeek thinking/tool turns.
        deepseek_wire = self.provider_kind == "deepseek" or (
            (urlparse(self.base_url or "").hostname or "").lower() == "api.deepseek.com"
        )
        if deepseek_wire and not self._injected_chat:
            if not hasattr(base_chat, "_get_request_payload") or not hasattr(base_chat, "client"):
                raise RuntimeError("DeepSeek适配器缺少原生工具协议接口，未切换模型或禁用thinking")
            payload = base_chat._get_request_payload(
                convert_to_messages(messages), tools=list(tools), tool_choice="auto"
            )
            if len(payload["messages"]) != len(messages):
                raise ValueError("DeepSeek消息转换改变了历史长度")
            for raw, encoded in zip(messages, payload["messages"], strict=True):
                if raw.get("role") == "assistant" and "reasoning_content" in raw:
                    encoded["reasoning_content"] = raw["reasoning_content"]
            with _auth_errors_explained(self.api_key_env):
                response = base_chat.client.create(**payload)
            message = cast(Any, base_chat)._create_chat_result(response).generations[0].message
            # ChatOpenAI drops vendor fields while parsing its own response;
            # an explicitly compatible DeepSeek endpoint still needs the same
            # official thinking/tool protocol. Preserve only returned content.
            raw_message = response.choices[0].message
            reasoning = getattr(raw_message, "reasoning_content", None)
            if reasoning is not None:
                message.additional_kwargs["reasoning_content"] = reasoning
        else:
            chat = base_chat.bind_tools(list(tools), tool_choice="auto")
            message = chat.invoke(convert_to_messages(messages))
        self._raise_if_truncated(message)
        parsed_calls: list[ToolCall] = []
        for tc in message.tool_calls:
            args = tc.get("args")
            parsed_calls.append(
                ToolCall(
                    call_id=str(tc.get("id") or ""),
                    name=str(tc.get("name") or ""),
                    arguments=dict(args) if isinstance(args, dict) else {},
                )
            )
        # Malformed argument JSON lands in invalid_tool_calls (args kept as the raw string);
        # degrade those to {} after the well-formed calls rather than dropping them.
        for bad in message.invalid_tool_calls:
            parsed_calls.append(
                ToolCall(
                    call_id=str(bad.get("id") or ""),
                    name=str(bad.get("name") or ""),
                    arguments={},
                )
            )
        in_tok, out_tok, cached = _usage_of(message)
        return ToolCallResponse(
            text=str(message.text),
            tool_calls=parsed_calls,
            input_tokens=in_tok,
            output_tokens=out_tok,
            cached_input_tokens=cached,
            response_model=_response_model_of(message),
            reasoning_content=message.additional_kwargs.get("reasoning_content"),
        )


# ---------------------------------------------------------------------------
# Agent-path resilience (env opt-in) — predicate-aware failover + per-provider circuit breakers.
# ---------------------------------------------------------------------------

#: Env contract, carried over verbatim from ``resilience.build_real_provider`` (the text-path
#: failover) so a studio configures ONE set of fallback vars for both paths.
FALLBACK_MODEL_ENV = "OWCOPILOT_FALLBACK_MODEL"
FALLBACK_BASE_URL_ENV = "OWCOPILOT_FALLBACK_BASE_URL"
FALLBACK_API_KEY_ENV = "OWCOPILOT_FALLBACK_API_KEY"


def _configured_agent_deadline(value: float | None) -> float:
    if value is None:
        value = float(os.getenv("OWCOPILOT_LLM_TOTAL_DEADLINE_SEC", "120"))
    # Keep disabled as an explicit 0 so bind()/bind_tools() do not reinterpret it as "read env".
    return value if value > 0 else 0.0


def _configured_agent_transport_budget(value: int | None, derived_cap: int) -> int:
    if value is None:
        configured = os.getenv("OWCOPILOT_LLM_MAX_TRANSPORT_ATTEMPTS", "").strip()
        value = int(configured) if configured else derived_cap
    return min(derived_cap, max(0, value))


async def _await_under_current_deadline(
    call: Callable[[], Awaitable[BaseMessage]],
) -> BaseMessage:
    remaining = remaining_invocation_time()
    if remaining is None:
        result = await call()
        ensure_invocation_time()
        return result
    if remaining <= 0:
        raise LLMDeadlineExceededError("Agent model-call total deadline exceeded")
    try:
        result = await asyncio.wait_for(call(), timeout=remaining)
    except TimeoutError as exc:
        # Preserve a provider TimeoutError that arrived before the whole-chain timer. When
        # wait_for itself expired, the shared monotonic deadline has expired too and is promoted
        # to the distinct, non-failoverable total-deadline category.
        try:
            ensure_invocation_time()
        except LLMDeadlineExceededError as deadline_exc:
            raise deadline_exc from exc
        raise
    ensure_invocation_time()
    return result


class DeadlineBudgetChatModel(Runnable[LanguageModelInput, BaseMessage]):
    """Whole-chain deadline/transport-budget guard for a single Agent chat-model path."""

    manages_invocation_budget = True

    def __init__(
        self,
        inner: Runnable[LanguageModelInput, BaseMessage],
        *,
        total_deadline_seconds: float | None = None,
        max_total_transport_attempts: int | None = None,
        clock: Any = time.monotonic,
        inner_transport_attempt_cap: int | None = None,
    ) -> None:
        self.inner = inner
        self.total_deadline_seconds = _configured_agent_deadline(total_deadline_seconds)
        self.max_transport_attempts = (
            provider_transport_attempt_cap(inner)
            if inner_transport_attempt_cap is None
            else max(1, inner_transport_attempt_cap)
        )
        self.max_total_transport_attempts = _configured_agent_transport_budget(
            max_total_transport_attempts, self.max_transport_attempts
        )
        self._clock = clock

    def invoke(
        self,
        input: LanguageModelInput,
        config: RunnableConfig | None = None,
        **kwargs: Any,
    ) -> BaseMessage:
        with invocation_budget_scope(
            total_deadline_seconds=self.total_deadline_seconds,
            max_transport_attempts=self.max_total_transport_attempts,
            clock=self._clock,
        ):
            prepare_provider_invocation(self.inner, attempt_cap=self.max_transport_attempts)
            result = self.inner.invoke(input, config, **kwargs)
            ensure_invocation_time()
            return result

    async def ainvoke(
        self,
        input: LanguageModelInput,
        config: RunnableConfig | None = None,
        **kwargs: Any,
    ) -> BaseMessage:
        with invocation_budget_scope(
            total_deadline_seconds=self.total_deadline_seconds,
            max_transport_attempts=self.max_total_transport_attempts,
            clock=self._clock,
        ):
            prepare_provider_invocation(self.inner, attempt_cap=self.max_transport_attempts)
            return await _await_under_current_deadline(
                lambda: self.inner.ainvoke(input, config, **kwargs)
            )

    def bind_tools(self, tools: Any, **kwargs: Any) -> DeadlineBudgetChatModel:
        inner: Any = self.inner
        return DeadlineBudgetChatModel(
            inner.bind_tools(tools, **kwargs),
            total_deadline_seconds=self.total_deadline_seconds,
            max_total_transport_attempts=self.max_total_transport_attempts,
            clock=self._clock,
            inner_transport_attempt_cap=self.max_transport_attempts,
        )

    def bind(self, **kwargs: Any) -> DeadlineBudgetChatModel:
        return DeadlineBudgetChatModel(
            self.inner.bind(**kwargs),
            total_deadline_seconds=self.total_deadline_seconds,
            max_total_transport_attempts=self.max_total_transport_attempts,
            clock=self._clock,
            inner_transport_attempt_cap=self.max_transport_attempts,
        )


class PolicyFailoverChatModel(Runnable[LanguageModelInput, BaseMessage]):
    """Predicate-aware primary/secondary Runnable with matching sync and async policy.

    LangChain's stock ``with_fallbacks`` selects exceptions by a static type tuple, so it cannot
    honor response metadata such as ``x-should-retry:false`` or an explicitly retryable 409. This
    wrapper consults the same :func:`should_failover` classifier as the provider path at runtime.
    It remains a plain Runnable, so only the actually invoked chat model emits model callbacks.
    """

    manages_invocation_budget = True

    def __init__(
        self,
        primary: Runnable[LanguageModelInput, BaseMessage],
        secondary: Runnable[LanguageModelInput, BaseMessage],
        *,
        total_deadline_seconds: float | None = None,
        max_total_transport_attempts: int | None = None,
        clock: Any = time.monotonic,
        primary_transport_attempt_cap: int | None = None,
        secondary_transport_attempt_cap: int | None = None,
    ) -> None:
        self.primary = primary
        self.secondary = secondary
        self._primary_transport_attempt_cap = (
            provider_transport_attempt_cap(primary)
            if primary_transport_attempt_cap is None
            else max(1, primary_transport_attempt_cap)
        )
        self._secondary_transport_attempt_cap = (
            provider_transport_attempt_cap(secondary)
            if secondary_transport_attempt_cap is None
            else max(1, secondary_transport_attempt_cap)
        )
        self.max_transport_attempts = (
            self._primary_transport_attempt_cap + self._secondary_transport_attempt_cap
        )
        self.total_deadline_seconds = _configured_agent_deadline(total_deadline_seconds)
        self.max_total_transport_attempts = _configured_agent_transport_budget(
            max_total_transport_attempts, self.max_transport_attempts
        )
        self._clock = clock

    def _invoke_chain(
        self, input: LanguageModelInput, config: RunnableConfig | None, kwargs: dict[str, Any]
    ) -> BaseMessage:
        try:
            prepare_provider_invocation(
                self.primary, attempt_cap=self._primary_transport_attempt_cap
            )
            result = self.primary.invoke(input, config, **kwargs)
            ensure_invocation_time()
            return result
        except Exception as exc:
            ensure_invocation_time()
            if not should_failover(exc):
                raise
            prepare_provider_invocation(
                self.secondary, attempt_cap=self._secondary_transport_attempt_cap
            )
            result = self.secondary.invoke(input, config, **kwargs)
            ensure_invocation_time()
            return result

    def invoke(
        self,
        input: LanguageModelInput,
        config: RunnableConfig | None = None,
        **kwargs: Any,
    ) -> BaseMessage:
        with invocation_budget_scope(
            total_deadline_seconds=self.total_deadline_seconds,
            max_transport_attempts=self.max_total_transport_attempts,
            clock=self._clock,
        ):
            return self._invoke_chain(input, config, kwargs)

    async def _ainvoke_chain(
        self, input: LanguageModelInput, config: RunnableConfig | None, kwargs: dict[str, Any]
    ) -> BaseMessage:
        try:
            prepare_provider_invocation(
                self.primary, attempt_cap=self._primary_transport_attempt_cap
            )
            return await self.primary.ainvoke(input, config, **kwargs)
        except Exception as exc:
            ensure_invocation_time()
            if not should_failover(exc):
                raise
            prepare_provider_invocation(
                self.secondary, attempt_cap=self._secondary_transport_attempt_cap
            )
            return await self.secondary.ainvoke(input, config, **kwargs)

    async def ainvoke(
        self,
        input: LanguageModelInput,
        config: RunnableConfig | None = None,
        **kwargs: Any,
    ) -> BaseMessage:
        with invocation_budget_scope(
            total_deadline_seconds=self.total_deadline_seconds,
            max_transport_attempts=self.max_total_transport_attempts,
            clock=self._clock,
        ):
            return await _await_under_current_deadline(
                lambda: self._ainvoke_chain(input, config, kwargs)
            )

    def bind_tools(self, tools: Any, **kwargs: Any) -> PolicyFailoverChatModel:
        """Bind the same tool schemas onto both providers while preserving dynamic policy."""
        primary: Any = self.primary
        secondary: Any = self.secondary
        return PolicyFailoverChatModel(
            primary.bind_tools(tools, **kwargs),
            secondary.bind_tools(tools, **kwargs),
            total_deadline_seconds=self.total_deadline_seconds,
            max_total_transport_attempts=self.max_total_transport_attempts,
            clock=self._clock,
            primary_transport_attempt_cap=self._primary_transport_attempt_cap,
            secondary_transport_attempt_cap=self._secondary_transport_attempt_cap,
        )

    def bind(self, **kwargs: Any) -> PolicyFailoverChatModel:
        """Bind identical invocation kwargs onto both providers."""
        return PolicyFailoverChatModel(
            self.primary.bind(**kwargs),
            self.secondary.bind(**kwargs),
            total_deadline_seconds=self.total_deadline_seconds,
            max_total_transport_attempts=self.max_total_transport_attempts,
            clock=self._clock,
            primary_transport_attempt_cap=self._primary_transport_attempt_cap,
            secondary_transport_attempt_cap=self._secondary_transport_attempt_cap,
        )


class CircuitBreakerChatModel(Runnable[LanguageModelInput, BaseMessage]):
    """A circuit breaker around an agent-path chat model — the ``BaseChatModel``-side sibling of the
    text path's :class:`~owcopilot.llm.resilience.CircuitBreakerProvider`.

    Wraps *inner* (normally one concrete chat model) and shares the text
    path's CLOSED / OPEN / half-open state machine (:class:`~owcopilot.llm.circuit._CircuitState`):
    consecutive failures open the circuit, an open circuit fast-fails with :class:`CircuitOpenError`
    for a cooldown, and the first call after the cooldown is a half-open trial.

    It is deliberately a plain ``Runnable``, NOT a ``BaseChatModel``: ``invoke`` delegates straight
    to ``inner.invoke`` without starting a second chat-model run, so the inner model fires the
    ``on_chat_model_start`` / ``on_llm_end`` callbacks exactly once and per-call telemetry is not
    double-counted: a wrapper Runnable is a chain, not a chat model. ``create_agent`` accepts it
    because it duck-types the model on ``bind_tools``, and the orchestrator's bare ``invoke``
    passes through the circuit directly.

    ``bind_tools`` / ``bind`` return a NEW breaker wrapping the inner's bound result but SHARING
    this breaker's circuit state — ``create_agent`` re-binds tools on every model call, so a fresh
    state per bound wrapper would reset the circuit each call. Production assembly places one
    breaker around each concrete provider inside :class:`PolicyFailoverChatModel`.
    """

    manages_invocation_budget = True

    def __init__(
        self,
        inner: Runnable[LanguageModelInput, BaseMessage],
        state: _CircuitState,
        *,
        inner_transport_attempt_cap: int | None = None,
    ) -> None:
        self._inner = inner
        self._state = state
        self.max_transport_attempts = (
            provider_transport_attempt_cap(inner)
            if inner_transport_attempt_cap is None
            else max(1, inner_transport_attempt_cap)
        )

    @property
    def is_open(self) -> bool:
        """True while the circuit is fast-failing (delegated to the shared state machine)."""
        return self._state.is_open

    @property
    def trips(self) -> int:
        """How many times the circuit has opened (delegated to the shared state machine)."""
        return self._state.trips

    def invoke(
        self,
        input: LanguageModelInput,
        config: RunnableConfig | None = None,
        **kwargs: Any,
    ) -> BaseMessage:
        def invoke_inner() -> BaseMessage:
            prepare_provider_invocation(self._inner, attempt_cap=self.max_transport_attempts)
            return self._inner.invoke(input, config, **kwargs)

        return cast(
            BaseMessage,
            self._state.run(
                invoke_inner,
                should_count_failure=should_count_breaker_failure,
            ),
        )

    async def ainvoke(
        self,
        input: LanguageModelInput,
        config: RunnableConfig | None = None,
        **kwargs: Any,
    ) -> BaseMessage:
        async def invoke_inner() -> BaseMessage:
            prepare_provider_invocation(self._inner, attempt_cap=self.max_transport_attempts)
            return await self._inner.ainvoke(input, config, **kwargs)

        return cast(
            BaseMessage,
            await self._state.arun(
                invoke_inner,
                should_count_failure=should_count_breaker_failure,
            ),
        )

    def bind_tools(self, tools: Any, **kwargs: Any) -> CircuitBreakerChatModel:
        """Bind tools onto the inner model, returning a breaker that shares this circuit's state."""
        inner: Any = self._inner
        return CircuitBreakerChatModel(
            inner.bind_tools(tools, **kwargs),
            self._state,
            inner_transport_attempt_cap=self.max_transport_attempts,
        )

    def bind(self, **kwargs: Any) -> CircuitBreakerChatModel:
        """Bind kwargs onto the inner model, returning a breaker sharing this circuit's state."""
        return CircuitBreakerChatModel(
            self._inner.bind(**kwargs),
            self._state,
            inner_transport_attempt_cap=self.max_transport_attempts,
        )


def build_chat_model_with_resilience(
    primary: BaseChatModel,
    *,
    fallback: BaseChatModel | None = None,
    total_deadline_seconds: float | None = None,
    max_total_transport_attempts: int | None = None,
    _clock: Any = time.monotonic,
) -> Runnable[LanguageModelInput, BaseMessage]:
    """Wrap *primary* with the agent path's failover and/or circuit breaker, or return it unchanged.

    The ``BaseChatModel`` mirror of the text path's ``build_real_provider`` — same two env-opt-in
    layers. Circuit breakers protect each concrete provider *inside* failover, so a repeatedly
    failing primary opens while the secondary keeps serving:

    * **Failover** (``OWCOPILOT_FALLBACK_MODEL`` / injected *fallback*): the secondary is built
      through :class:`ChatModelProvider` under the ``OWCOPILOT_FALLBACK_*`` env names, and
      :class:`PolicyFailoverChatModel` evaluates the shared dynamic predicate, including provider
      retry headers, on both sync and async paths. ``bind_tools`` binds both concrete models.
    * **Circuit breaker** (``OWCOPILOT_CIRCUIT_BREAKER`` — see the shared
      :mod:`~owcopilot.llm.circuit` env contract): each concrete provider gets its own
      :class:`CircuitBreakerChatModel`. An open primary raises ``CircuitOpenError``, which is
      failover-eligible, so later calls skip it quickly and continue on the secondary.

    A whole-chain deadline and conservative transport-attempt ceiling always guard the returned
    Runnable (``OWCOPILOT_LLM_TOTAL_DEADLINE_SEC`` defaults to 120 seconds; optional
    ``OWCOPILOT_LLM_MAX_TRANSPORT_ATTEMPTS`` may lower the SDK-declared cap). The SDK still owns
    its internal retries; this layer never adds a transport retry loop.

    *fallback* is a test-only injection seam (mirrors ``ChatModelProvider(chat_model=…)``): it
    bypasses the failover env gate so the composition can be exercised at $0 without a live model.
    """
    secondary = fallback
    if secondary is None:
        fallback_model = os.getenv(FALLBACK_MODEL_ENV, "").strip()
        if fallback_model:
            secondary = ChatModelProvider(
                model=fallback_model,
                base_url_env=FALLBACK_BASE_URL_ENV,
                api_key_env=FALLBACK_API_KEY_ENV,
                provider_env="FALLBACK_PROVIDER",
            ).chat_model()
    circuit_enabled = circuit_enabled_from_env()
    primary_model: Runnable[LanguageModelInput, BaseMessage] = primary
    if circuit_enabled:
        primary_model = CircuitBreakerChatModel(primary_model, circuit_state_from_env())
    if secondary is None:
        return DeadlineBudgetChatModel(
            primary_model,
            total_deadline_seconds=total_deadline_seconds,
            max_total_transport_attempts=max_total_transport_attempts,
            clock=_clock,
        )

    secondary_model: Runnable[LanguageModelInput, BaseMessage] = secondary
    if circuit_enabled:
        secondary_model = CircuitBreakerChatModel(secondary_model, circuit_state_from_env())
    return PolicyFailoverChatModel(
        primary_model,
        secondary_model,
        total_deadline_seconds=total_deadline_seconds,
        max_total_transport_attempts=max_total_transport_attempts,
        clock=_clock,
    )
