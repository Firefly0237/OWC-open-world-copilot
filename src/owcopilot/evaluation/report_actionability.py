# 报告可执行率:让一个不知情的修复者拿着审计报告去改内容,看它能不能改对。
# 关键:① 修复者只拿到"这一条问题的报告文本 + 内容文件读写",没有审计工具、没有代码库、
#   没有任何设计说明,所以无法对号入座;② 判分不靠人看,靠**重跑同一套审计**——这条的指纹
#   消失、且没冒出新指纹,才算修好;③ 世界与报告文本都从生产代码取(种子缺陷世界 + 生产的
#   报告渲染器),所以报告一改版,这里量到的就是改版后的报告,不需要同步维护第二份。
"""Report actionability: can a naive fixer act on the audit report alone?

The audit can be complete and still useless: if a finding says only "this object is wrong" in
English with an internal id, a designer cannot locate or fix it. The industry failure mode is
"the tool finds things nobody can act on". This module measures that directly, with a protocol
that cannot be gamed by wishful reading:

1. Build seeded-defect worlds (reused from the agent task bench — same worlds, same audit) and
   write each to disk as ordinary content files.
2. Run the shipping audit. Every OPEN finding becomes one trial.
3. For each finding, spawn a FRESH fixer agent whose whole world is: the finding's own report
   text (rendered by the shipping report renderer, whatever it currently says) plus read/write
   access to the content files. No audit tool, no source, no design notes.
4. Let it edit, then re-run the same audit and judge mechanically: the finding's fingerprint is
   gone, no fingerprint appeared that was not there before, AND the content survived — see
   :func:`check_preservation`. Without that last clause the measurement has a hole a fixer walks
   straight through: deleting the offending object makes its fingerprint disappear and usually
   introduces nothing, so vandalism scores as a fix.

**Actionability rate = fixed / total.** The point of the number is the before/after delta across
a report change, so the measurement is deliberately reusable: nothing here knows which fields the
report carries, so the same run re-measures a changed report without edits.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from ..audit.baseline import issue_fingerprint
from ..audit.context import AuditContext
from ..audit.default_rules import build_default_rule_registry
from ..audit.models import Issue, IssueStatus
from ..audit.report import render_issue_markdown
from ..audit.runner import AuditRunner
from ..content.hash import model_hash
from ..content.models import ContentBundle
from ..content.store import ContentStore
from .agent_task_bench import build_task_worlds

#: Hard spend stop for a whole measurement run (USD, estimated from the telemetry tier prices).
#: One trial is a handful of small tool-calling turns, so the default leaves generous headroom
#: over the ~35-finding corpus.
DEFAULT_ACTIONABILITY_BUDGET_USD = 1.0

#: Model-call budget for one fixer. A fix is "look at the files, read the one that matters, write
#: it back", so a small ceiling is enough — and it keeps a confused fixer from burning the budget.
DEFAULT_FIXER_MAX_STEPS = 12

#: The fixer's brief. Deliberately says nothing about the audit's rules, the defect families or
#: what a "good" fix looks like: everything it knows about the problem must come from the report
#: text itself, otherwise the measurement would be scoring our prompt instead of our report.
#: Frozen text — the before/after comparison is only meaningful if this is byte-identical in both
#: runs.
FIXER_SYSTEM_PROMPT = """\
你是一名游戏叙事策划。内容质检报告里有**一条**待处理的问题,请你直接改内容文件把它修掉。

你手上只有内容文件:
- `list_content_files` 列出所有内容文件;
- `read_content_file` 读一个文件的全文;
- `write_content_file` 整份覆盖写回一个文件(所以要写完整内容,不能只写片段;路径不存在就是新建)。

你没有质检工具,也看不到质检规则是怎么写的,只能靠报告和文件内容判断。

要求:
- 只改与这条问题相关的地方,不要顺手改别的,也不要为了让文件"更整齐"而重排无关内容;
- 内容文件是 JSON,写回去必须仍是合法 JSON;
- 改完用一两句话说明你改了什么、改在哪个文件。"""

#: The ``--no-file-listing`` variant's brief: same task, same rules, one capability removed.
#: With the whole file tree visible, a capable fixer finds the right file from an object id by
#: scanning names, and the report's "which file" element buys nothing — the headline rate saturates
#: and stops being able to move. Taking the listing away makes locating depend on the report and
#: nothing else, which is precisely the element under change. Frozen text, same as above.
FIXER_SYSTEM_PROMPT_NO_LISTING = """\
你是一名游戏叙事策划。内容质检报告里有**一条**待处理的问题,请你直接改内容文件把它修掉。

你手上只有内容文件:
- `read_content_file` 读一个文件的全文(要自己给出路径,没有目录清单可翻);
- `write_content_file` 整份覆盖写回一个文件(所以要写完整内容,不能只写片段;路径不存在就是新建)。

你没有质检工具,也看不到质检规则是怎么写的,只能靠报告和文件内容判断。

