"""Small cross-cutting helpers shared across modules.

Console/env helpers (stdout encoding, ``.env`` loading) were previously inline in ``demo.py``; the
id helpers (``slugify``/``unique_id``) were copy-pasted — with a drifting CJK range — into half a
dozen pipelines. Single-sourcing them here means the same name slugs to the same id everywhere,
which is what keeps cross-pipeline references (e.g. an extracted entity and its dialogue) lined up.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

# Collapse every run of non-[a-z0-9] / non-CJK characters to a single "_". The CJK range
# U+3400–U+9FFF covers Extension A plus the main block, so Chinese names stay legible in ids. This
# is the ONE definition; pipelines used to each carry their own copy and they had drifted (one
# started at U+4E00, dropping Extension A), so the same name slugged to different ids per pipeline.
_SLUG_NON_WORD = re.compile(r"[^a-z0-9㐀-鿿]+")


def slugify(value: str, *, fallback: str = "") -> str:
    """A lowercase, underscore-joined id stem from text; ``fallback`` when it would slug empty."""
    text = _SLUG_NON_WORD.sub("_", value.strip().lower()).strip("_")
    return text or fallback


def unique_id(prefix: str, raw: str, used: set[str], *, fallback: str = "item") -> str:
    """A ``prefix_stem`` id unique within ``used``, suffixing ``_2``, ``_3`` … on collision. The
    chosen id is added to ``used`` so successive calls with the same set never collide."""
    stem = slugify(raw, fallback=fallback)
    base = stem if stem.startswith(f"{prefix}_") else f"{prefix}_{stem}"
    candidate = base
    index = 2
    while candidate in used:
        candidate = f"{base}_{index}"
        index += 1
    used.add(candidate)
    return candidate


def use_utf8_stdout() -> None:
    """Force UTF-8 console output. Windows shells often default to a legacy codepage
    (e.g. GBK on a Chinese system), which raises UnicodeEncodeError on non-ASCII content
    (Chinese NPC/location names). Called only by the console entry points, so test capture
    is untouched.
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
