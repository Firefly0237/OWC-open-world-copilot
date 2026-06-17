"""WS-F · localization workflow: coverage/missing overview + per-string status machine + assign."""

from __future__ import annotations

import pytest

from owcopilot.app.actions import (
    loc_assign_action,
    loc_transition_action,
    localization_overview_action,
)
from owcopilot.content.models import ContentBundle, LocalizedText
from owcopilot.content.store import ContentStore
from owcopilot.localization import (
    LocState,
    LocStatus,
    assign,
    build_localization_overview,
    status_of,
    transition,
)


def _bundle() -> ContentBundle:
    return ContentBundle(
        localized_texts={
            "a_zh": LocalizedText(id="a_zh", text_key="ui.greet", locale="zh-CN", text="你好"),
            "a_en": LocalizedText(id="a_en", text_key="ui.greet", locale="en", text="Hello"),
            "b_zh": LocalizedText(id="b_zh", text_key="ui.bye", locale="zh-CN", text="再见"),
            # ui.bye has no English -> a coverage gap
        }
    )


def test_overview_reports_coverage_and_missing() -> None:
    overview = build_localization_overview(_bundle(), LocState())
    assert set(overview["locales"]) == {"zh-CN", "en"}
    assert overview["keys"] == 2
    bye = next(r for r in overview["rows"] if r["text_key"] == "ui.bye")
    assert bye["missing_locales"] == ["en"]  # English missing for ui.bye
    # present strings default to 已译, the missing one to 待译
    assert overview["by_status"][LocStatus.UNTRANSLATED.value] == 1


def test_status_machine_legal_and_illegal() -> None:
    state = LocState()
    transition(state, text_key="ui.greet", locale="en", to=LocStatus.REVIEWING, by="ed")
    assert status_of(state, "ui.greet", "en", present=True) == LocStatus.REVIEWING
    transition(state, text_key="ui.greet", locale="en", to=LocStatus.FINAL, by="ed")
    # cannot jump final -> untranslated directly
    with pytest.raises(ValueError, match="不允许的流转"):
        transition(state, text_key="ui.greet", locale="en", to=LocStatus.UNTRANSLATED, by="ed")


def test_transition_requires_signature() -> None:
    with pytest.raises(ValueError, match="署名"):
        transition(LocState(), text_key="k", locale="en", to=LocStatus.REVIEWING, by="  ")


def test_assign_keeps_status() -> None:
    state = LocState()
    transition(state, text_key="ui.greet", locale="en", to=LocStatus.REVIEWING, by="ed")
    entry = assign(state, text_key="ui.greet", locale="en", assignee="bob")
    assert entry.assignee == "bob" and entry.status == LocStatus.REVIEWING


# --------------------------------------------------------------- action level
def test_actions_persist_status_and_assignment(tmp_path) -> None:
    root = tmp_path / "content"
    ContentStore(root).save(_bundle())
    loc_transition_action(root, text_key="ui.greet", locale="en", to="reviewing", by="ed")
    loc_assign_action(root, text_key="ui.greet", locale="en", assignee="bob")
    overview = localization_overview_action(root)["overview"]
    greet = next(r for r in overview["rows"] if r["text_key"] == "ui.greet")
    assert greet["status"]["en"] == "reviewing"
