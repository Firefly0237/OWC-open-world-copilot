"""Recognize entities + relations from an articy:draft JSON export.

We walk the documented export shape — ``Packages[].Models[]`` (each model a ``{Type, Properties}``),
``GlobalVariables``, ``Hierarchy`` — the same structure the community parsers traverse
(AaronJessen/ArticyProjectJsonParser maps it into SQLite; DasDingoCodes/articy3RenPy-Code-Generator
walks Flow + entities + variables to emit Ren'Py). We borrowed their reading of the format but not
their goal: instead of a database dump or engine code, we lift it into OWCopilot's content model as
*reviewable proposals*, classifying worldbuilding objects vs. flow nodes and turning Connections /
Speaker links into relations between them.

Parsing is deliberately tolerant — articy templates rename ``Type`` freely and the schema drifts
across versions — so anything unrecognized is skipped with a warning, never guessed at. No LLM here.
"""

from __future__ import annotations

from typing import Any

from .models import ImportPlan, ProposedEntity, ProposedRelation, SourceRef

# Flow/structural node types — narrative units, surfaced as "event" entities so Connections between
# them resolve. Everything else with a label (Entity, or a custom template) is a worldbuilding obj.
_FLOW_TYPES = {
    "Dialogue",
    "DialogueFragment",
    "FlowFragment",
    "Hub",
    "Jump",
    "Condition",
    "Instruction",
    "Comment",
}
_CONNECTION_TYPE = "Connection"
# Built-in articy entity templates we map confidently; unknown templates → "concept" for re-typing.
_ENTITY_TYPE_MAP = {"location": "location", "zone": "region", "spot": "location"}


def _props(model: Any) -> dict[str, Any]:
    props = model.get("Properties") if isinstance(model, dict) else None
    return props if isinstance(props, dict) else {}


def _endpoint_id(value: Any) -> str:
    """A Connection endpoint is ``{"IdRef": "0x..", "PinRef": "0x.."}`` or sometimes a bare id."""
    if isinstance(value, dict):
        ref = value.get("IdRef") or value.get("Target") or value.get("Id")
        return str(ref).strip() if ref else ""
    return str(value).strip() if value else ""


def _label(props: dict[str, Any]) -> str:
    for key in ("DisplayName", "MenuText", "Text", "TechnicalName"):
        val = props.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
        if isinstance(val, dict):  # localized strings export as {"en": "...", ...}
            for inner in val.values():
                if isinstance(inner, str) and inner.strip():
                    return inner.strip()
    return ""


def recognize_articy(data: Any, *, source_file: str = "") -> ImportPlan:
    """Turn a parsed articy:draft JSON export into a reviewable ImportPlan."""
    warnings: list[str] = []
    if not isinstance(data, dict):
        return ImportPlan(source_format="articy", warnings=["不是合法的 articy JSON 对象"])

    packages = data.get("Packages")
    if not isinstance(packages, list):
        warnings.append("缺少 Packages 数组——可能不是 articy:draft 的 JSON 导出")
        packages = []

    entities: list[ProposedEntity] = []
    entity_ids: set[str] = set()
    connections: list[tuple[str, str]] = []  # (source_id, target_id)
    speakers: list[tuple[str, str]] = []  # (speaker_entity_id, fragment_id)

    for package in packages:
        if not isinstance(package, dict):
            continue
        models = package.get("Models")
        if not isinstance(models, list):
            continue
        for model in models:
            if not isinstance(model, dict):
                continue
            mtype = str(model.get("Type", "")).strip()
            props = _props(model)
            obj_id = str(props.get("Id", "")).strip()

            if mtype == _CONNECTION_TYPE:
                src = _endpoint_id(props.get("Source"))
                tgt = _endpoint_id(props.get("Target"))
                if src and tgt:
                    connections.append((src, tgt))
                continue
            if not obj_id:
                warnings.append(f"跳过一个无 Id 的 {mtype or '对象'}")
                continue

            speaker = str(props.get("Speaker", "")).strip()
            if speaker:
                speakers.append((speaker, obj_id))

            if mtype in _FLOW_TYPES:
                our_type = "event"
            else:
                our_type = _ENTITY_TYPE_MAP.get(mtype.lower(), "concept")
            text = props.get("Text")
            entity_ids.add(obj_id)
            entities.append(
                ProposedEntity(
                    id=obj_id,
                    name=_label(props) or obj_id,
                    type=our_type,
                    description=text.strip() if isinstance(text, str) else "",
                    fields={"articy_type": mtype} if mtype else {},
                    source_ref=SourceRef(file=source_file, locator=f"articy:{obj_id}"),
                )
            )

    relations: list[ProposedRelation] = []
    for src, tgt in connections:
        if src in entity_ids and tgt in entity_ids:
            relations.append(
                ProposedRelation(
                    source=src, target=tgt, kind="leads_to", evidence="articy Connection",
                    source_ref=SourceRef(file=source_file, locator=f"articy:{src}->{tgt}"),
                )
            )
    for speaker, fragment in speakers:
        if speaker in entity_ids and fragment in entity_ids:
            relations.append(
                ProposedRelation(
                    source=speaker, target=fragment, kind="speaks_in", evidence="articy Speaker",
                    source_ref=SourceRef(file=source_file, locator=f"articy:{speaker}@{fragment}"),
                )
            )

    variables: list[dict[str, Any]] = []
    for namespace in data.get("GlobalVariables") or []:
        if not isinstance(namespace, dict):
            continue
        ns_name = str(namespace.get("Namespace", "")).strip()
        for var in namespace.get("Variables") or []:
            if isinstance(var, dict):
                variables.append(
                    {
                        "namespace": ns_name,
                        "variable": str(var.get("Variable", "")).strip(),
                        "type": str(var.get("Type", "")).strip(),
                        "value": var.get("Value"),
                    }
                )

    return ImportPlan(
        source_format="articy",
        entities=entities,
        relations=relations,
        variables=variables,
        warnings=warnings,
    )
