# 外部系统对接客户端（UE 远程传输等），OWCopilot 侧的薄壳。
"""Integrations — thin OWCopilot-side clients for external systems (e.g. a live Unreal editor).

The in-editor importer itself ships as a content-only UE plugin under ``integrations/unreal/`` (out
of the Python package on purpose — it travels with the game project). This package holds only the
venv-side transports that push to it.
"""

from __future__ import annotations

from .unreal_remote import (
    DATATABLE_LIBRARY_CDO,
    EDITOR_ASSET_LIBRARY_CDO,
    RemoteResult,
    build_remote_exec_command,
    call_remote_object,
    push_datatable_via_remote_control,
    run_bundle_via_remote_execution,
)

__all__ = [
    "RemoteResult",
    "call_remote_object",
    "push_datatable_via_remote_control",
    "build_remote_exec_command",
    "run_bundle_via_remote_execution",
    "DATATABLE_LIBRARY_CDO",
    "EDITOR_ASSET_LIBRARY_CDO",
]
