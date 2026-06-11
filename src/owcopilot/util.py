"""Small console / environment helpers shared by the demo entry points.

These were previously defined inline in ``demo.py``. They live here so the demo module can focus
on pipeline assembly while the cross-cutting helpers (stdout encoding, ``.env`` loading) are reused
without importing the whole demo surface. Neither touches pipeline logic.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def use_utf8_stdout() -> None:
    """Force UTF-8 console output. Windows shells often default to a legacy codepage
    (e.g. GBK on a Chinese system), which raises UnicodeEncodeError on the demo's bullets —
    and on any non-ASCII World Bible content (Chinese NPC/location names). Called only by the
    console entry points, so test capture is untouched.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass


def load_dotenv() -> None:
    """Minimal, dependency-free .env loader (project root). Existing env vars win.

    Lets the real-model path pick up OPENAI_BASE_URL / OPENAI_API_KEY without adding a
    python-dotenv dependency. Offline runs never call this.
    """
    env_path = Path(__file__).resolve().parents[2] / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())