要求:
- 只改与这条问题相关的地方,不要顺手改别的,也不要为了让文件"更整齐"而重排无关内容;
- 内容文件是 JSON,写回去必须仍是合法 JSON;
- 改完用一两句话说明你改了什么、改在哪个文件。"""


# ---------------------------------------------------------------------------
# 内容保全:这条问题是被修掉的,还是被删掉的?
# ---------------------------------------------------------------------------

#: 正文单元"被掏空"的判据:剩下不到原来的三成才算。合法改写会换词、会缩句,但极少把一句话砍到
#: 三成以下;砍到三成以下的,拿到手的已经不是原来那段内容了。语料里正文单元中位数 22 字,三成
#: 意味着 20 字的台词只剩 6 字 —— 这个门槛不会把"改写"误判成"删除"。
TEXT_GUT_RATIO = 0.30

#: 短到这个字数以下的正文单元不参与"掏空"判定:名字、称谓这类本来就只有几个字,改一个字就能
#: 越过任何比例门槛,拿比例判它只会制造噪音。它们真被删掉时由"对象消失"那一条兜住。
TEXT_UNIT_MIN_CHARS = 10

#: 全世界正文总量的地板。一条问题只该动一处,单条修复把整个世界的正文改少 5% 以上,说明删掉的
#: 东西远超这条问题的范围。语料里最长的一段正文只占全世界的 1.7%,所以合法修复碰不到这条线。
TOTAL_TEXT_FLOOR = 0.95

#: 允许"删掉报告指名的那个对象"的规则,以及为什么允许。删除并非一律非法 —— 这三条规则说的
#: 就是"这条关系本身多余/无效/自相矛盾",删掉它正是规则要求的修法,删完事实与正文都还在
#: (关系是一句断言,不是内容载体)。表以外的规则一律不许删对象:那些问题说的是"这个对象的某个
#: 字段不对",删掉对象是毁内容不是修内容。
OBJECT_DELETION_SANCTIONED_RULES: dict[str, str] = {
    "DUPLICATE_RELATION": "同一条关系重复了两遍,删掉副本正是这条规则要求的修法,事实本身还在",
    "MISSING_RELATION_ENDPOINT": "这条关系指向一个不存在的对象,它没有断言任何有效事实",
    "RELATION_CONFLICT": "两条关系互相矛盾,删掉其中一条是这条规则要求的取舍",
}

#: 允许"删掉报告指名的那个对话节点"的规则。豁免只落在报告点名的那个节点上 —— 同一条问题下
#: 把整棵对话树删掉,仍然按毁内容算(所以这张表不能和上面那张合并)。
NODE_DELETION_SANCTIONED_RULES: dict[str, str] = {
    "DIALOGUE_TREE_UNREACHABLE_NODE": "不可达节点玩家永远走不到,删掉它是规则给的两条修法之一",
}

#: 允许"把目标对象自己的正文改短"的规则,以及为什么允许。这两条规则要求的修法本身就是删字:
#: 不删就修不掉,拿"正文变少"判它们失败等于要求一件做不到的事。
SHORTENING_SANCTIONED_RULES: dict[str, str] = {
    "TEXT_TOO_LONG_FOR_UI": "这条规则要求的就是把这段文本改到 UI 长度以内",
    "PROMPT_INJECTION": "这条规则要求的就是把混进正文里的指令删掉",
}


class ContentCensus(BaseModel):
    """一份内容的体量清点:每个对象一份指纹、每个对话节点一份指纹、每段正文一个字数。

    A census of what a world contains, taken before and after a fixer touches it. Object identity
    is the audit's own ``target_ref`` spelling (``quest:q1``, ``relation:a:knows:b``), so a
    finding's target can be looked up here directly.
    """

    #: ``target_ref`` → 该 ref 下每个对象的 payload 指纹(关系可以重复,所以是列表)。
    objects: dict[str, list[str]] = Field(default_factory=dict)
    #: ``dialogue_tree:<tree>/<node>`` → 节点 payload 指纹。
    nodes: dict[str, str] = Field(default_factory=dict)
    #: ``<对象或节点 ref>#<字段>`` → 这段正文的字数。
    text_units: dict[str, int] = Field(default_factory=dict)

    @property
    def text_chars(self) -> int:
        return sum(self.text_units.values())


def take_census(bundle: ContentBundle) -> ContentCensus:
    """Count everything a fixer could destroy: objects, dialogue nodes, authored text.

    正文只数"人写的、玩家或策划会读到的那些字":描述、目标、台词、选项、本地化文本、词条释义。
    id、标签、路径这些机器字段不算正文 —— 它们变了是改结构,不是毁内容。
    """
    census = ContentCensus()

    def add_object(ref: str, model: BaseModel) -> None:
        census.objects.setdefault(ref, []).append(model_hash(model))

    def add_text(owner: str, field: str, value: str | None) -> None:
        if value and value.strip():
            census.text_units[f"{owner}#{field}"] = len(value)

    for entity in bundle.entities.values():
        ref = f"entity:{entity.id}"
        add_object(ref, entity)
        add_text(ref, "name", entity.name)
        add_text(ref, "description", entity.description)
    for relation in bundle.relations:
        add_object(f"relation:{relation.source}:{relation.kind}:{relation.target}", relation)
    for event_ref in bundle.quest_event_refs.values():
        ref = f"quest_event_ref:{event_ref.id}"
        add_object(ref, event_ref)
        add_text(ref, "note", event_ref.note)
    for quest in bundle.quests.values():
        ref = f"quest:{quest.id}"
        add_object(ref, quest)
        add_text(ref, "title", quest.title)
        add_text(ref, "objective", quest.objective)
        for stage in quest.stages:
            add_text(ref, f"stage.{stage.id}", stage.summary)
    for region in bundle.regions.values():
        ref = f"region:{region.id}"
        add_object(ref, region)
        add_text(ref, "name", region.name)
    for poi in bundle.pois.values():
        ref = f"poi:{poi.id}"
        add_object(ref, poi)
        add_text(ref, "name", poi.name)
        add_text(ref, "purpose", poi.purpose)
    for dialogue in bundle.dialogues.values():
        ref = f"dialogue:{dialogue.id}"
        add_object(ref, dialogue)
        add_text(ref, "text", dialogue.text)
    for tree in bundle.dialogue_trees.values():
        ref = f"dialogue_tree:{tree.id}"
        add_object(ref, tree)
        add_text(ref, "title", tree.title)
        for node in tree.nodes.values():
            node_ref = f"{ref}/{node.id}"
            census.nodes[node_ref] = model_hash(node)
            add_text(node_ref, "text", node.text)
            for index, choice in enumerate(node.choices):
                add_text(node_ref, f"choice{index}", choice.text)
    for text in bundle.localized_texts.values():
        ref = f"localized_text:{text.id}"
        add_object(ref, text)
        add_text(ref, "text", text.text)
    for term in bundle.terms.values():
        ref = f"term:{term.id}"
        add_object(ref, term)
        add_text(ref, "canonical", term.canonical)
        add_text(ref, "description", term.description)
    for style in bundle.style_guides.values():
        ref = f"style_guide:{style.id}"
        add_object(ref, style)
        add_text(ref, "body", style.body)
    return census


class ContentEdit(BaseModel):
    """修复者这一次实际改了什么 —— 对象级差异摘要,不存全文。

    存下来是为了"到底是修的还是删的"以后能直接查、不用重跑:每条 trial 的 JSON 里都带着它。
    """

    removed_objects: list[str] = Field(default_factory=list)
    added_objects: list[str] = Field(default_factory=list)
    changed_objects: list[str] = Field(default_factory=list)
    removed_nodes: list[str] = Field(default_factory=list)
    added_nodes: list[str] = Field(default_factory=list)
    changed_nodes: list[str] = Field(default_factory=list)
    #: 正文单元 ref → ``[改前字数, 改后字数]``,只收掉到 :data:`TEXT_GUT_RATIO` 以下的。
    #: 属于已被删掉的对象/节点的正文不重复记在这里 —— 那笔账记在对象与节点上。
    gutted_texts: dict[str, list[int]] = Field(default_factory=dict)
    text_chars_before: int = 0
    text_chars_after: int = 0

    @property
    def touched_nothing(self) -> bool:
        return not (
            self.removed_objects
            or self.added_objects
            or self.changed_objects
            or self.removed_nodes
            or self.added_nodes
            or self.changed_nodes
        )


def diff_census(before: ContentCensus, after: ContentCensus) -> ContentEdit:
    """What changed between two censuses, as an object-level summary."""
    edit = ContentEdit(
        text_chars_before=before.text_chars,
        text_chars_after=after.text_chars,
    )
    for ref in sorted(set(before.objects) | set(after.objects)):
        old = before.objects.get(ref, [])
        new = after.objects.get(ref, [])
        edit.removed_objects.extend([ref] * max(0, len(old) - len(new)))
        edit.added_objects.extend([ref] * max(0, len(new) - len(old)))
        if len(old) == len(new) and sorted(old) != sorted(new):
            edit.changed_objects.append(ref)
    for ref in sorted(set(before.nodes) | set(after.nodes)):
        old_hash = before.nodes.get(ref)
        new_hash = after.nodes.get(ref)
        if old_hash is None:
            edit.added_nodes.append(ref)
        elif new_hash is None:
            edit.removed_nodes.append(ref)
        elif old_hash != new_hash:
            edit.changed_nodes.append(ref)
    gone = set(edit.removed_objects) | set(edit.removed_nodes)
    for ref, size in sorted(before.text_units.items()):
        if _text_owner(ref) in gone or _object_of(ref) in gone:
            continue  # 对象/节点整个没了,这笔账已经记过,不重复计
        left = after.text_units.get(ref, 0)
        if size >= TEXT_UNIT_MIN_CHARS and left < size * TEXT_GUT_RATIO:
            edit.gutted_texts[ref] = [size, left]
    return edit


def _text_owner(unit_ref: str) -> str:
    """``quest:q1#objective`` → ``quest:q1``;``dialogue_tree:t/n#text`` → ``dialogue_tree:t/n``."""
    return unit_ref.split("#", 1)[0]


