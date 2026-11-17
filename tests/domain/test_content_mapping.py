from __future__ import annotations

from owcopilot.content.importers.base import RawObject
from owcopilot.content.mapping import FieldMapping, apply_field_mapping


def test_field_mapping_renames_columns_and_sets_default_kind() -> None:
    raw = RawObject(
        kind="entity",
        data={"编号": "npc_aldric", "名称": "Aldric", "类型": "npc"},
        source_path="table.xlsx",
        row=2,
    )

    mapped = apply_field_mapping(
        [raw],
        FieldMapping(
            columns={"编号": "id", "名称": "name", "类型": "type"},
            default_kind="entity",
        ),
    )

    assert mapped[0].kind == "entity"
    assert mapped[0].data["id"] == "npc_aldric"
    assert mapped[0].data["name"] == "Aldric"
    assert mapped[0].data["type"] == "npc"
    assert mapped[0].row == 2


def test_field_mapping_default_kind_does_not_overwrite_business_kind() -> None:
    raw = RawObject(
        kind="entity",
        data={"来源阵营": "fac_a", "关系": "enemy_of", "目标阵营": "fac_b"},
        source_path="relations.xlsx",
        row=2,
    )

    mapped = apply_field_mapping(
        [raw],
        FieldMapping(
            columns={"来源阵营": "source", "关系": "kind", "目标阵营": "target"},
            default_kind="relation",
        ),
    )

    assert mapped[0].kind == "relation"
    assert mapped[0].data["kind"] == "enemy_of"
