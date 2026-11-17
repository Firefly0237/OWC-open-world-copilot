# 世界级状态变量一致性规则：声明冲突 / 门槛永不开启 / 选择无后果 / 引用了更晚才发生的事。
"""World-level state-flag consistency (built on :mod:`owcopilot.logic.xref`).

Every other logic rule in this package reasons **inside one quest**. That is the right scope for
reachability and for the CTL checks, and the wrong scope for the questions a planner actually asks
about a shared flag: *who else declares this? does anyone ever set it? does anything read what I
write?* In a commercial world flags are shared across quests by default — CD Projekt Red's FactsDB
is one flat global table, Bethesda quests read each other's progress through ``GetStage`` — so these
questions are not exotic, they are the normal failure surface, and nothing in the tool answered them
before this rule.

Four sub-codes, all decided from the cross-reference and all restricted to flags the tool owns
(declared in some quest's variable table). ``quest:<id>.done`` and ``rep:<faction>`` are
implicit-scope references and an undeclared bare reference may be written by engine gameplay this
tool cannot see; judging any of those would be guessing, so they are skipped entirely.

* ``FLAG_DECL_CONFLICT`` — two quests declare the same flag with disagreeing type or initial value.
  Provable, zero guessing. Today this is worse than invisible: the reachability rule's value-domain
  seeder takes whichever declaration it meets first and silently drops the other (see
  ``quest_reachability``), so the conflict decides behaviour without ever being reported.
* ``FLAG_NEVER_WRITTEN`` — a declared flag is read as a gate but nothing anywhere writes it, so the
  gate can never open and everything behind it is unreachable.
* ``FLAG_NEVER_READ`` — a declared flag is written but never read: the player's choices at those
  sites produce no observable difference ("choice without consequence"). Withheld entirely when any
  expression in the world failed to parse, because then the reader set is incomplete and the verdict
  would be a guess.
* ``PHASE_REFERENCE_TOO_EARLY`` — content reads a flag that only later content ever writes. This is
  the decidable form of "the NPC talks about something that has not happened yet"; it mirrors the
  existing quest↔event ordering check in :mod:`..rules.lore_rules`, applied to the flag layer.

Severity is WARNING across the board on purpose. An ERROR is a release blocker
(``pipeline/export_gate.py``), and none of these four has yet been run against a large body of real
content; promoting one is a separate decision that needs its own evidence, not a default.
"""

from __future__ import annotations

from collections.abc import Iterable

from ...content.models import ContentBundle
from ...logic.xref import FlagFacts, FlagScope, FlagSite, FlagXref, build_flag_xref
from ..context import AuditContext
from ..models import Category, Evidence, Issue, Severity

_TYPE_LABEL = {"bool": "布尔", "int": "整数", "enum": "枚举"}


def _value_label(value: bool | int | str) -> str:
    if isinstance(value, bool):  # bool before int: True is an int in Python
        return "真" if value else "假"
    return str(value)


def _declaration_label(var_type: str, default: bool | int | str) -> str:
    return f"{_TYPE_LABEL.get(var_type, var_type)}（初始值 {_value_label(default)}）"


def _site_evidence(site: FlagSite) -> Evidence:
    """One site → one evidence entry pointing at the owning object's own file and field.

    ``field_path`` is already the JSON path shape ``source_location`` navigates, so the runner
    annotates file/line (and, inside a dialogue tree, the node id) without this rule knowing
    anything about where content lives on disk."""
    return Evidence(
        kind=site.owner_kind,
        target_ref=f"{site.owner_kind}:{site.owner_id}",
        path=site.field_path,
        data={"detail": site.detail},
    )


def _where(site: FlagSite) -> str:
    """Human-facing "在《对象》的某处" phrase for a message."""
    return f"《{site.owner_id}》的{site.detail}"


def _timeline_order(facts_owner: FlagSite, bundle: ContentBundle) -> int | None:
    """The timeline position of the content unit a site belongs to.

    A dialogue tree has no position of its own; it inherits the position of the quest it is bound
    to, which is exactly how a tree gets sequenced in play."""
    quest_id = facts_owner.quest_id or (
        facts_owner.owner_id if facts_owner.owner_kind == "quest" else None
    )
    if quest_id is None:
        return None
    quest = bundle.quests.get(quest_id)
    return None if quest is None else quest.timeline_order


