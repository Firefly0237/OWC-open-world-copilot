"""P3 demo: land a verified Quest into Unreal Engine 5 (and optionally Unity).

Offline (default, $0):   python scripts/run_ue_demo.py
    intent -> grounded gen -> caught -> repair -> clean -> land into a FakeUnrealBridge -> snapshot.

Two engines (offline):   python scripts/run_ue_demo.py --two-engines
    the same consistent Quest landed via UnrealAdapter AND UnityAdapter ("one core, two engines").

Live against UE5 (manual, needs an open editor with Remote Control on :30010 + the QuestCopilot
helper — see docs/P3_results.md):
    python scripts/run_ue_demo.py --ue          # real DataTable upsert + read-back
    python scripts/run_ue_demo.py --ue --real   # ...also drive generation with real DeepSeek
"""

import os
import sys

from owcopilot.demo import run_two_engine_demo, run_ue_demo

if __name__ == "__main__":
    if "--two-engines" in sys.argv:
        run_two_engine_demo()
    else:
        use_ue = "--ue" in sys.argv or os.getenv("OWCOPILOT_UE") == "1"
        use_real = "--real" in sys.argv
        run_ue_demo(use_real_bridge=use_ue, use_real_model=use_real)
