"""UE remote-push client: exact Remote Control HTTP request bodies, the fill-then-save sequence, the
Remote Execution command string, and guided failures when a transport is unavailable.

Wire correctness is asserted offline: a stdlib ``HTTPServer`` captures the
``PUT /remote/object/call`` bodies, and a fake ``remote_execution`` module captures the command.
Connecting to a real editor is real-machine-only (see scripts/run_ue_verify.py).
"""

from __future__ import annotations

import json
import socket
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from owcopilot.integrations import unreal_remote as rc
from owcopilot.integrations.unreal_remote import (
    DATATABLE_LIBRARY_CDO,
    EDITOR_ASSET_LIBRARY_CDO,
    build_remote_exec_command,
    call_remote_object,
    push_datatable_via_remote_control,
    run_bundle_via_remote_execution,
)


class _Recorder:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict]] = []  # (method, path, body)
        # The HTTP 200 body the fake editor returns. Default mirrors a successful function call
        # (``ReturnValue: true``); tests override it to exercise the ReturnValue gate.
        self.response_body: bytes = b'{"ReturnValue": true}'


def _make_handler(recorder: _Recorder) -> type[BaseHTTPRequestHandler]:
    class _Handler(BaseHTTPRequestHandler):
        def do_PUT(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler dispatch name
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length))
            recorder.requests.append((self.command, self.path, body))
            payload = recorder.response_body
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args: object) -> None:  # silence test-server logging
            return

    return _Handler


@pytest.fixture
def rc_server() -> Iterator[tuple[str, _Recorder]]:
    recorder = _Recorder()
    server = HTTPServer(("127.0.0.1", 0), _make_handler(recorder))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", recorder
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


# --- Remote Control HTTP -----------------------------------------------------------------------


def test_call_remote_object_builds_put_body(rc_server: tuple[str, _Recorder]) -> None:
    url, recorder = rc_server
    result = call_remote_object(url, "/Script/Engine.Default__X", "DoThing", {"A": 1})

    assert result.ok and result.transport == "remote_control"
    assert result.response == {"ReturnValue": True}
    # The callee's boolean ReturnValue is now parsed out of the body into function_ok.
    assert result.function_ok is True
    method, path, body = recorder.requests[0]
    assert method == "PUT" and path == "/remote/object/call"
    assert body == {
        "objectPath": "/Script/Engine.Default__X",
        "functionName": "DoThing",
        "parameters": {"A": 1},
        "generateTransaction": True,
    }


def test_push_datatable_fill_then_save_on_cdos(rc_server: tuple[str, _Recorder]) -> None:
    url, recorder = rc_server
    # Default body is {"ReturnValue": true} → the fill succeeded engine-side → save proceeds.
    results = push_datatable_via_remote_control(url, "/Game/QuestTable", "[]")

    assert len(results) == 2 and all(r.ok for r in results)
    assert results[0].function_ok is True  # fill's ReturnValue gated the save open
    fill_body = recorder.requests[0][2]
    assert fill_body["objectPath"] == DATATABLE_LIBRARY_CDO
    assert fill_body["functionName"] == "FillDataTableFromJSONString"
    assert fill_body["parameters"] == {"DataTable": "/Game/QuestTable", "JSONString": "[]"}
    save_body = recorder.requests[1][2]
    assert save_body["objectPath"] == EDITOR_ASSET_LIBRARY_CDO
    assert save_body["functionName"] == "SaveAsset"
    assert save_body["parameters"]["AssetToSave"] == "/Game/QuestTable"


def test_push_datatable_passes_import_row_struct(rc_server: tuple[str, _Recorder]) -> None:
    url, recorder = rc_server
    push_datatable_via_remote_control(
        url, "/Game/QuestTable", "[]", import_row_struct_path="/Game/FQuestRow", save=False
    )
    assert len(recorder.requests) == 1  # save suppressed
    assert recorder.requests[0][2]["parameters"]["ImportRowStruct"] == "/Game/FQuestRow"


def test_call_remote_object_connection_error_is_guided() -> None:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    closed_port = probe.getsockname()[1]
    probe.close()  # nothing is listening on this port now -> connection refused

    result = call_remote_object(f"http://127.0.0.1:{closed_port}", "/x", "f", {}, timeout=2.0)
    assert not result.ok and result.transport == "remote_control"
    assert "Remote Control" in result.detail and str(rc.DEFAULT_RC_HTTP_PORT) in result.detail
    # Transport failed before any body was read, so the function outcome is simply unknown.
    assert result.function_ok is None


def test_call_remote_object_parses_return_value_false(rc_server: tuple[str, _Recorder]) -> None:
    """A ``{"ReturnValue": false}`` body (HTTP 200) is parsed into ``function_ok is False``.

    Transport still succeeded, so ``ok`` stays True — the function-level failure lives in the new
    ``function_ok`` field, exactly the distinction the fix introduces.
    """
    url, recorder = rc_server
    recorder.response_body = b'{"ReturnValue": false}'
    result = call_remote_object(url, "/Script/Engine.Default__X", "DoThing", {})
    assert result.ok is True
    assert result.function_ok is False
    assert result.response == {"ReturnValue": False}


