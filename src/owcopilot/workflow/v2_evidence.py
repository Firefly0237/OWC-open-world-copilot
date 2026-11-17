"""Read-only export of source-bound v2 execution and independent author decisions."""

from __future__ import annotations

import json

from ..content.hash import content_hash
from .harness import HarnessStore
from .intent_store import IntentStore
from .models import now


def _checked(raw: str, key: str) -> dict:
    record = json.loads(raw)
    if record.get(key) != content_hash(
        {name: value for name, value in record.items() if name != key}
    ):
        raise ValueError("导出关联记录校验失败")
    return record


def _task_links(db, project: str, task_id: str) -> dict:
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    relevant = (
        "creative_edit_preview_forks",
        "creative_original_preview_forks",
        "previews",
        "method_trial_scopes",
        "method_trial_scope_events",
        "method_trial_scope_claims",
        "method_trial_scope_results",
        "method_trial_scope_grants",
        "creative_v2_stage_changes",
        "creative_v2_collaboration",
    )
    result: dict = {
        "coverage": {name: "available" if name in tables else "not_recorded" for name in relevant},
        "edit_preview_forks": [],
        "original_preview_forks": [],
        "derived_task_previews": [],
        "method_trial_scopes": [],
        "method_trial_events": [],
        "method_trial_claims": [],
        "method_trial_results": [],
        "method_trial_grants": [],
        "stage_changes": [],
        "policy_versions": [],
    }
    if "creative_edit_preview_forks" in tables:
        result["edit_preview_forks"] = [
            _checked(row[0], "migration_hash")
            for row in db.execute(
                "SELECT payload FROM creative_edit_preview_forks WHERE project=? "
                "AND json_extract(payload,'$.result.project')=? "
                "AND json_extract(payload,'$.result.task_id')=? ORDER BY rowid",
                (project, project, task_id),
            )
        ]
    if "previews" in tables:
        result["derived_task_previews"] = [
            json.loads(row[0])
            for row in db.execute(
                "SELECT payload FROM previews WHERE json_extract(payload,'$.project')=? "
                "AND json_extract(payload,'$.task_id')=? "
                "AND (json_extract(payload,'$.application_id') IS NOT NULL "
                "OR json_extract(payload,'$.version_mode')='fixed_historical') ORDER BY rowid",
                (project, task_id),
            )
        ]
        if "creative_original_preview_forks" in tables:
            result["original_preview_forks"] = [
                _checked(row[0], "migration_hash")
                for row in db.execute(
                    "SELECT f.payload FROM creative_original_preview_forks f "
                    "JOIN previews p ON p.id=json_extract(f.payload,'$.preview_id') "
                    "WHERE f.project=? AND json_extract(p.payload,'$.project')=? "
                    "AND json_extract(p.payload,'$.task_id')=? ORDER BY f.rowid",
                    (project, project, task_id),
                )
            ]
    if "method_trial_scopes" in tables:
        result["method_trial_scopes"] = [
            _checked(row[0], "permit_hash")
            for row in db.execute(
                "SELECT payload FROM method_trial_scopes WHERE project=? "
                "AND json_extract(payload,'$.project')=? AND json_extract(payload,'$.task_id')=? "
                "ORDER BY rowid",
                (project, project, task_id),
            )
        ]
        for table, output, hash_key in (
            ("method_trial_scope_events", "method_trial_events", "event_hash"),
            ("method_trial_scope_claims", "method_trial_claims", "claim_hash"),
            ("method_trial_scope_results", "method_trial_results", "receipt_hash"),
        ):
            if table not in tables:
                continue
            project_clause = "" if table == "method_trial_scope_events" else " AND child.project=?"
            parameters = (project, project, task_id) + (() if not project_clause else (project,))
            result[output] = [
                _checked(row[0], hash_key)
                for row in db.execute(
                    f"SELECT child.payload FROM {table} child "
                    "JOIN method_trial_scopes scope ON scope.id=child.scope_id "
                    "WHERE scope.project=? AND json_extract(scope.payload,'$.project')=? "
                    "AND json_extract(scope.payload,'$.task_id')=?"
                    + project_clause
                    + " ORDER BY child.rowid",
                    parameters,
                )
            ]
        if "method_trial_scope_grants" in tables:
            result["method_trial_grants"] = [
                dict(row)
                for row in db.execute(
                    "SELECT grant_row.operation_id,grant_row.request_hash,grant_row.scope_id "
                    "FROM method_trial_scope_grants grant_row JOIN method_trial_scopes scope "
                    "ON scope.id=grant_row.scope_id WHERE grant_row.project=? AND scope.project=? "
                "AND json_extract(scope.payload,'$.project')=? "
                    "AND json_extract(scope.payload,'$.task_id')=? ORDER BY grant_row.rowid",
                (project, project, project, task_id),
                )
            ]
    if "creative_v2_stage_changes" in tables:
        result["stage_changes"] = [
            _checked(row[0], "receipt_hash")
            for row in db.execute(
                "SELECT payload FROM creative_v2_stage_changes WHERE project=? "
                "AND json_extract(payload,'$.project')=? AND json_extract(payload,'$.task_id')=? "
                "ORDER BY rowid",
                (project, project, task_id),
            )
        ]
    if "creative_v2_collaboration" in tables:
        result["policy_versions"] = [
            json.loads(row[0])
            for row in db.execute(
                "SELECT c.payload FROM creative_v2_collaboration c JOIN runs r ON r.id=c.run_id "
                "WHERE c.project=? AND r.task_id=? ORDER BY c.rowid",
                (project, task_id),
            )
        ]
    return result


