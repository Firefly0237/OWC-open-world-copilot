# 信任与溯源审计规则。
"""Trust and provenance audit rules."""

from __future__ import annotations

from collections.abc import Iterable

from ...content.models import Origin, ReviewStatus
from ...trust import iter_provenance_records
from ..context import AuditContext
from ..models import Category, Evidence, Issue, Severity

# target_ref 的对象类型 -> 策划看得懂的名字；这条规则会扫到内容树里几乎所有对象类型。
_KIND_LABELS = {
    "entity": "角色/物件",
    "quest": "任务",
    "region": "区域",
    "poi": "地点",
    "dialogue": "对话",
    "dialogue_tree": "对话树",
    "localized_text": "本地化文本",
    "term": "术语",
    "style_guide": "风格指南",
    "relation": "关系",
    "quest_event_ref": "任务事件关联",
}

# 审核状态 -> (给策划看的现状描述, 这一条实例该怎么处理)。两种未通过状态需要的动作不一样：
# 待评审的还没人看过，需要先安排评审；被驳回的已经有结论，需要按意见改完再重新提交。
_REVIEW_STATUS_TEXT = {
    ReviewStatus.PENDING_REVIEW: (
        "还没有经过人工评审",
        "安排一次人工评审;确认没问题后,把这条内容的审核状态改成『已通过』。",
    ),
    ReviewStatus.REJECTED: (
        "之前被人工评审驳回,还没有修改后重新提交",
        "按驳回时的意见修改这条内容,改完之后重新提交评审,状态会变回『待评审』。",
    ),
}


class UnreviewedAIContentRule:
    code = "UNREVIEWED_AI_CONTENT"
    severity = Severity.WARNING
    category = Category.TRUST

    def check(self, ctx: AuditContext) -> Iterable[Issue]:
        for record in iter_provenance_records(ctx.bundle):
            if record.origin == Origin.HUMAN or record.review_status == ReviewStatus.APPROVED:
                continue
            kind, _, object_id = record.ref.partition(":")
            label = _KIND_LABELS.get(kind, kind)
            status_desc, action_desc = _REVIEW_STATUS_TEXT[record.review_status]
            yield Issue(
                rule_code=self.code,
                severity=self.severity,
                category=self.category,
                target_ref=record.ref,
                message=(
                    f"{label}「{object_id}」的内容是 AI 生成的,{status_desc},还不能当成定稿使用。"
                ),
                evidence=[
                    Evidence(
                        kind="provenance",
                        target_ref=record.ref,
                        data={
                            "origin": record.origin.value,
                            "review_status": record.review_status.value,
                            "source_path": record.source_path,
                        },
                    )
                ],
                suggested_action=action_desc,
            )
