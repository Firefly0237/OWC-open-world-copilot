"""版号 / 内容合规 整改工作流的数据模型（WS-D）。

把一次性的敏感词清查（`assist/sweep.py`）升级成可交付审查的**整改闭环**：每条违规成为一个 case，走
标记→指派→修复→复扫→签核 的生命周期，每次流转留下不可抵赖的痕迹。规则包按版号/发行要求可配置。
对标企业合规"发现→整改→留痕→签核"闭环与国家新闻出版署内容审核要点（类目）。
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class CaseStatus(str, Enum):
    FLAGGED = "flagged"  # 由清查开案，待处理
    ASSIGNED = "assigned"  # 已指派负责人
    FIXING = "fixing"  # 整改中（含复扫未通过被打回）
    RESCAN_PASSED = "rescan_passed"  # 复扫确认违规已消除，待签核
    SIGNED_OFF = "signed_off"  # 已签核（终态）
    DISMISSED = "dismissed"  # 判定为误报/可接受，关闭（终态）


# 人工流转的合法迁移。注意：FIXING→RESCAN_PASSED 只能由 rescan_case 在复扫"确认干净"后驱动，
# 人工不能直接把 case 标成复扫通过——这是"未消除不可签核"的关键约束。
MANUAL_TRANSITIONS: dict[CaseStatus, set[CaseStatus]] = {
    CaseStatus.FLAGGED: {CaseStatus.ASSIGNED, CaseStatus.DISMISSED},
    CaseStatus.ASSIGNED: {CaseStatus.FIXING, CaseStatus.DISMISSED},
    CaseStatus.FIXING: {CaseStatus.DISMISSED},  # 要进复扫必须跑 rescan_case
    CaseStatus.RESCAN_PASSED: {CaseStatus.SIGNED_OFF, CaseStatus.FIXING, CaseStatus.DISMISSED},
    CaseStatus.SIGNED_OFF: set(),
    CaseStatus.DISMISSED: set(),
}


class CaseEvent(BaseModel):
    """一次状态流转的不可抵赖痕迹。"""

    at: str  # ISO 时间
    operator: str
    from_status: str
    to_status: str
    note: str = ""


class RemediationCase(BaseModel):
    id: str
    object_ref: str  # 命中的正典对象，如 "entity:npc_x" / "term:t1"
    rule_id: str  # 命中的规则/层，如 "term" / "semantic" / "judge"
    category: str  # 类目，如 "敏感词命中"
    evidence: str
    status: CaseStatus = CaseStatus.FLAGGED
    assignee: str = ""
    history: list[CaseEvent] = Field(default_factory=list)


class RulePack(BaseModel):
    """可配置规则包：按版号/发行要求切换。默认包仅示例，真实词表由发行配置。"""

    id: str
    name: str
    version_label: str = ""
    categories: list[str] = Field(default_factory=list)
    terms: list[str] = Field(default_factory=list)
    semantic_threshold: float = 0.0


class ComplianceReport(BaseModel):
    rule_pack: str
    generated_at: str
    total: int
    by_status: dict[str, int]
    open_unresolved: int  # 既未签核也未关闭
    signed_off: int
    cases: list[RemediationCase]


DEFAULT_RULE_PACK = RulePack(
    id="default",
    name="通用版号合规（示例包）",
    version_label="example",
    categories=["涉政导向", "涉赌", "涉黄低俗", "暴恐", "价值导向"],
    terms=["赌博", "私服", "外挂"],  # 示例主题词；真实词表请按发行要求配置
    semantic_threshold=0.5,
)
