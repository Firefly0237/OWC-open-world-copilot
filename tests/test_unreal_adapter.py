"""P3 Unreal translation layer + bridges (offline, $0).

The translation (`Quest` -> DataTable command) and the `RemoteControlBridge` *request
construction* are fully unit-tested without a running editor; the live round-trip is a manual
machine test (scripts/run_ue_demo.py --ue).
"""

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from owcopilot.adapters.unreal import (
    UnrealAdapter,
    default_row_name,
    fields_to_quest,
    quest_to_fields,
)
from owcopilot.adapters.unreal.bridge import (
    FakeUnrealBridge,
    RemoteControlBridge,
    UnrealBridgeError,
)

QUEST = {
    "title": "Smoke Over the Marsh",
    "giver_npc": "Aldric",
    "location": "Northwatch",
    "objective": "Hold the depot",
    "reward": "150 gold",
    "prerequisites": ["The Caravan Ambush"],
}


# --------------------------------------------------------------------- translation layer
def test_quest_to_fields_maps_struct_names_and_keeps_list():
    f = quest_to_fields(QUEST)
    assert f == {
        "Title": "Smoke Over the Marsh",
        "GiverNPC": "Aldric",
        "Location": "Northwatch",
        "Objective": "Hold the depot",
        "Reward": "150 gold",
        "Prerequisites": ["The Caravan Ambush"],
    }
    assert isinstance(f["Prerequisites"], list)  # TArray<FString>, not a joined string


def test_fields_to_quest_is_inverse():
    assert fields_to_quest(quest_to_fields(QUEST)) == QUEST


def test_default_row_name_slugs_the_title():
    assert default_row_name(QUEST) == "Quest_smoke_over_the_marsh"
    assert default_row_name({"title": ""}) == "Quest_untitled"


def test_apply_lands_one_row_and_snapshot_reads_it_back():
    bridge = FakeUnrealBridge()
    adapter = UnrealAdapter(bridge, table="QuestTable", commit=True)
    adapter.apply(QUEST)

    assert len(bridge.upserts) == 1  # exactly one landing
    table, row_name, fields = bridge.upserts[0]
    assert table == "QuestTable"
    assert row_name == "Quest_smoke_over_the_marsh"
    assert fields == quest_to_fields(QUEST)

    snap = adapter.snapshot()
    assert snap["engine"] == "unreal"
    assert snap["table"] == "QuestTable"
    assert snap["row_name"] == "Quest_smoke_over_the_marsh"
    assert snap["row"] == quest_to_fields(QUEST)
    assert snap["committed"] is True
    assert snap["command"]["fields"] == quest_to_fields(QUEST)
    assert fields_to_quest(snap["row"]) == QUEST  # round-trips back to the quest


def test_adapter_defaults_to_dry_run_command_only():
    a = UnrealAdapter()  # defaults to dry-run sandbox
    a.apply(QUEST)
    snap = a.snapshot()
    assert snap["row"] is None
    assert snap["committed"] is False
    assert snap["command"]["row_name"] == "Quest_smoke_over_the_marsh"


def test_unreal_table_allowlist_blocks_unapproved_targets():
    with pytest.raises(ValueError, match="allowlist"):
        UnrealAdapter(table="ArbitraryTable")


# --------------------------------------------------------- RemoteControlBridge request shapes
def test_remote_control_upsert_builds_correct_request():
    seen = []

    def transport(method, url, body):
        seen.append((method, url, body))
        return {}

    bridge = RemoteControlBridge(
        base_url="http://host:30010/", helper_object="/Game/X.Helper", transport=transport
    )
    bridge.upsert_datatable_row("QuestTable", "Quest_x", {"Title": "T", "Prerequisites": []})

    method, url, body = seen[0]
    assert method == "PUT"
    assert url == "http://host:30010/remote/object/call"
    assert body["objectPath"] == "/Game/X.Helper"
    assert body["functionName"] == "UpsertQuestRow"
    assert body["generateTransaction"] is True
    assert body["parameters"] == {
        "TableName": "QuestTable",
        "RowName": "Quest_x",
        "Title": "T",
        "Prerequisites": [],
    }


def test_remote_control_read_unwraps_return_value():
    row = {"Title": "T", "Location": "Northwatch", "Prerequisites": []}
    bridge = RemoteControlBridge(transport=lambda m, u, b: {"ReturnValue": row})
    assert bridge.read_datatable_row("QuestTable", "Quest_x") == row


def test_remote_control_read_missing_row_is_none():
    bridge = RemoteControlBridge(transport=lambda m, u, b: {"ReturnValue": "None"})
    assert bridge.read_datatable_row("QuestTable", "Quest_x") is None


def test_remote_control_wraps_transport_failure_with_actionable_error():
    def boom(method, url, body):
        raise OSError("connection refused")

    bridge = RemoteControlBridge(transport=boom)
    with pytest.raises(UnrealBridgeError, match="Remote Control"):
        bridge.upsert_datatable_row("QuestTable", "Quest_x", {"Title": "T"})


def test_remote_control_bridge_round_trips_through_local_http_server():
    rows = {}

    class Handler(BaseHTTPRequestHandler):
        def do_PUT(self):
            import json

            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            fn = body["functionName"]
            params = body["parameters"]
            key = (params["TableName"], params["RowName"])
            if fn == "UpsertQuestRow":
                rows[key] = {k: v for k, v in params.items() if k not in {"TableName", "RowName"}}
                payload = {}
            elif fn == "ReadQuestRow":
                payload = {"ReturnValue": rows.get(key, "None")}
            else:
                self.send_response(400)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode("utf-8"))

        def log_message(self, fmt, *args):
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base_url = f"http://127.0.0.1:{server.server_port}"
        bridge = RemoteControlBridge(base_url=base_url)
        adapter = UnrealAdapter(bridge, commit=True)
        adapter.apply(QUEST)
        snap = adapter.snapshot()

        assert snap["row"]["Title"] == "Smoke Over the Marsh"
        assert fields_to_quest(snap["row"]) == QUEST
    finally:
        server.shutdown()
        server.server_close()
