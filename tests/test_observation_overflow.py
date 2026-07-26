from __future__ import annotations

import pytest

from owcopilot.llm.tokenizer import count_tokens, reset_encoder_cache
from owcopilot.skills import SkillError
from owcopilot.skills.observation_overflow import (
    bind_overflow,
    read_observation,
    truncate_observation,
)


def test_read_observation_restores_full_text() -> None:
    # The restore path returns the exact original text stored before truncation (Manus principle ③:
    # compression must be restorable), plus its length — no lossy paraphrase.
    store = {"obs-0-0": "FULL" * 5000}
    with bind_overflow(store):
        out = read_observation("obs-0-0")
        assert out["content"] == store["obs-0-0"]
        assert out["chars"] == len(store["obs-0-0"])


def test_read_observation_unknown_id_is_guided_error() -> None:
    # A guided, actionable error (Anthropic tool-writing guidance): it names the unknown id AND
    # lists the valid ones so the model's next move is spelled out.
    with bind_overflow({"obs-0-0": "x"}):
        with pytest.raises(SkillError, match="unknown obs_id 'nope'") as exc:
            read_observation("nope")
    assert "obs-0-0" in str(exc.value)


def test_read_observation_outside_run_is_guided_error() -> None:
    # No bound store (outside any agent run) → a guided error, never a raw crash.
    with pytest.raises(SkillError, match="only available during an agent run"):
        read_observation("obs-0-0")


def test_bind_overflow_resets_contextvar() -> None:
    # The ContextVar token is reset on block exit, so a later read_observation sees no stale store
    # (no cross-run leak) and raises the outside-run error.
    with bind_overflow({"obs-0-0": "x"}):
        pass
    with pytest.raises(SkillError, match="only available during an agent run"):
        read_observation("obs-0-0")


def test_untruncated_observation_byte_identical() -> None:
    # T5 back-compat (byte level): a small dump under BOTH the token and char limits comes back
    # exactly (raw_dump, False) with no store write — the untruncated path is unchanged.
    store: dict[str, str] = {}
    raw = '{"ok": true, "open_errors": 0, "entities": ["npc_aldric"]}'
    out, truncated = truncate_observation(store, raw, 0, token_limit=1000, char_cap=8000)
    assert out == raw
    assert truncated is False
    assert store == {}


def test_observation_truncation_is_token_not_char() -> None:
    # T5 core behavior: truncation is driven by TOKENS, not chars. Two dumps of the SAME char
    # length, one CJK one English, sit on opposite sides of the token limit. With a large char_cap
    # (so the char ceiling never binds) only the token-dense CJK dump truncates. Old char-based
    # logic would have truncated BOTH identically at L chars (or neither).
    length = 200
    cjk = ("在遥远古老的王国里住着骑士与巨龙" * 20)[:length]
    eng = ("the quick brown fox jumps over a lazy dog " * 10)[:length]
    assert len(cjk) == len(eng) == length

    token_limit = 100
    # Regime precondition (via production count_tokens): CJK is over the limit, English is under it,
    # despite identical char length — this is exactly the char-vs-token drift T5 fixes.
    assert count_tokens(cjk) > token_limit >= count_tokens(eng)

    cjk_store: dict[str, str] = {}
    _, cjk_truncated = truncate_observation(
        cjk_store, cjk, 0, token_limit=token_limit, char_cap=10_000
    )
    eng_store: dict[str, str] = {}
    _, eng_truncated = truncate_observation(
        eng_store, eng, 0, token_limit=token_limit, char_cap=10_000
    )

    assert cjk_truncated is True
    assert eng_truncated is False
    assert cjk_store == {"obs-0-0": cjk}  # full CJK text stored, recoverable
    assert eng_store == {}  # English fit, nothing stored


def test_truncate_observation_char_cap_is_secondary_ceiling() -> None:
    # The char cap still binds as a safety ceiling even when the token count is UNDER the token
    # limit. A run of identical bytes packs ~8 chars/token in cl100k, so 6000 chars is only ~750
    # tokens — under a 1000-token limit yet over a 4000-char cap. The char ceiling forces
    # truncation and the full text stays recoverable.
    store: dict[str, str] = {}
    raw = "x" * 6000
    assert count_tokens(raw) <= 1000  # under the token limit ...
    out, truncated = truncate_observation(store, raw, 0, token_limit=1000, char_cap=4000)
    assert truncated is True  # ... but the char ceiling binds
    assert len(out) >= 4000  # head clamped to char_cap plus the marker suffix
    assert store["obs-0-0"] == raw  # full text recoverable


def test_truncate_observation_fallback_no_tiktoken(monkeypatch) -> None:
    # T5: with tiktoken forced unavailable, truncate_observation still truncates an oversized dump
    # (via the ÷4 fallback) and stores the full text recoverably — never a silent pass-through.
    import owcopilot.llm.tokenizer as tok_mod

    reset_encoder_cache()
    monkeypatch.setattr(tok_mod, "_ENCODER", tok_mod._FALLBACK_SENTINEL)
    try:
        store: dict[str, str] = {}
        raw = "y" * 1000  # fallback count = max(1, 1000//4) = 250 tokens
        out, truncated = truncate_observation(store, raw, 2, token_limit=10, char_cap=10_000)
        assert truncated is True
        assert "read_observation" in out
        assert store["obs-2-0"] == raw  # full text recoverable under fallback
    finally:
        reset_encoder_cache()
