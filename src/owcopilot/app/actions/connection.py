# 服务商连接探活。
# Provider connection probe.

"""connection actions — split from the former ``app/actions.py`` monolith by domain.

Behaviour is unchanged; only the module boundary is new. The ``app.actions`` package
re-exports every action so existing imports keep working."""

from __future__ import annotations

import os
from typing import Any

from ...llm.gateway import OpenAICompatProvider


def probe_llm_connection_action(
    *,
    base_url: str,
    api_key: str,
    model: str,
    timeout: float = 12.0,
    provider: Any | None = None,
) -> dict[str, Any]:
    """Probe the configured provider with a minimal completion (BYO-key onboarding).

    The key is used in-process only; env vars are restored afterwards. `provider` is
    injectable so tests run offline.
    """
    import time

    from ...llm.gateway import _classify_provider_error

    overrides = {"OPENAI_BASE_URL": base_url.strip(), "OPENAI_API_KEY": api_key.strip()}
    saved = {key: os.environ.get(key) for key in overrides}
    try:
        for key, value in overrides.items():
            if value:
                os.environ[key] = value
            else:
                os.environ.pop(key, None)
        probe = provider or OpenAICompatProvider(model=model, timeout=timeout, max_output_tokens=64)
        started = time.perf_counter()
        result = probe.complete(
            system="You are a connectivity probe.",
            user="Reply with the single word: pong",
            model="cheap",
        )
        latency_ms = (time.perf_counter() - started) * 1000
        text = str(result[0]) if result else ""
        return {
            "ok": True,
            "model": model,
            "latency_ms": round(latency_ms, 1),
            "sample": text[:60],
        }
    except ModuleNotFoundError:
        return {
            "ok": False,
            "category": "missing_dependency",
            "message": "未安装真实模型依赖：pip install owcopilot[live]",
        }
    except Exception as e:  # classified, never raised: the UI shows a friendly verdict
        return {
            "ok": False,
            "category": _classify_provider_error(e),
            "message": str(e)[:200],
        }
    finally:
        for env_key, previous in saved.items():
            if previous is None:
                os.environ.pop(env_key, None)
            else:
                os.environ[env_key] = previous
