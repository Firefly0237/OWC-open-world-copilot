from owcopilot.workflow.evolution_v2 import compare_production_runs


def request(role, model):
    return {
        "context_manifest": {"task": {"id": "fixed"}},
        "request": {"role_id": role, "config": {"model": model}},
    }


def test_all_role_configurations_are_compared_but_call_counts_are_not_identity():
    parent = {"run_id": "left", "references": [], "requirements": {}, "result": {}}
    child = {**parent, "run_id": "right"}
    left = [request("planner", "a"), request("writer", "b")]
    assert compare_production_runs(parent, child, left, left + [left[1]])["same_frozen_input"]
    changed = [request("planner", "a"), request("writer", "changed")]
    assert compare_production_runs(parent, child, left, changed)["status"] == "withheld"
    assert compare_production_runs(parent, child, [], [])["status"] == "withheld"
    assert compare_production_runs(parent, child, [{}], [{}])["status"] == "withheld"
