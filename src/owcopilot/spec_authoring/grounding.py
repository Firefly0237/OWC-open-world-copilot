# 闭世界符号接地：把意图里的大白话符号钉死到状态机真实 flag（库外符号一律打回）。
"""Closed-world symbol grounding — the first wall against hallucinated flags.

Game flags are a finite, enumerable set, so grounding a surface name ("密钥已获取") to a real symbol
degrades to table lookup + normalization, not open-world entity linking. The hard rule: a surface
that resolves to no logic flag is rejected. A surface that names a canon *entity* but has no flag
representing its state is rejected with a specific, actionable message (declare a flag first) —
never silently accepted, which is exactly how a mis-grounded spec would pass vacuously.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..content.models import ContentBundle, Quest
from .models import OperandGrounding, OperandStatus


def _normalize(surface: str) -> str:
    """Fold a surface for tolerant matching: drop surrounding quotes/brackets, spaces and casing."""
    stripped = surface.strip().strip("「」『』\"'“”【】《》()（）")
    return "".join(stripped.split()).casefold()


@dataclass
class SymbolCatalog:
    """The closed world a spec may reference: this quest's logic flags (the checkable symbols) plus
    the bundle's entity surfaces (only to explain an ``entity_no_flag`` rejection)."""

    quest_id: str
    # normalized surface -> (flag_id, flag_name); exact id/name keys are added verbatim too.
    flag_by_surface: dict[str, tuple[str, str]] = field(default_factory=dict)
    flag_ids: set[str] = field(default_factory=set)
    entity_by_surface: dict[str, str] = field(default_factory=dict)  # normalized -> entity id

    def flag_display_name(self, flag_id: str) -> str:
        for fid, name in self.flag_by_surface.values():
            if fid == flag_id:
                return name or flag_id
        return flag_id


def build_symbol_catalog(quest: Quest, bundle: ContentBundle | None = None) -> SymbolCatalog:
    """Build the closed-world catalog for ``quest``: its declared logic flags (matchable by id or
    name) and, when a bundle is given, entity names/aliases (for the ``entity_no_flag`` message)."""
    catalog = SymbolCatalog(quest_id=quest.id)
    if quest.logic is not None:
        for var in quest.logic.variables:
            catalog.flag_ids.add(var.id)
            entry = (var.id, var.name or var.id)
            catalog.flag_by_surface[_normalize(var.id)] = entry
            if var.name:
                catalog.flag_by_surface.setdefault(_normalize(var.name), entry)
    if bundle is not None:
        for entity in bundle.entities.values():
            catalog.entity_by_surface.setdefault(_normalize(entity.name), entity.id)
            for alias in entity.aliases:
                catalog.entity_by_surface.setdefault(_normalize(alias), entity.id)
    return catalog


def ground_surface(surface: str, catalog: SymbolCatalog) -> OperandGrounding:
    """Resolve one surface against the closed world: exact flag id → flag name/alias → entity (a
    rejection with guidance) → unknown (rejection). Never invents a symbol."""
    raw = surface.strip()
    if raw in catalog.flag_ids:
        name = catalog.flag_display_name(raw)
        return OperandGrounding(
            surface=surface, status=OperandStatus.GROUNDED, flag_id=raw, flag_name=name,
            confidence=1.0, note="按 flag id 精确接地。",
        )
    key = _normalize(surface)
    hit = catalog.flag_by_surface.get(key)
    if hit is not None:
        flag_id, flag_name = hit
        exact = key == _normalize(flag_name) or key == _normalize(flag_id)
        note = (
            "按 flag 名精确接地。" if exact
            else "按规范化 flag 名接地（低置信度，建议人工复核）。"
        )
        return OperandGrounding(
            surface=surface, status=OperandStatus.GROUNDED, flag_id=flag_id, flag_name=flag_name,
            confidence=1.0 if exact else 0.8, note=note,
        )
    entity_id = catalog.entity_by_surface.get(key)
    if entity_id is not None:
        return OperandGrounding(
            surface=surface, status=OperandStatus.ENTITY_NO_FLAG,
            note=(
                f"『{surface}』对应设定实体 '{entity_id}'，但没有表示其状态的逻辑 flag；"
                "请先为该状态声明一个 flag，或直接引用已有 flag 名。"
            ),
        )
    return OperandGrounding(
        surface=surface, status=OperandStatus.UNKNOWN,
        note=f"『{surface}』不在该任务的 flag 表内（闭世界外符号，已打回）。",
    )
