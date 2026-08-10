# 素材泄漏的确定性检测：草稿 × 被引素材原文的逐字重合 + 专名照搬。
"""Deterministic detection of reference leakage — did a generated draft copy its material?

**Why this exists.** Genesis / expansion ground a draft in the inspiration library, and the
per-source 溯源 report used to carry only the model's *own account* of how it transformed what it
read ("未复用原文"). That is an unfalsifiable claim: nothing recomputed it. This module is the
recomputation. It never asks the model anything; it takes the draft the chain just produced, the
reference chunk text that was actually put in front of the model, and reports two deterministic
signals:

* **逐字重合 (verbatim overlap)** — maximal spans the draft and the chunk share, found by
  character/word shingling. CJK is shingled over ideographs only (whitespace/punctuation/Latin
  dropped) so a copied line still matches after re-punctuation; Latin is shingled over lowercased
  word runs.
* **专名照搬 (proper-noun leakage)** — a *name the draft declares* (entity/POI/region/term/quest
  title) that occurs in the chunk text, is not already an established canon name, and was not
  written by the human in the brief. Taking the draft's own name fields as the candidate set is
  what makes this deterministic without a CJK named-entity recogniser: those fields are proper
  nouns by construction.

**What it is NOT.** This is a *detection report field*, not an audit rule and not an ERROR — it is
attached to the reference report the reviewer already reads (see
``inspiration.report.ReferenceReportItem.leakage``). The default rule registry still holds 35 rules;
nothing here changes that count. Zero findings mean "nothing crossed this threshold", never "no
reuse happened" — every message says which threshold was applied, so the reader can tell the
difference.

---

## 阈值定标 (threshold calibration)

阈值是**量出来的，不是拍出来的**。定标口径：对每对（草稿, 素材）二分求出**最长共同连续单元数**
（CJK 计汉字、Latin 计词），阈值必须落在「诚实改写的上限」与「照抄的下限」之间。

三套语料（草稿一侧全部是仓库里真有的文本；素材一侧为定标手写，仓库里没有第三方作品原文）：

1. ``demo/honglou`` —— 仓库自带的演示世界（6 条实体简介 + 3 个任务的目标与阶段摘要 + 3 棵对白
   树）当草稿。它本身就是对一部古典作品的**改编**，是手上最难的真实样本：同题材、同人物、改写过
   的文字。配的"同题材素材"是为定标手写的一段仿古白话，讲同一段情节，并像真实素材那样含有 demo
   简介里也引用了的那句四字成语连用。
2. 离线创世替身（``worldgen/offline.py``）真跑一遍产出的整份草稿 —— 生产链路上的误报地板，素材
   用它自己检索到的那几条。
3. 合成对照：一段 155 字的中文散文与一段 113 词的英文散文当素材，各配四种草稿——整段照抄 /
   只抄一句 / 人工改写转述 / 同题材但无关的另写。

实测（数值为最长共同连续单元数）：

| 语料 · variant | CJK 最长共同汉字 | Latin 最长共同词 |
|---|---|---|
| 合成 · 整段照抄 | 155 | 113 |
| 合成 · 只抄一句 | **29** | **15** |
| 合成 · 改写转述 | 4 | 2 |
| 合成 · 同题材无关文 | 2 | 2 |
| demo 世界 × 同题材素材 | 8 | — |
| demo 世界 × 无关素材 | 2 | — |
| 离线生成世界 × 它自己检索到的同题材素材 | 4 | 0 |

「诚实改写」这一侧的上限是 4；唯一的 8 是 demo 那条实体简介里**带引号的四字成语连用**（真的是
逐字引用，不是巧合）。「照抄」这一侧的下限是 29 / 15（一句话）。默认档 ``12`` / ``8`` 正落在
8→29、2→15 这两段空当里，两侧都有余量。严格档 ``8`` / ``5`` 压到"诚实改写上限"之上一档：它会
把 demo 那条带引号的引用也报出来（那本来就该被人看一眼），同时仍是 4 字改写上限的 2 倍。严格档
只用于收录时声明为「第三方参考」的素材，以及归属未知的素材（未知按最保守处理，与
``ReferenceSource.provenance`` 的默认值同向）。

专名一侧同样实测。素材换成一段会反复点名的同题材情节概述（真实素材就是这样），对 demo 的声明专名
计数：主要人物出现 3–4 次（林黛玉 4、贾宝玉 3、贾母 3），次要人物 2 次（王夫人 2、王熙凤 2），
只被提一句的 1 次（邢夫人 1）；而"巧合撞名"的对照——草稿自造的通用地名撞上一段无关素材——实测
「码头」出现 2 次、「东市」1 次。两侧在 2 这一档是**重叠**的，所以默认档取 ``>=3``（把两字通用
名词挡在外面，代价是漏掉只被提两次的次要人物），严格档取 ``>=2``（换取召回，代价是通用二字名词
可能被报出来，例如上面的「码头」）。这条残留误报是已知的、量过的，不假装不存在——本模块的产出是
给人过目的检测字段，不是拦截门。

上面这些档位不是只写在注释里：``tests/retrieval/test_reference_leakage.py``
用一段刚好 10 字的重合把两档钉住（自有/已授权放过、第三方报出），并把
"改写转述/同题材另写/沿用正典专名/人自己写的名字/单字名"
五条误报边界各留一个用例，改动阈值会直接打断这些测试。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass

from pydantic import BaseModel, Field

from ..content.models import ContentBundle
from .models import Provenance

# 只保留表意文字：逐字重合按"汉字流"比对，标点/空白/拉丁字母都不参与，
# 这样把原文重新断句、换标点后照抄仍然能被认出来。
_CJK_RUN = re.compile(r"[^㐀-䶿一-鿿]+")
# 拉丁侧按词比对（小写化后的字母数字串），大小写与标点差异不影响判定。
_LATIN_WORD = re.compile(r"[a-z0-9]+")

#: 展示用的片段上限——命中片段是给人看的证据，不是要把素材原文再抄一遍到报告里。
_SPAN_DISPLAY_CHARS = 40
#: 一条结论里最多列几处片段 / 几个专名。
_MAX_SPANS = 5
_MAX_NAMES = 8


@dataclass(frozen=True)
class LeakageThresholds:
    """一档检测阈值。数值来源见模块文档的「阈值定标」。"""

    cjk_ngram: int
    latin_ngram: int
    name_min_occurrences: int
    #: 给人看的档位名（会出现在检测结论里，所以是中文）。
    label: str


#: 自有 / 已授权素材：借鉴本来就是允许的，只抓真正的整句照搬。
STANDARD_THRESHOLDS = LeakageThresholds(
    cjk_ngram=12, latin_ngram=8, name_min_occurrences=3, label="常规（连续 12 字 / 8 词）"
)
#: 第三方参考素材（含来源未声明的）：收紧到刚好压住"诚实改写"的上限。
STRICT_THRESHOLDS = LeakageThresholds(
    cjk_ngram=8,
    latin_ngram=5,
    name_min_occurrences=2,
    label="严格（第三方参考 · 连续 8 字 / 5 词）",
)


def thresholds_for(provenance: Provenance | str | None) -> LeakageThresholds:
    """来源归属决定检测严格度；认不出的归属一律按最保守的那一档，与素材模型的默认值一致。"""
    if provenance is None:
        return STRICT_THRESHOLDS
    value = provenance.value if isinstance(provenance, Provenance) else str(provenance)
    if value in {Provenance.OWN_IP.value, Provenance.LICENSED.value}:
        return STANDARD_THRESHOLDS
    return STRICT_THRESHOLDS


class ReferenceLeakage(BaseModel):
    """一条素材的泄漏检测结论。

    ``checked=False`` 是诚实的"没查"——拿不到素材原文时不假装查过。``detected=False`` 只表示
    "没有越过本次阈值"，不等于"没有复用"，所以 ``message`` 一定把阈值说出来。
    """

    #: 是否真的做了核查（拿到了素材原文才算）。
    checked: bool = False
    #: 是否有片段/专名越过阈值。``checked=False`` 时恒为 False。
    detected: bool = False
    #: 命中的逐字重合片段（截断展示，最多几条）。
    verbatim_spans: list[str] = Field(default_factory=list)
    #: 命中的专名（草稿自己声明的名字，同时出现在素材原文里）。
    leaked_names: list[str] = Field(default_factory=list)
    #: 本次用的阈值档位（人看的中文）。
    threshold_label: str = ""
    #: 面向策划的中文结论 + 下一步。
    message: str = ""


def detect_reference_leakage(
    *,
    draft_texts: Sequence[str],
    candidate_names: Sequence[str],
    reference_text: str,
    source_title: str,
    provenance: Provenance | str | None,
    excluded_names: Iterable[str] = (),
) -> ReferenceLeakage:
    """把一份草稿与一段素材原文比一遍，给出确定性的泄漏结论。

    ``excluded_names`` 是"本来就该出现"的名字：既有正典里的名字/别名，以及人在需求里自己写下的
    词——这两类即便同时出现在素材里也不是照搬，是策划的原意。
    """
    thresholds = thresholds_for(provenance)
    material = reference_text.strip()
    if not material:
        return ReferenceLeakage(
            checked=False,
            threshold_label=thresholds.label,
            message=f"未取到素材《{source_title}》的原文，本次没有做逐字重合核查。",
        )

    spans = _verbatim_spans(draft_texts, material, thresholds)
    names = _leaked_names(candidate_names, material, thresholds, excluded_names)
    detected = bool(spans or names)
    return ReferenceLeakage(
        checked=True,
        detected=detected,
        verbatim_spans=spans[:_MAX_SPANS],
        leaked_names=names[:_MAX_NAMES],
        threshold_label=thresholds.label,
        message=_message(source_title, spans, names, thresholds),
    )


def draft_prose(bundle: ContentBundle) -> list[str]:
    """草稿里会被读者读到的自由文本面——逐字重合就查这些地方。"""
    return list(_iter_prose(bundle))


def declared_names(bundle: ContentBundle) -> list[str]:
    """一个世界自己声明的名字：这些字段按定义就是专名。

    用在两侧：草稿这边是专名照搬检查的候选集，既有正典那边是豁免名单——草稿沿用正典里已有的名字
    是接地，不是照搬。
    """
    seen: dict[str, None] = {}
    for name in _iter_names(bundle):
        cleaned = name.strip()
        if cleaned:
            seen.setdefault(cleaned, None)
    return list(seen)


# --- 逐字重合 -------------------------------------------------------------------------------------


def _verbatim_spans(
    draft_texts: Sequence[str], material: str, thresholds: LeakageThresholds
) -> list[str]:
    cjk_material = _cjk_stream(material)
    latin_material = _latin_stream(material)
    cjk_shingles = _shingles(cjk_material, thresholds.cjk_ngram, "")
    latin_shingles = _shingles(latin_material, thresholds.latin_ngram, " ")
    spans: list[str] = []
    for text in draft_texts:
        spans += _matched_spans(_cjk_stream(text), cjk_shingles, thresholds.cjk_ngram, "")
        spans += _matched_spans(_latin_stream(text), latin_shingles, thresholds.latin_ngram, " ")
    # 长的先给——最长的那段最能说明问题；同长度按字典序稳定，便于测试与复算。
    unique = sorted(dict.fromkeys(spans), key=lambda span: (-len(span), span))
    return [_ellipsis(span) for span in unique]


def _cjk_stream(text: str) -> list[str]:
    return list(_CJK_RUN.sub("", text))


def _latin_stream(text: str) -> list[str]:
    return _LATIN_WORD.findall(text.lower())


def _shingles(units: list[str], size: int, joiner: str) -> set[str]:
    if len(units) < size:
        return set()
    return {joiner.join(units[i : i + size]) for i in range(len(units) - size + 1)}


def _matched_spans(units: list[str], shingles: set[str], size: int, joiner: str) -> list[str]:
    """把命中的窗口合并成极大片段，避免一句抄袭被拆成十几条重复证据。"""
    if not shingles or len(units) < size:
        return []
    spans: list[str] = []
    start = 0
    total = len(units)
    while start + size <= total:
        if joiner.join(units[start : start + size]) not in shingles:
            start += 1
            continue
        end = start + size
        while end < total and joiner.join(units[end - size + 1 : end + 1]) in shingles:
            end += 1
        spans.append(joiner.join(units[start:end]))
        start = end
    return spans


def _ellipsis(span: str) -> str:
    """长片段只展示开头，但把总长度带上——截断不能把"抄了多长"这个最要紧的信号也截掉。"""
    if len(span) <= _SPAN_DISPLAY_CHARS:
        return span
    return f"{span[:_SPAN_DISPLAY_CHARS]}…（共 {len(span)} 字）"


# --- 专名照搬 -------------------------------------------------------------------------------------


def _leaked_names(
    names: Sequence[str],
    material: str,
    thresholds: LeakageThresholds,
    excluded: Iterable[str],
) -> list[str]:
    blocked = {item.strip() for item in excluded if item.strip()}
    leaked: list[str] = []
    for name in names:
        candidate = name.strip()
        if not candidate or candidate in blocked or not _is_name_shaped(candidate):
            continue
        if material.count(candidate) >= thresholds.name_min_occurrences:
            leaked.append(candidate)
    return sorted(dict.fromkeys(leaked))


def _is_name_shaped(name: str) -> bool:
    """太短的名字撞上素材是巧合而非照搬：中文名至少两字，拉丁名至少四个字母。"""
    cjk = len(_cjk_stream(name))
    if cjk:
        return cjk >= 2
    letters = sum(1 for char in name if char.isalpha())
    return letters >= 4


# --- 文案 -----------------------------------------------------------------------------------------


def _message(
    source_title: str,
    spans: list[str],
    names: list[str],
    thresholds: LeakageThresholds,
) -> str:
    if not spans and not names:
        return (
            f"已按{thresholds.label}核查草稿与素材《{source_title}》："
            "没有发现越过该阈值的逐字重合或专名照搬。"
            "这只说明没越过本次阈值，不等于完全没有借用——更短的化用不在本次判定范围内。"
        )
    parts = [f"草稿与素材《{source_title}》存在需要人工确认的重合（{thresholds.label}）："]
    if spans:
        quoted = "、".join(f"“{span}”" for span in spans[:_MAX_SPANS])
        parts.append(f"逐字重合 {len(spans)} 处，例如 {quoted}；")
    if names:
        parts.append(f"专名照搬 {len(names)} 个：{'、'.join(names[:_MAX_NAMES])}；")
    parts.append(
        "请把这些地方改写成自己的表述再提交；若这份素材本就是自有或已获授权的内容，"
        "请到素材库把它的来源归属改成「自有」或「已授权」，再重新生成一次。"
    )
    return "".join(parts)


# --- 草稿文本面 -----------------------------------------------------------------------------------


def _iter_prose(bundle: ContentBundle) -> Iterator[str]:
    for entity in bundle.entities.values():
        if entity.description:
            yield entity.description
    for quest in bundle.quests.values():
        if quest.objective:
            yield quest.objective
        for stage in quest.stages:
            if stage.summary:
                yield stage.summary
    for poi in bundle.pois.values():
        if poi.purpose:
            yield poi.purpose
    for term in bundle.terms.values():
        if term.description:
            yield term.description
    for style in bundle.style_guides.values():
        if style.body:
            yield style.body
        yield from (rule for rule in style.rules if rule)
    for dialogue in bundle.dialogues.values():
        if dialogue.text:
            yield dialogue.text
    for text in bundle.localized_texts.values():
        if text.text:
            yield text.text
    for tree in bundle.dialogue_trees.values():
        for node in tree.nodes.values():
            if node.text:
                yield node.text
            yield from (choice.text for choice in node.choices if choice.text)


def _iter_names(bundle: ContentBundle) -> Iterator[str]:
    for entity in bundle.entities.values():
        yield entity.name
        yield from entity.aliases
    for poi in bundle.pois.values():
        yield poi.name
    for region in bundle.regions.values():
        yield region.name
    for term in bundle.terms.values():
        yield term.canonical
        yield from term.aliases
    for quest in bundle.quests.values():
        yield quest.title
    for tree in bundle.dialogue_trees.values():
        if tree.title:
            yield tree.title
