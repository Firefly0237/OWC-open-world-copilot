# 本地化：覆盖度总览 + 状态流转/分派。
# Localization coverage overview + transition/assign.

"""localization actions — split from the former ``app/actions.py`` monolith by domain.

Behaviour is unchanged; only the module boundary is new. The ``app.actions`` package
re-exports every action so existing imports keep working."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .._common import deterministic_cost_budget as _deterministic_cost_budget
from .._common import open_project as _project


def localization_overview_action(
    content_root: str | Path,
    *,
    locales: list[str] | None = None,
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    """WS-F · localization coverage + per-string status (待译/已译/待校/定稿). $0."""
    from ...localization import LocStore, build_localization_overview

    with _project(content_root, sqlite_path) as project:
        state = LocStore(project.content_store.root).load()
        return {
            "overview": build_localization_overview(project.bundle, state, locales=locales),
            "cost_budget": _deterministic_cost_budget("localization_overview"),
        }


def loc_transition_action(
    content_root: str | Path,
    *,
    text_key: str,
    locale: str,
    to: str,
    by: str,
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    from ...localization import LocStatus, LocStore, transition

    with _project(content_root, sqlite_path) as project:
        store = LocStore(project.content_store.root)
        state = store.load()
        entry = transition(state, text_key=text_key, locale=locale, to=LocStatus(to), by=by)
        store.save(state)
        return {"entry": entry.model_dump(mode="json")}


def loc_assign_action(
    content_root: str | Path,
    *,
    text_key: str,
    locale: str,
    assignee: str,
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    from ...localization import LocStore, assign

    with _project(content_root, sqlite_path) as project:
        store = LocStore(project.content_store.root)
        state = store.load()
        entry = assign(state, text_key=text_key, locale=locale, assignee=assignee)
        store.save(state)
        return {"entry": entry.model_dump(mode="json")}
