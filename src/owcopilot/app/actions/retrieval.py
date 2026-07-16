# 检索问答：有据 lore 问答 + 社区总览构建。
# Cited lore QA + community-overview build actions.

"""retrieval actions — split from the former ``app/actions.py`` monolith by domain.

Behaviour is unchanged; only the module boundary is new. The ``app.actions`` package
re-exports every action so existing imports keep working."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from ...qa.community_index import CommunityIndexService
from ...qa.offline import OfflineCommunityReportProvider, OfflineQAProvider
from ...qa.service import LoreQAService
from ...telemetry import llm_step, summarize_workflow
from .._common import open_project as _project
from ._shared import _gateway


def run_ask_action(
    content_root: str | Path,
    *,
    query: str,
    sqlite_path: str | None = None,
    budget_tokens: int = 800,
    llm_mode: str = "offline",
    llm_model: str = "deepseek-v4-flash",
    max_cost_usd: float | None = None,
) -> dict[str, Any]:
    with _project(content_root, sqlite_path) as project:
        gateway, telemetry = _gateway(
            task="qa_answer",
            llm_mode=llm_mode,
            llm_model=llm_model,
            offline_provider=OfflineQAProvider(),
            extra_tasks=["qa_expand"],
        )
        answer = LoreQAService(
            gateway=gateway,
            # QA-only builder: also recalls GraphRAG macro-overview reports so holistic questions
            # ground on cluster/global summaries instead of thinning out on row retrieval.
            context_builder=project.qa_context_builder(),
            bundle=project.bundle,
            # Widen recall with LLM query expansion on the real path; offline stays deterministic
            # (the offline provider returns no variants), so $0 tests are unaffected.
            expand=(llm_mode == "real"),
        ).ask(query, budget_tokens=budget_tokens)
        telemetry_summary = telemetry.summary()
        return {
            "answer": answer.model_dump(mode="json"),
            "llm_mode": llm_mode,
            "telemetry": telemetry_summary,
            "cost_budget": summarize_workflow(
                [llm_step("ask_lore", telemetry_summary)], budget_usd=max_cost_usd
            ).budget.model_dump(mode="json"),
        }


def run_build_overview_action(
    content_root: str | Path,
    *,
    sqlite_path: str | None = None,
    llm_mode: str = "offline",
    llm_model: str = "deepseek-v4-flash",
    progress: Callable[[str, dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Build (or refresh) the GraphRAG macro-overview index: deterministic community detection +
    cached LLM community/global reports. Holistic questions in `ask` then retrieve over these.

    Cost scales with the number of *changed* communities (each is a small cheap-tier call); a
    re-run on an unchanged world is $0 (every report is a fingerprint cache hit)."""
    with _project(content_root, sqlite_path) as project:
        gateway, telemetry = _gateway(
            task="community_report",
            llm_mode=llm_mode,
            llm_model=llm_model,
            offline_provider=OfflineCommunityReportProvider(),
        )
        result = CommunityIndexService(
            gateway=gateway, store=project.sqlite_store, bundle=project.bundle
        ).build(progress=progress)
        telemetry_summary = telemetry.summary()
        return {
            "community_count": result.community_count,
            "regenerated": result.regenerated,
            "reports": [r.model_dump(mode="json") for r in result.reports],
            "llm_mode": llm_mode,
            "telemetry": telemetry_summary,
            "cost_budget": summarize_workflow(
                [llm_step("build_overview", telemetry_summary)]
            ).budget.model_dump(mode="json"),
        }
