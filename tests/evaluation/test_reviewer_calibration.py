"""Reviewer calibration: pair the critic's final verdict with the human's accept/reject decision,
and surface the false-pass blind spot (critic said pass, human rejected)."""

from __future__ import annotations

from owcopilot.assist.calibration import build_calibration_report, critic_from_trail
from owcopilot.assist.review_queue import ReviewItem, ReviewItemType, ReviewQueue
from owcopilot.storage import SQLiteStore


def _item(status: str, *, verdict: str | None, score: float | None) -> ReviewItem:
    return ReviewItem(
        item_type=ReviewItemType.QUEST_DRAFT,
        object_ref=f"quest:{verdict}-{score}-{status}",
        payload={"id": "q"},
        status=status,
        critic_verdict=verdict,
        critic_score=score,
    )


def test_critic_from_trail_reads_last_round() -> None:
    trail = [
        {"verdict": "revise", "score": 0.4, "auto_review_ok": True},
        {"verdict": "pass", "score": 0.9, "auto_review_ok": True},
    ]
    assert critic_from_trail(trail) == ("pass", 0.9)


def test_unparsable_critique_is_not_a_verdict() -> None:
    # an unparsable last critique must not be recorded as a real verdict
    assert critic_from_trail([{"verdict": "revise", "score": 0.0, "auto_review_ok": False}]) == (
        None,
        None,
    )
    assert critic_from_trail([]) == (None, None)  # single-shot draft, no critic ran


def test_calibration_surfaces_the_false_pass_blind_spot() -> None:
    resolved = [
        _item("accepted", verdict="pass", score=0.9),  # agreement
        _item("accepted", verdict="pass", score=0.85),  # agreement
        _item("rejected", verdict="pass", score=0.8),  # FALSE PASS — the blind spot
        _item("accepted", verdict="revise", score=0.5),  # critic harsher than human
        _item("rejected", verdict="revise", score=0.3),  # agreement
        _item("accepted", verdict=None, score=None),  # single-shot, no critic signal
    ]
    report = build_calibration_report(resolved)

    assert report.sample_size == 5  # the single-shot item is excluded
    assert report.skipped_no_verdict == 1
    assert report.matrix.critic_pass_human_reject == 1  # the false pass is counted
    assert report.false_pass_rate == 1 / 3  # 1 of 3 critic "pass" was rejected
    assert report.false_revise_rate == 1 / 2
    assert report.agreement_rate == 3 / 5  # pass&accept(2) + revise&reject(1)

    # the false-pass item is named so a human can revisit it
    assert len(report.false_pass_items) == 1
    assert report.false_pass_items[0].critic_score == 0.8

    # the score tracks quality: accepted drafts scored higher on average than rejected ones
    assert report.mean_score_accepted is not None and report.mean_score_rejected is not None
    assert report.mean_score_accepted > report.mean_score_rejected


def test_calibration_small_sample_is_flagged_with_wide_interval() -> None:
    # a single false pass reads as rate=1.0, but the report must NOT pretend that is confident:
    # the sample is flagged insufficient and the Wilson interval is wide.
    report = build_calibration_report([_item("rejected", verdict="pass", score=0.8)])
    assert report.false_pass_rate == 1.0
    assert report.sufficient_sample is False
    assert report.false_pass_rate_ci is not None
    low, high = report.false_pass_rate_ci
    assert low < 0.3 and high == 1.0  # 1-of-1 spans almost the whole range — honestly uncertain


def test_calibration_sufficient_sample_when_enough_history() -> None:
    resolved = [_item("accepted", verdict="pass", score=0.9) for _ in range(_threshold())]
    report = build_calibration_report(resolved)
    assert report.sufficient_sample is True


def _threshold() -> int:
    from owcopilot.assist.calibration import _MIN_SUFFICIENT_SAMPLE

    return _MIN_SUFFICIENT_SAMPLE


def test_calibration_empty_history_is_all_none() -> None:
    report = build_calibration_report([])
    assert report.sample_size == 0
    assert report.false_pass_rate is None
    assert report.agreement_rate is None
    assert report.false_pass_items == []


def test_calibration_end_to_end_through_review_queue() -> None:
    queue = ReviewQueue()
    passed = queue.add(_item("pending_review", verdict="pass", score=0.9))
    revised = queue.add(_item("pending_review", verdict="revise", score=0.4))
    queue.mark(passed.id, "rejected", decided_by="editor")  # a false pass
    queue.mark(revised.id, "rejected", decided_by="editor")

    report = build_calibration_report(queue.list_resolved())
    assert report.sample_size == 2
    assert report.matrix.critic_pass_human_reject == 1
    assert report.false_pass_rate == 1.0


def test_calibration_resolved_survives_sqlite_round_trip() -> None:
    store = SQLiteStore()
    try:
        queue = ReviewQueue(store)
        passed = queue.add(_item("pending_review", verdict="pass", score=0.92))
        revised = queue.add(_item("pending_review", verdict="revise", score=0.45))
        queue.add(_item("pending_review", verdict="pass", score=0.8))  # left pending → excluded
        queue.mark(passed.id, "accepted", decided_by="editor")
        queue.mark(revised.id, "rejected", decided_by="editor")

        resolved = queue.list_resolved()
        assert {item.status for item in resolved} == {"accepted", "rejected"}  # no pending leak
        report = build_calibration_report(resolved)
        assert report.sample_size == 2
        assert report.agreement_rate == 1.0  # pass→accept, revise→reject both agree
    finally:
        store.close()