def _object_of(ref: str) -> str:
    """节点 ref 或正文单元 ref 所属的对象 ref。"""
    return _text_owner(ref).split("/", 1)[0]


def check_preservation(edit: ContentEdit, *, issue: Issue) -> list[str]:
    """内容保全判据:返回"哪里被毁了"的中文清单,空清单 = 内容保住了。

    Four criteria, applied identically to every report generation being compared:

    1. 这条问题指向的对象不许消失(为了消掉一条问题把整个任务删了,不算修好);
    2. 任何对象都不许消失(删别人的对象比删自己的更糟);
    3. 对话节点不许消失(节点是正文的载体,删节点就是删台词);
    4. 正文不许塌缩:单段正文不许被掏空到三成以下,全世界正文总量不许掉 5% 以上。

    每条都有例外,例外只给"规则本身要求的那个动作、且只在**这条问题自己指名的**元素上":
    :data:`OBJECT_DELETION_SANCTIONED_RULES` 允许删掉这条问题点名的关系,至多一条;
    :data:`NODE_DELETION_SANCTIONED_RULES` 允许删掉报告指名的那个对话节点(整棵树仍然不许删);
    :data:`SHORTENING_SANCTIONED_RULES` 允许把目标对象自己的正文改短。
    """
    damage: list[str] = []

    # 豁免至多兑一次:矛盾的两条关系里删一条是修,两条都删掉这个事实就没了。
    deletable = _sanctioned_object_deletions(issue)
    budget = 1 if deletable else 0
    for ref in edit.removed_objects:
        if budget and ref in deletable:
            budget -= 1
            continue
        if ref == issue.target_ref:
            damage.append(f"把这条问题指向的对象 {ref} 整个删了")
        else:
            damage.append(f"删掉了对象 {ref}")

    sanctioned_node = (
        f"{issue.target_ref}/{issue.node_ref}"
        if issue.node_ref and issue.rule_code in NODE_DELETION_SANCTIONED_RULES
        else ""
    )
    for ref in edit.removed_nodes:
        if ref == sanctioned_node:
            continue
        damage.append(f"删掉了对话节点 {ref}")

    shortening_ok = issue.rule_code in SHORTENING_SANCTIONED_RULES
    for ref, (size, left) in sorted(edit.gutted_texts.items()):
        if shortening_ok and _object_of(ref) == issue.target_ref:
            continue
        damage.append(f"正文 {ref} 从 {size} 字掏到 {left} 字")

    floor = edit.text_chars_before * TOTAL_TEXT_FLOOR
    if edit.text_chars_before and edit.text_chars_after < floor:
        lost = 1 - edit.text_chars_after / edit.text_chars_before
        damage.append(
            f"全世界正文从 {edit.text_chars_before} 字掉到 {edit.text_chars_after} 字"
            f"(少了 {lost:.1%},门槛 {1 - TOTAL_TEXT_FLOOR:.0%})"
        )
    return damage


