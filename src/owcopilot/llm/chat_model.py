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
  raise the ``openai.*`` exception family, which ``_classify_provider_error`` already
  classifies (so failover's ``auth`` passthrough keeps working).
* :func:`build_chat_model_with_resilience` — the AGENT path's resilience, the mirror of the text
  path's ``build_real_provider``: wraps a chat model in LangChain-native ``.with_fallbacks``
  failover and/or a :class:`CircuitBreakerChatModel` (both env opt-in) so the ``create_agent``
  host / orchestrator ``invoke`` gain a secondary model on an availability error AND stop hammering
  a hard-down provider — the ``BaseChatModel`` counterparts of the provider-layer
  ``FailoverProvider`` / ``CircuitBreakerProvider`` (which only cover the text ``complete`` path).
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

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

from .circuit import (
    CircuitOpenError,
    _CircuitState,
    circuit_enabled_from_env,
    circuit_state_from_env,
)
from .gateway import OutputTruncatedError, ToolCall, ToolCallResponse

__all__ = [
    "ChatModelProvider",
    "CircuitBreakerChatModel",
    "CircuitOpenError",
    "build_chat_model",
    "build_chat_model_with_resilience",
]

_LIVE_INSTALL_HINT = (
    "the real LLM mode needs the LangChain model integrations from the 'live' extra. "
    "Install them with: pip install owcopilot[live]"
)



logger = logging.getLogger(__name__)

def build_chat_model(
    *,
    model: str,
    api_key: str | None = None,
    base_url: str | None = None,
    # Parity with the retired provider: temperature is NOT sent unless explicitly configured
    # (None is excluded from the request payload, so the API-side default applies).
    temperature: float | None = None,
    max_retries: int = 2,
    timeout: float | None = None,
    max_tokens: int | None = None,
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
    replacement for this function's two-branch dispatch and is behaviourally equivalent on the
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
    if base_url:
        try:
            from langchain_openai import ChatOpenAI  # noqa: PLC0415 — live extra, lazy
        except ModuleNotFoundError as exc:
            raise ModuleNotFoundError(
                f"langchain-openai is not installed; {_LIVE_INSTALL_HINT}"
            ) from exc
        return ChatOpenAI(
            model=model,
            base_url=base_url,
            api_key=api_key,  # type: ignore[arg-type]  # pydantic coerces str -> SecretStr
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
    return ChatDeepSeek(
        model=model,
        api_key=api_key,  # type: ignore[arg-type]  # None -> DEEPSEEK_API_KEY env default
        temperature=temperature,
        max_retries=max_retries,
        timeout=timeout,
        max_tokens=max_tokens,
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
    return str(message.response_metadata.get("model_name") or "")


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
    changes: env-driven base_url/api_key (BYO OpenAI-compatible endpoint), prompt-gated JSON
    mode, and the ``OWCOPILOT_MAX_OUTPUT_TOKENS`` runaway-completion cap.

    *chat_model* is injectable so tests drive the adapter with the standard LangChain fakes
    (``scripted_chat_model``) at $0; when omitted, the first call lazily builds the real model
    via :func:`build_chat_model` (lazy so offline processes never import the live extra).
    """

    # Probed by ``provider_supports_tools``: LangChain chat models speak native tool-calling
    # uniformly via ``bind_tools``.
    supports_tools = True

    _PROJECT_ENV_PREFIX = "OWCOPILOT_"

    def __init__(
        self,
        model: str,
        *,
        base_url_env: str = "OPENAI_BASE_URL",
        api_key_env: str = "OPENAI_API_KEY",
        json_mode: bool = True,
        timeout: float | None = None,
        max_output_tokens: int | None = None,
        chat_model: BaseChatModel | None = None,
    ):
        self.model = model
        # 项目专属变量优先于通用变量：装过别家 SDK 的机器上,全局 OPENAI_API_KEY 往往已经指向
        # 真正的 OpenAI,而本项目的 .env 配的可能是另一家兼容端点 —— 通用名一撞车,就会拿着
        # A 家的 key 去敲 B 家的门,只报一句 401 而看不出原因。项目前缀让两者互不干扰
        # (与既有的 OWCOPILOT_FALLBACK_* 惯例一致)。
        self.base_url = os.getenv(_project_env(base_url_env)) or os.getenv(base_url_env)
        self.api_key = os.getenv(_project_env(api_key_env)) or os.getenv(api_key_env)
        self.api_key_env = (
            _project_env(api_key_env)
            if os.getenv(_project_env(api_key_env))
            else api_key_env
        )
        self.json_mode = json_mode
        self.timeout = (
            timeout
            if timeout is not None
            else float(os.getenv("OWCOPILOT_PROVIDER_TIMEOUT_SEC", "60"))
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
                timeout=self.timeout,
                max_tokens=self.max_output_tokens if self.max_output_tokens > 0 else None,
            )
        return self._chat

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
        return self.json_mode and ("json" in f"{system}\n{user}".lower())

    def _raise_if_truncated(self, message: AIMessage) -> None:
        """Fail loud-and-guided when the reply was cut at the output cap (text path only)."""
        if str(message.response_metadata.get("finish_reason") or "") != "length":
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
        chat = self._ensure_chat().bind_tools(list(tools), tool_choice="auto")
        message = chat.invoke(convert_to_messages(messages))
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
        )


# ---------------------------------------------------------------------------
# Agent-path resilience (env opt-in) — LangChain-native ``.with_fallbacks`` + a circuit breaker.
# ---------------------------------------------------------------------------

#: Env contract, carried over verbatim from ``resilience.build_real_provider`` (the text-path
#: failover) so a studio configures ONE set of fallback vars for both paths.
FALLBACK_MODEL_ENV = "OWCOPILOT_FALLBACK_MODEL"
FALLBACK_BASE_URL_ENV = "OWCOPILOT_FALLBACK_BASE_URL"
FALLBACK_API_KEY_ENV = "OWCOPILOT_FALLBACK_API_KEY"


class CircuitBreakerChatModel(Runnable[LanguageModelInput, BaseMessage]):
    """A circuit breaker around an agent-path chat model — the ``BaseChatModel``-side sibling of the
    text path's :class:`~owcopilot.llm.resilience.CircuitBreakerProvider`.

    Wraps *inner* (a chat model, or a ``.with_fallbacks`` failover Runnable) and shares the text
    path's CLOSED / OPEN / half-open state machine (:class:`~owcopilot.llm.circuit._CircuitState`):
    consecutive failures open the circuit, an open circuit fast-fails with :class:`CircuitOpenError`
    for a cooldown, and the first call after the cooldown is a half-open trial.

    It is deliberately a plain ``Runnable``, NOT a ``BaseChatModel``: ``invoke`` delegates straight
    to ``inner.invoke`` without starting a second chat-model run, so the inner model fires the
    ``on_chat_model_start`` / ``on_llm_end`` callbacks exactly once and per-call telemetry is not
    double-counted (the same reason ``.with_fallbacks`` doesn't inflate it — a wrapper Runnable is a
    chain, not a chat model). ``create_agent`` accepts it because it duck-types the model on
    ``bind_tools`` (exactly as it already accepts the ``RunnableWithFallbacks`` failover), and the
    orchestrator's bare ``invoke`` passes through the circuit directly.

    ``bind_tools`` / ``bind`` return a NEW breaker wrapping the inner's bound result but SHARING
    this breaker's circuit state — ``create_agent`` re-binds tools on every model call, so a fresh
    state per bound wrapper would reset the circuit each call. When *inner* is a failover Runnable,
    ``inner.bind_tools`` rebinds onto both primary and secondary
    (``RunnableWithFallbacks.__getattr__``), so a call counts as ONE circuit failure only when the
    whole failover attempt fails.
    """

    def __init__(
        self, inner: Runnable[LanguageModelInput, BaseMessage], state: _CircuitState
    ) -> None:
        self._inner = inner
        self._state = state

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
        return self._state.run(lambda: self._inner.invoke(input, config, **kwargs))

    async def ainvoke(
        self,
        input: LanguageModelInput,
        config: RunnableConfig | None = None,
        **kwargs: Any,
    ) -> BaseMessage:
        return await self._state.arun(lambda: self._inner.ainvoke(input, config, **kwargs))

    def bind_tools(self, tools: Any, **kwargs: Any) -> CircuitBreakerChatModel:
        """Bind tools onto the inner model, returning a breaker that shares this circuit's state."""
        inner: Any = self._inner
        return CircuitBreakerChatModel(inner.bind_tools(tools, **kwargs), self._state)

    def bind(self, **kwargs: Any) -> CircuitBreakerChatModel:
        """Bind kwargs onto the inner model, returning a breaker sharing this circuit's state."""
        return CircuitBreakerChatModel(self._inner.bind(**kwargs), self._state)


def _failover_exceptions() -> tuple[type[BaseException], ...]:
    """The availability exceptions the agent-path failover hands off on — the ``openai`` family,
    lazily imported (``live`` extra).

    Auth / permission / bad-request are DELIBERATELY excluded: a misconfiguration must surface,
    not be masked by a secondary that would fail identically. This is the type-tuple equivalent of
    the retired ``FailoverProvider``'s runtime ``auth`` passthrough — ``.with_fallbacks`` matches on
    exception *type*, not a runtime classifier, so the passthrough set is expressed by omission.
    """
    import openai  # noqa: PLC0415 — live extra, lazy

    return (
        openai.APITimeoutError,
        openai.APIConnectionError,
        openai.InternalServerError,
        openai.RateLimitError,
    )


def build_chat_model_with_resilience(
    primary: BaseChatModel,
    *,
    fallback: BaseChatModel | None = None,
) -> Runnable[LanguageModelInput, BaseMessage]:
    """Wrap *primary* with the agent path's failover and/or circuit breaker, or return it unchanged.

    The ``BaseChatModel`` mirror of the text path's ``build_real_provider`` — same two env-opt-in
    layers, same composition order (failover innermost, circuit breaker outermost):

    * **Failover** (``OWCOPILOT_FALLBACK_MODEL`` / injected *fallback*): the secondary is built
      through :class:`ChatModelProvider` under the ``OWCOPILOT_FALLBACK_*`` env names, and
      ``primary.with_fallbacks([secondary], exceptions_to_handle=…)`` hands off on an availability
      error only (auth passes through — see :func:`_failover_exceptions`). Passed to
      ``create_agent``, the wrapper's ``bind_tools`` rebinds the tool schemas onto BOTH primary and
      secondary (``RunnableWithFallbacks.__getattr__``), so tool-calling failover is real, not
      primary-only; passed to a bare ``.invoke`` (orchestrator decompose), the fallback applies
      directly.
    * **Circuit breaker** (``OWCOPILOT_CIRCUIT_BREAKER`` — see the shared
      :mod:`~owcopilot.llm.circuit` env contract): the failover result (or bare *primary*) is
      wrapped in a :class:`CircuitBreakerChatModel`, so a hard-down provider fast-fails for a
      cooldown instead of being hammered every call. This closes the earlier gap where the agent
      path had failover but no breaker: it now has resilience parity with the text ``complete``
      path (``FailoverProvider`` + ``CircuitBreakerProvider``).

    Default (both envs unset and no injected *fallback*): returns *primary* verbatim, so the agent
    path is byte-for-byte identical to a run without resilience.

    *fallback* is a test-only injection seam (mirrors ``ChatModelProvider(chat_model=…)``): it
    bypasses the failover env gate so the composition can be exercised at $0 without a live model.
    """
    model: Runnable[LanguageModelInput, BaseMessage] = primary
    secondary = fallback
    if secondary is None:
        fallback_model = os.getenv(FALLBACK_MODEL_ENV, "").strip()
        if fallback_model:
            secondary = ChatModelProvider(
                model=fallback_model,
                base_url_env=FALLBACK_BASE_URL_ENV,
                api_key_env=FALLBACK_API_KEY_ENV,
            ).chat_model()
    if secondary is not None:
        model = primary.with_fallbacks([secondary], exceptions_to_handle=_failover_exceptions())
    if circuit_enabled_from_env():
        model = CircuitBreakerChatModel(model, circuit_state_from_env())
    return model
