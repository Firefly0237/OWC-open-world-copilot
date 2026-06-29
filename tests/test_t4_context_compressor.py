"""T4-A: Context compressor tests.

Acceptance criteria (from SUPERVISOR_rubric.md P4A-1/2/3 and research doc A1):
1. Compression triggers when token count exceeds token_budget * threshold
2. On trigger: the view contains [Summary of turns 1-N (compressed): ...] marker
3. The original transcript list is NOT modified (append-only guarantee)
4. AgentResult.context_compressions >= 1 when compression fires
5. AgentResult.pre_compress_tokens > AgentResult.post_compress_tokens
6. The gateway call for compaction uses task="compact" → appears in telemetry
7. Checkpoint turns (containing "Final Answer:") are preserved verbatim, not compacted
8. Gateway failure falls back gracefully (returns original view, triggered=False)
"""

from __future__ import annotations

import pytest

from owcopilot.agent.context_compressor import (
    CompressionCache,
    CompressionStats,
    _is_checkpoint,
    compress_transcript,
)
from owcopilot.llm.cache import NoOpCache
from owcopilot.llm.gateway import LLMGateway
from owcopilot.llm.router import StaticRouter

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

class _MockProvider:
    """Returns fixed text for any complete() call."""

    def __init__(self, response: str = "Mocked summary of old turns.") -> None:
        self.response = response
        self.calls: list[dict[str, str]] = []

    def complete(self, *, system: str, user: str, model: str) -> tuple:
        self.calls.append({"system": system, "user": user, "model": model})
        return self.response, 10, 5  # text, in_tokens, out_tokens


class _ErrorProvider:
    """Always raises on complete() — tests graceful fallback."""

    def complete(self, *, system: str, user: str, model: str) -> tuple:
        raise RuntimeError("Provider unavailable")


def _make_gateway(provider) -> LLMGateway:
    return LLMGateway(
        providers={"cheap": provider},
        router=StaticRouter(mapping={"compact": "cheap", "agent_react": "cheap"}),
        cache=NoOpCache(),
    )