def _sanctioned_object_deletions(issue: Issue) -> set[str]:
    """这条问题**自己指名**的、可以删掉的关系 ref。

    不能只认 ``target_ref``:``RELATION_CONFLICT`` 说的是"结盟和敌对这两条只能留一条,具体删
    哪条要策划确认",两条关系都写在这条问题的证据里,而 ``target_ref`` 是一个合成出来的
    ``relation:<a>:conflict:<b>``,根本不是磁盘上的任何一条关系。只认 target_ref 会把"照着
    建议动作删掉其中一条"判成毁内容 —— 那是在罚修复者照做,不是在护内容。所以可删集合 =
    target_ref + 证据里点名的关系,额度仍然只有一个。
    """
    if issue.rule_code not in OBJECT_DELETION_SANCTIONED_RULES:
        return set()
    refs = {issue.target_ref}
    for evidence in issue.evidence:
        if evidence.relation:
            source, kind, target = evidence.relation
            refs.add(f"relation:{source}:{kind}:{target}")
    return refs


class ActionabilityTrial(BaseModel):
    """One finding put in front of one fixer, and what came of it."""

    world_id: str
    rule_code: str
    target_ref: str
    fingerprint: str
    #: Exactly what the fixer was shown about this finding — the shipping report's own text.
    report_text: str
    #: This finding's fingerprint is no longer open after the fix.
    target_gone: bool
    #: Fingerprints that were not open before but are open now, as ``rule_code @ target_ref``.
    introduced: list[str] = Field(default_factory=list)
    #: 内容保全过了没有(:func:`check_preservation`)。False = 问题是删出来的,不是修出来的。
    preserved: bool = True
    #: 保全没过时,具体毁在哪儿。
    damage: list[str] = Field(default_factory=list)
    #: 修复者这一次实际改了什么(对象级差异摘要)——留档,便于事后复核"修的还是删的"。
    edit: ContentEdit = Field(default_factory=lambda: ContentEdit())
    #: 旧判据:``target_gone and not introduced``。留着是为了报出"加保全检查前"的那个数。
    fingerprint_ok: bool = False
    #: ``fingerprint_ok and preserved``。The headline metric counts this.
    fixed: bool
    #: Short Chinese verdict for the per-finding table in the write-up.
    verdict: str
    #: Content files the fixer actually wrote (relative paths), in write order.
    files_written: list[str] = Field(default_factory=list)
    #: Paths the fixer guessed wrong before finding the object (``--no-file-listing`` only).
    missed_reads: list[str] = Field(default_factory=list)
    fixer_reply: str = ""
    llm_calls: int = 0
    cost_usd: float = 0.0
    error: str = ""


class ActionabilityReport(BaseModel):
    """The measurement outcome — one run over the whole finding corpus."""

    #: Free-form marker for which report generation was measured ("改造前基线" / "改造后").
    label: str
    llm_mode: str
    model: str
    #: Whether the fixer could browse the file tree. False = the stricter variant in which finding
    #: the right file depends on the report alone.
    file_listing: bool = True
    total_findings: int
    #: 通过全部判据(指纹消失 + 不引入新问题 + 内容保住了)的条数。
    fixed_count: int
    #: fixed / total, rounded. THE number.
    actionable_rate: float
    #: 只按旧判据(指纹消失且不引入新问题)算的条数 —— 加保全检查**之前**的那个数,同一批 trial
    #: 一次跑出来,不需要跑第二遍就能给出"加检查前 / 加检查后"的对照。
    fingerprint_fixed_count: int = 0
    fingerprint_rate: float = 0.0
    #: 旧判据算通过、保全判据不通过的条数 —— 也就是漏判被堵掉的**翻转条数**。
    damaged_count: int = 0
    #: How many findings disappeared but took collateral damage with them — the gap between this
    #: and ``fixed_count`` is how often a fixer half-understood the report.
    target_gone_count: int
    total_cost_usd: float
    budget_stopped: bool = False
    trials: list[ActionabilityTrial] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# The corpus: seeded defect worlds and their open findings
# ---------------------------------------------------------------------------


