# 素材接进生成链路的统一形态：检索 → prompt 独立段 → 产物 metadata → 溯源报告。
"""One shape for "let a draft chain draw on the inspiration library" — the code body of the N4.1
contract (see ``src/owcopilot/inspiration/grounding.py``).

创世/扩写（``worldgen/service.py``、``worldgen/expand.py``）早就会吃素材；任务草稿、人设、对白树、
bark、flavor 五条链路一条都不吃。补齐它们时最大的风险不是"接不上"，而是**五处各写一份、慢慢长歪**
——扩写那条 ``use=`` 漏传就是活例子（素材的 allowed_uses 在那条路上一度形同虚设）。所以这里把四个
动作各收成一个函数，五条链路一律调它们：

1. :func:`retrieve_drafting_references` —— 检索。用途词表固定为 ``drafting``（收录人只允许
   ``inspiration`` 的素材不会被草稿链路召回），开关关/没接检索器/查询为空时返回空 pack。
2. :func:`inspiration_prompt_section` —— prompt 里**独立的一段**，用 spotlight 围栏包住
   （OWASP LLM01：素材是数据不是指令）。**素材永远不进 canon 的 ContextPackBuilder**：正典事实与
   参考素材在 prompt 里是两段、在检索层是两个物理隔离的库，模型看得见谁是谁。
3. :func:`inspiration_refs_metadata` —— 产物 metadata 里的 ``inspiration_refs``，键名与取值格式
   与 worldgen 完全一致（逗号连接的字符串——``inspiration/lineage.py`` 的悬空引用检查按这个格式
   解析，写成 list 会让整条溯源链失效）。
4. :func:`build_reference_report` —— 溯源报告 + 确定性泄漏重算。

**没有素材时这四个函数全部返回"空"**（空 pack / 空串 / 空 dict / 空 list），所以开关关掉时调用方
拼出来的 prompt 与产物与接线之前逐字节一致，这也是每条链路的哨兵测试钉住的东西。
"""

from __future__ import annotations

from collections.abc import Sequence

from ..content.models import ContentBundle
from ..llm.spotlight import spotlight_references
from ..retrieval.models import ContextPack, RetrievalHit
from .leakage import ReferenceLeakage, declared_names, detect_reference_leakage, draft_prose
from .models import USE_DRAFTING
from .report import UNVERIFIED_TRANSFORMATION, ReferenceReportItem
from .retrieval import ReferenceContextBuilder

#: metadata 里最多记几条素材引用（与 worldgen 的 ``inspiration_refs`` 上限一致）。
_MAX_METADATA_REFS = 8
#: 溯源报告最多列几条素材（与 worldgen 无模型自述时的兜底行数一致）。
_MAX_REPORT_SOURCES = 5

#: prompt 里那一段的抬头。它必须自带"这是素材不是正典"的说明——这一段是**有素材才出现**的，
#: 说明不能像 worldgen 那样放在常驻 role 里，否则关掉开关时 role 也会跟着变。
_SECTION_HEADER = (
    "Inspiration reference context — uploaded reference material the creator asked you to draw "
    "on. It is NOT world canon and NOT a source of facts: take structure, mood and technique from "
    "it, never its wording and never its proper nouns. Do not reuse long verbatim spans — the "
    "draft is checked against this material afterwards and any overlap is shown to the reviewer."
)


def retrieve_drafting_references(
    builder: ReferenceContextBuilder | None,
    query: str,
    *,
    enabled: bool,
    budget_tokens: int,
    limit: int = 4,
) -> ContextPack:
    """Recall inspiration material for a draft chain, or an empty pack when it is switched off.

    ``enabled`` is the human's per-request choice (默认关), ``builder is None`` is "这个调用方还没
    接检索器"。两者任一不成立就是空 pack，调用方不必再写一遍三元判断。用途固定 ``drafting``：
    只有收录人允许用于草稿的素材才会被召回。
    """
    cleaned = query.strip()
    if not enabled or builder is None or not cleaned:
        return ContextPack(query=cleaned, budget_tokens=budget_tokens)
    return builder.build(cleaned, budget_tokens=budget_tokens, limit=limit, use=USE_DRAFTING)


