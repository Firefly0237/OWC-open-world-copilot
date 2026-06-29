"""Vocabulary-constraint injection for LLM prompts.

Builds a [vocabulary-constraints] block from a project's Term objects and injects it
into generation and critique prompts so the LLM knows which words are forbidden and
which canonical forms to prefer.

Two public functions:
- build_term_block: for writer/generator prompts (uses context_hits to filter when >20 terms)
- build_term_block_for_critic: for critic prompts (only forbidden terms; no context_hits needed)
"""

from __future__ import annotations

from ..content.models import Term


def build_term_block(
    terms: list[Term],
    *,
    context_hits: list[str],
    inject_terms: bool = True,
) -> str:
    """Build a [vocabulary-constraints] block for generator prompts.

    Grading logic:
    1. inject_terms=False -> return ""
    2. len(terms) <= 20 -> inject all terms
    3. len(terms) > 20 -> inject only terms whose id, canonical, or any alias
       appears in context_hits (relevance filter)
    4. working set empty after filter -> return ""

    Output format (lines omitted when empty):
        [vocabulary-constraints]
        MUST NOT use: <comma-separated forbidden words>
        PREFER: <canonical>（代替 <alias1>, <alias2>）, ...
    """
    if not inject_terms:
        return ""
    if not terms:
        return ""

    if len(terms) <= 20:
        working_set = list(terms)
    else:
        hits_set = set(context_hits)
        working_set = [
            t for t in terms
            if (
                t.id in hits_set
                or t.canonical in hits_set
                or any(a in hits_set for a in t.aliases)
                or any(w in hits_set for w in t.forbidden)  # BE-5: forbidden-word hits
            )
        ]

    if not working_set:
        return ""

    return _build_block(working_set)


def build_term_block_for_critic(terms: list[Term]) -> str:
    """Build a [vocabulary-constraints] block for critic prompts.

    Critics don't have context_hits; when there are >20 terms, only inject terms
    that have forbidden words (hard_forbidden terms). Prefer-canonical-only terms
    are skipped in the >20 case to avoid distracting the critic from its core job.

    Returns "" when there is nothing to inject.
    """
    if not terms:
        return ""

    if len(terms) <= 20:
        working_set = list(terms)
    else:
        # Only inject hard-forbidden terms (those that have a non-empty forbidden list)
        working_set = [t for t in terms if t.forbidden]

    if not working_set:
        return ""

    return _build_block(working_set)


_MAX_FORBIDDEN_LEN = 100   # a forbidden word longer than this is almost certainly injected content
_MAX_ALIAS_LEN = 150       # aliases are slightly longer (compound names) but still bounded


def _safe_forbidden(w: str) -> str | None:
    """Return w if it looks like a real vocabulary constraint word; None if it should be skipped.

    Item 4 (render-side last-resort): over-length words and words that embed a newline are
    structural injection signals — newlines break prompt formatting; extreme length carries
    instructions. Dropping them here is a belt-and-suspenders guard (the audit PromptInjectionRule
    on security_rules._texts is the earlier interception point).
    """
    if "\n" in w or len(w) > _MAX_FORBIDDEN_LEN:
        return None
    return w


def _safe_alias(a: str) -> str | None:
    """Same guard for alias strings."""
    if "\n" in a or len(a) > _MAX_ALIAS_LEN:
        return None
    return a


def _build_block(working_set: list[Term]) -> str:
    """Render the two-line vocabulary block from a set of terms."""
    # MUST NOT line: collect all forbidden words from all working_set terms, dedup
    must_not_words: list[str] = []
    seen_forbidden: set[str] = set()
    for t in working_set:
        for w in t.forbidden:
            safe = _safe_forbidden(w) if w else None
            if safe and safe not in seen_forbidden:
                must_not_words.append(safe)
                seen_forbidden.add(safe)

    # PREFER line: canonical with aliases notation (only when term has aliases)
    prefer_items: list[str] = []
    for t in working_set:
        if t.aliases:
            safe_aliases = [a for raw in t.aliases if (a := _safe_alias(raw))]
            if not safe_aliases:
                continue
            aliases_str = ", ".join(safe_aliases)
            prefer_items.append(f"{t.canonical}（代替 {aliases_str}）")
        # terms without aliases don't add anything meaningful to PREFER line

    if not must_not_words and not prefer_items:
        return ""

    lines = ["[vocabulary-constraints]"]
    if must_not_words:
        lines.append(f"MUST NOT use: {', '.join(must_not_words)}")
    if prefer_items:
        lines.append(f"PREFER: {', '.join(prefer_items)}")

    return "\n".join(lines)
