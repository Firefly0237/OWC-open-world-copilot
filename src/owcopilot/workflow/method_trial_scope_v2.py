"""Task-bound, revocable method trials over the production execution path.

A trial permit never grants adoption. Preparation and subsequent dispatches check
the same task revision, requirements and immutable candidate version.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from ..content.hash import content_hash
from .models import new_id, now


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _sealed(value: dict[str, Any], field: str) -> dict[str, Any]:
    return {**value, field: content_hash(value)}


def _read(raw: str, field: str) -> dict[str, Any]:
    value = json.loads(raw)
    if value.get(field) != content_hash({key: item for key, item in value.items() if key != field}):
        raise ValueError("技术试用scope记录完整性错误")
    return value


def _initialize(store) -> None:
    with store.connection() as db:
        db.executescript("""
          CREATE TABLE IF NOT EXISTS method_trial_scopes(
            id TEXT PRIMARY KEY,project TEXT NOT NULL,payload TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS method_trial_scope_events(
            seq INTEGER PRIMARY KEY AUTOINCREMENT,scope_id TEXT NOT NULL,payload TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS method_trial_scope_claims(
            scope_id TEXT PRIMARY KEY,project TEXT NOT NULL,payload TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS method_trial_scope_results(
            scope_id TEXT PRIMARY KEY,project TEXT NOT NULL,payload TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS method_trial_scope_grants(
            project TEXT NOT NULL,operation_id TEXT NOT NULL,request_hash TEXT NOT NULL,
            scope_id TEXT NOT NULL,PRIMARY KEY(project,operation_id));
        """)


def _scope(db, project, scope_id):
    row = db.execute(
        "SELECT payload FROM method_trial_scopes WHERE id=? AND project=?", (scope_id, project)
    ).fetchone()
    if row is None:
        raise ValueError("没有该任务的明确技术试用作用域")
    permit = _read(row[0], "permit_hash")
    events = [
        _read(row[0], "event_hash")
        for row in db.execute(
            "SELECT payload FROM method_trial_scope_events WHERE scope_id=? ORDER BY seq",
            (scope_id,),
        )
    ]
    return {
        "permit": permit,
        "events": events,
        "status": events[-1]["status"] if events else "active",
    }


def grant_trial_scope(
    store,
    project,
    task_id,
    version_ids,
    *,
    machine_requirements,
    authority: Literal[
        "development_technical_trial", "explicit_author_action", "preauthorized_policy"
    ],
    reason,
    operation_id: str | None = None,
) -> dict[str, Any]:
    from .requirements_v2 import MachineRequirements
    from .v2_skills import NarrativeEvolutionStore

    if (
        authority not in {
            "development_technical_trial", "explicit_author_action", "preauthorized_policy"
        }
        or not reason.strip()
        or operation_id is not None and not operation_id.strip()
    ):
        raise ValueError("技术试用必须有明确目的和授权来源")
    task = store.get_task(task_id)
    if task.project != project or not version_ids or len(version_ids) != len(set(version_ids)):
        raise ValueError("技术试用任务／版本不合法")
    skills = NarrativeEvolutionStore(store)
    versions = {}
    for version_id in version_ids:
        version = skills.get_version(project, version_id)
        family = skills.get_family(project, version["family_id"])
        if (
            version["state"]["trial_status"] != "allowed"
            or family["disabled"]
            or version["state"]["author_acceptance"] == "rejected"
        ):
            raise ValueError("作用域不能绕过方法版本原有的试用许可")
        versions[version_id] = version["version_hash"]
    requirements = MachineRequirements.model_validate(machine_requirements or {}).model_dump(
        mode="json"
    )
    request_hash = content_hash(
        [project, task.id, task.revision, task.base_hash, versions, requirements, authority, reason]
    )
    permit = _sealed(
        {
            "id": new_id(),
            "project": project,
            "task_id": task_id,
            "task_revision": task.revision,
            "base_hash": task.base_hash,
            "requirements_hash": content_hash(requirements),
            "requirements": requirements,
            "allowed_versions": versions,
            "authority": authority,
            "purpose": "technical_transfer",
            "reason": reason,
            "author_acceptance": "unknown",
            "created_at": now(),
        },
        "permit_hash",
    )
    _initialize(store)
    with store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        current = db.execute("SELECT payload FROM tasks WHERE id=?", (task.id,)).fetchone()
        snapshot = json.loads(current[0]) if current else None
        if snapshot is None or (
            snapshot["project"], snapshot["revision"], snapshot["base_hash"]
        ) != (project, task.revision, task.base_hash):
            raise ValueError("许可保存前任务版本已改变，请重新读取")
        if operation_id:
            previous = db.execute(
                "SELECT request_hash,scope_id FROM method_trial_scope_grants "
                "WHERE project=? AND operation_id=?", (project, operation_id)
            ).fetchone()
            if previous:
                if previous[0] != request_hash:
                    raise ValueError("同一试用许可命令不能更改任务、范围或理由")
                return _scope(db, project, previous[1])["permit"]
        # Multiple windows granting the same range share one revocable permit.
        # A fresh command after revocation remains an explicit new grant.
        if operation_id:
            for row in db.execute(
                "SELECT id FROM method_trial_scopes WHERE project=? ORDER BY rowid DESC", (project,)
            ).fetchall():
                existing = _scope(db, project, row[0])
                value = existing["permit"]
                if (
                    existing["status"] == "active"
                    and (value["task_id"], value["task_revision"], value["base_hash"])
                    == (task.id, task.revision, task.base_hash)
                    and value["allowed_versions"] == versions
                    and value["requirements_hash"] == content_hash(requirements)
                ):
                    db.execute(
                        "INSERT INTO method_trial_scope_grants VALUES(?,?,?,?)",
                        (project, operation_id, request_hash, value["id"]),
                    )
                    return value
        db.execute(
            "INSERT INTO method_trial_scopes VALUES(?,?,?)", (permit["id"], project, _json(permit))
        )
        if operation_id:
            db.execute(
                "INSERT INTO method_trial_scope_grants VALUES(?,?,?,?)",
                (project, operation_id, request_hash, permit["id"]),
            )
    return permit


def get_trial_scope(store, project, scope_id) -> dict[str, Any]:
    _initialize(store)
    with store.connection() as db:
        return _scope(db, project, scope_id)


def task_trial_scopes(store, project, task_id) -> list[dict[str, Any]]:
    task = store.get_task(task_id)
    if task.project != project:
        raise ValueError("任务不属于当前项目")
    _initialize(store)
    with store.connection() as db:
        rows = db.execute(
            "SELECT id,payload FROM method_trial_scopes WHERE project=? ORDER BY rowid DESC",
            (project,),
        ).fetchall()
        return [
            _scope(db, project, row[0])
            for row in rows
            if _read(row[1], "permit_hash")["task_id"] == task_id
        ]


def validate_trial_scope(
    store, task, version_id, scope_id, expected_permit_hash, machine_requirements
) -> dict[str, Any]:
    from .requirements_v2 import MachineRequirements

    if not scope_id or not expected_permit_hash:
        raise ValueError("候选流程需要本任务的有效试用许可")
    state = get_trial_scope(store, task.project, scope_id)
    permit = state["permit"]
    requirements = MachineRequirements.model_validate(machine_requirements).model_dump(mode="json")
    if (
        state["status"] != "active"
        or permit["permit_hash"] != expected_permit_hash
        or (permit["task_id"], permit["task_revision"], permit["base_hash"])
        != (task.id, task.revision, task.base_hash)
        or permit["requirements_hash"] != content_hash(requirements)
        or version_id not in permit["allowed_versions"]
    ):
        raise ValueError("试用许可已撤销、过期或不覆盖本任务／版本／要求")
    return permit


def revoke_trial_scope(store, project, scope_id, *, expected_permit_hash, reason):
    if not reason.strip():
        raise ValueError("撤销技术试用作用域需要理由")
    _initialize(store)
    with store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        state = _scope(db, project, scope_id)
        if state["permit"]["permit_hash"] != expected_permit_hash:
            raise ValueError("技术试用作用域CAS失败")
        if state["status"] == "revoked":
            return state
        event = _sealed(
            {
                "scope_id": scope_id,
                "permit_hash": expected_permit_hash,
                "status": "revoked",
                "reason": reason,
                "created_at": now(),
            },
            "event_hash",
        )
        db.execute(
            "INSERT INTO method_trial_scope_events(scope_id,payload) VALUES(?,?)",
            (scope_id, _json(event)),
        )
        return _scope(db, project, scope_id)


def assert_trial_scope_current(store, project, run):
    scope_id = run.get("method_trial_scope_id")
    if not scope_id:
        if (run.get("skill_authorization") or {}).get("selection") == "trial":
            raise ValueError("候选运行缺少绑定本任务的试用许可；旧记录仍可查看")
        return
    from .requirements_v2 import MachineRequirements

    with store.connection() as db:
        state = _scope(db, project, scope_id)
        version_row = db.execute(
            "SELECT payload FROM evolution_versions WHERE id=? AND project=?",
            (run.get("skill_version_id"), project),
        ).fetchone()
    version = _read(version_row[0], "version_hash") if version_row else None
    permit = state["permit"]
    task = store.get_task(run["task_id"])
    requirements = MachineRequirements.model_validate(run["requirements"]["machine"]).model_dump(
        mode="json"
    )
    if (
        state["status"] != "active"
        or permit["permit_hash"] != run.get("method_trial_scope_hash")
        or (permit["task_id"], permit["task_revision"], permit["base_hash"])
        != (task.id, task.revision, task.base_hash)
        or permit["requirements_hash"] != content_hash(requirements)
        or run.get("skill_version_id") not in permit["allowed_versions"]
        or version is None
        or version["version_hash"] != permit["allowed_versions"].get(run.get("skill_version_id"))
    ):
        raise ValueError("技术试用作用域已撤销、过期或与当前任务不一致")


def prepare_trial_matched_run(
    store,
    root,
    task,
    scope_id,
    *,
    expected_permit_hash,
    machine_requirements,
    collaboration_policy=None,
) -> dict[str, Any]:
    from .requirements_v2 import MachineRequirements
    from .v2_skills import CAPABILITIES, NarrativeEvolutionStore, _machine_values, prepare_skill_run

    with store.connection() as db:
        state = _scope(db, task.project, scope_id)
    permit = state["permit"]
    requirements = MachineRequirements.model_validate(machine_requirements).model_dump(mode="json")
    if (
        state["status"] != "active"
        or permit["permit_hash"] != expected_permit_hash
        or (permit["task_id"], permit["task_revision"], permit["base_hash"])
        != (task.id, task.revision, task.base_hash)
        or permit["requirements_hash"] != content_hash(requirements)
    ):
        raise ValueError("技术试用作用域不覆盖本次任务／需求")
    skills = NarrativeEvolutionStore(store)
    matches = skills.match(
        task.project,
        task.request.brief,
        resources=set(task.base_bundle.get("entities", {})),
        capabilities=CAPABILITIES,
        requirements=_machine_values(requirements),
        confirmed_requirement_keys=set(_machine_values(requirements)),
        include_trials=True,
        limit=20,
    )
    admitted = [
        row
        for row in matches
        if row["version_id"] in permit["allowed_versions"]
        and row["eligible_to_execute"]
        and row["score"] > 0
        and row["version"]["version_hash"] == permit["allowed_versions"][row["version_id"]]
    ]
    if not admitted:
        raise ValueError("许可作用域内没有匹配且适用条件成立的真实方法，不回退模板")
    selected = admitted[0]
    with store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        current = _scope(db, task.project, scope_id)
        if current["status"] != "active":
            raise ValueError("技术试用作用域已撤销")
        old = db.execute(
            "SELECT payload FROM method_trial_scope_claims WHERE scope_id=?", (scope_id,)
        ).fetchone()
        if old:
            settled = db.execute(
                "SELECT payload FROM method_trial_scope_results WHERE scope_id=?", (scope_id,)
            ).fetchone()
            return (
                _read(settled[0], "receipt_hash")
                if settled
                else {
                    "status": "claimed_unknown",
                    "scope_id": scope_id,
                    "new_dispatch": False,
                }
            )
        claim = _sealed(
            {
                "scope_id": scope_id,
                "permit_hash": expected_permit_hash,
                "selected_version_id": selected["version_id"],
                "created_at": now(),
            },
            "claim_hash",
        )
        db.execute(
            "INSERT INTO method_trial_scope_claims VALUES(?,?,?)",
            (scope_id, task.project, _json(claim)),
        )
    receipt = {
        "scope_id": scope_id,
        "permit_hash": expected_permit_hash,
        "selection": {key: value for key, value in selected.items() if key != "version"},
        "author_acceptance": "unknown",
    }
    try:
        prepared = prepare_skill_run(
            store,
            root,
            task,
            selected["version_id"],
            machine_requirements=requirements,
            collaboration_policy=collaboration_policy,
            trial_scope_id=scope_id,
            expected_trial_scope_hash=expected_permit_hash,
        )
        cancel_prepared = False
        with store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if _scope(db, task.project, scope_id)["status"] != "active":
                cancel_prepared = True
            else:
                raw = db.execute(
                    "SELECT payload FROM creative_v2_runs WHERE run_id=? AND project=?",
                    (prepared["run_id"], task.project),
                ).fetchone()
                run = json.loads(raw[0])
                run.update(
                    method_trial_scope_id=scope_id, method_trial_scope_hash=expected_permit_hash
                )
                db.execute(
                    "UPDATE creative_v2_runs SET payload=? WHERE run_id=? AND project=?",
                    (_json(run), prepared["run_id"], task.project),
                )
        if cancel_prepared:
            store.update_run(prepared["run_id"], status="cancelled", cancel_requested=True)
            raise ValueError("技术试用作用域在准备期间撤销，不派发")
        receipt.update(status="prepared", run_id=prepared["run_id"])
    except Exception as exc:
        receipt.update(status="failed", error=f"{type(exc).__name__}: {exc}")
    receipt["created_at"] = now()
    receipt = _sealed(receipt, "receipt_hash")
    with store.connection() as db:
        db.execute(
            "INSERT INTO method_trial_scope_results VALUES(?,?,?)",
            (scope_id, task.project, _json(receipt)),
        )
    return receipt
