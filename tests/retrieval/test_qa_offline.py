from __future__ import annotations

import json

from owcopilot.qa.offline import OfflineQAProvider


def _complete(query: str, context_lines: list[str]) -> dict:
    system = "Lore context:\n" + "\n".join(context_lines)
    raw, _input_tokens, _output_tokens = OfflineQAProvider().complete(
        system=system,
        user=query,
        model="offline",
    )
    return json.loads(raw)


def test_offline_qa_expands_referenced_ids_for_relation_facts() -> None:
    payload = _complete(
        "陆忘所属的势力和控制雾隐渡口的势力是什么关系?",
        [
            "- [entity:npc_lu_wang] 陆忘: faction=fac_xuantie",
            "- [poi:poi_wuyin_dock] 雾隐渡口: controlling_faction=fac_heifeng",
            (
                "- [entity:fac_xuantie] 玄铁盟: relation entity:fac_xuantie "
                "enemy_of entity:fac_heifeng"
            ),
            "- [entity:fac_heifeng] 黑风寨: 山匪",
        ],
    )

    assert payload["refused"] is False
    assert "enemy_of" in payload["answer"]
    assert {"entity:fac_xuantie", "entity:fac_heifeng"} <= {
        citation["ref"] for citation in payload["citations"]
    }


def test_offline_qa_refuses_missing_property_in_context() -> None:
    payload = _complete(
        "白素素的师父是谁?",
        ["- [entity:npc_bai_susu] 白素素: 黑风寨二当家"],
    )

    assert payload["refused"] is True
    assert payload["citations"] == []
