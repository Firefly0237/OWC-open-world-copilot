# 灵感参考库：新增/列出/检索参考素材。
# Inspiration reference store: add, list, search.

"""references actions — split from the former ``app/actions.py`` monolith by domain.

Behaviour is unchanged; only the module boundary is new. The ``app.actions`` package
re-exports every action so existing imports keep working."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .._common import deterministic_cost_budget as _deterministic_cost_budget
from .._common import open_project as _project


def add_reference_action(
    content_root: str | Path,
    *,
    title: str,
    text: str,
    sqlite_path: str | None = None,
    source_type: str = "uploaded_file",
    original_filename: str | None = None,
    allowed_uses: list[str] | None = None,
) -> dict[str, Any]:
    with _project(content_root, sqlite_path) as project:
        result = project.reference_store.add_text(
            title=title,
            text=text,
            source_type=source_type,
            original_filename=original_filename,
            allowed_uses=allowed_uses,
        )
        project.reference_store.sync_index(project.sqlite_store)
        return {
            "source": result.source.model_dump(mode="json"),
            "chunks": [chunk.model_dump(mode="json") for chunk in result.chunks],
            "indexed_count": result.indexed_count,
            # Untrusted reference text that matched a prompt-injection pattern, surfaced so the
            # human can make a risk call before it grounds a generation prompt (OWASP LLM01).
            "injection_flagged_chunks": result.injection_flagged_chunks,
            "cost_budget": _deterministic_cost_budget("reference_add"),
        }


def list_references_action(
    content_root: str | Path,
    *,
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    with _project(content_root, sqlite_path) as project:
        sources = project.reference_store.list_sources()
        return {
            "count": len(sources),
            "sources": [source.model_dump(mode="json") for source in sources],
            "cost_budget": _deterministic_cost_budget("reference_list"),
        }


def search_references_action(
    content_root: str | Path,
    *,
    query: str,
    sqlite_path: str | None = None,
    budget_tokens: int = 1000,
) -> dict[str, Any]:
    with _project(content_root, sqlite_path) as project:
        pack = project.reference_context_builder.build(query, budget_tokens=budget_tokens)
        return {
            "query": query,
            "refs": pack.refs,
            "hits": [hit.model_dump(mode="json") for hit in pack.hits],
            "cost_budget": _deterministic_cost_budget("reference_search"),
        }
