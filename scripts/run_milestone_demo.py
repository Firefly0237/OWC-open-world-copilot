"""P1 milestone demo.

Offline (default, $0):   python scripts/run_milestone_demo.py
Live against DeepSeek:   python scripts/run_milestone_demo.py --real
                         (set OPENAI_BASE_URL / OPENAI_API_KEY in .env, `pip install openai`)
"""

import os
import sys

from owcopilot.demo import run_milestone_demo

if __name__ == "__main__":
    use_real = "--real" in sys.argv or os.getenv("OWCOPILOT_REAL") == "1"
    run_milestone_demo(use_real_model=use_real)
