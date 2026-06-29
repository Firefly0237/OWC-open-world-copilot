"""Cross-session lesson archive for the refine loop.

Lessons are extracted deterministically (no LLM) from calibration false-pass data and
injected into generation prompts so the model learns from historically problematic patterns.

Guards against pollution:
- min_false_pass=3 threshold: a single bad review cannot create a lesson
- inject cap: at most 3 lessons per prompt
- 90-day deprioritisation: stale lessons sort behind recent ones (not deleted)
- inject_lessons=False: can be fully disabled per call
- No LLM: lesson text is a fixed template, no model hallucination risk
"""

from __future__ import annotations

from collections import Counter

from pydantic import BaseModel

from ..assist.calibration import CalibrationReport
from ..storage.sqlite import SQLiteStore


class LessonRecord(BaseModel):
    id: str
    item_type: str
    dimension: str
    lesson_text: str
    false_pass_count: int
    created_at: str
    last_seen_at: str


def extract_lessons_from_report(
    report: CalibrationReport,
    store: SQLiteStore,
    *,
    min_false_pass: int = 3,
) -> int:
    """Extract lessons from a CalibrationReport and persist them.

    IN-B1 M2: Groups by (item_type, dimension) so each failing dimension gets its own lesson,
    rather than lumping all failures of a type into one generic lesson.

    For each (item_type, dimension) pair with >= min_false_pass false-pass items, writes (upserts)
    one lesson using a deterministic template. Returns the number of lessons written/updated.

    This function is purely deterministic: it never calls an LLM.
    Signature is unchanged; callers are unaware of the internal grouping change.
    """
    counts: Counter[tuple[str, str]] = Counter(
        (item.item_type, item.dimension) for item in report.false_pass_items
    )
    written = 0
    for (item_type, dimension), count in counts.items():
        if count < min_false_pass:
            continue
        if dimension != "general":
            lesson_text = (
                f"此类 {item_type} 内容在「{dimension}」维度上历史有 {count} 次"
                "被人审拒绝（critic 误判为通过）。"
                f"生成时请着重提高「{dimension}」维度的质量，不要依赖 critic 的宽松判断。"
            )
        else:
            lesson_text = (
                f"此类 {item_type} 内容历史上有 {count} 次被人审拒绝（critic 误判为通过）。"
                "生成时请整体提高质量标准，不要依赖 critic 的宽松判断。"
            )
        store.save_lesson(item_type, lesson_text, dimension=dimension)
        written += 1
    return written


def build_lesson_block(lessons: list[dict], *, inject_lessons: bool = True) -> str:
    """Build a [lesson-memory] block for prompt injection.

    Returns "" when inject_lessons=False or lessons is empty.
    At most 3 lessons are injected (caller should already pass max_count=3 to
    get_lessons_for_type, but this is a belt-and-suspenders cap).
    """
    if not inject_lessons or not lessons:
        return ""
    items = lessons[:3]
    lines = [
        "[lesson-memory]",
        "以下是此类内容历史系统性问题总结，生成时请特别注意：",
    ]
    for i, lesson in enumerate(items, 1):
        lines.append(f"{i}. {lesson['lesson_text']}")
    return "\n".join(lines)


def build_critic_lesson_block(
    lessons: list[dict],
    *,
    inject_lessons: bool = True,
) -> str:
    """Build a [critic-lesson-memory] block for critic prompt injection.

    IN-B3 M1: A critic-specific lesson block, distinct from the generation-side [lesson-memory].
    Returns "" when inject_lessons=False or lessons is empty.
    At most 3 lessons are injected (belt-and-suspenders cap).

    Wording emphasises dimension-specific scrutiny ("historically rejected in this dimension,
    scrutinise harder") rather than the generation-side "raise overall quality bar" phrasing,
    to reduce over-generalisation and direct the critic's attention to the specific weak dimension.

    The block ends with an instruction to upgrade severity to blocker for historically weak dims.
    """
    if not inject_lessons or not lessons:
        return ""
    items = lessons[:3]
    lines = [
        "[critic-lesson-memory]",
        "以下是此类内容历史上评判侧的系统性漏判记录，请据此加强核查：",
    ]
    for i, lesson in enumerate(items, 1):
        dim = lesson.get("dimension", "general")
        # BE-4: lesson_text is authored with generation-side wording. In the critic context,
        # rephrase to evaluation-side wording. Cover both authored phrasings: the
        # dimension-specific "生成时请着重提高" (line ~62) and the general "生成时请整体提高"
        # (line ~67); the latter was previously missed, leaking generation-side wording.
        text = (
            lesson["lesson_text"]
            .replace("生成时请着重提高", "评判时请着重核查")
            .replace("生成时请整体提高", "评判时请整体核查")
        )
        dim_hint = f"（重点核查「{dim}」维度）" if dim != "general" else ""
        lines.append(f"{i}. {text}{dim_hint}")
    lines.append("如发现上述历史薄弱维度有类似问题，请将 severity 提升为 blocker。")
    return "\n".join(lines)
