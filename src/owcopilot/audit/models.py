"""Audit domain models shared by rules, reports, API and CLI."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, model_serializer


class Severity(str, Enum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class Category(str, Enum):
    IMPORT = "import"
    REFERENCE = "reference"
    GRAPH = "graph"
    LORE = "lore"
    REGION = "region"
    PIPELINE = "pipeline"
    STYLE = "style"
    TRUST = "trust"
    LOGIC = "logic"


class IssueStatus(str, Enum):
    OPEN = "open"
    SUPPRESSED = "suppressed"
    FIXED = "fixed"


class Evidence(BaseModel):
    kind: str
    target_ref: str | None = None
    path: str | None = None
    relation: tuple[str, str, str] | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class Issue(BaseModel):
    id: str | None = None
    rule_code: str
    severity: Severity = Severity.ERROR
    category: Category
    target_ref: str
    message: str
    evidence: list[Evidence] = Field(default_factory=list)
    fingerprint: str | None = None
    audit_run_id: str | None = None
    status: IssueStatus = IssueStatus.OPEN
    # 行业报告 §15.9 的六要素里，除 target_ref（任务 id）与 message（原因）之外的四项。
    # 文件/行号/节点 id 由 ``audit/source_location.py`` 确定性推导，建议动作由各条规则自己给出。
    # 默认全空 = 尚未定位，此时整组字段不进序列化（见 ``_omit_empty_location_fields``）。
    source_path: str = ""  # 相对内容根的文件路径，如 quests/hl_x.json
    source_line: int | None = None
    node_ref: str = ""  # 对话节点 id，从证据路径的 nodes/<id>/... 里取
    suggested_action: str = ""

    @model_serializer(mode="wrap")
    def _omit_empty_location_fields(self, handler: Any) -> Any:
        # 空值一律不写进 dump，所以既有的审计报告 JSON、内容哈希与金样字节保持逐字节不变；
        # 缺键读回来就是各字段的默认值。与 ``content.models.DialogueChoice`` 的做法同源。
        data = handler(self)
        if isinstance(data, dict):
            for name in ("source_path", "source_line", "node_ref", "suggested_action"):
                if not data.get(name):
                    data.pop(name, None)
        return data


class AuditRun(BaseModel):
    id: str
    content_hash: str
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    rule_set_version: str = "v2.0"
    totals: dict[str, int] = Field(default_factory=dict)
    baseline_delta: dict[str, int] = Field(default_factory=dict)