def build_actionability_worlds() -> list[tuple[str, ContentBundle]]:
    """The measured worlds: every seeded-defect world from the agent task bench that has findings.

    Reused rather than re-seeded so the corpus is the one already pinned by that bench (its clean
    base audits to zero, so every finding here is attributable to a seeded defect). The clean
    negative-control world is dropped — it has nothing to act on.
    """
    return [(task.task_id, bundle) for task, bundle in build_task_worlds() if task.task_id != "T00"]


def open_issues_of(bundle: ContentBundle, root: Path | None = None) -> list[Issue]:
    """The shipping audit's OPEN findings for *bundle*, fingerprinted.

    *root* is the on-disk content root the bundle was loaded from. Passing it is what the shipping
    audit pipeline does (``pipeline.audit.run_full_audit``), and it is what makes the findings carry
    file/line/node — so the report text below is the one the product actually emits. Judging is
    unaffected either way: a fingerprint is computed from rule code, target and evidence only.
    """
    runner = AuditRunner(build_default_rule_registry())
    result = runner.run(AuditContext.from_bundle(bundle, content_root=root))
    return [issue for issue in result.issues if issue.status is IssueStatus.OPEN]


def finding_report_text(issue: Issue) -> str:
    """What the report tells a reader about this one finding — verbatim from the renderer."""
    return "\n".join(render_issue_markdown(issue))


# ---------------------------------------------------------------------------
# The fixer: content files in, content files out, nothing else
# ---------------------------------------------------------------------------

#: Directories a content root keeps objects in. Used only to enumerate what the fixer may see.
_CONTENT_SUFFIXES = (".json", ".jsonl")


class _ContentFileTools:
    """Read/write access to ONE content root, and the record of what got written.

    A class rather than three closures because the write log is shared state the caller reads
    back afterwards (which files the fixer touched is part of the trial record, and an untouched
    world is a distinguishable failure mode from a wrongly-edited one).
    """

    def __init__(self, root: Path, *, file_listing: bool = True) -> None:
        self.root = root.resolve()
        self.file_listing = file_listing
        self.written: list[str] = []
        #: Paths the fixer tried to read that do not exist — how many wrong guesses the report
        #: cost it before it found the object. Meaningless with the listing tool available.
        self.missed_reads: list[str] = []

    def _resolve(self, path: str) -> Path | None:
        """The absolute path inside the content root, or None when *path* escapes it.

        Escaping returns None rather than raising: a wrong guess must come back as an observation
        the fixer can react to, exactly like "no such file". A raise would abort the whole trial
        and score a bad guess as a *report* failure, which is a different thing.
        """
        candidate = (self.root / path).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            return None
        return candidate

    def list_files(self) -> str:
        names = sorted(
            str(p.relative_to(self.root)).replace("\\", "/")
            for p in self.root.rglob("*")
            if p.is_file() and p.suffix in _CONTENT_SUFFIXES
        )
        return "\n".join(names)

    def read_file(self, path: str) -> str:
        target = self._resolve(path)
        if target is None or not target.is_file():
            self.missed_reads.append(path)
            return f"没有这个文件:{path}。只能读写内容目录里的文件,路径要相对内容目录。"
        return target.read_text(encoding="utf-8")

    def write_file(self, path: str, content: str) -> str:
        target = self._resolve(path)
        if target is None:
            self.missed_reads.append(path)
            return f"写不了 {path}:只能读写内容目录里的文件,路径要相对内容目录。"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        relative = str(target.relative_to(self.root)).replace("\\", "/")
        self.written.append(relative)
        return f"已写入 {relative}({len(content)} 字符)。"

    def as_tools(self) -> list[StructuredTool]:
        listing = [
            StructuredTool(
                name="list_content_files",
                description="列出内容目录下所有内容文件的相对路径。",
                args_schema={"type": "object", "properties": {}},
                func=lambda: self.list_files(),
            )
        ]
        return [
            *(listing if self.file_listing else []),
            StructuredTool(
                name="read_content_file",
                description="读取一个内容文件的全文。",
                args_schema={
                    "type": "object",
                    "properties": {"path": {"type": "string", "description": "相对路径。"}},
                    "required": ["path"],
                },
                func=lambda path: self.read_file(path),
            ),
            StructuredTool(
                name="write_content_file",
                description="整份覆盖写回一个内容文件(必须写完整内容)。",
                args_schema={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "相对路径。"},
                        "content": {"type": "string", "description": "文件的完整新内容。"},
                    },
                    "required": ["path", "content"],
                },
                func=lambda path, content: self.write_file(path, content),
            ),
        ]


def _run_fixer(
    *,
    chat_model: Any,
    tools: _ContentFileTools,
    report_text: str,
    max_steps: int,
    callbacks: list[Any],
) -> str:
    """Drive one fixer agent to completion and return its closing message."""
    from langchain.agents import create_agent  # noqa: PLC0415 — 与 agent 宿主一致的延迟导入
    from langchain.agents.middleware import ModelCallLimitMiddleware  # noqa: PLC0415

    agent = create_agent(
        model=chat_model,
        tools=tools.as_tools(),
        system_prompt=(
            FIXER_SYSTEM_PROMPT if tools.file_listing else FIXER_SYSTEM_PROMPT_NO_LISTING
        ),
        middleware=[ModelCallLimitMiddleware(run_limit=max_steps, exit_behavior="end")],
    )
    state = agent.invoke(
        {"messages": [{"role": "user", "content": f"质检报告里的这条问题:\n\n{report_text}"}]},
        # The model-call ceiling must be the ONLY thing that stops a fixer: a graph-recursion abort
        # loses the whole trial (it raises instead of returning a state), which would score a
        # chatty fixer as a report failure. One ReAct turn is several graph nodes, so the
        # recursion budget is set well above ``max_steps`` turns' worth and the middleware binds.
        config={"callbacks": callbacks, "recursion_limit": max_steps * 6 + 10},
    )
    messages = state.get("messages", [])
    for message in reversed(messages):
        text = getattr(message, "content", "")
        if isinstance(text, str) and text.strip():
            return text.strip()
    return ""


