# UE 远程客户端：两条官方无 C++ 传输（Remote Control HTTP / Python Remote Execution），结构化引导。
"""Thin, opt-in client for pushing DataTable updates to a live Unreal editor from this venv.

Two officially-supported, editor-only transports of the built-in Python plugin — neither needs any
C++:

* **Remote Control HTTP** — ``PUT /remote/object/call`` invokes any BlueprintCallable function on a
  UObject by path with no preset. We call ``FillDataTableFromJSONString`` on the
  ``DataTableFunctionLibrary`` CDO, then ``SaveAsset`` on the ``EditorAssetLibrary`` CDO — but the
  save is gated on the fill's own boolean ``ReturnValue`` (parsed from the response body), so a
  fill that fails engine-side behind an HTTP 200 never overwrites the asset on disk. Stdlib
  ``urllib`` only — no new dependency.
* **Python Remote Execution** — multicast discovery + a TCP command channel that runs arbitrary
  Python in-editor. Epic SHIPS ``remote_execution.py`` as the reference client inside the Python
  plugin; we reuse it as an *optional runtime import* (never vendored) to send
  ``owcopilot_bridge.import_bundle(...)``.

Every entry point returns a :class:`RemoteResult` with a guided message on failure instead of a raw
traceback. Connecting to a real editor over the wire is verifiable only on a real machine (see
scripts/run_ue_verify.py); these functions are structured so the request/command
shape is unit-tested offline.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

# CDO object paths (real-machine-only exact literals; the module names are UE5's).
DATATABLE_LIBRARY_CDO = "/Script/Engine.Default__DataTableFunctionLibrary"
EDITOR_ASSET_LIBRARY_CDO = "/Script/EditorScriptingUtilities.Default__EditorAssetLibrary"

DEFAULT_RC_HTTP_PORT = 30010
_REMOTE_CONTROL = "remote_control"
_REMOTE_EXECUTION = "remote_execution"


@dataclass(frozen=True)
class RemoteResult:
    ok: bool
    transport: str
    detail: str = ""
    error: str = ""
    response: dict[str, Any] | None = None
    # The invoked function's OWN return value, parsed from the response body's ``ReturnValue``:
    # ``True``/``False`` when the body carries a boolean, ``None`` when it has no boolean
    # ``ReturnValue`` (so the function outcome is unknown even though transport succeeded).
    # Additive field with a safe default — existing consumers that only read ``ok`` are unaffected.
    function_ok: bool | None = None
    # Free-form annotation for a non-error caveat (e.g. the function result could not be confirmed).
    note: str = ""


def _safe_json(raw: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


# --- Remote Control HTTP -----------------------------------------------------------------------


def call_remote_object(
    rc_url: str,
    object_path: str,
    function_name: str,
    parameters: dict[str, Any],
    *,
    generate_transaction: bool = True,
    timeout: float = 30.0,
) -> RemoteResult:
    """Invoke one BlueprintCallable function via ``PUT /remote/object/call``.

    ``ok`` reflects TRANSPORT success only (HTTP 2xx with a JSON body). The invoked function's OWN
    return value is parsed from the response body's ``ReturnValue`` field into ``function_ok``:
    ``True``/``False`` when the body carries a boolean, ``None`` when it has no boolean
    ``ReturnValue``. Epic's Remote Control API returns the callee's value there (e.g.
    ``{"ReturnValue": true}``), so a ``FillDataTableFromJSONString`` that fails engine-side is an
    HTTP 200 with ``function_ok is False`` — callers must check ``function_ok``, not just ``ok``.
    The full parsed body is still available in ``response``.
    """
    body = json.dumps(
        {
            "objectPath": object_path,
            "functionName": function_name,
            "parameters": parameters,
            "generateTransaction": generate_transaction,
        }
    ).encode("utf-8")
    url = rc_url.rstrip("/") + "/remote/object/call"
    request = urllib.request.Request(
        url, data=body, method="PUT", headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return RemoteResult(
            ok=False,
            transport=_REMOTE_CONTROL,
            error=f"HTTP {exc.code}",
            detail=(
                f"Remote Control 拒绝调用（HTTP {exc.code}）。请确认 objectPath/functionName 合法、"
                "目标函数为 BlueprintCallable，且该资产路径存在。"
            ),
        )
    except urllib.error.URLError as exc:
        return RemoteResult(
            ok=False,
            transport=_REMOTE_CONTROL,
            error=str(exc.reason),
            detail=(
                f"无法连接 Remote Control（{url}）。请在 UE 启用 Remote Control API 插件"
                f"（默认端口 {DEFAULT_RC_HTTP_PORT}），并确认主机/端口与防火墙允许访问。"
            ),
        )
    parsed = _safe_json(raw)
    return RemoteResult(
        ok=True,
        transport=_REMOTE_CONTROL,
        detail=f"已调用 {function_name}",
        response=parsed,
        function_ok=_parse_return_value(parsed),
    )


def _parse_return_value(body: dict[str, Any] | None) -> bool | None:
    """Extract the callee's boolean ``ReturnValue`` from a Remote Control response body.

    Returns the bool when present and boolean-typed; ``None`` otherwise (no ``ReturnValue`` key, or
    a non-bool value such as a number/string/null) — an unknown function outcome, distinct from a
    definite ``False``.
    """
    if body is None:
        return None
    value = body.get("ReturnValue")
    return value if isinstance(value, bool) else None


def push_datatable_via_remote_control(
    rc_url: str,
    table_path: str,
    rows_json: str,
    *,
    import_row_struct_path: str | None = None,
    save: bool = True,
    timeout: float = 30.0,
) -> list[RemoteResult]:
    """Headless push: fill a DataTable from JSON on the CDO, then (optionally) save the asset.

    ``rows_json`` is the full row-array to fill with (the caller merges via
    ``owcopilot_bridge.merge_rows_by_name`` or the exporter output). Returns one
    :class:`RemoteResult` per wire call. ``SaveAsset`` runs only when the fill call BOTH transported
    ok AND did not return ``false``: ``FillDataTableFromJSONString`` returns a bool (failure is
    ``false`` behind an HTTP 200), so saving after a ``false`` fill would persist a table the fill
    left empty/partial — exactly the overwrite this gate prevents. A ``false`` fill yields a single
    result carrying a guided error; an unconfirmed fill (no boolean ``ReturnValue``) still saves but
    the fill result is annotated with a ``note``.
    """
    fill_params: dict[str, Any] = {"DataTable": table_path, "JSONString": rows_json}
    if import_row_struct_path:
        fill_params["ImportRowStruct"] = import_row_struct_path
    results = [
        call_remote_object(
            rc_url,
            DATATABLE_LIBRARY_CDO,
            "FillDataTableFromJSONString",
            fill_params,
            timeout=timeout,
        )
    ]
    fill = results[0]
    if not (save and fill.ok):
        return results

    if fill.function_ok is False:
        # HTTP 200 but the engine rejected the rows — do NOT save, or we would overwrite the asset
        # with the failed (often emptied) fill. Turn the transport-ok result into a guided failure.
        results[0] = replace(
            fill,
            error=(
                "FillDataTableFromJSONString 返回 false（引擎侧导入失败，常见原因：Row Struct "
                "不匹配 / JSON 形状错 / 列名不符），已跳过 SaveAsset 以免把未成功导入的表存盘。"
            ),
        )
        return results

    if fill.function_ok is None:
        # Body carried no boolean ReturnValue — the fill outcome is unconfirmed. Transport was ok,
        # so we still save (prior behaviour) but flag that the result could not be verified.
        results[0] = replace(
            fill,
            note=(
                "响应体未包含布尔 ReturnValue，无法确认 FillDataTableFromJSONString 是否成功；"
                "传输层正常，仍按常规继续 SaveAsset。"
            ),
        )

    results.append(
        call_remote_object(
            rc_url,
            EDITOR_ASSET_LIBRARY_CDO,
            "SaveAsset",
            {"AssetToSave": table_path, "OnlyIfIsDirty": False},
            timeout=timeout,
        )
    )
    return results


# --- Python Remote Execution -------------------------------------------------------------------


def build_remote_exec_command(
    unreal_dir: str | Path, table_map: dict[str, str] | None = None
) -> str:
    """Build the in-editor Python statement that drives ``owcopilot_bridge.import_bundle``.

    Arguments are embedded as a JSON string literal (via ``repr``) so paths/mappings can never break
    out of the statement.
    """
    payload = json.dumps({"unreal_dir": str(unreal_dir), "table_map": table_map or {}})
    return (
        "import owcopilot_bridge, json; "
        f"_owc = owcopilot_bridge.import_bundle(**json.loads({payload!r})); "
        "print(_owc)"
    )


def _load_remote_execution_module() -> Any:
    """Epic's reference client, shipped inside the Python plugin. None if it isn't importable."""
    try:
        import remote_execution
    except ImportError:
        return None
    return remote_execution


def _drive_remote_execution(module: Any, command: str) -> RemoteResult:
    remote = module.RemoteExecution(module.RemoteExecutionConfig())
    remote.start()
    try:
        nodes = list(remote.remote_nodes)
        if not nodes:
            return RemoteResult(
                ok=False,
                transport=_REMOTE_EXECUTION,
                error="no_nodes",
                detail=(
                    "多播发现未找到运行中的 UE 编辑器。请在 Project Settings > Plugins > Python "
                    "勾选 Enable Remote Execution，并确认本机与编辑器在同一多播网段。"
                ),
            )
        remote.open_command_connection(nodes[0].node_id)
        result = remote.run_command(command, unattended=True, exec_mode=module.MODE_EXEC_STATEMENT)
    finally:
        remote.stop()
    ok = bool(result.get("success"))
    detail = (
        "已在编辑器内执行 import_bundle"
        if ok
        else "命令在编辑器内执行失败，请查看编辑器 Output Log 排查。"
    )
    return RemoteResult(ok=ok, transport=_REMOTE_EXECUTION, detail=detail, response=result)


def run_bundle_via_remote_execution(
    unreal_dir: str | Path,
    table_map: dict[str, str] | None = None,
    *,
    remote_execution_module: Any = None,
) -> RemoteResult:
    """Send ``import_bundle`` to a live editor over Python Remote Execution.

    ``remote_execution_module`` may be injected (tests); by default Epic's ``remote_execution`` is
    imported at runtime. A missing module or any wire error yields a guided :class:`RemoteResult`.
    """
    module = remote_execution_module or _load_remote_execution_module()
    if module is None:
        return RemoteResult(
            ok=False,
            transport=_REMOTE_EXECUTION,
            error="module_unavailable",
            detail=(
                "未找到 remote_execution 模块（Epic 随 Python 插件分发）。请把引擎的 "
                "Plugins/Experimental/PythonScriptPlugin/.../Content/Python 加入 PYTHONPATH，"
                "或改用 Remote Control HTTP 传输。"
            ),
        )
    command = build_remote_exec_command(unreal_dir, table_map)
    try:
        return _drive_remote_execution(module, command)
    except Exception as exc:  # noqa: BLE001 — surface any wire error as a guided result
        return RemoteResult(
            ok=False,
            transport=_REMOTE_EXECUTION,
            error=str(exc),
            detail="通过 Python Remote Execution 驱动编辑器时出错，请检查多播网络与编辑器设置。",
        )
