"""Quest logic-layer rules (WS-A): surface the deterministic logic problems found by
``logic.audit_quest_logic`` (undefined vars / type errors / unreachable stages / deadlocks) plus
the bundle-aware dangling-reference check (stage/quest ids that do not exist)."""

from __future__ import annotations

from collections.abc import Iterable

from ...content.models import EntityType
from ...logic import audit_quest_logic, parse_expr, refs_in
from ...logic.expr import LogicSyntaxError
from ..context import AuditContext
from ..models import Category, Evidence, Issue, Severity


class QuestLogicRule:
    code = "QUEST_LOGIC"
    severity = Severity.ERROR
    category = Category.LOGIC

    def check(self, ctx: AuditContext) -> Iterable[Issue]:
        for quest in ctx.bundle.quests.values():
            if quest.logic is None:
                continue
            target = f"quest:{quest.id}"
            for issue in audit_quest_logic(quest):
                yield self._issue(target, issue.code, issue.message)
            yield from self._dangling(ctx, quest, target)

    def _dangling(self, ctx: AuditContext, quest, target: str) -> Iterable[Issue]:
        logic = quest.logic
        assert logic is not None
        stage_ids = {stage.id for stage in quest.stages}
        for stage in logic.stage_logic:
            if stage.stage_id not in stage_ids:
                yield self._issue(
                    target,
                    "LOGIC_DANGLING_STATE_REF",
                    f"stage_logic references unknown stage '{stage.stage_id}'",
                )
        for branch in logic.branches:
            for ref_id in (branch.from_stage, branch.to_stage):
                if ref_id and ref_id not in stage_ids:
                    yield self._issue(
                        target,
                        "LOGIC_DANGLING_STATE_REF",
                        f"branch '{branch.id}' references unknown stage '{ref_id}'",
                    )
        for quest_id in logic.unlocks:
            if quest_id not in ctx.bundle.quests:
                yield self._issue(
                    target, "LOGIC_DANGLING_STATE_REF", f"unlocks unknown quest '{quest_id}'"
                )
        yield from self._dangling_quest_refs(ctx, quest, target)
        yield from self._dangling_reputation_refs(ctx, quest, target)

    def _dangling_reputation_refs(self, ctx: AuditContext, quest, target: str) -> Iterable[Issue]:
        """A `rep:<faction_id>` reference (read in a condition, or written by a stage/branch effect)
        must name a real faction entity — otherwise a choice's reputation consequence points at a
        faction that does not exist, the kind of silent inconsistency the audit is here to catch."""
        logic = quest.logic
        assert logic is not None
        factions = {e.id for e in ctx.bundle.entities.values() if e.type == EntityType.FACTION}
        seen: set[str] = set()

        def check(ref_name: str) -> Iterable[Issue]:
            if ref_name.startswith("rep:") and ref_name not in seen:
                seen.add(ref_name)
                faction = ref_name[len("rep:") :]
                if faction not in factions:
                    yield self._issue(
                        target,
                        "LOGIC_DANGLING_STATE_REF",
                        f"reputation reference 'rep:{faction}' is not a known faction",
                    )

        conditions = [logic.precondition]
        conditions += [s.precondition for s in logic.stage_logic]
        conditions += [b.condition for b in logic.branches]
        for source in conditions:
            if not source.strip():
                continue
            try:
                tree = parse_expr(source)
            except LogicSyntaxError:
                continue
            for ref in refs_in(tree):
                yield from check(ref)

        effects = [e for s in logic.stage_logic for e in s.effects_on_complete]
        effects += [e for b in logic.branches for e in b.effects]
        for effect in effects:
            yield from check(effect.var)

    def _dangling_quest_refs(self, ctx: AuditContext, quest, target: str) -> Iterable[Issue]:
        logic = quest.logic
        assert logic is not None
        sources = [logic.precondition]
        sources += [stage.precondition for stage in logic.stage_logic]
        sources += [branch.condition for branch in logic.branches]
        for source in sources:
            if not source.strip():
                continue
            try:
                tree = parse_expr(source)
            except LogicSyntaxError:
                continue  # syntax errors are already reported by audit_quest_logic
            for ref in refs_in(tree):
                if ref.startswith("quest:") and ref.endswith(".done"):
                    quest_id = ref[len("quest:") : -len(".done")]
                    if quest_id not in ctx.bundle.quests:
                        yield self._issue(
                            target,
                            "LOGIC_DANGLING_STATE_REF",
                            f"references state of unknown quest '{quest_id}'",
                        )

    def _issue(self, target: str, code: str, message: str) -> Issue:
        return Issue(
            rule_code=self.code,
            severity=self.severity,
            category=self.category,
            target_ref=target,
            message=f"[{code}] {message}",
            evidence=[Evidence(kind="logic", target_ref=target, path=code)],
        )
