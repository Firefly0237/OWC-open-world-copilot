"""Free-text consistency check: does this chapter agree with the world archive?

Deterministic v1 (zero LLM): resolve known mentions, flag unknown proper-noun candidates,
catch forbidden term spellings. The same seeded-error benchmark methodology as the audit
suite keeps it honest: tests plant violations and require they are caught.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field

from ..content.models import ContentBundle

_QUOTED = re.compile(r"「([一-鿿A-Za-z0-9·]{2,12})」")
_SPEAKER = re.compile(r"([一-鿿]{2,4})(?:说道|喊道|低声道|答道|问道|说)")
_ARRIVE = re.compile(r"(?:来到|前往|抵达|回到)([一-鿿]{2,6})")

_GENERIC_WORDS = {
    "这里",
    "那里",
    "他们",
    "我们",
    "你们",
    "自己",
    "大家",
    "什么",
    "怎么",
    "此地",
    "此处",
    "现在",
    "今天",
    "明天",
    "昨天",
}


class ProseMention(BaseModel):
    name: str
    ref: str
    count: int


class ProseIssue(BaseModel):
    kind: str  # forbidden_term | unknown_mention
    excerpt: str
    message: str
    position: int
    suggestion: str = ""


class ProseReport(BaseModel):
    resolved_mentions: list[ProseMention] = Field(default_factory=list)
    issues: list[ProseIssue] = Field(default_factory=list)
    stats: dict[str, int] = Field(default_factory=dict)


def check_prose(text: str, bundle: ContentBundle, *, max_issues: int = 200) -> ProseReport:
    clean = text.strip()
    if not clean:
        raise ValueError("prose text is empty")
    known = _known_names(bundle)
    report = ProseReport()

    # 1) resolved mentions: every known name/alias that actually appears (longest-first
    #    so 「沈青澜」 wins over a shorter alias contained in it)
    counted: dict[str, ProseMention] = {}
    for name in sorted(known, key=len, reverse=True):
        count = clean.count(name)
        if count <= 0:
            continue
        ref = known[name]
        slot = counted.get(ref)
        if slot is None:
            counted[ref] = ProseMention(name=name, ref=ref, count=count)
        else:
            slot.count += count
    report.resolved_mentions = sorted(counted.values(), key=lambda m: -m.count)

    # 2) forbidden spellings from the term sheet
    for term in bundle.terms.values():
        for forbidden in term.forbidden:
            if not forbidden.strip():
                continue
            for match in re.finditer(re.escape(forbidden), clean):
                report.issues.append(
                    ProseIssue(
                        kind="forbidden_term",
                        excerpt=_excerpt(clean, match.start(), len(forbidden)),
                        message=f"出现了禁用写法「{forbidden}」",
                        position=match.start(),
                        suggestion=f"改用标准名「{term.canonical}」",
                    )
                )

    # 3) unknown proper-noun candidates (quoted terms, speakers, destinations)
    seen_unknown: set[str] = set()
    for pattern, label in ((_QUOTED, "术语/名词"), (_SPEAKER, "人物"), (_ARRIVE, "地点")):
        for match in pattern.finditer(clean):
            candidate = match.group(1).strip()
            if (
                not candidate
                or candidate in _GENERIC_WORDS
                or candidate in seen_unknown
                or _is_known(candidate, known)
            ):
                continue
            seen_unknown.add(candidate)
            report.issues.append(
                ProseIssue(
                    kind="unknown_mention",
                    excerpt=_excerpt(clean, match.start(1), len(candidate)),
                    message=f"{label}「{candidate}」不在世界档案中",
                    position=match.start(1),
                    suggestion="确认是否新设定：先在档案中补全，或修正为已有名称",
                )
            )

    report.issues.sort(key=lambda issue: issue.position)
    report.issues = report.issues[:max_issues]
    report.stats = {
        "chars": len(clean),
        "resolved_mentions": len(report.resolved_mentions),
        "issues": len(report.issues),
        "forbidden_terms": sum(1 for i in report.issues if i.kind == "forbidden_term"),
        "unknown_mentions": sum(1 for i in report.issues if i.kind == "unknown_mention"),
    }
    return report


def _known_names(bundle: ContentBundle) -> dict[str, str]:
    names: dict[str, str] = {}

    def put(name: str, ref: str) -> None:
        cleaned = name.strip()
        if len(cleaned) >= 2:
            names.setdefault(cleaned, ref)

    for entity in bundle.entities.values():
        put(entity.name, f"entity:{entity.id}")
        for alias in entity.aliases:
            put(alias, f"entity:{entity.id}")
    for poi in bundle.pois.values():
        put(poi.name, f"poi:{poi.id}")
    for region in bundle.regions.values():
        put(region.name, f"region:{region.id}")
    for quest in bundle.quests.values():
        put(quest.title, f"quest:{quest.id}")
    for term in bundle.terms.values():
        put(term.canonical, f"term:{term.id}")
        for alias in term.aliases:
            put(alias, f"term:{term.id}")
    return names


def _is_known(candidate: str, known: dict[str, str]) -> bool:
    if candidate in known:
        return True
    # a candidate inside a longer known name (or vice versa) counts as resolved enough
    return any(candidate in name or name in candidate for name in known if len(name) >= 2)


def _excerpt(text: str, position: int, length: int, *, margin: int = 14) -> str:
    start = max(0, position - margin)
    end = min(len(text), position + length + margin)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return f"{prefix}{text[start:end]}{suffix}".replace("\n", " ")
