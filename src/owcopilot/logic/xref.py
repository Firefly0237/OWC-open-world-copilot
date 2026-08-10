# 世界级状态变量交叉引用：每个 flag 谁声明、谁写、谁读（跨任务与对白树的单一事实源）。
"""World-level cross-reference for state flags — who declares, writes and reads each one.

Why this layer exists
---------------------
The quest-local checkers (:mod:`.temporal`, :mod:`.explore`) see one quest at a time. When a guard
reads a flag the quest itself never writes, :mod:`.temporal` must *withhold* its soft-lock verdict:
some other layer — a dialogue, another quest, engine gameplay — may own that flag and clear the
block. Withholding is correct in isolation and wrong in aggregate: in real commercial worlds flags
are shared across quests **by default** (CD Projekt Red's global FactsDB; Bethesda quests reading
each other's stage via ``GetStage``), so the flagship soft-lock check quietly disarms itself on
exactly the content where it matters most.

This module supplies the missing scope. One pass over the whole bundle yields, for every flag, its
declarations / writers / readers, each carrying a location the audit layer can resolve to a file and
line. :mod:`.temporal` then asks *the world* who owns a flag instead of assuming "not written by me
⇒ owned by someone else".

Method
------
A textbook use/def (reaching-definitions) cross-reference — the same artefact narrative editors ship
as a where-used report (Bethesda Creation Kit's *Use Info*). No new algorithm, no new dependency.

Soundness, stated precisely
---------------------------
* **Writes are exact.** ``Effect.var`` is a plain string field, never parsed. The writer set can
  therefore never be under-counted, which is the only direction that could manufacture a false
  "nobody writes this".
* **Reads can be under-counted**, because a guard is an expression and an expression can fail to
  parse. Every failure is recorded in :attr:`FlagXref.unparsable`, and
  :attr:`FlagXref.reads_complete` gates read-side verdicts: when it is false no caller may claim
  "nobody reads this".
* **Only declared flags get verdicts.** ``quest:<id>.done`` and ``rep:<faction>`` are implicit-scope
  references this tool does not own (see :mod:`.semantics`), and a bare undeclared reference may be
  written by engine gameplay the tool cannot see. For all of those the world stays *unknown*, never
  *never-written*. That is the safety valve keeping :meth:`FlagXref.provably_never_written` sound.

Locators are JSON field paths into the owning object's own file (``logic/branches/2/condition``,
``nodes/n030/choices/0/condition``) — the exact shape :mod:`owcopilot.audit.source_location` already
navigates, so issues built from these sites get file/line annotation for free.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from ..content.models import ContentBundle, LogicVarType, Quest
from .expr import LogicSyntaxError, parse_expr, refs_in

_QUEST_STATE_PREFIX = "quest:"
_QUEST_STATE_SUFFIX = ".done"
_FACTION_REP_PREFIX = "rep:"


class FlagScope(str, Enum):
    """Where a flag reference gets its meaning — decides whether the tool may judge it at all."""

    DECLARED = "declared"  # listed in some quest's variable table: the tool owns it
    QUEST_STATE = "quest_state"  # quest:<id>.done — implicit boolean, written by that quest
    FACTION_REP = "faction_rep"  # rep:<faction> — implicit integer standing
    UNDECLARED = "undeclared"  # a bare reference nobody declared; engine gameplay may own it


class SiteRole(str, Enum):
    READ = "read"
    WRITE = "write"


class FlagSite(BaseModel):
    """One place a flag is read or written, located precisely enough to annotate an issue."""

    role: SiteRole
    owner_kind: str  # "quest" | "dialogue_tree" — the object kind whose file holds this site
    owner_id: str
    quest_id: str | None = None  # declaring scope for a dialogue tree bound to a quest
    field_path: str  # JSON field path inside the owner's file
    detail: str  # Chinese, human-facing; free of code identifiers so messages can quote it


class FlagDeclaration(BaseModel):
    """A flag's declaration in one quest's variable table."""

    quest_id: str
    var_type: LogicVarType
    default: bool | int | str
    field_path: str

    def shape(self) -> tuple[str, str]:
        """The comparable part of a declaration: two declarations disagree iff their shapes differ.

        ``default`` is rendered rather than compared raw because ``True == 1`` in Python and a
        bool/int disagreement is exactly the kind of conflict this is meant to surface."""
        return (self.var_type.value, f"{type(self.default).__name__}:{self.default!r}")


class UnparsableExpression(BaseModel):
    """A guard the reader could not parse — recorded so read-side verdicts stay honest."""

    owner_kind: str
    owner_id: str
    field_path: str
    source: str
    error: str


class FlagFacts(BaseModel):
    """Everything the world knows about one flag."""

    flag: str
    scope: FlagScope
    declarations: list[FlagDeclaration] = Field(default_factory=list)
    readers: list[FlagSite] = Field(default_factory=list)
    writers: list[FlagSite] = Field(default_factory=list)

    def declaration_conflict(self) -> bool:
        """True when two quests declare this flag with disagreeing type or default."""
        return len({decl.shape() for decl in self.declarations}) > 1


class FlagXref(BaseModel):
    """The world-level flag fact base. Deterministic: identical bundles produce identical dumps."""

    facts: dict[str, FlagFacts] = Field(default_factory=dict)
    unparsable: list[UnparsableExpression] = Field(default_factory=list)

    @property
    def reads_complete(self) -> bool:
        """Whether every guard in the world was parsed. False ⇒ no "nobody reads this" verdict."""
        return not self.unparsable

    def declared_flags(self) -> frozenset[str]:
        return frozenset(
            flag for flag, facts in self.facts.items() if facts.scope is FlagScope.DECLARED
        )

    def declaration_conflicts(self) -> list[FlagFacts]:
        """Flags declared more than once with disagreeing shape, in flag order."""
        return [facts for facts in self.facts.values() if facts.declaration_conflict()]

    def provably_never_written(self) -> frozenset[str]:
        """Declared flags with zero writers anywhere in the world.

        Sound because writes are exact (never parsed) and the set is restricted to flags the tool
        owns. A caller may treat a block on one of these as sealed: no layer can ever clear it."""
        return frozenset(
            flag
            for flag, facts in self.facts.items()
            if facts.scope is FlagScope.DECLARED and not facts.writers
        )

    def never_read(self) -> frozenset[str]:
        """Declared flags written somewhere but read nowhere. Empty when reads are incomplete."""
        if not self.reads_complete:
            return frozenset()
        return frozenset(
            flag
            for flag, facts in self.facts.items()
            if facts.scope is FlagScope.DECLARED and facts.writers and not facts.readers
        )

    def report(self, flag: str | None = None) -> dict[str, Any]:
        """A JSON-ready where-used report. One projection shared by every product surface (CLI,
        MCP tool, agent skill) so they can never drift into showing different answers."""
        conflicts = {facts.flag for facts in self.declaration_conflicts()}
        never_written = self.provably_never_written()
        never_read = self.never_read()

        entries: list[dict[str, Any]] = []
        for name, facts in self.facts.items():
            if flag is not None and name != flag:
                continue
            verdicts: list[str] = []
            if name in conflicts:
                verdicts.append("declaration_conflict")
            if name in never_written and facts.readers:
                verdicts.append("never_written")
            if name in never_read:
                verdicts.append("never_read")
            entries.append(
                {
                    "flag": name,
                    "scope": facts.scope.value,
                    "declared_in": [decl.quest_id for decl in facts.declarations],
                    "writers": [site.model_dump(mode="json") for site in facts.writers],
                    "readers": [site.model_dump(mode="json") for site in facts.readers],
                    "verdicts": verdicts,
                }
            )
        if flag is not None and not entries:
            raise ValueError(
                f"这个世界里没有名叫 '{flag}' 的状态变量。先不带变量名跑一次看看有哪些变量，"
                "或者检查一下拼写。"
            )
        return {
            "flags": entries,
            "reads_complete": self.reads_complete,
            "unparsable": [item.model_dump(mode="json") for item in self.unparsable],
            "summary": {
                "declared": len(self.declared_flags()),
                "declaration_conflicts": sorted(conflicts),
                "provably_never_written": sorted(never_written),
                "never_read": sorted(never_read),
            },
        }


class _Collector:
    """Accumulates sites per flag, then classifies scope once every declaration is known."""

    def __init__(self) -> None:
        self._declarations: dict[str, list[FlagDeclaration]] = {}
        self._readers: dict[str, list[FlagSite]] = {}
        self._writers: dict[str, list[FlagSite]] = {}
        self.unparsable: list[UnparsableExpression] = []

    def declare(self, flag: str, declaration: FlagDeclaration) -> None:
        self._declarations.setdefault(flag, []).append(declaration)
        self._readers.setdefault(flag, [])
        self._writers.setdefault(flag, [])

    def write(self, flag: str, site: FlagSite) -> None:
        self._writers.setdefault(flag, []).append(site)
        self._readers.setdefault(flag, [])

    def read_expression(
        self,
        source: str,
        *,
        owner_kind: str,
        owner_id: str,
        quest_id: str | None,
        field_path: str,
        detail: str,
    ) -> None:
        """Parse one guard and record every flag it reads; a parse failure is recorded, not dropped.

        :func:`.temporal._refs_of` swallows a syntax error into an empty set, which is right for a
        checker that only needs a conservative slice. Here it would be wrong: silently losing a
        reader is what becomes a false "nobody reads this", so the failure is surfaced instead."""
        if not source.strip():
            return
        try:
            refs = refs_in(parse_expr(source))
        except LogicSyntaxError as exc:
            self.unparsable.append(
                UnparsableExpression(
                    owner_kind=owner_kind,
                    owner_id=owner_id,
                    field_path=field_path,
                    source=source,
                    error=str(exc),
                )
            )
            return
        for flag in sorted(refs):
            self._readers.setdefault(flag, []).append(
                FlagSite(
                    role=SiteRole.READ,
                    owner_kind=owner_kind,
                    owner_id=owner_id,
                    quest_id=quest_id,
                    field_path=field_path,
                    detail=detail,
                )
            )
            self._writers.setdefault(flag, [])

    def build(self) -> FlagXref:
        flags = sorted(set(self._declarations) | set(self._readers) | set(self._writers))
        facts = {
            flag: FlagFacts(
                flag=flag,
                scope=_scope_of(flag, declared=flag in self._declarations),
                declarations=self._declarations.get(flag, []),
                readers=self._readers.get(flag, []),
                writers=self._writers.get(flag, []),
            )
            for flag in flags
        }
        return FlagXref(facts=facts, unparsable=self.unparsable)


def _scope_of(flag: str, *, declared: bool) -> FlagScope:
    if declared:
        return FlagScope.DECLARED
    if flag.startswith(_QUEST_STATE_PREFIX) and flag.endswith(_QUEST_STATE_SUFFIX):
        return FlagScope.QUEST_STATE
    if flag.startswith(_FACTION_REP_PREFIX):
        return FlagScope.FACTION_REP
    return FlagScope.UNDECLARED


def _collect_quest(collector: _Collector, quest: Quest) -> None:
    qid = quest.id

    # A quest writes its own completion flag by existing: reaching its end sets quest:<id>.done
    # (see quest_reachability.mark_done / semantics._ref_type). Recorded before the logic-less
    # early return, because a legacy quest without a logic block still completes — a cross-quest
    # guard on its completion flag must never look unwritten just because that quest is legacy.
    collector.write(
        f"{_QUEST_STATE_PREFIX}{qid}{_QUEST_STATE_SUFFIX}",
        FlagSite(
            role=SiteRole.WRITE,
            owner_kind="quest",
            owner_id=qid,
            quest_id=qid,
            field_path="id",
            detail="任务自身完成时置位的完成标记",
        ),
    )

    logic = quest.logic
    if logic is None:
        return

    for index, var in enumerate(logic.variables):
        collector.declare(
            var.id,
            FlagDeclaration(
                quest_id=qid,
                var_type=var.type,
                default=var.default,
                field_path=f"logic/variables/{index}",
            ),
        )

    collector.read_expression(
        logic.precondition,
        owner_kind="quest",
        owner_id=qid,
        quest_id=qid,
        field_path="logic/precondition",
        detail="任务的开启条件",
    )

    for index, stage in enumerate(logic.stage_logic):
        collector.read_expression(
            stage.precondition,
            owner_kind="quest",
            owner_id=qid,
            quest_id=qid,
            field_path=f"logic/stage_logic/{index}/precondition",
            detail=f"阶段『{stage.stage_id}』的进入条件",
        )
        for eff_index, effect in enumerate(stage.effects_on_complete):
            collector.write(
                effect.var,
                FlagSite(
                    role=SiteRole.WRITE,
                    owner_kind="quest",
                    owner_id=qid,
                    quest_id=qid,
                    field_path=f"logic/stage_logic/{index}/effects_on_complete/{eff_index}",
                    detail=f"阶段『{stage.stage_id}』完成后的后果",
                ),
            )

    for index, branch in enumerate(logic.branches):
        collector.read_expression(
            branch.condition,
            owner_kind="quest",
            owner_id=qid,
            quest_id=qid,
            field_path=f"logic/branches/{index}/condition",
            detail=f"分支『{branch.id}』的成立条件",
        )
        for eff_index, effect in enumerate(branch.effects):
            collector.write(
                effect.var,
                FlagSite(
                    role=SiteRole.WRITE,
                    owner_kind="quest",
                    owner_id=qid,
                    quest_id=qid,
                    field_path=f"logic/branches/{index}/effects/{eff_index}",
                    detail=f"分支『{branch.id}』被选中后的后果",
                ),
            )


def build_flag_xref(bundle: ContentBundle) -> FlagXref:
    """Cross-reference every state flag in a world: who declares it, who writes it, who reads it.

    Pure, deterministic and model-free — the same properties the deterministic audit relies on."""
    collector = _Collector()

    for quest_id in sorted(bundle.quests):
        _collect_quest(collector, bundle.quests[quest_id])

    for tree_id in sorted(bundle.dialogue_trees):
        tree = bundle.dialogue_trees[tree_id]
        for node_id in sorted(tree.nodes):
            node = tree.nodes[node_id]
            for choice_index, choice in enumerate(node.choices):
                ordinal = choice_index + 1
                collector.read_expression(
                    choice.condition,
                    owner_kind="dialogue_tree",
                    owner_id=tree_id,
                    quest_id=tree.quest_id,
                    field_path=f"nodes/{node_id}/choices/{choice_index}/condition",
                    detail=f"对白节点『{node_id}』第 {ordinal} 个选项的出现条件",
                )
                for eff_index, effect in enumerate(choice.effects):
                    collector.write(
                        effect.var,
                        FlagSite(
                            role=SiteRole.WRITE,
                            owner_kind="dialogue_tree",
                            owner_id=tree_id,
                            quest_id=tree.quest_id,
                            field_path=f"nodes/{node_id}/choices/{choice_index}/effects/{eff_index}",
                            detail=f"对白节点『{node_id}』第 {ordinal} 个选项被选中后的后果",
                        ),
                    )

    return collector.build()
