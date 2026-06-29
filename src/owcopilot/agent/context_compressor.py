"""Read-time context compressor for the ReAct transcript.

Design principles (Claude Code 5-level compaction, Level-2 adapted)
---------------------------------------------------------------------
* **Append-only source**: The raw ``transcript`` list is never mutated; compression
  happens only when building the *read-time view* passed to the prompt.
* **LLM micro-compact summarisation**: Oldest turns are sent to the gateway with
  ``task="compact"`` and replaced in the view by a single summary block with an
  explicit ``[Summary of turns 1-N (compressed): ...]`` marker — never silently dropped.
* **Read-time projection**: Token counts before and after compression are recorded and
  returned in :class:`CompressionStats` so billing and progress tracking stay accurate.
* **Recursion guard**: The gateway call uses ``task="compact"`` which the agent loop
  (``task="agent_react"``) does not set, so the compressor cannot trigger itself.
* **Telemetry**: The ``gateway.complete(task="compact", ...)`` call is routed through the
  normal gateway path and is therefore automatically counted in ``TelemetryCollector``.

Compression trigger
-------------------
Compression fires when the total token count of the full transcript exceeds
``token_budget * threshold`` (default 0.70).  The oldest ``head_turns`` (default: all
turns except the most recent ``keep_recent`` ones, minimum 1 turn) are compacted into a
single summary.  The most recent ``keep_recent`` turns are always preserved verbatim.

If compression is triggered but the gateway call fails (e.g. in tests that inject an
error), the function falls back gracefully to the original uncompressed view with
``CompressionStats.triggered = False``.

Checkpoint awareness (P4A-2)
-----------------------------
Turns that contain a checkpoint signal (``Final Answer:`` text, goal-declaration
patterns, or explicit error-recovery markers) are **never included in the summary
batch** — they are preserved verbatim ahead of the summary block.  This prevents
information loss at critical decision points.

Usage
-----
    from owcopilot.agent.context_compressor import compress_transcript, CompressionStats

    stats: CompressionStats
    view, stats = compress_transcript(
        gateway=gw,
        transcript=transcript,      # the append-only list from ReActAgent
        token_budget=8_000,
        keep_recent=3,
        compression_threshold=0.70,
    )
    # view is a list[str] to substitute for transcript in _user_prompt()
    # stats.pre_compress_tokens / post_compress_tokens / context_compressions are
    # available for AgentResult fields.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from ..llm.gateway import LLMGateway
from ..llm.tokenizer import count_tokens

_log = logging.getLogger(__name__)

# Patterns that mark a turn as a checkpoint — these are never included in the compaction
# batch and are instead preserved verbatim in the view.
_CHECKPOINT_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"Final Answer:", re.IGNORECASE),
    re.compile(r"\bGoal\s*:", re.IGNORECASE),
    re.compile(r"\bError\s*:", re.IGNORECASE),
    re.compile(r"\[Summary of turns", re.IGNORECASE),  # already-compressed marker
]

_COMPACT_SYSTEM_PROMPT = (
    "You are a transcript compressor. Produce a concise factual summary of the provided "
    "agent turns. Preserve: goals, errors encountered, key facts discovered, and pending "
    "tasks. Omit redundant observations, repeated tool calls, and filler reasoning. "
    "Output only the summary text — no preamble, no labels."
)


def _is_checkpoint(turn: str) -> bool:
    """Return True if *turn* should never be included in the compaction batch."""
    return any(p.search(turn) for p in _CHECKPOINT_PATTERNS)


def _token_count_list(turns: list[str]) -> int:
    """Sum token counts for a list of turn strings."""
    return sum(count_tokens(t) for t in turns)


@dataclass
class CompressionStats:
    """Token accounting and compression metadata from one compress_transcript() call."""

    pre_compress_tokens: int = 0
    post_compress_tokens: int = 0
    # Number of compression rounds performed (0 if no compression triggered).
    context_compressions: int = 0
    # True iff the LLM summarisation actually ran (gate opened and call succeeded).
    triggered: bool = False
    # The marker string injected into the view (empty if no compression).
    summary_marker: str = ""

    @property
    def compression_ratio(self) -> float:
        """post / pre ratio (1.0 = no compression; 0.0 = total compression)."""
        if self.pre_compress_tokens == 0:
            return 1.0
        return self.post_compress_tokens / self.pre_compress_tokens


class CompressionCache:
    """Memoises the compacted prefix across compression rounds of one ReAct run.

    Why this exists
    ---------------
    Compression is read-time and runs once per agent step.  The transcript is **append-only**,
    so the set of compactable head turns only ever *grows* by appending.  Without caching, every
    step past the threshold re-summarises the entire (growing) head from scratch — O(steps)
    redundant ``task="compact"`` LLM calls, each re-doing the work of the previous one over
    overlapping content (a real token+latency cost amplification).

    With this cache, a round whose compactable head *extends* a previously-summarised prefix only
    needs to fold the **new** turns into the cached summary (incremental compaction), and a round
    whose compactable head is *identical* to a cached one makes **zero** LLM calls.

    Semantics preserved
    -------------------
    * Append-only: the cache only ever keys on prefixes of the compactable turns; it never mutates
      the transcript.
    * Not a silent downgrade: every round still produces an explicit ``[Summary of turns ...]``
      marker; the only thing cached is the (deterministic-input) summary text, so behaviour and
      stats stay honest.
    """

    def __init__(self) -> None:
        # The compactable turns that produced the cached summary, and that summary text.
        self._cached_turns: tuple[str, ...] = ()
        self._cached_summary: str = ""

    def lookup(self, compactable: list[str]) -> tuple[str | None, list[str]]:
        """Resolve *compactable* against the cache.

        Returns ``(reuse_summary, to_compress)``:
        * ``(summary, [])``        — exact cache hit; reuse summary verbatim, no LLM call.
        * ``(prev_summary, new)``  — *compactable* extends the cached prefix; fold ``new`` turns
          into ``prev_summary`` (one incremental LLM call over a bounded input).
        * ``(None, compactable)``  — no usable prefix (first round, or the head diverged, e.g.
          a checkpoint shifted which turns are compactable); compress everything.
        """
        cur = tuple(compactable)
        if self._cached_turns and cur == self._cached_turns:
            return self._cached_summary, []
        if self._cached_turns and _is_prefix(self._cached_turns, cur):
            return self._cached_summary, list(cur[len(self._cached_turns):])
        return None, list(cur)

    def store(self, compactable: list[str], summary: str) -> None:
        """Record that *compactable* was summarised into *summary*."""
        self._cached_turns = tuple(compactable)
        self._cached_summary = summary


def _is_prefix(prefix: tuple[str, ...], whole: tuple[str, ...]) -> bool:
    """True iff *prefix* is a proper leading slice of *whole*."""
    return len(prefix) < len(whole) and whole[: len(prefix)] == prefix


def compress_transcript(
    gateway: LLMGateway,
    transcript: list[str],
    token_budget: int,
    *,
    keep_recent: int = 3,
    compression_threshold: float = 0.70,
    compact_task: str = "compact",
    compact_tier: str | None = None,
    cache: CompressionCache | None = None,
) -> tuple[list[str], CompressionStats]:
    """Return a read-time view of *transcript* with oldest turns LLM-summarised if needed.

    Parameters
    ----------
    gateway:
        The LLM gateway used for the micro-compact call (``task="compact"``).  The call is
        recorded by the gateway's own TelemetryCollector automatically.
    transcript:
        The agent's append-only turn list.  This list is **never modified**.
    token_budget:
        The total token budget for the transcript view (corresponds to
        ``transcript_token_budget`` on ReActAgent).
    keep_recent:
        Minimum number of most-recent turns to always preserve verbatim.
    compression_threshold:
        Fraction of ``token_budget`` that triggers compression (default 0.70 = 70 %).
    compact_task:
        Task label for the gateway call — allows cost attribution without interfering with
        the main agent task ("agent_react").
    compact_tier:
        Optional tier override for the compact call (e.g. "cheap").  None = router decides.
    cache:
        Optional :class:`CompressionCache` carried across steps of one run.  When supplied, a
        round whose compactable head extends the previously-summarised prefix only folds the new
        turns into the cached summary (incremental), and an identical head makes zero LLM calls —
        eliminating the O(steps) redundant re-summarisation of overlapping content.  When None,
        every round compresses the full head from scratch (the original stateless behaviour).

    Returns
    -------
    (view, stats) where *view* is the list of strings to substitute for *transcript* in
    the prompt, and *stats* carries token accounting and compression metadata.
    """
    stats = CompressionStats()

    if not transcript:
        return list(transcript), stats

    stats.pre_compress_tokens = _token_count_list(transcript)

    # Compression gate: only trigger when tokens exceed threshold
    trigger_tokens = int(token_budget * compression_threshold)
    if stats.pre_compress_tokens <= trigger_tokens:
        stats.post_compress_tokens = stats.pre_compress_tokens
        return list(transcript), stats

    # Split transcript: [head_turns (candidates for compaction)] + [tail_turns (kept verbatim)]
    keep_recent_clamped = max(1, min(keep_recent, len(transcript)))
    tail_turns = transcript[-keep_recent_clamped:]
    head_turns = transcript[: len(transcript) - keep_recent_clamped]

    if not head_turns:
        # Nothing to compact — all turns are in the protected recent window.
        stats.post_compress_tokens = stats.pre_compress_tokens
        return list(transcript), stats

    # Separate checkpoint turns (must preserve verbatim) from compactable turns.
    checkpoint_turns: list[str] = []
    compactable_turns: list[str] = []
    for turn in head_turns:
        if _is_checkpoint(turn):
            checkpoint_turns.append(turn)
        else:
            compactable_turns.append(turn)

    if not compactable_turns:
        # Every head turn is a checkpoint; nothing safe to compact.
        stats.post_compress_tokens = stats.pre_compress_tokens
        return list(transcript), stats

    n_compacted = len(compactable_turns)

    # Resolve against the cache: reuse a prior summary outright, fold only new turns into it, or
    # (no cache / divergent head) compress everything. `to_compress` is what actually goes to the
    # LLM this round; `reuse_summary` (if any) is the cached summary of the unchanged prefix.
    reuse_summary, to_compress = (
        cache.lookup(compactable_turns) if cache is not None else (None, list(compactable_turns))
    )

    summary_text: str | None
    if not to_compress and reuse_summary is not None:
        # Exact cache hit — no LLM call this round; reuse the cached summary verbatim.
        summary_text = reuse_summary
    else:
        summary_text = _run_compaction(
            gateway=gateway,
            reuse_summary=reuse_summary,
            to_compress=to_compress,
            compact_task=compact_task,
            compact_tier=compact_tier,
        )
        if summary_text is None:
            # Gateway failed — graceful fallback to the uncompressed view (not a silent downgrade:
            # the caller sees triggered=False and the full transcript).
            stats.post_compress_tokens = stats.pre_compress_tokens
            return list(transcript), stats
        if cache is not None:
            cache.store(compactable_turns, summary_text)

    # Build the summary marker — visible in the prompt so the model is never surprised.
    first_idx = 1
    last_idx = n_compacted
    summary_marker = f"[Summary of turns {first_idx}-{last_idx} (compressed): {summary_text}]"
    stats.summary_marker = summary_marker

    # Assemble the read-time view: checkpoint_turns + [summary_marker] + tail_turns
    view: list[str] = checkpoint_turns + [summary_marker] + tail_turns

    stats.post_compress_tokens = _token_count_list(view)
    stats.context_compressions = 1
    stats.triggered = True

    _log.info(
        "context_compressor: compressed %d turns → summary+%d tail turns "
        "(%d → %d tokens, %.0f%% reduction).",
        n_compacted,
        len(tail_turns),
        stats.pre_compress_tokens,
        stats.post_compress_tokens,
        (1 - stats.compression_ratio) * 100,
    )

    return view, stats


def _run_compaction(
    *,
    gateway: LLMGateway,
    reuse_summary: str | None,
    to_compress: list[str],
    compact_task: str,
    compact_tier: str | None,
) -> str | None:
    """Summarise *to_compress* (optionally folding in a prior *reuse_summary*) via the gateway.

    Returns the summary text, or ``None`` if the gateway call failed (caller falls back to the
    uncompressed view).  When *reuse_summary* is supplied, the prompt asks the model to extend the
    existing summary with only the new turns — this is the incremental path that keeps the LLM
    input bounded instead of re-summarising the whole growing head every round.
    """
    n_new = len(to_compress)
    turns_text = "\n\n---\n\n".join(to_compress)
    if reuse_summary:
        user_prompt = (
            "Below is a running summary of earlier agent turns, followed by "
            f"{n_new} new turn(s). Produce a single updated concise summary that folds the new "
            "turns into the existing one. Preserve goals, errors, key facts, and pending tasks.\n\n"
            f"Existing summary:\n{reuse_summary}\n\nNew turns:\n{turns_text}"
        )
    else:
        user_prompt = (
            f"Summarise the following {n_new} agent turns into a single concise paragraph:\n\n"
            f"{turns_text}"
        )

    try:
        return gateway.complete(
            task=compact_task,
            system=_COMPACT_SYSTEM_PROMPT,
            user=user_prompt,
            tier=compact_tier,
        )
    except Exception as exc:
        _log.warning(
            "context_compressor: gateway.complete(task=%r) failed (%r); "
            "returning uncompressed transcript view.",
            compact_task,
            exc,
        )
        return None