def test_push_datatable_skips_save_when_fill_returns_false(
    rc_server: tuple[str, _Recorder],
) -> None:
    """A fill that returns ``false`` (engine-side failure behind HTTP 200) must SKIP SaveAsset.

    This is the core bug fix: previously the save fired whenever the fill *transported* ok, so a
    failed fill would overwrite the asset with an empty/partial table. Now the fill's
    ``ReturnValue`` gates the save closed, and the fill result carries a guided Chinese error.
    """
    url, recorder = rc_server
    recorder.response_body = b'{"ReturnValue": false}'
    results = push_datatable_via_remote_control(url, "/Game/QuestTable", "[]")

    # Only the fill call was made — SaveAsset was NOT attempted.
    assert len(results) == 1
    assert len(recorder.requests) == 1
    assert recorder.requests[0][2]["functionName"] == "FillDataTableFromJSONString"
    fill = results[0]
    assert fill.ok is True  # transport still succeeded (HTTP 200)
    assert fill.function_ok is False
    assert "返回 false" in fill.error and "SaveAsset" in fill.error


def test_push_datatable_saves_when_fill_returns_true(rc_server: tuple[str, _Recorder]) -> None:
    """An explicit ``{"ReturnValue": true}`` fill proceeds to SaveAsset (both calls made)."""
    url, recorder = rc_server
    recorder.response_body = b'{"ReturnValue": true}'
    results = push_datatable_via_remote_control(url, "/Game/QuestTable", "[]")

    assert len(results) == 2
    assert recorder.requests[1][2]["functionName"] == "SaveAsset"
    assert results[0].function_ok is True
    assert all(r.ok for r in results)
    assert results[0].error == "" and results[0].note == ""


def test_push_datatable_saves_but_notes_when_returnvalue_absent(
    rc_server: tuple[str, _Recorder],
) -> None:
    """A body with no boolean ``ReturnValue`` → outcome unknown → still saves, but with a note.

    ``function_ok is None`` (unconfirmed) must NOT block the save — it preserves the prior behaviour
    for engines/functions that don't echo a bool — but the fill result is annotated so the caller
    knows the function outcome could not be verified. It is a note, not an error.
    """
    url, recorder = rc_server
    recorder.response_body = b'{"SomethingElse": 1}'  # no ReturnValue key at all
    results = push_datatable_via_remote_control(url, "/Game/QuestTable", "[]")

    assert len(results) == 2  # unknown outcome → save still proceeds
    assert recorder.requests[1][2]["functionName"] == "SaveAsset"
    fill = results[0]
    assert fill.function_ok is None
    assert fill.error == ""  # unconfirmed is not a failure
    assert fill.note and "ReturnValue" in fill.note


# --- Python Remote Execution -------------------------------------------------------------------


def test_build_remote_exec_command_is_valid_and_embeds_args() -> None:
    cmd = build_remote_exec_command("/tmp/export/unreal", {"Quests": "/Game/Q"})
    compile(cmd, "<remote>", "exec")  # a valid Python statement
    assert "import owcopilot_bridge" in cmd
    assert "import_bundle(**json.loads(" in cmd  # args embedded as a safe JSON string literal
    assert "/tmp/export/unreal" in cmd and "/Game/Q" in cmd


def test_run_bundle_remote_execution_missing_module_is_guided(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(rc, "_load_remote_execution_module", lambda: None)
    result = run_bundle_via_remote_execution("/tmp/x")
    assert not result.ok and result.error == "module_unavailable"
    assert "remote_execution" in result.detail


class _FakeNode:
    node_id = "node-1"


class _FakeRemoteExecution:
    instances: list[_FakeRemoteExecution] = []

    def __init__(self, config: object) -> None:
        self.config = config
        self.commands: list[tuple[str, bool, str]] = []
        self.opened: str | None = None
        self.started = False
        self.stopped = False
        _FakeRemoteExecution.instances.append(self)

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    @property
    def remote_nodes(self) -> list[_FakeNode]:
        return [_FakeNode()]

    def open_command_connection(self, node_id: str) -> None:
        self.opened = node_id

    def run_command(self, command: str, unattended: bool, exec_mode: str) -> dict:
        self.commands.append((command, unattended, exec_mode))
        return {"success": True, "result": "None", "output": []}


class _FakeRemoteExecutionModule:
    MODE_EXEC_STATEMENT = "ExecuteStatement"
    RemoteExecution = _FakeRemoteExecution

    class RemoteExecutionConfig:
        pass


def test_run_bundle_remote_execution_drives_module() -> None:
    _FakeRemoteExecution.instances.clear()
    result = run_bundle_via_remote_execution(
        "/tmp/export/unreal",
        {"Quests": "/Game/Q"},
        remote_execution_module=_FakeRemoteExecutionModule,
    )

    assert result.ok and result.transport == "remote_execution"
    inst = _FakeRemoteExecution.instances[0]
    assert inst.started and inst.stopped and inst.opened == "node-1"
    (command, unattended, exec_mode) = inst.commands[0]
    assert unattended is True and exec_mode == "ExecuteStatement"
    assert "owcopilot_bridge.import_bundle" in command and "/Game/Q" in command


def test_run_bundle_remote_execution_no_nodes_is_guided() -> None:
    class _NoNodes(_FakeRemoteExecution):
        @property
        def remote_nodes(self) -> list[_FakeNode]:
            return []

    module = type(
        "M",
        (),
        {
            "MODE_EXEC_STATEMENT": "ExecuteStatement",
            "RemoteExecution": _NoNodes,
            "RemoteExecutionConfig": object,
        },
    )
    result = run_bundle_via_remote_execution("/tmp/x", remote_execution_module=module)
    assert not result.ok and result.error == "no_nodes"