def export_v2_evidence(store, root, project, run_id):
    from .v2_service import evolution_store, v2_detail

    state = v2_detail(store, project, run_id)
    task = store.get_task(state["task_id"])
    harness = HarnessStore(store)
    events = []
    cursor = 0
    while page := harness.events(run_id, after_seq=cursor, limit=1000):
        events.extend(page)
        cursor = page[-1]["seq"]
    skills = evolution_store(store)
    related = state.get("skill_version_id") or (state.get("saved_skill") or {}).get("version_id")
    family = None
    versions = []
    experiences = []
    episodes = []
    traces = []
    continuations = []
    planning_traces = []
    planning_ids = {
        node.get("parameters", {}).get("initial_planning", {}).get("run_id")
        for node in state["graph"]["nodes"]
    }
    for planning_id in planning_ids - {None}:
        planning_traces.append(
            {
                "run": store.get_run(planning_id).model_dump(mode="json"),
                "requests": harness.requests(planning_id),
                "events": harness.events(planning_id, limit=1000),
            }
        )
    if related:
        version = skills.get_version(project, related)
        family = skills.get_family(project, version["family_id"])
        versions = skills.versions(project, family["id"])
        experiences = skills.experiences(project, family["id"])
        with store.connection() as db:
            rows = db.execute(
                "SELECT id FROM evolution_episodes WHERE project=? AND family_id=? ORDER BY rowid",
                (project, family["id"]),
            ).fetchall()
        episodes = [skills.get_episode(project, row[0]) for row in rows]
        episode_ids = {row["id"] for row in episodes}
        with store.connection() as db:
            continuation_table = db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='evolution_continuation_results'"
            ).fetchone()
            if continuation_table:
                continuations = [
                    json.loads(row[0])
                    for row in db.execute(
                        "SELECT payload FROM evolution_continuation_results WHERE project=?",
                        (project,),
                    )
                ]
                continuations = [
                    row for row in continuations if row.get("episode_id") in episode_ids
                ]
        for continuation in continuations:
            continuation_id = continuation["trace_run_id"]
            traces.append(
                {
                    "run": store.get_run(continuation_id).model_dump(mode="json"),
                    "requests": harness.requests(continuation_id),
                    "events": harness.events(continuation_id, limit=1000),
                }
            )
        for candidate in store.list_runs(task.id):
            page = harness.events(candidate.id, limit=1000)
            if any(row["payload"].get("episode_id") in episode_ids for row in page):
                traces.append(
                    {
                        "run": candidate.model_dump(mode="json"),
                        "events": page,
                        "requests": harness.requests(candidate.id),
                    }
                )
    IntentStore(store)
    with store.connection() as db:
        proposal_table = db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='creative_intent_ai_proposals'"
        ).fetchone()
        proposal_records = (
            [
                json.loads(row[0])
                for row in db.execute(
                    "SELECT payload FROM creative_intent_ai_proposals WHERE project=?", (project,)
                )
            ]
            if proposal_table
            else []
        )
        proposal_records = [row for row in proposal_records if row.get("task_id") == task.id]
        contracts = [
            json.loads(row[0])
            for row in db.execute(
                "SELECT payload FROM creative_intent_contracts "
                "WHERE project=? AND task_id=? ORDER BY version",
                (project, task.id),
            ).fetchall()
        ]
        reviews = [
            json.loads(row[0])
            for row in db.execute(
                "SELECT payload FROM creative_intent_reviews "
                "WHERE project=? AND task_id=? ORDER BY rowid",
                (project, task.id),
            ).fetchall()
        ]
        decisions = [
            json.loads(row[0])
            for row in db.execute(
                "SELECT payload FROM creative_intent_decisions WHERE project=? AND task_id=?",
                (project, task.id),
            ).fetchall()
        ]
        available = db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='creative_intent_applications'"
        ).fetchone()
        receipts = (
            [
                json.loads(row[0])
                for row in db.execute(
                    "SELECT payload FROM creative_intent_applications "
                    "WHERE project=? AND task_id=?",
                    (project, task.id),
                ).fetchall()
            ]
            if available
            else []
        )
        task_links = _task_links(db, project, task.id)
    result = {
        "schema_version": 2,
        "exported_at": now(),
        "project": project,
        "task": task.model_dump(mode="json"),
        "execution": state,
        "harness": {
            "events": events,
            "requests": harness.requests(run_id),
            "replay_model_dispatches": 0,
            "initial_planning": planning_traces,
        },
        "artifacts": [row.model_dump(mode="json") for row in store.artifacts(task.id)],
        "skill": {
            "family": family,
            "versions": versions,
            "experiences": experiences,
            "episodes": episodes,
            "evolution_traces": list({row["run"]["id"]: row for row in traces}.values()),
            "continuation_receipts": continuations,
        },
        "intent": {
            "contracts": contracts,
            "reviews": reviews,
            "decisions": decisions,
            "applications": receipts,
            "ai_proposals": proposal_records,
            "task_links": task_links,
            "proposal_traces": [
                {
                    "run": store.get_run(row["trace_run_id"]).model_dump(mode="json"),
                    "requests": harness.requests(row["trace_run_id"]),
                    "events": harness.events(row["trace_run_id"], limit=1000),
                }
                for row in proposal_records
                if row.get("trace_run_id")
            ],
        },
        "limits": [
            "author acceptance only from explicit recorded action",
            "mechanical coverage is not literary intention proof",
            "single case is not generalized improvement",
            "local provider requests do not reconstruct undisclosed SDK defaults "
            "or private model reasoning",
        ],
    }
    return {**result, "export_hash": content_hash(result)}
