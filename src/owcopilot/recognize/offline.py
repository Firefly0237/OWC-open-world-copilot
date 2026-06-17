"""A deterministic, test/CI-only relation provider so the LLM-assist wiring can run offline.

This is NOT a product mode — like the other offline doubles it is reachable only when the gateway is
allowed offline (``OWCOPILOT_ALLOW_OFFLINE_LLM``). It emits one grounded proposal (between the first
two given ids, quoting a verbatim slice of the source) so the §8 guards have something real to pass.
"""

from __future__ import annotations

import json
import re

_IDS = re.compile(r"实体 id 列表：(.+)")


def _known_ids(user: str) -> list[str]:
    match = _IDS.search(user)
    if not match:
        return []
    head = match.group(1).split("\n", 1)[0]
    return [part.strip() for part in head.split(",") if part.strip()]


class OfflineRelationProvider:
    """Gateway provider double: returns a single grounded relation as a JSON array."""

    def complete(self, *, system: str, user: str, model: str) -> tuple[str, int, int]:
        ids = _known_ids(user)
        text = user.split("原文：", 1)[1].strip() if "原文：" in user else ""
        payload: list[dict[str, object]] = []
        if len(ids) >= 2 and text:
            span = text.splitlines()[0].strip()[:40]  # verbatim slice -> passes evidence grounding
            payload = [
                {
                    "source": ids[0],
                    "target": ids[1],
                    "kind": "related_to",
                    "evidence": span,
                    "confidence": 0.9,
                }
            ]
        out = json.dumps(payload, ensure_ascii=False)
        return out, max(1, len(user) // 4), max(1, len(out) // 4)