# ---------------------------------------------------------------------------
# Judging: re-run the same audit, compare fingerprints, then check the content survived
# ---------------------------------------------------------------------------


class FixJudgement(BaseModel):
    """一条 trial 的判定结果:指纹怎么变的、内容有没有被毁、以及改了什么。"""

    target_gone: bool
    introduced: list[str] = Field(default_factory=list)
    preserved: bool = True
    damage: list[str] = Field(default_factory=list)
    edit: ContentEdit = Field(default_factory=ContentEdit)
    verdict: str = ""

    @property
    def fingerprint_ok(self) -> bool:
        """旧判据:这条指纹消失且没引入新指纹。保留是为了报出"加保全检查前"的那个数。"""
        return self.target_gone and not self.introduced

    @property
    def fixed(self) -> bool:
        return self.fingerprint_ok and self.preserved


def judge_fix(
    root: Path, *, issue: Issue, fingerprint: str, before: dict[str, str], census: ContentCensus
) -> FixJudgement:
    """Re-audit the edited world and judge it on fingerprints AND content preservation.

    *before* maps each open fingerprint from the pre-fix audit to a ``rule_code @ target_ref``
    label. A fingerprint covers the rule, the target and the evidence, so "the same finding" is
    decided by the audit engine itself rather than by string matching on messages. *census* is the
    same world's pre-fix census, which is what makes "the content survived" checkable.
    """
    try:
        bundle = ContentStore(root).load()
    except Exception as exc:  # 修复者把文件写坏了,本身就是一种"没修好"
        return FixJudgement(
            target_gone=False,
            preserved=False,
            damage=[f"内容文件写坏了({type(exc).__name__})"],
            verdict=f"内容文件写坏了,加载失败({type(exc).__name__})",
        )
    after = {issue_fingerprint(other): other for other in open_issues_of(bundle, root)}
    target_gone = fingerprint not in after
    introduced = [
        f"{other.rule_code} @ {other.target_ref}"
        for fp, other in sorted(after.items())
        if fp not in before
    ]
    edit = diff_census(census, take_census(bundle))
    damage = check_preservation(edit, issue=issue)
    judgement = FixJudgement(
        target_gone=target_gone,
        introduced=introduced,
        preserved=not damage,
        damage=damage,
        edit=edit,
    )
    judgement.verdict = _verdict_of(judgement)
    return judgement


def _verdict_of(judgement: FixJudgement) -> str:
    """The one-line Chinese reason shown in the per-finding table."""
    if judgement.fixed:
        return "修好了"
    tail = []
    if judgement.introduced:
        tail.append("引入了新问题:" + "; ".join(judgement.introduced))
    if judgement.damage:
        tail.append("内容被毁:" + "; ".join(judgement.damage))
    if judgement.target_gone:
        return "这条消失了,但" + ",而且".join(tail)
    if not tail:
        return "没修掉(问题原样还在)"
    return "没修掉,还" + ",而且".join(tail)


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


def _trial_plan(
    workspace: Path, world_ids: set[str] | None
) -> Iterator[tuple[str, Path, Issue, dict[str, str], ContentCensus]]:
    """Yield ``(world_id, pristine_root, issue, before_fingerprints, census)`` per measured finding.

    The world is written to disk ONCE per world and the findings are taken from reading it BACK,
    never from the in-memory bundle. That is not a detail: at least one rule's output depends on
    the bundle's insertion order (the placeholder rule takes the first locale it sees as the
    expected one), and the seeded in-memory order differs from the on-disk load order. Comparing
    an in-memory "before" against an on-disk "after" would then invent a phantom regression on a
    finding nobody touched. Both sides of the comparison must come from the same round trip.
    """
    for world_id, bundle in build_actionability_worlds():
        if world_ids is not None and world_id not in world_ids:
            continue
        pristine = workspace / f"{world_id}_pristine"
        if pristine.exists():
            shutil.rmtree(pristine)
        ContentStore(pristine).save(bundle)
        loaded = ContentStore(pristine).load()
        issues = open_issues_of(loaded, pristine)
        before = {
            issue_fingerprint(issue): f"{issue.rule_code} @ {issue.target_ref}" for issue in issues
        }
        # 同一次磁盘往返的清点:保全判据比的必须是"修复者拿到的那份世界",不是内存里的种子。
        census = take_census(loaded)
        for issue in issues:
            yield world_id, pristine, issue, before, census


