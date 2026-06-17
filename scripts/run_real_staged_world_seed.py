"""Real-LLM head-to-head: single-shot world seed vs the staged grounding chain.

Both runs hit the SAME real model with the SAME brief and the SAME inspiration reference, and both
are normalised by the SAME ``_bundle_from_payload`` — only the generation strategy differs (one big
call vs premise→factions→regions→cast→quests). We then score coherence on the free text the model
actually wrote (quest objectives and stage summaries that name the cast/places it defined), because
that prose is something the deterministic id round-robin in ``_bundle_from_payload`` can never
fabricate after the fact — so it is a fair measure of whether the world is internally connected.

This spends a few cents of provider tokens. Usage:
    .venv\\Scripts\\python.exe scripts\\run_real_staged_world_seed.py
    .venv\\Scripts\\python.exe scripts\\run_real_staged_world_seed.py --model deepseek-v4-flash
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from owcopilot.app.actions import add_reference_action  # noqa: E402
from owcopilot.content.models import ContentBundle  # noqa: E402
from owcopilot.content.store import ContentStore  # noqa: E402
from owcopilot.llm.cache import NoOpCache  # noqa: E402
from owcopilot.llm.gateway import LLMGateway, OpenAICompatProvider  # noqa: E402
from owcopilot.llm.router import StaticRouter  # noqa: E402
from owcopilot.llm.telemetry import TelemetryCollector  # noqa: E402
from owcopilot.pipeline.project import ProjectContext  # noqa: E402
from owcopilot.telemetry import llm_step, summarize_workflow  # noqa: E402
from owcopilot.util import load_dotenv, use_utf8_stdout  # noqa: E402
from owcopilot.worldgen import WorldSeedBrief, parse_world_seed_payload  # noqa: E402
from owcopilot.worldgen.service import (  # noqa: E402
    _brief_user_message,
    _bundle_from_payload,
    _context_lines,
    _reference_report,
    _section_plan,
)

BRIEF: dict[str, Any] = {
    "idea": "一个靠蒸汽巨树维持生命的边境群岛，玩家调查能源衰竭背后的旧战争记忆。",
    "medium": "开放世界游戏",
    "game_genre": "开放世界 RPG",
    "world_styles": ["蒸汽朋克", "魔幻"],
    "tone": "克制、悬疑、史诗",
    "era": "工业革命早期与古代仪式并存",
    "player_fantasy": "流亡调查员",
    "core_conflict": "能源衰竭、阶层对立、旧神信仰复苏",
    "reference_mode": "参考剧情结构",
    "reference_query": "three faction infrastructure forest ritual free city",
    "faction_count": 3,
    "region_count": 2,
    "npc_count": 6,
    "quest_count": 4,
    "term_count": 4,
}

REFERENCE_TEXT = (
    "A design reference for a three-faction open-world RPG: an industrial guild controls failing "
    "infrastructure, a forest order protects memory rituals, and a free city trades secrets "
    "between both sides. The desired structure is exploration, evidence gathering, faction "
    "choice, and visible consequences in hub locations."
)


def _single_shot_system(brief: WorldSeedBrief, project_pack: Any, inspiration_pack: Any) -> str:
    """The pre-refactor single prompt (verbatim from git HEAD) — a faithful baseline."""
    project_lines = _context_lines(project_pack.hits)
    inspiration_lines = _context_lines(inspiration_pack.hits)
    return (
        "You are a senior worldbuilding and narrative designer. Create an original "
        "structured world seed from the creator's brief, in the brief's own genre, medium "
        "and language — do not assume a default genre or audience. "
        "Return ONE JSON object only. Do not wrap it in markdown. "
        "The JSON keys must be: summary, style_guide, factions, regions, locations, npcs, "
        "quests, terms, relations, reference_report. "
        "Per-item fields the pipeline reads — factions: name, description; "
        "regions: name, description, themes, level_min, level_max; "
        "locations: name, description, purpose, region_id, controlling_faction, tags; "
        "npcs: name, description, faction_id, location_id; "
        "quests: title (a player-facing quest-log headline, never an id), objective "
        "(one concrete sentence: who wants what done and why it matters now), giver_npc, "
        "location, stages (2-4 stage summaries, each naming where it happens and what the "
        "player does, referencing npcs/locations you defined), tags; "
        "terms: canonical, description (an in-world definition stating what the word "
        "concretely means in THIS world), aliases; "
        "relations: source, target, kind — use ids/names you defined above. "
        "Descriptions must be specific enough for a level designer to build from: concrete "
        "detail, a current tension or hook, no generic filler. "
        "Use uploaded references only as inspiration or structure according to reference_mode; "
        "do not treat them as canonical lore facts. If project facts are provided, preserve them "
        "as higher-priority facts. Avoid long verbatim reuse from references unless the brief "
        "explicitly asks for quotation. "
        "Each reference_report item must include source_ref, source_title, used_for, "
        "transformation, excluded. "
        + _section_plan(brief)
        + "\n\nProject facts context:\n"
        + ("\n".join(project_lines) if project_lines else "(none)")
        + "\n\nInspiration reference context:\n"
        + ("\n".join(inspiration_lines) if inspiration_lines else "(none)")
    )


def _real_gateway(model: str) -> tuple[LLMGateway, TelemetryCollector]:
    telemetry = TelemetryCollector()
    provider = OpenAICompatProvider(model=model, timeout=240.0, max_output_tokens=6000)
    gateway = LLMGateway(
        providers={"cheap": provider},
        router=StaticRouter(mapping={"world_seed": "cheap"}),
        cache=NoOpCache(),
        telemetry=telemetry,
        max_retries=1,
        retry_backoff_seconds=1.0,
    )
    return gateway, telemetry


def _single_shot(content_root: Path, model: str) -> dict[str, Any]:
    """One call for the whole world, then the same deterministic normaliser the staged path uses."""
    gateway, telemetry = _real_gateway(model)
    project = ProjectContext.open(content_root, sqlite_path=content_root / "single.sqlite")
    try:
        brief = WorldSeedBrief.model_validate(BRIEF)
        from owcopilot.retrieval.models import ContextPack

        query = brief.idea
        project_pack = ContextPack(query=query, budget_tokens=900)
        inspiration_pack = project.reference_context_builder.build(
            brief.reference_query, budget_tokens=1800, limit=8
        )
        raw = gateway.complete(
            task="world_seed",
            system=_single_shot_system(brief, project_pack, inspiration_pack),
            user=_brief_user_message(brief),
        )
        payload = parse_world_seed_payload(raw)
        bundle = _bundle_from_payload(
            payload,
            draft_id="single_shot",
            brief=brief,
            existing=ContentBundle(),
            inspiration_pack=inspiration_pack,
            project_pack=project_pack,
        )
        _ = _reference_report(payload, inspiration_pack, brief)
        summary = telemetry.summary()
        return {
            "bundle": bundle.model_dump(mode="json", exclude_none=True),
            "raw": payload,
            "telemetry": summary,
            "cost_usd": summarize_workflow([llm_step("world_seed", summary)]).budget.used_usd,
        }
    finally:
        project.close()


class _RecordingGateway:
    """Wraps a real gateway and records each stage's (system, raw) so the comparison can recover the
    staged chain's per-stage RAW output — duck-typed against ``LLMGateway.complete`` (scripts aren't
    mypy-checked, and the service only ever calls ``.complete``)."""

    def __init__(self, inner: LLMGateway) -> None:
        self.inner = inner
        self.calls: list[tuple[str, str, str]] = []

    def complete(self, *, task: str, system: str, user: str, tier: str | None = None) -> str:
        raw = self.inner.complete(task=task, system=system, user=user, tier=tier)
        self.calls.append((system, user, raw))
        return raw


def _staged(content_root: Path, model: str) -> dict[str, Any]:
    """The staged chain via WorldSeedService, recording per-stage raw output for the raw-fidelity
    read (the same call run_world_seed_action makes, just instrumented)."""
    from owcopilot.worldgen import WorldSeedService

    inner, telemetry = _real_gateway(model)
    recording = _RecordingGateway(inner)
    project = ProjectContext.open(content_root, sqlite_path=content_root / "staged.sqlite")
    seen: list[str] = []
    try:
        draft = WorldSeedService(
            gateway=recording,  # type: ignore[arg-type]
            bundle=project.bundle,
            project_context_builder=project.context_builder,
            reference_context_builder=project.reference_context_builder,
        ).generate(
            WorldSeedBrief.model_validate(BRIEF),
            progress=lambda _type, data: seen.append(str(data.get("name"))),
        )
        summary = telemetry.summary()
        return {
            "bundle": draft.bundle.model_dump(mode="json", exclude_none=True),
            "raw": _merge_raw(recording.calls),
            "stages_emitted": seen,
            "telemetry": summary,
            "cost_usd": summarize_workflow([llm_step("world_seed", summary)]).budget.used_usd,
        }
    finally:
        project.close()


def _merge_raw(calls: list[tuple[str, str, str]]) -> dict[str, Any]:
    """Reassemble the staged chain's raw per-stage JSON into one payload, keyed off the stage marker
    each system prompt carried (the same dispatch the offline double uses)."""
    from owcopilot.worldgen.stages import (
        CAST,
        FACTIONS,
        PREMISE,
        QUESTS,
        REGIONS,
        stage_from_system,
    )

    payload: dict[str, Any] = {
        "factions": [],
        "regions": [],
        "locations": [],
        "npcs": [],
        "quests": [],
        "terms": [],
    }
    for system, _user, raw in calls:
        stage = stage_from_system(system)
        try:
            slice_ = parse_world_seed_payload(raw)
        except Exception:  # noqa: BLE001 - a malformed stage just contributes nothing
            continue
        if stage == PREMISE:
            payload["terms"] = slice_.get("terms") or []
        elif stage == FACTIONS:
            payload["factions"] = slice_.get("factions") or []
        elif stage == REGIONS:
            payload["regions"] = slice_.get("regions") or []
            payload["locations"] = slice_.get("locations") or []
        elif stage == CAST:
            payload["npcs"] = slice_.get("npcs") or []
        elif stage == QUESTS:
            payload["quests"] = slice_.get("quests") or []
    return payload


def _raw_fidelity(payload: dict[str, Any]) -> dict[str, Any]:
    """The discriminating coherence read: of the cross-references the model WROTE (before
    ``_bundle_from_payload`` rescues dangling ids by round-robin), what fraction point at an entity
    the model actually defined? A single-shot world that invents a quest-giver it never added to the
    cast scores low here; a grounded chain that is handed the cast ids scores high."""

    def refset(items: Any) -> set[str]:
        out: set[str] = set()
        for item in items or []:
            if isinstance(item, dict):
                for key in ("id", "name"):
                    value = str(item.get(key) or "").strip()
                    if value:
                        out.add(value)
        return out

    npcs = [item for item in payload.get("npcs") or [] if isinstance(item, dict)]
    locs = [item for item in payload.get("locations") or [] if isinstance(item, dict)]
    quests = [item for item in payload.get("quests") or [] if isinstance(item, dict)]
    npc_refs, loc_refs = refset(npcs), refset(locs)
    fac_refs, reg_refs = refset(payload.get("factions")), refset(payload.get("regions"))

    def frac(items: list[dict[str, Any]], field: str, valid: set[str]) -> float | None:
        if not items:
            return None
        hit = sum(1 for item in items if str(item.get(field) or "").strip() in valid)
        return round(hit / len(items), 3)

    return {
        "quest_giver_resolves": frac(quests, "giver_npc", npc_refs),
        "quest_location_resolves": frac(quests, "location", loc_refs),
        "npc_faction_resolves": frac(npcs, "faction_id", fac_refs),
        "location_faction_resolves": frac(locs, "controlling_faction", fac_refs),
        "location_region_resolves": frac(locs, "region_id", reg_refs),
    }


def _coherence(bundle: dict[str, Any]) -> dict[str, Any]:
    """Score on the model's OWN prose: do quest objectives and stage summaries name the cast and
    places the world defines? Round-robin id-rescue cannot touch this text, so it is a fair read of
    whether the world is internally connected rather than a bag of disconnected lists."""
    entities = bundle.get("entities", {})
    names: set[str] = set()
    for entity in entities.values():
        if entity.get("name"):
            names.add(str(entity["name"]))
        for alias in entity.get("aliases") or []:
            names.add(str(alias))
    for region in bundle.get("regions", {}).values():
        if region.get("name"):
            names.add(str(region["name"]))
    for term in bundle.get("terms", {}).values():
        if term.get("canonical"):
            names.add(str(term["canonical"]))
    names = {name for name in names if len(name) >= 2}

    def mentions(text: str) -> bool:
        return any(name in text for name in names)

    quests = bundle.get("quests", {})
    total_stages = grounded_stages = obj_grounded = 0
    for quest in quests.values():
        if mentions(str(quest.get("objective", ""))):
            obj_grounded += 1
        for stage in quest.get("stages", []) or []:
            total_stages += 1
            if mentions(str(stage.get("summary", ""))):
                grounded_stages += 1
    relations = bundle.get("relations", [])
    return {
        "entities": len(entities),
        "relations": len(relations),
        "quests": len(quests),
        "stages_total": total_stages,
        "stage_text_grounded": grounded_stages,
        "stage_text_grounded_ratio": round(grounded_stages / total_stages, 3)
        if total_stages
        else 0,
        "objective_grounded_ratio": round(obj_grounded / len(quests), 3) if quests else 0,
        "relations_per_entity": round(len(relations) / max(1, len(entities)), 3),
    }


def _aggregate(fidelities: list[dict[str, Any]]) -> dict[str, Any]:
    """Mean ± population-std per fidelity metric across samples — the whole point of --samples is to
    stop reporting a single run as if it were a conclusion. None values (a metric with no items that
    run) are dropped before averaging, and ``n`` records how many runs actually contributed."""
    out: dict[str, Any] = {}
    for key in sorted({k for fidelity in fidelities for k in fidelity}):
        values = [
            fidelity[key] for fidelity in fidelities if isinstance(fidelity.get(key), (int, float))
        ]
        if not values:
            out[key] = {"n": 0, "mean": None, "std": None}
            continue
        mean = sum(values) / len(values)
        std = (sum((v - mean) ** 2 for v in values) / len(values)) ** 0.5
        out[key] = {
            "n": len(values),
            "mean": round(mean, 3),
            "std": round(std, 3),
            "min": round(min(values), 3),
            "max": round(max(values), 3),
        }
    return out


def _run_once(workspace: Path, run_id: str, sample: int, model: str) -> dict[str, Any]:
    single_root = workspace / run_id / f"single_{sample}"
    ContentStore(single_root).save(ContentBundle())
    add_reference_action(single_root, title="ref", text=REFERENCE_TEXT, allowed_uses=["structure"])
    single = _single_shot(single_root, model)

    staged_root = workspace / run_id / f"staged_{sample}"
    ContentStore(staged_root).save(ContentBundle())
    add_reference_action(staged_root, title="ref", text=REFERENCE_TEXT, allowed_uses=["structure"])
    staged = _staged(staged_root, model)

    return {
        "single_shot": {
            "cost_usd": single["cost_usd"],
            "raw_fidelity": _raw_fidelity(single["raw"]),
            "coherence": _coherence(single["bundle"]),
        },
        "staged": {
            "cost_usd": staged["cost_usd"],
            "raw_fidelity": _raw_fidelity(staged["raw"]),
            "coherence": _coherence(staged["bundle"]),
        },
    }


def main() -> int:
    use_utf8_stdout()
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="deepseek-v4-flash")
    parser.add_argument("--workspace", default=".tmp/real_staged_world_seed")
    parser.add_argument("--output", default=".tmp/real_staged_world_seed.json")
    parser.add_argument(
        "--samples", type=int, default=1, help="Repeat N times and aggregate (escape n=1)."
    )
    args = parser.parse_args()

    load_dotenv()
    os.environ.setdefault("OWCOPILOT_PROVIDER_TIMEOUT_SEC", "240")
    run_id = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    samples = max(1, args.samples)

    runs = [_run_once(Path(args.workspace), run_id, i, args.model) for i in range(samples)]

    report = {
        "model": args.model,
        "run_id": run_id,
        "samples": samples,
        "brief": BRIEF,
        "single_shot": {
            "per_sample": [r["single_shot"] for r in runs],
            "fidelity_aggregate": _aggregate([r["single_shot"]["raw_fidelity"] for r in runs]),
            "total_cost_usd": round(sum(r["single_shot"]["cost_usd"] for r in runs), 6),
        },
        "staged": {
            "per_sample": [r["staged"] for r in runs],
            "fidelity_aggregate": _aggregate([r["staged"]["raw_fidelity"] for r in runs]),
            "total_cost_usd": round(sum(r["staged"]["cost_usd"] for r in runs), 6),
        },
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
