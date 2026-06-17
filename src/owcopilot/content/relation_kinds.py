"""A curated, extensible catalog of relationship kinds.

``Relation.kind`` has always been a free string, so the data model never forced a fixed
taxonomy — but the UI needs something to offer. This catalog pre-provides the common kinds
(grouped by category, each marked directed or symmetric) so a planner picks from a menu, while a
fully custom kind is still allowed. Symmetric kinds (盟友/敌对/接壤…) are peer relations with no
direction — the graph draws them without an arrow — which is how we support decentralized,
non-hierarchical structure instead of forcing everything into a faction→member tree.
"""

from __future__ import annotations

from pydantic import BaseModel


class RelationKindDef(BaseModel):
    id: str
    label: str
    category: str
    symmetric: bool = False


# Ordered for display; categories group in the picker. Symmetric = peer/undirected.
_CATALOG: list[RelationKindDef] = [
    # 阵营 / 组织
    RelationKindDef(id="ally_of", label="盟友", category="阵营 · 组织", symmetric=True),
    RelationKindDef(id="enemy_of", label="敌对", category="阵营 · 组织", symmetric=True),
    RelationKindDef(id="rival_of", label="竞争", category="阵营 · 组织", symmetric=True),
    RelationKindDef(id="at_war_with", label="交战", category="阵营 · 组织", symmetric=True),
    RelationKindDef(id="trades_with", label="贸易往来", category="阵营 · 组织", symmetric=True),
    RelationKindDef(id="member_of", label="隶属", category="阵营 · 组织"),
    RelationKindDef(id="controls", label="控制", category="阵营 · 组织"),
    RelationKindDef(id="funds", label="资助", category="阵营 · 组织"),
    RelationKindDef(id="vassal_of", label="附庸", category="阵营 · 组织"),
    # 人物
    RelationKindDef(id="kin_of", label="亲属", category="人物", symmetric=True),
    RelationKindDef(id="friend_of", label="挚友", category="人物", symmetric=True),
    RelationKindDef(id="nemesis_of", label="宿敌", category="人物", symmetric=True),
    RelationKindDef(id="lover_of", label="恋人", category="人物", symmetric=True),
    RelationKindDef(id="companion_of", label="同伴", category="人物", symmetric=True),
    RelationKindDef(id="knows", label="相识", category="人物", symmetric=True),
    RelationKindDef(id="mentor_of", label="师从", category="人物"),
    RelationKindDef(id="superior_of", label="上级", category="人物"),
    RelationKindDef(id="employs", label="雇佣", category="人物"),
    RelationKindDef(id="owes", label="欠债于", category="人物"),
    # 地理
    RelationKindDef(id="located_in", label="位于", category="地理"),
    RelationKindDef(id="borders", label="接壤", category="地理", symmetric=True),
    RelationKindDef(id="leads_to", label="通往", category="地理"),
    # 叙事
    RelationKindDef(id="involves", label="涉及", category="叙事"),
    RelationKindDef(id="references", label="引用", category="叙事"),
    RelationKindDef(id="triggers", label="触发", category="叙事"),
    RelationKindDef(id="requires", label="前置", category="叙事"),
]

_BY_ID = {kind.id: kind for kind in _CATALOG}
_SYMMETRIC_IDS = {kind.id for kind in _CATALOG if kind.symmetric}


def relation_kind_catalog() -> list[RelationKindDef]:
    """The pre-provided relationship kinds (the picker shows these; custom kinds still allowed)."""
    return list(_CATALOG)


def relation_kind_label(kind: str) -> str:
    """Display label for a kind id, falling back to the raw kind for custom relations."""
    known = _BY_ID.get(kind)
    return known.label if known else kind


def is_symmetric_kind(kind: str) -> bool:
    """Whether a *known* kind is a peer/undirected relation. Custom kinds default to directed."""
    return kind in _SYMMETRIC_IDS