def run_report_actionability(
    workspace: str | Path,
    *,
    chat_model_factory: Callable[[], Any],
    llm_mode: str = "offline",
    model: str = "offline-double",
    label: str = "未标注",
    max_steps: int = DEFAULT_FIXER_MAX_STEPS,
    budget_usd: float = DEFAULT_ACTIONABILITY_BUDGET_USD,
    world_ids: set[str] | None = None,
    file_listing: bool = True,
    report_texts: Mapping[tuple[str, str, str], str] | None = None,
    on_trial: Callable[[ActionabilityTrial], None] | None = None,
) -> ActionabilityReport:
    """Measure the actionability rate of the audit report as it currently reads.

    Each finding gets its own pristine copy of its world, so trials never see each other's edits
    and a fixer that mangles one world cannot contaminate the next finding's verdict.

    *file_listing* is the ablation switch (see :data:`FIXER_SYSTEM_PROMPT_NO_LISTING`): with the
    file tree browsable the fixer locates objects by scanning names and the rate saturates; with
    it off, locating depends on the report alone.

    *report_texts* replays a PREVIOUS generation's report text (see :func:`load_report_texts`),
    keyed by ``(world_id, rule_code, target_ref)``. That is what lets a judging change — such as
    the content-preservation clause — be re-measured against an older report without keeping a
    copy of that older report renderer around. A key that is missing raises rather than silently
    falling back to today's text: half-replayed is not a report generation, it is a mixture.
    """
    from ..llm.telemetry import TelemetryCollector  # noqa: PLC0415 — 与 bench 一致的延迟导入
    from ..llm.telemetry_callback import TelemetryCallbackHandler  # noqa: PLC0415

    root = Path(workspace)
    root.mkdir(parents=True, exist_ok=True)
    trials: list[ActionabilityTrial] = []
    total_cost = 0.0
    budget_stopped = False

    for index, (world_id, pristine, issue, before, census) in enumerate(
        _trial_plan(root, world_ids)
    ):
        if total_cost >= budget_usd:
            budget_stopped = True
            break
        fingerprint = issue_fingerprint(issue)
        trial_root = root / f"{world_id}_{index:02d}"
        if trial_root.exists():
            shutil.rmtree(trial_root)
        shutil.copytree(pristine, trial_root)

        tools = _ContentFileTools(trial_root, file_listing=file_listing)
        report_text = _text_for(issue, world_id=world_id, replay=report_texts)
        telemetry = TelemetryCollector()
        # The fixer is a cheap-tier agent, same as the ReAct host — so the cost figure is priced
        # off the same tier table and is comparable with the other benches.
        handler = TelemetryCallbackHandler(
            telemetry, tier="cheap", model=model, default_task="report_fixer"
        )
        error = ""
        reply = ""
        try:
            reply = _run_fixer(
                chat_model=chat_model_factory(),
                tools=tools,
                report_text=report_text,
                max_steps=max_steps,
                callbacks=[handler],
            )
        except Exception as exc:  # 单条失败不该毁掉整轮:记成"没修好"并留下原因
            error = f"{type(exc).__name__}: {exc}"
        summary = telemetry.summary()
        total_cost += float(summary["total_cost_usd"])

        judgement = judge_fix(
            trial_root, issue=issue, fingerprint=fingerprint, before=before, census=census
        )
        verdict = judgement.verdict
        if not tools.written and not judgement.target_gone:
            verdict = "修复者一个文件都没改"
        if error:
            verdict = f"修复者运行出错:{error}"
        trial = ActionabilityTrial(
            world_id=world_id,
            rule_code=issue.rule_code,
            target_ref=issue.target_ref,
            fingerprint=fingerprint,
            report_text=report_text,
            target_gone=judgement.target_gone,
            introduced=judgement.introduced,
            preserved=judgement.preserved,
            damage=judgement.damage,
            edit=judgement.edit,
            fingerprint_ok=judgement.fingerprint_ok,
            fixed=judgement.fixed,
            verdict=verdict,
            files_written=tools.written,
            missed_reads=tools.missed_reads,
            fixer_reply=reply,
            llm_calls=int(summary["calls"]),
            cost_usd=float(summary["total_cost_usd"]),
            error=error,
        )
        trials.append(trial)
        if on_trial is not None:
            on_trial(trial)

    fixed_count = sum(1 for trial in trials if trial.fixed)
    fingerprint_fixed = sum(1 for trial in trials if trial.fingerprint_ok)
    return ActionabilityReport(
        label=label,
        llm_mode=llm_mode,
        model=model,
        file_listing=file_listing,
        total_findings=len(trials),
        fixed_count=fixed_count,
        actionable_rate=round(fixed_count / len(trials), 4) if trials else 0.0,
        fingerprint_fixed_count=fingerprint_fixed,
        fingerprint_rate=round(fingerprint_fixed / len(trials), 4) if trials else 0.0,
        damaged_count=sum(1 for trial in trials if trial.fingerprint_ok and not trial.preserved),
        target_gone_count=sum(1 for trial in trials if trial.target_gone),
        total_cost_usd=round(total_cost, 6),
        budget_stopped=budget_stopped,
        trials=trials,
    )


def _text_for(
    issue: Issue, *, world_id: str, replay: Mapping[tuple[str, str, str], str] | None
) -> str:
    """What this fixer gets shown: today's report, or a replayed older generation's text."""
    if replay is None:
        return finding_report_text(issue)
    key = (world_id, issue.rule_code, issue.target_ref)
    if key not in replay:
        raise KeyError(
            f"要重放的报告文本里没有这一条:{key}。重放的那份记录必须覆盖同一批问题,"
            "否则两代报告会被混在一次测量里。"
        )
    return replay[key]


def load_report_texts(path: str | Path) -> dict[tuple[str, str, str], str]:
    """Read a stored :class:`ActionabilityReport` JSON back as replayable report texts.

    每条 trial 都留着"当时那一代报告给修复者看的那段字",所以判据变了以后,不需要把旧的报告
    渲染器找回来,就能拿同一套新判据重测旧报告。键是 ``(world_id, rule_code, target_ref)``,
    这三个字段唯一确定语料里的一条问题(指纹会随规则改版漂,不能当键)。
    """
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    texts: dict[tuple[str, str, str], str] = {}
    for trial in payload.get("trials", []):
        key = (trial["world_id"], trial["rule_code"], trial["target_ref"])
        if key in texts:
            raise ValueError(f"这份记录里有两条同名问题,没法用作重放键:{key}")
        texts[key] = trial["report_text"]
    return texts


