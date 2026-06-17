"""WS-G · template / archetype library: deterministic, parameterized starting points.

A blank world is an adoption barrier. Templates let an author stamp out a quest or faction from an
archetype by filling a few parameters — produced deterministically (no model), then routed through
the SAME review queue as any draft (human still signs off). Adding a template = data, not code.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ..content.models import (
    ContentBundle,
    Entity,
    EntityType,
    Origin,
    Quest,
    QuestStage,
    ReviewStatus,
    Reward,
)
from ..util import slugify


class TemplateParam(BaseModel):
    key: str
    label: str
    required: bool = True
    placeholder: str = ""


class TemplateDef(BaseModel):
    id: str
    name: str
    kind: str  # "quest" | "faction"
    description: str
    params: list[TemplateParam] = Field(default_factory=list)


def _slug(text: str, *, prefix: str) -> str:
    return f"{prefix}_{slugify(text, fallback='untitled')}"


def _unique(candidate: str, existing: set[str]) -> str:
    out, n = candidate, 2
    while out in existing:
        out, n = f"{candidate}_{n}", n + 1
    existing.add(out)
    return out


_QUEST_TEMPLATES = {
    "quest_escort": (
        "护送任务",
        "把人/物从一地安全护送到另一地。",
        ["title", "giver", "from", "to", "reward"],
        lambda p: (
            f"护送目标从{p.get('from', '起点')}安全抵达{p.get('to', '终点')}。",
            [
                ("s1", "接受护送委托"),
                ("s2", f"穿越{p.get('from', '起点')}至{p.get('to', '终点')}的路途"),
                ("s3", f"安全抵达{p.get('to', '终点')}"),
            ],
        ),
    ),
    "quest_investigate": (
        "调查任务",
        "在某地就某主题展开调查、取证。",
        ["title", "giver", "location", "subject"],
        lambda p: (
            f"在{p.get('location', '某地')}调查清楚「{p.get('subject', '疑点')}」的真相。",
            [
                ("s1", "接受调查委托"),
                ("s2", f"在{p.get('location', '某地')}走访取证"),
                ("s3", "汇报调查结论"),
            ],
        ),
    ),
    "quest_subdue": (
        "讨伐任务",
        "前往某地讨伐某目标。",
        ["title", "giver", "target", "location"],
        lambda p: (
            f"前往{p.get('location', '某地')}讨伐{p.get('target', '目标')}。",
            [
                ("s1", "接受讨伐委托"),
                ("s2", f"抵达{p.get('location', '某地')}"),
                ("s3", f"讨伐{p.get('target', '目标')}并回报"),
            ],
        ),
    ),
}


def list_templates() -> list[TemplateDef]:
    defs: list[TemplateDef] = []
    for tid, (name, desc, keys, _build) in _QUEST_TEMPLATES.items():
        defs.append(
            TemplateDef(
                id=tid,
                name=name,
                kind="quest",
                description=desc,
                params=[TemplateParam(key=k, label=k, required=k != "reward") for k in keys],
            )
        )
    defs.append(
        TemplateDef(
            id="faction",
            name="势力 / 组织",
            kind="faction",
            description="一个带主题与简介的势力骨架。",
            params=[
                TemplateParam(key="name", label="name"),
                TemplateParam(key="description", label="description", required=False),
                TemplateParam(key="theme", label="theme", required=False),
            ],
        )
    )
    return defs


def instantiate(
    template_id: str, params: dict[str, Any], *, existing_ids: set[str]
) -> ContentBundle:
    """Build the new content for a template + params (deterministic). Validates required params."""
    defs = {d.id: d for d in list_templates()}
    spec = defs.get(template_id)
    if spec is None:
        raise ValueError(f"模板不存在：{template_id}")
    missing = [p.key for p in spec.params if p.required and not str(params.get(p.key, "")).strip()]
    if missing:
        raise ValueError(f"缺少必填参数：{', '.join(missing)}")

    taken = set(existing_ids)
    bundle = ContentBundle()
    if spec.kind == "faction":
        fid = _unique(_slug(str(params["name"]), prefix="fac"), taken)
        bundle.entities[fid] = Entity(
            id=fid,
            name=str(params["name"]),
            type=EntityType.FACTION,
            description=str(params.get("description", "")).strip(),
            tags=[t for t in [str(params.get("theme", "")).strip()] if t],
            origin=Origin.HUMAN,
            review_status=ReviewStatus.PENDING_REVIEW,
        )
        return bundle

    name, desc, _keys, build = _QUEST_TEMPLATES[template_id]
    objective, stages = build(params)
    qid = _unique(_slug(str(params["title"]), prefix="quest"), taken)
    reward = str(params.get("reward", "")).strip()
    bundle.quests[qid] = Quest(
        id=qid,
        title=str(params["title"]),
        giver_npc=str(params.get("giver", "")).strip() or None,
        location=str(params.get("to") or params.get("location") or "").strip() or None,
        objective=objective,
        stages=[QuestStage(id=sid, summary=summary) for sid, summary in stages],
        rewards=[Reward(kind="misc", value=reward)] if reward else [],
        origin=Origin.HUMAN,
        review_status=ReviewStatus.PENDING_REVIEW,
    )
    return bundle


__all__ = ["TemplateDef", "TemplateParam", "instantiate", "list_templates"]
