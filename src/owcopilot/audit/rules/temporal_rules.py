# flag 时序不变式规则：把切片式 CTL 检查接进确定性审计门（软锁 AG EF + 策划声明的 AG/EF 规约）。
"""Flag temporal-invariant audit rule (A1) — wires the sliced CTL checker
(:mod:`owcopilot.logic.temporal`) into the deterministic audit runner.

Two spec sources, per the design:

* **Universal soundness lint (built-in, zero declaration).** For every quest with a logic layer the
  rule proves ``AG EF ending`` — *no soft-lock*: every reachable (stage, flag-state) can still reach
  some ending. This is the one genuinely universal CTL property (it needs no planner input) and the
  flagship the structural reachability audit cannot see once flag semantics gate the branches.
* **Plot-specific declared specs.** Any :class:`~owcopilot.logic.temporal.TemporalSpec` a planner
  has confirmed (via the spec-authoring agent) and stored on ``quest.metadata["temporal_specs"]`` is
  checked — ``AG p`` invariants (mutual exclusion, resource floors, "a done flag never regresses")
  and ``EF p`` reachability.

Zero-false-positive discipline (this is a hard audit gate, like the other 30 rules): a violation is
emitted as an ERROR **only when the CTL verdict is sound** — a concrete reachable counterexample, or
a proof over an exhaustively explored state space. A bound-truncated "could not prove" verdict emits
nothing (it is not a violation), and a *vacuous* pass (an invariant that holds only because its
trigger is never reachable — a likely mis-grounded spec) emits a WARNING for human review, never an
ERROR. Every emitted issue carries the replayable branch-id witness in its evidence.

消息与建议动作面向策划中文化：``logic/temporal.py`` 回传的 ``counterexample_detail``/``message``
本来就是中文（见该模块），这里只再补一条确定性的建议动作——点出具体的阶段与见证路径；至于该改哪个
分支/阶段的哪个字段，属于需要结合规约设计意图的人工判断，如实说明而不硬编一个可能是错的字段。
"""

from __future__ import annotations

from collections.abc import Iterable

from ...logic.temporal import (
    PropertyPattern,
    SpecStatus,
    TemporalResult,
    TemporalSpec,
    check_soft_lock,
    check_temporal_spec,
    parse_specs_from_quest,
)
from ..context import AuditContext
from ..models import Category, Evidence, Issue, Severity

_PATTERN_VIOLATION_CODE = {
    PropertyPattern.AG: "AG_INVARIANT_VIOLATED",
    PropertyPattern.EF: "EF_UNREACHABLE",
    PropertyPattern.AG_EF_ENDING: "QUEST_SOFT_LOCK",
}


class FlagTemporalInvariantRule:
    code = "FLAG_TEMPORAL_INVARIANT"
    severity = Severity.ERROR
    category = Category.LOGIC

    def check(self, ctx: AuditContext) -> Iterable[Issue]:
        for quest in ctx.bundle.quests.values():
            if quest.logic is None or not quest.stages:
                continue
            target = f"quest:{quest.id}"

            soft_lock = check_soft_lock(quest)
            if soft_lock.status is SpecStatus.VIOLATED:
                yield self._violation_issue(quest.id, target, soft_lock)

            for spec in parse_specs_from_quest(quest):
                yield from self._check_spec(quest.id, target, quest, spec)

    def _check_spec(self, quest_id: str, target: str, quest, spec: TemporalSpec) -> Iterable[Issue]:
        result = check_temporal_spec(quest, spec)
        if result.status is SpecStatus.VIOLATED:
            yield self._violation_issue(quest_id, target, result)
        elif result.status is SpecStatus.VACUOUS:
            yield self._advisory_issue(
                target, result, "SPEC_VACUOUS", Severity.WARNING,
                extra="规约在所有可达状态成立，但其前件从未被触达，疑似接地错误（空泛满足）。",
                action=(
                    f"检查规约《{spec.id}》的前件表达式，看看是不是变量名写错了，或者任务"
                    f"《{quest_id}》里确实缺了一条能触发这个前提的分支/效果——如果前提永远成立"
                    "不了，这条规约就等于没在测任何东西，需要人工确认设计意图。"
                ),
            )
        elif result.status is SpecStatus.ERROR:
            yield self._advisory_issue(
                target, result, "SPEC_MALFORMED", Severity.WARNING,
                extra="声明的时序规约无法在该任务上解析/求值，请检查表达式与符号。",
                action=(
                    f"打开任务《{quest_id}》，检查规约《{spec.id}》的表达式写法（变量名、运算符），"
                    "确认它引用的都是这个任务的变量表里已经声明过的变量。"
                ),
            )

    def _violation_issue(self, quest_id: str, target: str, result: TemporalResult) -> Issue:
        subcode = _PATTERN_VIOLATION_CODE[result.pattern]
        detail = result.counterexample_detail or result.message
        witness = " → ".join(result.counterexample_witness) or "无(初始状态即违反)"
        if result.pattern is PropertyPattern.AG_EF_ENDING:
            action = (
                f"任务《{quest_id}》在阶段 '{result.counterexample_stage}' 走进了死路，途经"
                f"分支：{witness}。检查这条路径上的分支/阶段条件，补一条能继续走向结局的路径，"
                "或者去掉这个走不出去的阶段。"
            )
        else:
            action = (
                f"规约在任务《{quest_id}》阶段 '{result.counterexample_stage}' 被打破，途经"
                f"分支：{witness}。检查这条路径上各步的效果，看是哪一步违反了规约；具体应该改"
                "哪个分支/阶段的效果，需要结合这条规约的设计意图人工判断。"
            )
        return Issue(
            rule_code=self.code,
            severity=Severity.ERROR,
            category=self.category,
            target_ref=target,
            message=f"[{subcode}] {detail}",
            evidence=[
                Evidence(
                    kind="temporal",
                    target_ref=target,
                    path=result.spec_id,
                    data={
                        "pattern": result.pattern.value,
                        "witness": result.counterexample_witness,
                        "stage": result.counterexample_stage,
                        "strong": result.strong,
                        "states_explored": result.states_explored,
                        "slice_kept_vars": result.slice_kept_vars,
                    },
                )
            ],
            suggested_action=action,
        )

    def _advisory_issue(
        self,
        target: str,
        result: TemporalResult,
        subcode: str,
        severity: Severity,
        *,
        extra: str,
        action: str,
    ) -> Issue:
        return Issue(
            rule_code=self.code,
            severity=severity,
            category=self.category,
            target_ref=target,
            message=f"[{subcode}] {extra}",
            evidence=[
                Evidence(
                    kind="temporal",
                    target_ref=target,
                    path=result.spec_id,
                    data={"pattern": result.pattern.value, "status": result.status.value},
                )
            ],
            suggested_action=action,
        )