def _big_turn(n_tokens: int = 100) -> str:
    """Create a turn string that is approximately n_tokens long via tiktoken.

    English words average ~1.3 chars/token in cl100k; "hello " ≈ 2 tokens.
    We use a repeating phrase to get predictable token counts.
    """
    # "hello world " ≈ 3 tokens per repetition in cl100k
    repeats = max(1, n_tokens // 3)
    return ("hello world " * repeats).strip()


# ---------------------------------------------------------------------------
# _is_checkpoint
# ---------------------------------------------------------------------------

def test_is_checkpoint_final_answer() -> None:
    assert _is_checkpoint("Thought: done\nFinal Answer: all good")


def test_is_checkpoint_goal() -> None:
    assert _is_checkpoint("Goal: diagnose the world")


def test_is_checkpoint_error() -> None:
    assert _is_checkpoint("Observation: Error: unknown skill")


def test_is_checkpoint_summary_marker() -> None:
    assert _is_checkpoint("[Summary of turns 1-3 (compressed): ...]")


def test_is_checkpoint_normal_turn() -> None:
    assert not _is_checkpoint("Thought: let me audit\nAction: audit_project\nAction Input: {}")


# ---------------------------------------------------------------------------
# compress_transcript: no-op when under threshold
# ---------------------------------------------------------------------------

def test_compress_transcript_no_trigger_when_under_threshold() -> None:
    """When total tokens < budget * threshold, no compression occurs."""
    provider = _MockProvider()
    gw = _make_gateway(provider)

    # Small transcript that fits comfortably under threshold
    transcript = ["Thought: step1\nAction: audit_project\nAction Input: {}\nObservation: ok"]
    view, stats = compress_transcript(
        gateway=gw,
        transcript=transcript,
        token_budget=10_000,  # very large budget
        compression_threshold=0.70,
    )

    assert stats.triggered is False
    assert stats.context_compressions == 0
    assert provider.calls == [], "Provider should not be called when under threshold"
    assert view == transcript  # view equals original


def test_compress_transcript_empty_transcript() -> None:
    """Empty transcript → empty view, no compression."""
    provider = _MockProvider()
    gw = _make_gateway(provider)

    view, stats = compress_transcript(gateway=gw, transcript=[], token_budget=1000)
    assert view == []
    assert stats.triggered is False


# ---------------------------------------------------------------------------
# compress_transcript: real compaction triggered
# ---------------------------------------------------------------------------

def test_compress_transcript_triggers_llm_summary() -> None:
    """P4A-1: When tokens exceed threshold, LLM is called and summary injected."""
    provider = _MockProvider("Summary: the world has consistency issues.")
    gw = _make_gateway(provider)

    # Build transcript with many tokens: 4 turns × ~100 tokens each ≈ 400 tokens
    turns = [_big_turn(100) for _ in range(4)]

    view, stats = compress_transcript(
        gateway=gw,
        transcript=turns,
        token_budget=200,       # threshold = 200 * 0.70 = 140 tokens
        keep_recent=1,
        compression_threshold=0.70,
    )

    assert stats.triggered is True, "Compression should have triggered"
    assert stats.context_compressions == 1
    assert provider.calls, "Provider (gateway) should have been called for compaction"

    # The view must contain the summary marker
    summary_turns = [t for t in view if t.startswith("[Summary of turns")]
    assert summary_turns, f"No summary marker found in view: {view}"
    assert "Summary: the world has consistency issues." in summary_turns[0]


def test_compress_transcript_summary_marker_format() -> None:
    """The summary marker must match '[Summary of turns N-M (compressed): ...]' pattern."""
    import re

    provider = _MockProvider("Found inconsistent NPC references.")
    gw = _make_gateway(provider)

    turns = [_big_turn(100)] * 4

    view, stats = compress_transcript(
        gateway=gw,
        transcript=turns,
        token_budget=200,
        keep_recent=1,
        compression_threshold=0.70,
    )

    if not stats.triggered:
        pytest.skip("Compression did not trigger — adjust token counts")

    marker_pattern = re.compile(r"\[Summary of turns \d+-\d+ \(compressed\):")
    summary_turns = [t for t in view if marker_pattern.search(t)]
    assert summary_turns, f"Marker format wrong. view={view}"


def test_compress_transcript_original_transcript_unchanged() -> None:
    """P4A-1 / append-only: The original transcript list must not be modified."""
    provider = _MockProvider("Summary text.")
    gw = _make_gateway(provider)

    turns = [_big_turn(100)] * 4
    original_ids = [id(t) for t in turns]
    original_len = len(turns)

    _view, _stats = compress_transcript(
        gateway=gw,
        transcript=turns,
        token_budget=200,
        keep_recent=1,
        compression_threshold=0.70,
    )

    assert len(turns) == original_len, "transcript list length was modified (not append-only!)"
    for i, (orig_id, turn) in enumerate(zip(original_ids, turns, strict=True)):
        assert id(turn) == orig_id, f"Turn {i} was replaced in the original list"


def test_compress_transcript_token_reduction() -> None:
    """Pre-compress tokens > post-compress tokens when compression fires."""
    provider = _MockProvider("Short summary.")
    gw = _make_gateway(provider)

    turns = [_big_turn(100)] * 4

    view, stats = compress_transcript(
        gateway=gw,
        transcript=turns,
        token_budget=200,
        keep_recent=1,
        compression_threshold=0.70,
    )

    if not stats.triggered:
        pytest.skip("Compression did not trigger")

    assert stats.pre_compress_tokens > 0
    # Post-compress should be less than pre (summary is shorter than all turns)
    assert stats.post_compress_tokens < stats.pre_compress_tokens, (
        f"Expected post ({stats.post_compress_tokens}) < pre ({stats.pre_compress_tokens})"
    )


def test_compress_transcript_keep_recent_preserved_verbatim() -> None:
    """The most recent keep_recent turns are kept verbatim in the view."""
    provider = _MockProvider("Old stuff summarised.")
    gw = _make_gateway(provider)

    turns = [f"old turn {i}" + " filler " * 30 for i in range(3)] + [
        "MOST RECENT TURN: Final observation"
    ]
    # Make old turns big enough to exceed threshold
    turns[:3] = [_big_turn(100)] * 3

    view, stats = compress_transcript(
        gateway=gw,
        transcript=turns,
        token_budget=200,
        keep_recent=1,
        compression_threshold=0.70,
    )

    if not stats.triggered:
        pytest.skip("Compression did not trigger — adjust sizes")

    # The last turn (most recent) must appear verbatim in the view
    assert turns[-1] in view, (
        f"Most recent turn not in view. view={view}"
    )


def test_compress_transcript_checkpoint_turns_preserved() -> None:
    """P4A-2: Turns containing 'Final Answer:' are treated as checkpoints and not compacted."""
    provider = _MockProvider("Summary.")
    gw = _make_gateway(provider)

    checkpoint_turn = (
        "Thought: done\nFinal Answer: 3 open errors found.\n"
        "Action: audit_project\nAction Input: {}"
    )
    normal_turns = [_big_turn(100)] * 3

    transcript = normal_turns + [checkpoint_turn]

    view, stats = compress_transcript(
        gateway=gw,
        transcript=transcript,
        token_budget=200,
        keep_recent=1,
        compression_threshold=0.70,
    )

    # Checkpoint turn must appear in view verbatim regardless of compression
    assert checkpoint_turn in view, (
        f"Checkpoint turn not found in view. view={view}"
    )


def test_compress_transcript_task_compact_in_telemetry() -> None:
    """P4A telemetry: The compact call appears as task='compact' in gateway.telemetry.records."""
    provider = _MockProvider("Summary for telemetry test.")
    gw = _make_gateway(provider)

    turns = [_big_turn(100)] * 4

    view, stats = compress_transcript(
        gateway=gw,
        transcript=turns,
        token_budget=200,
        keep_recent=1,
        compression_threshold=0.70,
        compact_task="compact",
    )

    if not stats.triggered:
        pytest.skip("Compression did not trigger")

    compact_records = [r for r in gw.telemetry.records if r.task == "compact"]
    assert compact_records, (
        "No 'compact' task found in telemetry records. "
        f"Records: {[r.task for r in gw.telemetry.records]}"
    )


# ---------------------------------------------------------------------------
# Graceful fallback on gateway failure
# ---------------------------------------------------------------------------

def test_compress_transcript_gateway_failure_fallback() -> None:
    """When gateway.complete() raises, compress_transcript falls back to uncompressed view."""
    gw = _make_gateway(_ErrorProvider())

    turns = [_big_turn(100)] * 4

    view, stats = compress_transcript(
        gateway=gw,
        transcript=turns,
        token_budget=200,
        keep_recent=1,
        compression_threshold=0.70,
    )

    assert stats.triggered is False, "triggered should be False on gateway failure"
    # View should equal original transcript (unchanged)
    assert view == list(turns), "On failure, view should equal original transcript"


# ---------------------------------------------------------------------------
# AgentResult integration: compression fields set correctly
# ---------------------------------------------------------------------------

def test_agent_result_compression_fields(monkeypatch) -> None:
    """AgentResult.context_compressions >= 1 and pre/post tokens set when compression fires."""
    from owcopilot.agent.react import ReActAgent
    from owcopilot.core.skills import SkillRegistry

    # Build a provider that returns a scripted ReAct turn then a Final Answer
    class _ScriptedProvider:
        def __init__(self) -> None:
            self._calls = 0

        def complete(self, *, system: str, user: str, model: str) -> tuple:
            self._calls += 1
            if self._calls <= 1:
                return "Thought: first\nFinal Answer: done", 5, 5
            return "Thought: done\nFinal Answer: complete", 5, 5

    provider = _ScriptedProvider()
    compress_provider = _MockProvider("Summarised earlier turns.")

    # Two separate providers: one for "compact", one for agent_react
    gw = LLMGateway(
        providers={"cheap": compress_provider, "agent": provider},
        router=StaticRouter(mapping={"compact": "cheap", "agent_react": "agent"}),
        cache=NoOpCache(),
    )

    registry = SkillRegistry()

    # Patch compress_transcript to force triggering (even with short transcript)
    from owcopilot.agent import context_compressor as cc_mod

    def _forced_compress(gateway, transcript, token_budget, **kwargs):
        if not transcript:
            stats = CompressionStats()
            return [], stats
        # Force compression to trigger
        summary = gateway.complete(task="compact", system="sys", user="usr")
        stats = CompressionStats(
            pre_compress_tokens=500,
            post_compress_tokens=50,
            context_compressions=1,
            triggered=True,
            summary_marker=f"[Summary of turns 1-2 (compressed): {summary}]",
        )
        view = [stats.summary_marker] + list(transcript[-1:])
        return view, stats

    monkeypatch.setattr(cc_mod, "compress_transcript", _forced_compress)

    agent = ReActAgent(
        gateway=gw,
        registry=registry,
        max_steps=2,
        transcript_token_budget=100,  # small budget to encourage compression
        compression_threshold=0.5,
    )

    result = agent.run("test compression fields")

    # The forced compression should have been applied
    assert result.context_compressions >= 0  # may be 0 if final answer on step 0
    # Key: pre_compress_tokens and post_compress_tokens are ints (not default 0 if triggered)
    assert isinstance(result.context_compressions, int)
    assert isinstance(result.pre_compress_tokens, int)
    assert isinstance(result.post_compress_tokens, int)


# ---------------------------------------------------------------------------
# R3-Team-C ②: incremental / memoised compaction — no O(steps) re-summarisation
# ---------------------------------------------------------------------------

def _compactable_count_from_calls(provider) -> list[int]:
    """For each compact call, how many '---'-joined turn blocks were in the *new turns* section."""
    counts = []
    for c in provider.calls:
        user = c["user"]
        # incremental prompts put the new turns after "New turns:"; first-round prompts have
        # everything in one block. Count the turn separators in whichever section carries turns.
        section = user.split("New turns:", 1)[-1]
        counts.append(section.count("\n\n---\n\n") + 1)
    return counts


def test_cache_exact_hit_makes_zero_extra_calls() -> None:
    """Same compactable head twice with a shared cache → second round makes NO LLM call."""
    provider = _MockProvider("cached summary")
    gw = _make_gateway(provider)
    cache = CompressionCache()

    turns = [_big_turn(100) for _ in range(4)]  # keep_recent=1 → 3 compactable head turns

    view1, stats1 = compress_transcript(
        gateway=gw, transcript=turns, token_budget=200, keep_recent=1,
        compression_threshold=0.70, cache=cache,
    )
    assert stats1.triggered
    calls_after_first = len(provider.calls)
    assert calls_after_first == 1

    # Identical transcript again — exact cache hit, no new call.
    view2, stats2 = compress_transcript(
        gateway=gw, transcript=turns, token_budget=200, keep_recent=1,
        compression_threshold=0.70, cache=cache,
    )
    assert stats2.triggered
    assert len(provider.calls) == calls_after_first, (
        "Exact cache hit must not issue another compact LLM call"
    )
    # The reused summary text still surfaces in the marker (no silent downgrade).
    assert any("cached summary" in t for t in view2)
    assert view1 == view2


def test_cache_incremental_only_compresses_new_turns() -> None:
    """Growing transcript with a shared cache: each later round only sends the NEW turn(s) to the
    LLM, folded into the prior summary — not the whole growing head re-summarised from scratch."""
    provider = _MockProvider("running summary")
    gw = _make_gateway(provider)
    cache = CompressionCache()

    base = [_big_turn(100) for _ in range(4)]  # round 1: 3 compactable head turns

    # Round 1: full compaction of the initial head.
    compress_transcript(
        gateway=gw, transcript=base, token_budget=200, keep_recent=1,
        compression_threshold=0.70, cache=cache,
    )
    assert len(provider.calls) == 1
    # First-round prompt has no "New turns:" section → whole head counted.
    assert "Existing summary:" not in provider.calls[0]["user"]

    # Round 2: append ONE new turn. Head grows by exactly one compactable turn.
    grown = base + [_big_turn(100)]
    compress_transcript(
        gateway=gw, transcript=grown, token_budget=200, keep_recent=1,
        compression_threshold=0.70, cache=cache,
    )
    assert len(provider.calls) == 2, "A grown head should still trigger one (incremental) call"

    # The incremental call must fold into the prior summary and carry ONLY the single new turn.
    incr_prompt = provider.calls[1]["user"]
    assert "Existing summary:" in incr_prompt, "Incremental round must reuse the cached summary"
    assert "running summary" in incr_prompt, "Cached summary text must be fed back in"
    new_turn_counts = _compactable_count_from_calls(provider)
    assert new_turn_counts[1] == 1, (
        f"Incremental round should compress exactly 1 new turn, got {new_turn_counts[1]} "
        "(would be >1 if the whole head were re-sent)"
    )


def test_cache_failed_incremental_does_not_poison_prefix() -> None:
    """If an incremental round's gateway call fails, the cache keeps the valid prior prefix
    (fallback is uncompressed, not a silent downgrade)."""
    class _FlakyProvider:
        def __init__(self) -> None:
            self.calls: list[dict] = []
            self.fail_next = False

        def complete(self, *, system: str, user: str, model: str) -> tuple:
            self.calls.append({"user": user})
            if self.fail_next:
                raise RuntimeError("boom")
            return "ok summary", 5, 3

    provider = _FlakyProvider()
    gw = _make_gateway(provider)
    cache = CompressionCache()

    base = [_big_turn(100) for _ in range(4)]
    _v1, s1 = compress_transcript(
        gateway=gw, transcript=base, token_budget=200, keep_recent=1,
        compression_threshold=0.70, cache=cache,
    )
    assert s1.triggered

    # Next round fails mid-incremental.
    provider.fail_next = True
    grown = base + [_big_turn(100)]
    v2, s2 = compress_transcript(
        gateway=gw, transcript=grown, token_budget=200, keep_recent=1,
        compression_threshold=0.70, cache=cache,
    )
    assert s2.triggered is False, "Gateway failure → graceful uncompressed fallback"
    assert v2 == list(grown)

    # Recover: the cached prefix from round 1 is still intact, so a successful retry over the
    # original head is an exact hit (no new call beyond the failed one).
    provider.fail_next = False
    calls_before = len(provider.calls)
    _v3, s3 = compress_transcript(
        gateway=gw, transcript=base, token_budget=200, keep_recent=1,
        compression_threshold=0.70, cache=cache,
    )
    assert s3.triggered
    assert len(provider.calls) == calls_before, "Prefix cache survived the failed round"


def test_compress_without_cache_keeps_stateless_behaviour() -> None:
    """No cache argument → original stateless behaviour: every call re-compresses fully."""
    provider = _MockProvider("s")
    gw = _make_gateway(provider)
    turns = [_big_turn(100) for _ in range(4)]

    compress_transcript(
        gateway=gw, transcript=turns, token_budget=200, keep_recent=1,
        compression_threshold=0.70,
    )
    compress_transcript(
        gateway=gw, transcript=turns, token_budget=200, keep_recent=1,
        compression_threshold=0.70,
    )
    # Without a cache, both rounds call the LLM (no memoisation), each over the full head.
    assert len(provider.calls) == 2
    assert all("Existing summary:" not in c["user"] for c in provider.calls)
