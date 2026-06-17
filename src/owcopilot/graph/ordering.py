"""Where an object sits on the world timeline.

A quest carries ``timeline_order`` as a first-class field, but events are entities and stash their
order in ``metadata['timeline_order']`` (or, legacy, an ``order=N`` tag). The timeline audit rules
and the timeline view both need to read that order the same way, so the single extractor lives here
rather than being copied into each caller.
"""

from __future__ import annotations

import re
from typing import Any

_ORDER_RE = re.compile(r"^order\s*=\s*(-?\d+)$")


def timeline_order_of(metadata: dict[str, Any] | None, tags: list[str]) -> int | None:
    """Read an object's timeline order from its metadata or an ``order=N`` tag, else None."""
    if metadata:
        raw = metadata.get("timeline_order")
        if isinstance(raw, int) and not isinstance(raw, bool):
            return raw
        if isinstance(raw, str) and raw.strip().lstrip("-").isdigit():
            return int(raw.strip())
    for tag in tags:
        match = _ORDER_RE.match(tag.strip())
        if match:
            return int(match.group(1))
    return None
