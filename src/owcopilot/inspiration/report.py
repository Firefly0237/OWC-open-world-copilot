# 溯源报告的一行：模型自述的四个字段 + 代码重算出来的泄漏结论。
"""The 溯源 report row shared by every chain that grounds on the inspiration library.

It used to live in ``worldgen/models.py`` because 创世/扩写 were the only chains that read the
library. Since the draft chains (任务草稿/人设/对白树/bark/flavor) ground on it too (N4.2/N4.3),
the row belongs to the inspiration line itself — ``worldgen.models`` re-exports it, so every
existing import keeps working, and ``assist.*`` can build the same row without importing
``worldgen`` (which imports ``assist`` — the two must not point at each other).
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from .leakage import ReferenceLeakage

#: What a reference row's ``transformation`` says when no model account exists — because the model
#: did not write one (worldgen), or because the chain never asks for one (the draft chains). It must
#: stay a statement about the MISSING evidence, never a compliance claim: this field records what
#: the MODEL said, so code asserting "没有复用原文" on its behalf would be an unfalsifiable claim
#: shown to the user as if the model had accounted for itself. ``excluded`` stays EMPTY in the same
#: situation for the same reason — an empty list reads as "nothing was reported", a filled one reads
#: as "these were verified to be excluded". The deterministic side of the question does now exist,
#: but it lives in its own field (``ReferenceReportItem.leakage``, computed by
#: :mod:`owcopilot.inspiration.leakage`) precisely so a recomputed result is never mistaken for the
#: model's self-report.
UNVERIFIED_TRANSFORMATION = "模型未提供转化说明（未核查）"


class ReferenceReportItem(BaseModel):
    source_ref: str
    source_title: str
    used_for: str
    transformation: str
    excluded: list[str] = Field(default_factory=list)
    #: 确定性的素材泄漏检测结论（草稿 × 这条素材原文）。上面四个字段都是**模型自述**，这一个是
    #: 代码重算出来的，两者故意分开放：自述可以是空话，重算的结论可核查。``None`` = 这条报告是
    #: 在检测接线之前产出的（或调用方没给草稿），读作"没查"，不是"查过没问题"。
    leakage: ReferenceLeakage | None = None


__all__ = ["UNVERIFIED_TRANSFORMATION", "ReferenceReportItem"]
