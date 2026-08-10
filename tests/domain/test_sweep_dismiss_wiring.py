# P3 接线验收：sweep 逐一排除 / judge_skipped 经产品入口（action + REST）可用且持久。
"""Wiring proof for the P3 follow-up: two capabilities that existed in the service layer
(``assist/sweep.py`` dismiss/exclusions, ``assist/contradiction.py`` judge_skipped) are now
reachable through the product entry points (``app/actions`` + the REST routers).

These tests never touch the service classes directly — they go through the SAME action/REST
surface a real client uses, so they prove the wiring, not the underlying capability (which has its
own suites). Deterministic, offline, $0: the semantic layer is pinned off (hashing stub via
conftest) and the LLM judge uses the offline double.
"""

from __future__ import annotations

import json

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from owcopilot.app.actions import (  # noqa: E402
    detect_contradictions_action,
    dismiss_sweep_finding_action,
    run_theme_sweep_action,
)
from owcopilot.content.models import (  # noqa: E402
    ContentBundle,
    Entity,
    EntityType,
    Quest,
    Relation,
    Term,
)
from owcopilot.content.store import ContentStore  # noqa: E402
from owcopilot.service.api import create_app  # noqa: E402


def _gambling_bundle() -> ContentBundle:
    return ContentBundle(
        entities={
            "npc_dicer": Entity(
                id="npc_dicer",
                name="掷骰人",
                type=EntityType.NPC,
                description="赌坊里讨生活的老千。",
            ),
            "npc_owner": Entity(
                id="npc_owner",
                name="金三爷",
                type=EntityType.NPC,
                description="码头一带的地下钱庄主人。",  # no direct term — graph flags it
            ),
            "npc_clean": Entity(
                id="npc_clean", name="采药童", type=EntityType.NPC, description="山里采药。"
            ),
        },
        relations=[Relation(source="npc_owner", target="npc_dicer", kind="employs")],
        quests={
            "quest_debt": Quest(id="quest_debt", title="赌债风波", objective="替船工抹平一笔赌债。")
        },
        terms={"term_house": Term(id="term_house", canonical="天和赌坊", description="城南赌坊。")},
    )


def _conflict_bundle() -> ContentBundle:
    """Two conflicting relation pairs so ``max_judge=1`` leaves exactly one candidate unjudged."""
    return ContentBundle(
        entities={
            "fac_a": Entity(id="fac_a", name="A", type=EntityType.FACTION),
            "fac_b": Entity(id="fac_b", name="B", type=EntityType.FACTION),
            "fac_c": Entity(id="fac_c", name="C", type=EntityType.FACTION),
            "fac_d": Entity(id="fac_d", name="D", type=EntityType.FACTION),
        },
        relations=[
            Relation(source="fac_a", target="fac_b", kind="盟友"),
            Relation(source="fac_a", target="fac_b", kind="死敌"),
            Relation(source="fac_c", target="fac_d", kind="盟友"),
            Relation(source="fac_c", target="fac_d", kind="附庸"),
        ],
    )


def _client(tmp_path, monkeypatch, bundle: ContentBundle) -> TestClient:
    root = tmp_path / "content"
    ContentStore(root).save(bundle)
    monkeypatch.setenv(
        "OWCOPILOT_PROJECTS_JSON", json.dumps({"demo": str(root).replace("\\", "/")})
    )
    monkeypatch.delenv("OWCOPILOT_API_KEY", raising=False)
    # managed worlds must live under the test home, never the developer's real one
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    return TestClient(create_app())


# ------------------------------------------------------- A1: sweep dismiss through the action layer
def test_action_sweep_dismiss_persists_across_reruns(tmp_path) -> None:
    root = tmp_path / "world"
    ContentStore(root).save(_gambling_bundle())

    first = run_theme_sweep_action(str(root), theme="赌", extra_terms=["押"])
    # finding_id (the dismissal handle) and locator (sentence-level location) are visible in the
    # product response — the whole point of "逐一排除 + 句级定位 对产品可见".
    dicer = next(f for f in first["hits"] if f["ref"] == "entity:npc_dicer")
    assert dicer["finding_id"]
    assert dicer["locator"] == "description"
    assert first["dismissed_count"] == 0  # A2: nothing dismissed yet

    dismissed = dismiss_sweep_finding_action(
        str(root),
        theme="赌",
        finding_id=dicer["finding_id"],
        ref=dicer["ref"],
        locator=dicer["locator"],
        layer=dicer["layer"],
        operator="ed",
        reason="市井背景，非赌博内容",
    )
    assert dismissed["dismissed"] is True
    assert dismissed["finding_id"] == dicer["finding_id"]
    assert dismissed["exclusion"]["operator"] == "ed"  # signed audit trail persisted

    # A re-sweep of the SAME theme (a later session, fresh project open) no longer surfaces it, and
    # the suppression is honestly counted — the dismiss travelled the product path and stuck.
    second = run_theme_sweep_action(str(root), theme="赌", extra_terms=["押"])
    assert dicer["finding_id"] not in {f["finding_id"] for f in second["hits"]}
    assert second["dismissed_count"] == 1