def render_actionability_markdown(report: ActionabilityReport) -> str:
    """The per-finding table for the write-up — every finding, fixed or not, with its reason."""
    trials = report.trials
    mean_calls = sum(trial.llm_calls for trial in trials) / len(trials) if trials else 0.0
    fixed_trials = [trial for trial in trials if trial.fixed]
    mean_calls_fixed = (
        sum(trial.llm_calls for trial in fixed_trials) / len(fixed_trials) if fixed_trials else 0.0
    )
    lines = [
        f"# 报告可执行率 · {report.label}",
        "",
        f"- 模型:`{report.model}`(模式 {report.llm_mode})",
        "- 修复者能翻目录:" + ("是(计划原定条件)" if report.file_listing else "**否**(加严对照)"),
        f"- 总条数:**{report.total_findings}**",
        f"- 修好:**{report.fixed_count}**",
        f"- **可执行率:{report.actionable_rate:.1%}**",
        f"- 加内容保全检查**之前**(只看指纹):{report.fingerprint_fixed_count} 条 = "
        f"{report.fingerprint_rate:.1%}",
        f"- **翻转(指纹算通过、内容却被毁):{report.damaged_count} 条**",
        f"- 其中「这条消失了」的:{report.target_gone_count}(差额=修掉了但引入新问题)",
        # 定位成本:同一条问题,报告说得越清楚,修复者摸到正确文件所需的来回就越少。它不参与
        # 主指标,但在主指标接近天花板时,它是"报告有没有变好"的第二个可比信号。
        f"- 平均来回次数:{mean_calls:.2f}(只算修好的:{mean_calls_fixed:.2f})",
        f"- 找错文件次数合计:{sum(len(trial.missed_reads) for trial in trials)}",
        f"- 实际花费:${report.total_cost_usd:.4f}",
        "",
        "| # | 世界 | 规则 | 对象 | 结果 | 只看指纹 | 来回 | 找错文件 | 实际改动 | 判定原因 |",
        "|---:|---|---|---|:---:|:---:|---:|---:|---|---|",
    ]
    for index, trial in enumerate(report.trials, start=1):
        mark = "✅" if trial.fixed else "❌"
        lines.append(
            f"| {index} | {trial.world_id} | `{trial.rule_code}` | `{trial.target_ref}` "
            f"| {mark} | {'✅' if trial.fingerprint_ok else '❌'} | {trial.llm_calls} "
            f"| {len(trial.missed_reads)} | {_edit_summary(trial.edit)} | {trial.verdict} |"
        )
    return "\n".join(lines) + "\n"


def _edit_summary(edit: ContentEdit) -> str:
    """修复者动了什么,一格话:新增/删除/改动各多少,删掉的东西直接点名。"""
    parts: list[str] = []
    if edit.removed_objects:
        parts.append("删对象 " + "、".join(f"`{ref}`" for ref in edit.removed_objects))
    if edit.removed_nodes:
        parts.append("删节点 " + "、".join(f"`{ref}`" for ref in edit.removed_nodes))
    if edit.gutted_texts:
        # 这里只陈述事实(哪段正文剩了多少),该不该算毁内容由 damage 那一列说了算 ——
        # 有几条规则的正规修法本来就是删字。
        parts.append(
            "正文大改 "
            + "、".join(f"`{ref}` {old}→{new}字" for ref, (old, new) in edit.gutted_texts.items())
        )
    if edit.added_objects:
        parts.append(f"新增对象 {len(edit.added_objects)}")
    if edit.added_nodes:
        parts.append(f"新增节点 {len(edit.added_nodes)}")
    if edit.changed_objects or edit.changed_nodes:
        parts.append(f"改对象 {len(edit.changed_objects)}/改节点 {len(edit.changed_nodes)}")
    delta = edit.text_chars_after - edit.text_chars_before
    if delta:
        parts.append(f"正文 {edit.text_chars_before}→{edit.text_chars_after}字")
    return "；".join(parts) if parts else "没动任何对象"


def write_actionability_artifacts(report: ActionabilityReport, output: str | Path) -> None:
    """Write the JSON record next to the Markdown table (same stem, ``.json`` / ``.md``)."""
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.with_suffix(".json").write_text(
        json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    path.with_suffix(".md").write_text(render_actionability_markdown(report), encoding="utf-8")


__all__ = [
    "DEFAULT_ACTIONABILITY_BUDGET_USD",
    "DEFAULT_FIXER_MAX_STEPS",
    "FIXER_SYSTEM_PROMPT",
    "FIXER_SYSTEM_PROMPT_NO_LISTING",
    "NODE_DELETION_SANCTIONED_RULES",
    "OBJECT_DELETION_SANCTIONED_RULES",
    "SHORTENING_SANCTIONED_RULES",
    "TEXT_GUT_RATIO",
    "TEXT_UNIT_MIN_CHARS",
    "TOTAL_TEXT_FLOOR",
    "ActionabilityReport",
    "ActionabilityTrial",
    "ContentCensus",
    "ContentEdit",
    "FixJudgement",
    "build_actionability_worlds",
    "check_preservation",
    "diff_census",
    "finding_report_text",
    "judge_fix",
    "load_report_texts",
    "open_issues_of",
    "render_actionability_markdown",
    "run_report_actionability",
    "take_census",
    "write_actionability_artifacts",
]
