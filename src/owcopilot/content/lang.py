"""Dependency-free language / script detection for content ingestion.

The product takes raw creator input — a pasted novel chapter, an uploaded design doc, a
spreadsheet of lore — and must handle whatever language(s) it is in *without asking the
creator to declare it*. Pushing that choice onto the user is exactly the irresponsible
default we refuse: the system detects the dominant language (and whether the text mixes
several), then instructs every downstream model call to answer in that language and keep
proper nouns verbatim.

This is a deterministic Unicode-script heuristic plus a small Latin stop-word vote — not a
statistical model and not a dependency. It is intentionally modest: it reliably separates
the languages this tool actually sees (Chinese / Japanese / Korean / English and the major
European Latin languages) and degrades to an honest "其他/未知" rather than guessing wildly.
Being deterministic means it is unit-testable and never adds latency or a model call.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# --- script ranges (counted per *letter*; whitespace / punctuation / digits are ignored) ---
_SCRIPT_RANGES: dict[str, list[tuple[int, int]]] = {
    "han": [(0x4E00, 0x9FFF), (0x3400, 0x4DBF), (0xF900, 0xFAFF), (0x20000, 0x2A6DF)],
    "kana": [(0x3040, 0x309F), (0x30A0, 0x30FF), (0x31F0, 0x31FF)],
    "hangul": [(0xAC00, 0xD7A3), (0x1100, 0x11FF), (0x3130, 0x318F)],
    "latin": [(0x0041, 0x005A), (0x0061, 0x007A), (0x00C0, 0x024F), (0x1E00, 0x1EFF)],
    "cyrillic": [(0x0400, 0x04FF), (0x0500, 0x052F)],
    "arabic": [(0x0600, 0x06FF), (0x0750, 0x077F)],
    "thai": [(0x0E00, 0x0E7F)],
    "devanagari": [(0x0900, 0x097F)],
    "hebrew": [(0x0590, 0x05FF)],
    "greek": [(0x0370, 0x03FF)],
}

# Human (Chinese-facing) labels and BCP-47-ish codes per detected language.
_LANG_LABEL: dict[str, str] = {
    "zh": "中文",
    "ja": "日文",
    "ko": "韩文",
    "en": "英文",
    "es": "西班牙文",
    "fr": "法文",
    "de": "德文",
    "pt": "葡萄牙文",
    "it": "意大利文",
    "ru": "俄文",
    "ar": "阿拉伯文",
    "th": "泰文",
    "hi": "印地文",
    "he": "希伯来文",
    "el": "希腊文",
    "und": "未知语言",
}

# Tiny stop-word votes to disambiguate Latin-script languages. Whole-word matched, lowercase.
_LATIN_STOPWORDS: dict[str, frozenset[str]] = {
    "en": frozenset({"the", "and", "of", "to", "in", "is", "that", "it", "for", "with", "was"}),
    "es": frozenset({"el", "la", "los", "las", "de", "que", "y", "un", "una", "por", "con", "no"}),
    "fr": frozenset({"le", "la", "les", "des", "et", "un", "une", "que", "qui", "dans", "pour"}),
    "de": frozenset({"der", "die", "das", "und", "ist", "ein", "eine", "nicht", "mit", "den"}),
    "pt": frozenset({"de", "que", "uma", "para", "com", "não", "os", "as", "do", "da", "em"}),
    "it": frozenset({"il", "la", "che", "di", "un", "una", "per", "con", "non", "del", "gli"}),
}

_WORD_RE = re.compile(r"[a-zÀ-ɏ]+")


@dataclass(frozen=True)
class LanguageProfile:
    """The detected language make-up of a piece of text.

    `dominant` is the single best-guess language code; `languages` lists every language
    holding a meaningful share (most-first) so a mixed-language manuscript is described
    honestly rather than flattened to one. `mixed` is true when a second language clears
    the significance threshold.
    """

    dominant: str = "und"
    languages: list[str] = field(default_factory=lambda: ["und"])
    scripts: dict[str, float] = field(default_factory=dict)
    mixed: bool = False
    letters: int = 0

    @property
    def label(self) -> str:
        return _LANG_LABEL.get(self.dominant, self.dominant)

    @property
    def labels(self) -> list[str]:
        return [_LANG_LABEL.get(code, code) for code in self.languages]


def detect_language(text: str, *, sample_chars: int = 40_000) -> LanguageProfile:
    """Detect the dominant language and the mix of a text.

    Only the first ``sample_chars`` characters are inspected — a representative head is
    plenty for language make-up and keeps detection O(1) on a whole novel.
    """
    sample = text[:sample_chars]
    counts = {name: 0 for name in _SCRIPT_RANGES}
    letters = 0
    for ch in sample:
        cp = ord(ch)
        for name, ranges in _SCRIPT_RANGES.items():
            if any(lo <= cp <= hi for lo, hi in ranges):
                counts[name] += 1
                letters += 1
                break
    if letters == 0:
        return LanguageProfile()

    scripts = {name: round(count / letters, 4) for name, count in counts.items() if count}
    languages = _languages_from_scripts(counts, letters, sample)
    dominant = languages[0]
    return LanguageProfile(
        dominant=dominant,
        languages=languages,
        scripts=scripts,
        mixed=len(languages) > 1,
        letters=letters,
    )


def _languages_from_scripts(counts: dict[str, int], letters: int, sample: str) -> list[str]:
    """Map raw script counts to language codes, ordered by share, keeping only significant ones.

    Japanese is recognised by the presence of kana even when kanji (Han) dominate the page;
    Korean by hangul. Han without kana/hangul reads as Chinese. Latin script is disambiguated
    by a stop-word vote (defaulting to English). A language must hold at least 12% of letters
    (or be the single dominant one) to count toward the mix — that keeps a stray foreign name
    in an otherwise-monolingual text from flipping it to "mixed".
    """
    # Collapse scripts into candidate (language, weight) pairs.
    weights: dict[str, int] = {}

    def add(code: str, weight: int) -> None:
        if weight > 0:
            weights[code] = weights.get(code, 0) + weight

    kana = counts["kana"]
    han = counts["han"]
    if kana > 0:
        # Japanese: kana is the giveaway; the kanji on the same page belong to it too.
        add("ja", kana + han)
    elif han > 0:
        add("zh", han)
    add("ko", counts["hangul"])
    add("ru", counts["cyrillic"])
    add("ar", counts["arabic"])
    add("th", counts["thai"])
    add("hi", counts["devanagari"])
    add("he", counts["hebrew"])
    add("el", counts["greek"])
    if counts["latin"] > 0:
        add(_dominant_latin(sample), counts["latin"])

    if not weights:
        return ["und"]
    ordered = sorted(weights, key=lambda code: (-weights[code], code))
    threshold = letters * 0.12
    significant = [ordered[0]] + [c for c in ordered[1:] if weights[c] >= threshold]
    return significant


def _dominant_latin(sample: str) -> str:
    """Vote across Latin-script languages by stop-word frequency; default English."""
    words = _WORD_RE.findall(sample.lower())
    if not words:
        return "en"
    window = words[:600]
    best, best_score = "en", 0
    for code, stops in _LATIN_STOPWORDS.items():
        score = sum(1 for w in window if w in stops)
        if score > best_score:
            best, best_score = code, score
    return best


def language_directive(profile: LanguageProfile) -> str:
    """A Chinese prompt line telling the model which language to answer in.

    For mixed input it names the primary language for prose and instructs the model to keep
    every proper noun in its original script — the round-26 "keep names verbatim" discipline,
    now language-aware so a Chinese-named cast in an English manuscript survives intact.
    """
    if profile.dominant == "und" or profile.letters == 0:
        return "请用与原文一致的语言输出，保留所有专有名词的原文写法。"
    if profile.mixed:
        others = "、".join(profile.labels[1:]) or "其他语言"
        return (
            f"原文为多语言混排（主要为{profile.label}，并含{others}）。"
            f"描述性文字统一用{profile.label}书写，但每个人物、地点、术语等专有名词必须保留其原文写法，不要翻译。"
        )
    return f"原文语言为{profile.label}，请全程用{profile.label}输出，并保留所有专有名词的原文写法。"
