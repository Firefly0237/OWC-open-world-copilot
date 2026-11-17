# 动作层共享脊柱：进程级缓存 + 每请求 LLM gateway 工厂 + 通用小助手。
# Shared spine: process-lifetime cache, per-request LLM gateway factory, common helpers.

"""_shared actions — split from the former ``app/actions.py`` monolith by domain.

Behaviour is unchanged; only the module boundary is new. The ``app.actions`` package
re-exports every action so existing imports keep working."""

from __future__ import annotations

import os
import threading
from typing import Any

from ...content.models import ContentBundle
from ...llm.cache import CacheBackend, NoOpCache, build_cache_backend
from ...llm.gateway import LLMGateway, require_offline_llm_allowed
from ...llm.resilience import build_real_provider
from ...llm.router import StaticRouter
from ...llm.telemetry import TelemetryCollector
from ...retrieval.embedding import resolve_embedder
from ...util import load_dotenv
from .._common import PROJECT_NAMESPACE as _PROJECT_NAMESPACE
from .._common import PROJECT_TELEMETRY_SINK as _PROJECT_TELEMETRY_SINK


def _merge_preview(existing: ContentBundle, new: ContentBundle) -> ContentBundle:
    """A throwaway existing∪new bundle for auditing what an expansion batch would land — never
    persisted (accept does the real merge through the review write path)."""
    merged = existing.model_copy(deep=True)
    merged.entities.update(new.entities)
    merged.pois.update(new.pois)
    merged.regions.update(new.regions)
    merged.quests.update(new.quests)
    merged.terms.update(new.terms)
    merged.relations.extend(new.relations)
    return merged


# Long-form generation needs more wall-clock and output headroom than chat-sized calls.
# Floors are combined with the user-tunable base (OWCOPILOT_PROVIDER_TIMEOUT_SEC, settable
# from 设置→高级): effective timeout = max(base, floor). Round-11 trigger: a default-scale
# world_seed (8 npcs / 5 quests) takes well past the old flat 30s and timed out for real.
_TASK_TIMEOUT_FLOOR_SEC: dict[str, float] = {
    "world_seed": 240.0,
    "extract_lore": 240.0,
    "character_profile": 120.0,
    "extract_fill": 120.0,
    "dialogue_tree": 120.0,
    "flavor_batch": 90.0,
    "quest_draft": 90.0,
    "barks_batch": 90.0,
    "patch_suggest": 90.0,
}


_TASK_TIMEOUT_DEFAULT_SEC = 60.0


# Output caps: enough headroom that a full default-scale bundle is not truncated
# mid-JSON, while still stopping runaway generations.
_TASK_MAX_OUTPUT_TOKENS: dict[str, int] = {
    "world_seed": 6000,
    "extract_lore": 4500,
    # dialogue_tree raised 4000->6000: the 二游 quality bar (subtext, distinct voices) makes lines
    # longer, so a 12-node tree over a rich world occasionally truncated mid-JSON and failed parse.
    "dialogue_tree": 6000,
    # quest_draft used to run terse; the 二游 quality bar makes stages real scenes, so the JSON is
    # bigger now — give it room or rich drafts truncate mid-object and fail to parse.
    "quest_draft": 3500,
}


def _task_timeout_sec(task: str) -> float:
    base = float(os.getenv("OWCOPILOT_PROVIDER_TIMEOUT_SEC", "0") or 0)
    floor = _TASK_TIMEOUT_FLOOR_SEC.get(task, _TASK_TIMEOUT_DEFAULT_SEC)
    return max(base, floor)


_ACTION_CACHE: CacheBackend | None = None


_ACTION_CACHE_LOCK = threading.Lock()


def _action_cache() -> CacheBackend:
    """One process-lifetime L1/L2 cache shared by every real-mode action gateway.

    Same assembly as the REST service (`service.dependencies._build_cache`): without it, every
    Workbench rerun built a throwaway gateway with NoOpCache and a user asking the same
    question twice paid twice. Offline mode stays uncached on purpose — it is the test
    substrate, and cross-test result sharing would couple unrelated tests.
    """
    global _ACTION_CACHE
    if _ACTION_CACHE is None:
        # Double-checked under a lock: concurrent job-runner threads must share one cache (and one
        # embedder load), not each build their own and defeat the reuse this exists for.
        with _ACTION_CACHE_LOCK:
            if _ACTION_CACHE is None:
                # bge-m3 (when installed) so the L2 paraphrase cache works for CJK; falls back
                # to the deterministic hashing stub by env (tests, or no [semantic] extra).
                _ACTION_CACHE = build_cache_backend("exact+semantic", embedder=resolve_embedder())
    return _ACTION_CACHE


def _gateway(
    *,
    task: str,
    llm_mode: str,
    llm_model: str,
    offline_provider: Any,
    extra_tasks: list[str] | None = None,
    cache_override: CacheBackend | None = None,
    namespace_override: str | None = None,
) -> tuple[LLMGateway, TelemetryCollector]:
    telemetry = TelemetryCollector(record_sink=_PROJECT_TELEMETRY_SINK.get())
    real = llm_mode == "real"
    if real:
        load_dotenv()  # pick up provider keys from .env; shell env wins
        provider: Any = build_real_provider(
            model=llm_model,
            timeout=_task_timeout_sec(task),
            max_output_tokens=_TASK_MAX_OUTPUT_TOKENS.get(task),
        )
    else:
        require_offline_llm_allowed()  # offline fakes are a test/CI fixture, not a product mode
        provider = offline_provider
    mapping = {t: "cheap" for t in (task, *(extra_tasks or []))}
    gateway = LLMGateway(
        providers={"cheap": provider},
        router=StaticRouter(mapping=mapping),
        # The FastAPI composition root injects its app-lifetime cache so repeated offline and real
        # HTTP requests share one client cache. CLI/action callers keep the original behaviour:
        # real calls use the process cache, offline test doubles remain isolated.
        cache=(
            cache_override
            if cache_override is not None
            else _action_cache()
            if real
            else NoOpCache()
        ),
        telemetry=telemetry,
        max_retries=1 if real else 0,
        retry_backoff_seconds=1.0 if real else 0.0,
        namespace=(
            namespace_override if namespace_override is not None else _PROJECT_NAMESPACE.get()
        ),
    )
    return gateway, telemetry


def _bundle_counts(bundle: Any) -> dict[str, int]:
    return {
        "entities": len(bundle.entities),
        "relations": len(bundle.relations),
        "quests": len(bundle.quests),
        "regions": len(bundle.regions),
        "pois": len(bundle.pois),
        "terms": len(bundle.terms),
        "style_guides": len(bundle.style_guides),
    }