def test_action_dismiss_requires_a_signature(tmp_path) -> None:
    root = tmp_path / "world"
    ContentStore(root).save(_gambling_bundle())
    target = run_theme_sweep_action(str(root), theme="赌")["hits"][0]
    with pytest.raises(ValueError, match="署名"):
        dismiss_sweep_finding_action(
            str(root),
            theme="赌",
            finding_id=target["finding_id"],
            ref=target["ref"],
            locator=target["locator"],
            layer=target["layer"],
            operator="   ",
        )


# ------------------------------------------------------- A1: sweep dismiss through the REST layer
def test_rest_sweep_dismiss_round_trip(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch, _gambling_bundle())

    first = client.post("/projects/demo/sweeps:run", json={"theme": "赌", "extra_terms": ["押"]})
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["dismissed_count"] == 0  # A2 additive field present, backward compatible
    assert "judge_skipped" in body
    dicer = next(f for f in body["hits"] if f["ref"] == "entity:npc_dicer")
    assert dicer["finding_id"] and dicer["locator"] == "description"  # exposed over REST

    dismiss = client.post(
        "/projects/demo/sweeps:dismiss",
        json={
            "theme": "赌",
            "finding_id": dicer["finding_id"],
            "ref": dicer["ref"],
            "locator": dicer["locator"],
            "layer": dicer["layer"],
            "operator": "ed",
            "reason": "市井背景",
        },
    )
    assert dismiss.status_code == 200, dismiss.text
    assert dismiss.json()["dismissed"] is True

    second = client.post(
        "/projects/demo/sweeps:run", json={"theme": "赌", "extra_terms": ["押"]}
    ).json()
    assert dicer["finding_id"] not in {f["finding_id"] for f in second["hits"]}
    assert second["dismissed_count"] == 1


def test_rest_sweep_dismiss_rejects_empty_signature(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch, _gambling_bundle())
    target = client.post("/projects/demo/sweeps:run", json={"theme": "赌"}).json()["hits"][0]
    resp = client.post(
        "/projects/demo/sweeps:dismiss",
        json={
            "theme": "赌",
            "finding_id": target["finding_id"],
            "ref": target["ref"],
            "locator": target["locator"],
            "layer": target["layer"],
            "operator": "   ",  # whitespace-only signature is refused with a guided 422
        },
    )
    assert resp.status_code == 422
    assert "署名" in resp.text


# ------------------------------------------- A2: no dismissals → the sweep response is unchanged
def test_sweep_without_dismissals_is_backward_compatible(tmp_path) -> None:
    root = tmp_path / "world"
    ContentStore(root).save(_gambling_bundle())
    result = run_theme_sweep_action(str(root), theme="赌", extra_terms=["押"])
    # The pre-existing contract is intact: hits/markdown/counters all present, nothing suppressed.
    assert result["scanned_total"] == 5  # 3 entities + 1 quest + 1 term
    assert any(f["ref"] == "entity:npc_dicer" for f in result["hits"])
    assert result["dismissed_count"] == 0
    assert "专项清查工作单" in result["markdown"]


# ------------------------------------------- A1: contradiction judge_skipped through action + REST
def test_action_contradiction_exposes_judge_skipped(tmp_path) -> None:
    root = tmp_path / "world"
    ContentStore(root).save(_conflict_bundle())
    # offline judge (llm_mode default "offline"), capped at 1 → one candidate never reaches it
    result = detect_contradictions_action(str(root), use_llm=True, max_judge=1)
    assert result["candidate_count"] == 2
    assert result["judged_count"] == 1
    assert result["judge_skipped"] == 1  # honest counter now visible to the product


def test_rest_contradiction_exposes_judge_skipped(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch, _conflict_bundle())
    resp = client.post(
        "/projects/demo/contradictions:scan", json={"use_llm": True, "max_judge": 1}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["candidate_count"] == 2
    assert body["judged_count"] == 1
    assert body["judge_skipped"] == 1