class WorldFlagConsistencyRule:
    code = "WORLD_FLAG_CONSISTENCY"
    severity = Severity.WARNING
    category = Category.LOGIC

    def check(self, ctx: AuditContext) -> Iterable[Issue]:
        xref = build_flag_xref(ctx.bundle)
        for facts in xref.facts.values():
            if facts.scope is not FlagScope.DECLARED:
                continue
            yield from self._declaration_conflict(facts)
            yield from self._never_written(facts)
            yield from self._never_read(facts, xref)
            yield from self._referenced_too_early(facts, ctx.bundle)

    # --- 子判定 ---------------------------------------------------------------------------------

    def _declaration_conflict(self, facts: FlagFacts) -> Iterable[Issue]:
        if not facts.declaration_conflict():
            return
        shown = "；".join(
            f"任务《{decl.quest_id}》里是{_declaration_label(decl.var_type.value, decl.default)}"
            for decl in facts.declarations
        )
        yield self._issue(
            facts.flag,
            "FLAG_DECL_CONFLICT",
            f"变量 '{facts.flag}' 在多个任务里被声明成了不一样的东西——{shown}。同一个变量有两套"
            "定义，求值时只会按加载顺序采信其中一个，另一个静默失效。",
            action=(
                f"统一 '{facts.flag}' 的定义：把这几个任务里它的类型与初始值改成一致；如果它们"
                "本来就是两件不同的事，把其中一个改个名字。"
            ),
            evidence=[
                Evidence(
                    kind="quest",
                    target_ref=f"quest:{decl.quest_id}",
                    path=decl.field_path,
                    data={"type": decl.var_type.value, "default": decl.default},
                )
                for decl in facts.declarations
            ],
        )

    def _never_written(self, facts: FlagFacts) -> Iterable[Issue]:
        # provably_never_written() is the same notion, computed世界级; a flag nobody reads either is
        # merely unused, not a broken gate, so a reader is required before this is worth reporting.
        if facts.writers or not facts.readers:
            return
        first = facts.readers[0]
        yield self._issue(
            facts.flag,
            "FLAG_NEVER_WRITTEN",
            f"变量 '{facts.flag}' 被{_where(first)}当门槛读，但全世界没有任何一处会写它——"
            "这个门槛永远过不去，它后面的内容玩家永远看不到。",
            action=(
                f"要么在某条分支或对白选项的后果里把 '{facts.flag}' 置成能通过的值，"
                "要么把这个永远成立不了的条件去掉。"
            ),
            evidence=[_site_evidence(site) for site in facts.readers],
        )

    def _never_read(self, facts: FlagFacts, xref: FlagXref) -> Iterable[Issue]:
        # 读取方可能因表达式解析失败而收不全；收不全时"没人读它"就是猜的，一律不报。
        if not xref.reads_complete or not facts.writers or facts.readers:
            return
        yield self._issue(
            facts.flag,
            "FLAG_NEVER_READ",
            f"变量 '{facts.flag}' 有 {len(facts.writers)} 处会写它，却没有任何条件读它——"
            "玩家在这些地方做的选择不产生任何可观测的差别。",
            action=(
                f"给 '{facts.flag}' 加一个真正读它的分支条件或对白出现条件（例如按它的高低给"
                f"不同结局）；如果 '{facts.flag}' 本来就是留给引擎侧读的，可以忽略这条。"
            ),
            evidence=[_site_evidence(site) for site in facts.writers],
        )

    def _referenced_too_early(self, facts: FlagFacts, bundle: ContentBundle) -> Iterable[Issue]:
        if not facts.writers or not facts.readers:
            return
        writer_orders = [_timeline_order(site, bundle) for site in facts.writers]
        if any(order is None for order in writer_orders):
            return  # 有一个写入方没排序号就无法比较，保守跳过
        earliest_write = min(order for order in writer_orders if order is not None)
        for reader in facts.readers:
            order = _timeline_order(reader, bundle)
            if order is None or order >= earliest_write:
                continue
            yield self._issue(
                facts.flag,
                "PHASE_REFERENCE_TOO_EARLY",
                f"{_where(reader)}读了变量 '{facts.flag}'，但全世界最早写它的内容排在更靠后的"
                f"位置（第 {earliest_write} 位，而这里是第 {order} 位）——玩家走到这里的时候，"
                "它必然还是初始值。",
                action=(
                    f"把写 '{facts.flag}' 的那段内容排到这里之前，或者把这里的条件改成不依赖"
                    "还没发生的事。"
                ),
                evidence=[_site_evidence(reader)]
                + [_site_evidence(site) for site in facts.writers],
            )

    # --- 构造 -----------------------------------------------------------------------------------

    def _issue(
        self,
        flag: str,
        subcode: str,
        message: str,
        *,
        action: str,
        evidence: list[Evidence],
    ) -> Issue:
        # target_ref 是合成的：这条问题的主体是一个变量，不是某个对象文件里的一行。定位靠证据里
        # 真实存在的对象引用（source_location 对 dialogue_key / quest_prerequisites 也是这么做的）。
        return Issue(
            rule_code=self.code,
            severity=self.severity,
            category=self.category,
            target_ref=f"flag:{flag}",
            message=f"[{subcode}] {message}",
            evidence=evidence,
            suggested_action=action,
        )
