# OWCopilotBridge UE 侧导入器：一个代码路径，供菜单/Remote Control/Remote Execution 三条驱动共用。
"""OWCopilot's in-editor DataTable importer (the single tested code path).

The menu button (``init_unreal.py``), Python Remote Execution, and Remote Control HTTP all call the
same functions here, so correctness lives in one place. Stdlib-only, and ``import unreal`` is
guarded so the package imports under CI/this venv with no engine present.
"""

from __future__ import annotations

from .bundle import BundleImportPlan, TablePlan, plan_bundle_import
from .importer import BundleImportResult, ImportResult, import_bundle, import_rows
from .merge import MergeOutcome, merge_rows_by_name

__all__ = [
    "import_bundle",
    "import_rows",
    "plan_bundle_import",
    "merge_rows_by_name",
    "BundleImportResult",
    "ImportResult",
    "BundleImportPlan",
    "TablePlan",
    "MergeOutcome",
]
