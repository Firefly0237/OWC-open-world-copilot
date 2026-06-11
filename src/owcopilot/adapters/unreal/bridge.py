"""The Unreal *bridge* — the thin I/O layer the adapter talks to.

P3's whole architecture is: a pure, offline-testable **translation layer** (`UnrealAdapter`,
in `__init__.py`) that turns a Quest into an engine-neutral command, plus a small, injectable
**bridge** that actually performs that command against a running editor. Swapping the bridge
swaps "where it lands" without touching the adapter or the engine-agnostic core.

  * `FakeUnrealBridge`   — in-memory; records rows so the translation layer + the milestone loop
                           run fully offline ($0, deterministic) and `snapshot()` round-trips.
  * `RemoteControlBridge`— real machine; talks to UE5's built-in **Remote Control API** (HTTP,
                           default :30010), calling a small UE-side helper (`UpsertQuestRow` /
                           `ReadQuestRow`). Its *request construction* is unit-tested offline via
                           an injectable transport; the live round-trip is a manual machine test.

The bridge interface is deliberately tiny (upsert / read / health) so it is cheap to back with
Remote Control today or a third-party UE-MCP server later.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from typing import Any, Protocol

from ...fakes import FakeUnrealBridge as FakeUnrealBridge  # re-export (offline test double)


class UnrealBridgeError(RuntimeError):
    """Raised when a real bridge cannot reach / drive the editor, with an actionable message."""


class UnrealBridge(Protocol):
    def upsert_datatable_row(self, table: str, row_name: str, fields: dict[str, Any]) -> None: ...
    def read_datatable_row(self, table: str, row_name: str) -> dict[str, Any] | None: ...
    def health(self) -> bool: ...


# A transport is (method, url, json_body_or_None) -> parsed_json_dict. Injectable so the request
# construction can be unit-tested without a network or a real editor.
Transport = Callable[[str, str, dict[str, Any] | None], dict[str, Any]]


class RemoteControlBridge:
    """Drive a running UE5 editor via its built-in Remote Control API (HTTP, no 3rd-party plugin).

    Rows are written/read by calling a small UE-side helper object's functions
    (`UpsertQuestRow` / `ReadQuestRow`) via `PUT /remote/object/call`. The helper (an Editor
    Utility Blueprint or an editor-Python-registered function) is documented in
    `docs/P3_results.md`; its object path and the port/table are configuration, never hard-coded.

    Only the *request construction* is exercised offline (inject a recording `transport`); the
    real round-trip is a manual machine test (see `scripts/run_ue_demo.py --ue`).
    """

    def __init__(
        self,
        *,
        base_url: str = "http://127.0.0.1:30010",
        helper_object: str = "/Engine/Transient.QuestCopilotHelper",
        upsert_fn: str = "UpsertQuestRow",
        read_fn: str = "ReadQuestRow",
        transport: Transport | None = None,
        timeout: float = 10.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.helper_object = helper_object
        self.upsert_fn = upsert_fn
        self.read_fn = read_fn
        self.timeout = timeout
        self._transport = transport or self._http_transport

    # --- public bridge API ---------------------------------------------------
    def upsert_datatable_row(self, table: str, row_name: str, fields: dict[str, Any]) -> None:
        self._call(
            self.upsert_fn, {"TableName": table, "RowName": row_name, **fields}, transaction=True
        )

    def read_datatable_row(self, table: str, row_name: str) -> dict[str, Any] | None:
        resp = self._call(self.read_fn, {"TableName": table, "RowName": row_name})
        return self._parse_row(resp)

    def health(self) -> bool:
        try:
            self._transport("GET", f"{self.base_url}/remote/info", None)
            return True
        except Exception:
            return False

    # --- internals -----------------------------------------------------------
    def _call(
        self, function_name: str, parameters: dict[str, Any], *, transaction: bool = False
    ) -> dict[str, Any]:
        url = f"{self.base_url}/remote/object/call"
        body = {
            "objectPath": self.helper_object,
            "functionName": function_name,
            "parameters": parameters,
            "generateTransaction": transaction,
        }
        try:
            return self._transport("PUT", url, body)
        except UnrealBridgeError:
            raise
        except Exception as e:
            raise UnrealBridgeError(
                f"Remote Control call '{function_name}' failed at {url}: {e}. Check that the UE5 "
                f"editor is open with the Remote Control plugin enabled on {self.base_url}, and "
                f"that the helper '{self.helper_object}' exposing {self.upsert_fn}/{self.read_fn} "
                f"exists (see docs/P3_results.md)."
            ) from e

    @staticmethod
    def _parse_row(resp: dict[str, Any]) -> dict[str, Any] | None:
        """Remote Control wraps a function's output; unwrap a `ReturnValue` if present and treat
        an empty / explicit-None result as 'row not found'."""
        if not resp:
            return None
        row = resp.get("ReturnValue", resp)
        if row in (None, {}, "None", "none"):
            return None
        return row if isinstance(row, dict) else {"ReturnValue": row}

    def _http_transport(self, method: str, url: str, body: dict[str, Any] | None) -> dict[str, Any]:
        import urllib.request  # stdlib only — no new dependency for the real path

        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            url, data=data, method=method, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as r:  # noqa: S310 (configured URL)
            raw = r.read().decode("utf-8")
        return json.loads(raw) if raw.strip() else {}


def make_unreal_bridge_from_env() -> RemoteControlBridge:
    """Build a `RemoteControlBridge` from environment config (so nothing is hard-coded).

    Reads OWCOPILOT_UE_RC_URL / OWCOPILOT_UE_HELPER / OWCOPILOT_UE_TABLE-adjacent settings; all
    have sensible defaults. Used by the `--ue` real-machine demo path.
    """
    return RemoteControlBridge(
        base_url=os.getenv("OWCOPILOT_UE_RC_URL", "http://127.0.0.1:30010"),
        helper_object=os.getenv("OWCOPILOT_UE_HELPER", "/Engine/Transient.QuestCopilotHelper"),
        upsert_fn=os.getenv("OWCOPILOT_UE_UPSERT_FN", "UpsertQuestRow"),
        read_fn=os.getenv("OWCOPILOT_UE_READ_FN", "ReadQuestRow"),
    )