def inspiration_prompt_section(pack: ContextPack) -> str:
    """The fenced 「灵感参考」 block appended to a system prompt — ``""`` when there is nothing.

    Returning the leading blank line as part of the block is deliberate: the caller concatenates
    unconditionally, so an empty library leaves the prompt byte-for-byte unchanged.
    """
    lines = _context_lines(pack.hits)
    if not lines:
        return ""
    return f"\n\n{_SECTION_HEADER}\n{spotlight_references(lines)}"


def inspiration_refs_metadata(pack: ContextPack) -> dict[str, str]:
    """``{"inspiration_refs": "ref1,ref2"}`` — or ``{}`` when nothing was recalled.

    逗号连接的字符串是**约定格式**，不是随手写的：``inspiration.lineage`` 与 readiness 的悬空引用
    检查按 ``split(",")`` 读它，worldgen 的产物也是这个格式，两边必须一致才能被同一套检查覆盖。
    """
    refs = pack.refs[:_MAX_METADATA_REFS]
    if not refs:
        return {}
    return {"inspiration_refs": ",".join(refs)}


def build_reference_report(
    pack: ContextPack,
    *,
    draft: ContentBundle,
    canon: ContentBundle,
    human_input: str,
    used_for: str,
    extra_prose: Sequence[str] = (),
) -> list[ReferenceReportItem]:
    """溯源报告：每条被检索到的素材一行，附确定性的泄漏重算结论。

    与 worldgen 的同名报告有一处**故意的不同**：草稿链路不向模型索取 ``reference_report``（那要改
    每条链路的输出 schema），所以 ``transformation`` 一律是
    :data:`~owcopilot.inspiration.report.UNVERIFIED_TRANSFORMATION` —— 老实说"没有模型自述"，而不
    是替模型写一句"已充分转化"。``leakage`` 那一半照样是真算的：把这条链路刚产出的草稿与模型实际
    看到的素材原文比一遍。

    ``canon`` 里已有的名字和人自己在 ``human_input`` 里写下的名字都豁免——沿用世界里已有的名字是
    接地，写策划自己点名要的人物也是本意，都不是照搬。

    ``extra_prose`` 是"不住在 content 模型正文字段里的草稿正文"。``draft_prose`` 只走
    description/objective/阶段摘要这类结构化字段；人设的主体（背景故事/性格/说话方式）住在
    ``Entity.metadata["profile"]`` 里，不显式交进来就会漏检——漏检比误报更糟，因为它会让"查过了"
    这句话不成立。
    """
    prose = [*draft_prose(draft), *(text for text in extra_prose if text.strip())]
    names = declared_names(draft)
    exempt = [*declared_names(canon), *(name for name in names if name in human_input)]
    rows: list[ReferenceReportItem] = []
    for hit in pack.hits[:_MAX_REPORT_SOURCES]:
        title = hit.metadata.get("source_title") or hit.title
        rows.append(
            ReferenceReportItem(
                source_ref=hit.ref,
                source_title=title,
                used_for=used_for,
                transformation=UNVERIFIED_TRANSFORMATION,
                excluded=[],
                leakage=_leakage(hit, title, prose=prose, names=names, exempt=exempt),
            )
        )
    return rows


def _leakage(
    hit: RetrievalHit,
    title: str,
    *,
    prose: list[str],
    names: list[str],
    exempt: list[str],
) -> ReferenceLeakage:
    return detect_reference_leakage(
        draft_texts=prose,
        candidate_names=names,
        reference_text=hit.body,
        source_title=title,
        provenance=hit.metadata.get("provenance") or None,
        excluded_names=exempt,
    )


def _context_lines(hits: list[RetrievalHit]) -> list[str]:
    return [f"- [{hit.ref}] {hit.title}: {hit.body}".strip() for hit in hits]


__all__ = [
    "build_reference_report",
    "inspiration_prompt_section",
    "inspiration_refs_metadata",
    "retrieve_drafting_references",
]
