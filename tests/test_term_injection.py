"""Tests for IN-1: vocabulary-constraint injection (term_injection.py).

Covers:
- forbidden terms appear in MUST NOT line
- inject_terms=False returns ""
- terms > 20 only injects context-hits subset
- terms <= 20 injects all (full injection)
- all terms unhit in >20 case returns ""
- critic variant: only forbidden terms when >20
- PREFER line with aliases
- empty forbidden + no aliases returns ""
"""

from __future__ import annotations

from owcopilot.assist.term_injection import build_term_block, build_term_block_for_critic
from owcopilot.content.models import Term


def _term(
    id: str,
    canonical: str,
    aliases: list[str] | None = None,
    forbidden: list[str] | None = None,
) -> Term:
    return Term(
        id=id,
        canonical=canonical,
        aliases=aliases or [],
        forbidden=forbidden or [],
    )


# ---------------------------------------------------------------------------
# build_term_block
# ---------------------------------------------------------------------------

def test_forbidden_term_in_must_not_line() -> None:
    """[硬] Forbidden term must appear in MUST NOT use: line."""
    terms = [_term("t1", "凋零", forbidden=["死亡", "死了"])]
    result = build_term_block(terms, context_hits=[], inject_terms=True)
    assert "MUST NOT use:" in result
    assert "死亡" in result
    assert "死了" in result


def test_inject_terms_false_returns_empty() -> None:
    """[硬] inject_terms=False -> return empty string."""
    terms = [_term("t1", "凋零", forbidden=["死亡"])]
    result = build_term_block(terms, context_hits=[], inject_terms=False)
    assert result == ""


def test_full_injection_le20() -> None:
    """[软] terms <= 20: inject all."""
    terms = [_term(f"t{i}", f"canonical_{i}", forbidden=[f"forb_{i}"]) for i in range(15)]
    result = build_term_block(terms, context_hits=[], inject_terms=True)
    assert "MUST NOT use:" in result
    for i in range(15):
        assert f"forb_{i}" in result


def test_subset_injection_gt20() -> None:
    """[硬] terms > 20: only inject context-hits-matched subset."""
    # 25 terms, only 3 hit by context_hits — use non-ambiguous names
    terms = [
        _term(f"tid_{i:03d}", f"canonical_{i:03d}", forbidden=[f"forbidden_{i:03d}"])
        for i in range(25)
    ]
    context_hits = ["tid_000", "tid_005", "tid_010"]  # matches by id
    result = build_term_block(terms, context_hits=context_hits, inject_terms=True)
    assert "MUST NOT use:" in result
    assert "forbidden_000" in result
    assert "forbidden_005" in result
    assert "forbidden_010" in result
    # Non-hit terms must not appear
    assert "forbidden_001" not in result
    assert "forbidden_023" not in result


def test_all_terms_unhit_gt20_returns_empty() -> None:
    """[硬] terms > 20, none in context_hits -> return ""."""
    terms = [_term(f"t{i}", f"canonical_{i}", forbidden=[f"forb_{i}"]) for i in range(25)]
    result = build_term_block(terms, context_hits=["no_match"], inject_terms=True)
    assert result == ""


def test_empty_terms_returns_empty() -> None:
    result = build_term_block([], context_hits=[], inject_terms=True)
    assert result == ""


def test_prefer_line_with_aliases() -> None:
    """PREFER line is generated for terms that have aliases."""
    terms = [
        _term("t1", "光明骑士", aliases=["圣骑士", "Light Knight"]),
    ]
    result = build_term_block(terms, context_hits=[], inject_terms=True)
    assert "PREFER:" in result
    assert "光明骑士" in result
    assert "圣骑士" in result


def test_no_prefer_line_without_aliases() -> None:
    """Terms without aliases don't produce a PREFER line."""
    terms = [_term("t1", "凋零", forbidden=["死亡"])]
    result = build_term_block(terms, context_hits=[], inject_terms=True)
    assert "MUST NOT use:" in result
    assert "PREFER:" not in result


def test_no_output_when_no_forbidden_and_no_aliases() -> None:
    """A term with neither forbidden nor aliases produces no block."""
    terms = [_term("t1", "some_canonical")]  # no forbidden, no aliases
    result = build_term_block(terms, context_hits=[], inject_terms=True)
    assert result == ""


def test_context_hits_match_by_canonical() -> None:
    """In >20 case, match by canonical."""
    terms = [_term(f"t{i}", f"canonical_{i}", forbidden=[f"forb_{i}"]) for i in range(25)]
    # Match t3 by canonical
    context_hits = ["canonical_3"]
    result = build_term_block(terms, context_hits=context_hits, inject_terms=True)
    assert "forb_3" in result
    assert "forb_0" not in result


def test_context_hits_match_by_alias() -> None:
    """In >20 case, match by alias."""
    terms = [
        _term(f"t{i}", f"canon_{i}", aliases=[f"alias_{i}"], forbidden=[f"forb_{i}"])
        for i in range(25)
    ]
    context_hits = ["alias_7"]
    result = build_term_block(terms, context_hits=context_hits, inject_terms=True)
    assert "forb_7" in result
    assert "forb_0" not in result


def test_forbidden_deduplication() -> None:
    """Duplicate forbidden words across terms are deduplicated."""
    terms = [
        _term("t1", "A", forbidden=["bad_word", "other"]),
        _term("t2", "B", forbidden=["bad_word", "extra"]),
    ]
    result = build_term_block(terms, context_hits=[], inject_terms=True)
    # bad_word should appear exactly once
    assert result.count("bad_word") == 1


# ---------------------------------------------------------------------------
# build_term_block_for_critic
# ---------------------------------------------------------------------------

def test_critic_block_le20_injects_all() -> None:
    """Critic: <= 20 terms -> inject all."""
    terms = [_term("t1", "凋零", forbidden=["死亡"])]
    result = build_term_block_for_critic(terms)
    assert "MUST NOT use:" in result
    assert "死亡" in result


def test_critic_block_gt20_only_forbidden_terms() -> None:
    """[硬] Critic: > 20 terms -> only inject terms with non-empty forbidden."""
    # 25 terms: first 3 have forbidden, rest don't
    terms = [
        _term("t0", "canon_0", forbidden=["bad_0"]),
        _term("t1", "canon_1", forbidden=["bad_1"]),
        _term("t2", "canon_2", forbidden=["bad_2"]),
    ] + [
        _term(f"t{i}", f"canon_{i}", aliases=[f"alias_{i}"])  # no forbidden
        for i in range(3, 25)
    ]
    result = build_term_block_for_critic(terms)
    assert "bad_0" in result
    assert "bad_1" in result
    assert "bad_2" in result
    # prefer-canonical-only terms should not appear in MUST NOT
    for i in range(3, 25):
        assert f"alias_{i}" not in result.split("MUST NOT use:")[-1].split("\n")[0]


def test_critic_block_empty_forbidden_no_output_gt20() -> None:
    """Critic: > 20 terms, none with forbidden -> return ""."""
    terms = [
        _term(f"t{i}", f"canon_{i}", aliases=[f"alias_{i}"])
        for i in range(25)
    ]
    result = build_term_block_for_critic(terms)
    assert result == ""


def test_vocabulary_block_header() -> None:
    """Block starts with [vocabulary-constraints]."""
    terms = [_term("t1", "凋零", forbidden=["死亡"])]
    result = build_term_block(terms, context_hits=[], inject_terms=True)
    assert result.startswith("[vocabulary-constraints]")
