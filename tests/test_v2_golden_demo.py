from __future__ import annotations

import json
import subprocess
import sys


def test_run_v2_golden_demo_script_outputs_passing_report(tmp_path) -> None:
    workspace = tmp_path / "demo"
    result = subprocess.run(
        [
            sys.executable,
            "scripts/run_v2_golden_demo.py",
            "--workspace",
            str(workspace),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    body = json.loads(result.stdout)
    assert body["passed"] is True
    assert (workspace / "exports" / "generic" / "manifest.json").exists()
