# 主题扫描：跨全世界找与某主题相关的所有对象，可定位到句级并逐一排除。
"""Theme sweep: locate every object related to an arbitrary theme, across the whole world.

The driving scenario is real and external: a compliance pass, an IP review, a sudden
"remove everything about X by Friday". The content under inspection is mostly NOT
created inside the workbench (imported spreadsheets, distilled manuscripts, hand-written
entries), so the sweep cannot rely on any preset feature list or tag taxonomy. Recall is
layered instead:

  1. term layer    — the user's theme + their synonyms, matched lexically over EVERY
                     string field of EVERY object (fields are walked generically from the
                     serialized model, so new content types join the sweep for free);
  1.5 semantic layer — a real multilingual embedder (bge-m3) scores each not-yet-hit object
                     against the theme by cosine; a near-match is surfaced for review even when
                     it shares NO word with the theme (euphemism / paraphrase). This is the
                     "DFA + word embeddings" idea — a lexical word-list alone misses the
                     euphemisms a compliance pass exists to find. Deterministic, $0/local; it
                     self-disables on the hashing stub. The scores also order the capped judge.
  2. judge layer   — optionally, an LLM reads each not-yet-hit object and answers
                     "related to this theme?" with a reason and a quote (classification
                     with evidence, never generation);
  3. graph layer   — relations expand confirmed hits to their neighbours as
                     "review suggested" (a casino owner is suspect even if his own
                     description never mentions gambling).

The output is a work order for a HUMAN: every finding carries its evidence and the
report states exactly what was scanned and what was not (no silent sampling), because
the user of this feature must be able to say "we checked everything".

Two structural capabilities beyond raw recall (Station-5 "contained"):

  * sentence-level localization — a finding no longer points only at an object. The term/
    judge layers locate to the exact FIELD and SENTENCE (a dialogue tree resolves to
    ``nodes.n1.text`` instead of one ref for the whole tree). ``ref`` stays object-level so
    every existing object-level consumer is unaffected; the sub-object location lives in the
    new ``locator`` field.
  * "dismiss one" (逐一排除) — a reviewer can mark a single finding as a false positive; the
    dismissal persists (``SweepExclusionStore``) so re-running the SAME sweep no longer
    re-surfaces it. Each finding carries a stable ``finding_id`` that is the dismissal key.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from ..content.hash import content_hash
from ..content.models import (
    ContentBundle,
    StyleElementKind,
    StyleElementPolarity,
    StyleRubric,
    StyleRubricElement,
)
from ..llm.cache import Embedder
from ..llm.gateway import LLMGateway
from ..llm.jsonio import extract_json_object
from ..storage.atomic_file import atomic_write_text, read_json_file

_SNIPPET_CHARS = 90
_JUDGE_BATCH_SIZE = 10
_JUDGE_TEXT_CHARS = 320
_EMBED_TEXT_CHARS = 1200  # how much of an object's text the semantic layer embeds (speed cap)

# The dismissal key for object-grain findings. semantic / judge / graph all describe "this
# WHOLE object looks related", so they share ONE id per object — deliberately layer-independent
# so that dismissing an object's semantic review also suppresses it when the LLM judge (turned on
# next run) would re-flag the same object. Term findings key on field+sentence instead (below),
# because there "this located sentence is a false positive" is a per-sentence decision.
_OBJECT_KEY = "\x00object"

# Sentence boundary: CJK/ASCII terminal punctuation and newlines. The lookbehind splits AFTER the
# delimiter so the punctuation stays attached to its sentence. Good enough for narrative text;
# rare over-splits (English abbreviations) only change granularity, never which objects are found.
_SENT_SPLIT = re.compile(r"(?<=[。！？!?\n.])")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _finding_id(ref: str, key: str) -> str:
    """Stable dismissal id for a finding. Deterministic over (object ref, location key) and
    deliberately independent of the detecting layer and of any similarity score — so a re-run
    (LLM toggled on/off, threshold nudged) produces the SAME id and a prior dismissal still bites.
    See ``_OBJECT_KEY`` for why the fuzzy layers collapse to one id per object."""
    return "sw_" + hashlib.sha256(f"{ref}\x1f{key}".encode()).hexdigest()[:12]


def _is_semantic(embedder: Embedder | None) -> bool:
    """A real neural embedder (bge-m3) tags its model_id ``st:*``; the hashing stub does not.

    Reads ``model_id`` live so a runtime degrade (semantic model failed to load → hashing
    fallback) is seen as non-semantic — never snapshot this, or a degraded process would keep
    claiming ``st:`` (same live-read rule as ``VectorRetriever.is_semantic``)."""
    return embedder is not None and embedder.model_id.startswith("st:")


def _normalise_rows(matrix: np.ndarray) -> np.ndarray:
    """L2-normalise each row so a plain dot product is cosine similarity."""
    if matrix.size == 0:
        return matrix
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return matrix / norms


_JUDGE_SYSTEM_PROMPT = (
    "You are a content-compliance reviewer. You will receive a THEME and a numbered list "
    "of content objects (any genre, any language). For each object decide whether it is "
    "related to the theme — directly mentioning it, depicting it, or clearly evoking it. "
    "Judge only from the given text; do not guess beyond it. Return ONE JSON object, no "
    'markdown: {"judgements": [{"index": <number>, "related": true|false, '
    '"reason": "<short reason in the theme\'s language>", "quote": "<the exact phrase '
    'from the object that triggered the call, empty if unrelated>"}]} . '
    "Every listed object must appear exactly once in judgements."
)


@dataclass
class SweepFinding:
    ref: str
    name: str
    object_kind: str
    layer: str  # "term" | "semantic" | "judge" | "graph"
    evidence: str
    verdict: str  # "hit" | "review"
    # Sentence-level location within the object. Object-level for the fuzzy layers
    # (semantic/judge/graph); a field/sentence path for the term layer (e.g. ``nodes.n1.text``,
    # ``description（第2句）``). Empty when a finding is built outside the sweep (e.g. tests).
    locator: str = ""
    # Stable dismissal key. Auto-derived from (ref, locator) when not supplied; the sweep sets it
    # explicitly so term findings key on the sentence TEXT (robust to unrelated edits) while the
    # fuzzy layers key on the object (see ``_finding_id`` / ``_OBJECT_KEY``).
    finding_id: str = ""

    def __post_init__(self) -> None:
        if not self.finding_id:
            self.finding_id = _finding_id(self.ref, self.locator)


@dataclass
class SweepReport:
    theme: str
    terms: list[str]
    scanned_total: int
    scanned_by_kind: dict[str, int]
    findings: list[SweepFinding]
    llm_used: bool
    judged_count: int
    judge_skipped: int  # objects NOT shown to the judge (cap reached) — honesty counter
    semantic_used: bool = False  # whether the bge-m3 paraphrase layer ran (needs a real model)
    semantic_flagged: int = 0  # objects surfaced by meaning alone (no lexical hit)
    semantic_threshold: float = 0.0
    dismissed_count: int = 0  # findings suppressed by a prior "逐一排除" — honesty counter

    @property
    def hits(self) -> list[SweepFinding]:
        return [f for f in self.findings if f.verdict == "hit"]

    @property
    def review_suggested(self) -> list[SweepFinding]:
        return [f for f in self.findings if f.verdict == "review"]


def _string_fields(
    value: Any, path: str = "", *, skip_keys: frozenset[str] = frozenset({"metadata"})
) -> list[tuple[str, str]]:
    """Collect every string leaf of a serialized model together with its JSON path. Generic on
    purpose: the sweep must see ALL text a future content type carries, without anyone remembering
    to register its fields. The path (``nodes.n1.text``, ``stages[2].summary``) is what lets a
    finding say WHICH field/sentence matched instead of only WHICH object.

    Traversal order (dict items, then list items) is identical to the old flat leaf walk, so the
    joined object text handed to the semantic/judge layers is byte-for-byte unchanged."""
    out: list[tuple[str, str]] = []
    if isinstance(value, str):
        if value.strip():
            out.append((path, value))
    elif isinstance(value, dict):
        for key, item in value.items():
            if key in skip_keys:
                continue
            child = f"{path}.{key}" if path else str(key)
            out.extend(_string_fields(item, child, skip_keys=skip_keys))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            out.extend(_string_fields(item, f"{path}[{index}]", skip_keys=skip_keys))
    return out


def _sentences(text: str) -> list[str]:
    """Split a field into sentences for sentence-level location. Deterministic; empties dropped."""
    return [part.strip() for part in _SENT_SPLIT.split(text) if part.strip()]


def _corpus(bundle: ContentBundle) -> list[dict[str, Any]]:
    """Every world object as (ref, name, kind, text, fields). Collections are enumerated from the
    bundle itself so nothing is silently left out of a sweep. ``fields`` keeps each string leaf
    with its JSON path (for sentence-level location); ``text`` is their join (for embedding/judge,
    unchanged from before)."""
    rows: list[dict[str, Any]] = []

    def add(kind: str, ref: str, name: str, payload: Any) -> None:
        dumped = payload.model_dump(mode="json")
        fields = _string_fields(dumped)
        text = "\n".join(leaf for _, leaf in fields)
        rows.append({"ref": ref, "name": name, "kind": kind, "text": text, "fields": fields})

    for entity_id, entity in bundle.entities.items():
        add("entity", f"entity:{entity_id}", entity.name, entity)
    for quest_id, quest in bundle.quests.items():
        add("quest", f"quest:{quest_id}", quest.title, quest)
    for region_id, region in bundle.regions.items():
        add("region", f"region:{region_id}", region.name, region)
    for poi_id, poi in bundle.pois.items():
        add("poi", f"poi:{poi_id}", poi.name, poi)
    for dlg_id, dialogue in bundle.dialogues.items():
        add("dialogue", f"dialogue:{dlg_id}", dlg_id, dialogue)
    for tree_id, tree in bundle.dialogue_trees.items():
        add("dialogue_tree", f"dialogue_tree:{tree_id}", tree.title or tree_id, tree)
    for term_id, term in bundle.terms.items():
        add("term", f"term:{term_id}", term.canonical, term)
    for text_id, localized in bundle.localized_texts.items():
        add("localized_text", f"localized_text:{text_id}", text_id, localized)
    for guide_id, guide in bundle.style_guides.items():
        add("style_guide", f"style_guide:{guide_id}", guide_id, guide)
    return rows


def _has_cjk(text: str) -> bool:
    return any("一" <= ch <= "鿿" for ch in text)


def _term_matches(term: str, text: str) -> bool:
    """Lexical membership of ``term`` in ``text``. CJK has no word boundaries, so it stays a
    substring test; a pure-Latin term uses ``\\b`` word boundaries so a compliance term like
    ``war`` does not false-hit ``warden`` / ``toward`` (same precision fix as S3's term rule)."""
    term = term.strip()
    if not term:
        return False
    if _has_cjk(term):
        return term.lower() in text.lower()
    return re.search(rf"\b{re.escape(term)}\b", text, re.IGNORECASE) is not None


def _snippet(text: str, term: str) -> str:
    lowered = text.lower()
    pos = lowered.find(term.lower())
    if pos < 0:
        return text[:_SNIPPET_CHARS]
    start = max(0, pos - _SNIPPET_CHARS // 3)
    end = min(len(text), pos + len(term) + _SNIPPET_CHARS)
    return (
        ("…" if start > 0 else "")
        + text[start:end].replace("\n", " ")
        + ("…" if end < len(text) else "")
    )


class ThemeSweepService:
    def __init__(
        self,
        *,
        bundle: ContentBundle,
        gateway: LLMGateway | None = None,
        embedder: Embedder | None = None,
    ) -> None:
        self.bundle = bundle
        self.gateway = gateway
        # A real semantic embedder (bge-m3) turns on the paraphrase layer; the hashing stub is
        # lexical, so it stays off to avoid surfacing char-overlap noise as "meaning".
        self.embedder = embedder if (embedder is not None and _is_semantic(embedder)) else None

    def sweep(
        self,
        theme: str,
        *,
        extra_terms: list[str] | None = None,
        use_llm: bool = False,
        max_judge: int = 400,
        semantic_threshold: float = 0.5,
        exclusions: Iterable[str] | None = None,
        progress: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> SweepReport:
        theme = theme.strip()
        if not theme:
            raise ValueError("清查主题不能为空。")
        terms = [theme] + [t.strip() for t in (extra_terms or []) if t.strip()]
        corpus = _corpus(self.bundle)
        findings: dict[str, SweepFinding] = {}

        # ---- layer 1: lexical, over every string field of every object, sentence by sentence so
        # a hit points at the exact field/sentence (a dialogue tree resolves to nodes.n1.text, not
        # one ref for the whole tree). One object can yield several findings, one per matched
        # sentence — "show me every located part" is the whole point of the core scenario.
        term_hit_refs: set[str] = set()
        for row in corpus:
            for field_path, field_text in row["fields"]:
                sentences = _sentences(field_text)
                multi = len(sentences) > 1
                for order, sentence in enumerate(sentences, start=1):
                    matched = [t for t in terms if _term_matches(t, sentence)]
                    if not matched:
                        continue
                    locator = f"{field_path}（第{order}句）" if multi else field_path
                    fid = _finding_id(row["ref"], f"{field_path}\x1f{sentence}")
                    snippet = _snippet(sentence, matched[0])
                    findings[fid] = SweepFinding(
                        ref=row["ref"],
                        name=row["name"],
                        object_kind=row["kind"],
                        layer="term",
                        evidence=f"命中词「{matched[0]}」（{locator}）：{snippet}",
                        verdict="hit",
                        locator=locator,
                        finding_id=fid,
                    )
                    term_hit_refs.add(row["ref"])

        # ---- layer 1.5: semantic — catch paraphrase/euphemism the lexical layer cannot (the
        # "DFA + word embeddings" idea, with a multilingual model). Deterministic, $0/local. Each
        # object with no lexical hit is scored against the terms by cosine; a near-match is
        # surfaced for review, and the scores order the (capped) judge so its budget hits likeliest.
        pending = [row for row in corpus if row["ref"] not in term_hit_refs]
        semantic_scores = self._semantic_scores(terms, pending) if self.embedder else {}
        semantic_flagged = 0
        for row in pending:
            score = semantic_scores.get(row["ref"], 0.0)
            if score >= semantic_threshold:
                fid = _finding_id(row["ref"], _OBJECT_KEY)
                findings[fid] = SweepFinding(
                    ref=row["ref"],
                    name=row["name"],
                    object_kind=row["kind"],
                    layer="semantic",
                    evidence=f"语义相近（相似度 {score:.2f}）：{row['text'][:_SNIPPET_CHARS]}",
                    verdict="review",
                    locator="（对象整体·语义近似）",
                    finding_id=fid,
                )
                semantic_flagged += 1
        # Re-read the embedder's backend live now that the semantic layer has embedded — a model
        # that degraded to hashing mid-run flips ``model_id`` to ``hashing-*``, so this reports the
        # backend actually used, not the construction-time guess (mirrors VectorRetriever).
        semantic_live = _is_semantic(self.embedder)

        # ---- layer 2: LLM judge on everything the lexical pass did not confirm, ordered by
        # semantic similarity so the cap is spent on the likeliest. A "related" verdict upgrades a
        # provisional semantic review to a confirmed hit (same object-grain id) with the model's
        # reason, and best-effort locates the model's quote to a field/sentence.
        judged_count = 0
        judge_skipped = 0
        if use_llm and self.gateway is not None:
            judge_pool = sorted(
                pending, key=lambda r: (-semantic_scores.get(r["ref"], 0.0), r["ref"])
            )
            to_judge = judge_pool[:max_judge]
            judge_skipped = len(judge_pool) - len(to_judge)
            for start in range(0, len(to_judge), _JUDGE_BATCH_SIZE):
                batch = to_judge[start : start + _JUDGE_BATCH_SIZE]
                judged_count += len(batch)
                if progress is not None:
                    progress("judge", {"done": judged_count, "total": len(to_judge)})
                for index, reason, quote in self._judge_batch(theme, terms, batch):
                    row = batch[index]
                    evidence = f"模型判定：{reason}" + (f"（原文：{quote}）" if quote else "")
                    fid = _finding_id(row["ref"], _OBJECT_KEY)
                    findings[fid] = SweepFinding(
                        ref=row["ref"],
                        name=row["name"],
                        object_kind=row["kind"],
                        layer="judge",
                        evidence=evidence,
                        verdict="hit",
                        locator=self._locate_quote(row, quote),
                        finding_id=fid,
                    )

        # ---- layer 3: graph neighbours of hits become "review suggested"
        hit_refs = {f.ref for f in findings.values() if f.verdict == "hit"}
        covered_refs = {f.ref for f in findings.values()}
        hit_ids = {ref.split(":", 1)[1] for ref in hit_refs}
        names = {row["ref"]: (row["name"], row["kind"]) for row in corpus}
        for relation in self.bundle.relations:
            for this_id, other_id in (
                (relation.source, relation.target),
                (relation.target, relation.source),
            ):
                if this_id not in hit_ids:
                    continue
                neighbour_ref = self._ref_of(other_id)
                if neighbour_ref is None or neighbour_ref in covered_refs:
                    continue
                name, kind = names.get(neighbour_ref, (other_id, "entity"))
                fid = _finding_id(neighbour_ref, _OBJECT_KEY)
                findings[fid] = SweepFinding(
                    ref=neighbour_ref,
                    name=name,
                    object_kind=kind,
                    layer="graph",
                    evidence=f"与命中对象 `{this_id}` 存在关系（{relation.kind}），建议人工复查。",
                    verdict="review",
                    locator="（关系扩散）",
                    finding_id=fid,
                )
                covered_refs.add(neighbour_ref)

        # ---- "逐一排除": drop findings a reviewer previously dismissed for THIS theme. The caller
        # passes the dismissed finding_ids (SweepExclusionStore.dismissed_ids(theme)); the count is
        # reported so the work order can honestly say "N previously-excluded matches suppressed".
        dismissed = set(exclusions or ())
        dismissed_count = sum(1 for fid in findings if fid in dismissed)
        surviving = [f for fid, f in findings.items() if fid not in dismissed]

        by_kind: dict[str, int] = {}
        for row in corpus:
            by_kind[row["kind"]] = by_kind.get(row["kind"], 0) + 1
        ordered = sorted(
            surviving, key=lambda f: (f.verdict != "hit", f.object_kind, f.ref, f.locator)
        )
        return SweepReport(
            theme=theme,
            terms=terms,
            scanned_total=len(corpus),
            scanned_by_kind=by_kind,
            findings=ordered,
            llm_used=bool(use_llm and self.gateway is not None),
            judged_count=judged_count,
            judge_skipped=judge_skipped,
            # Read the embedder's live backend AFTER the semantic layer has embedded: if the
            # model failed to load and degraded to the hashing stub mid-run, ``_is_semantic``
            # now reads ``hashing-*`` and reports False — never the construction-time snapshot,
            # so the work order can't claim "bge-m3 enabled" while running on the stub.
            semantic_used=semantic_live,
            semantic_flagged=semantic_flagged,
            semantic_threshold=semantic_threshold if semantic_live else 0.0,
            dismissed_count=dismissed_count,
        )

    def _semantic_scores(self, terms: list[str], rows: list[dict[str, Any]]) -> dict[str, float]:
        """Cosine of each object to its closest term, computed locally with the project embedder.

        Returns the max similarity over terms per object ref. Empty when there is nothing to score,
        so the lexical/judge layers are unaffected."""
        assert self.embedder is not None
        if not rows:
            return {}
        term_matrix = _normalise_rows(np.asarray(self.embedder.embed_many(terms), dtype=np.float32))
        object_matrix = _normalise_rows(
            np.asarray(
                self.embedder.embed_many([row["text"][:_EMBED_TEXT_CHARS] for row in rows]),
                dtype=np.float32,
            )
        )
        if term_matrix.size == 0 or object_matrix.size == 0:
            return {}
        # (objects × dim) @ (dim × terms) → (objects × terms); take each object's best term
        sims = object_matrix @ term_matrix.T
        best = sims.max(axis=1)
        return {row["ref"]: float(best[index]) for index, row in enumerate(rows)}

    def _locate_quote(self, row: dict[str, Any], quote: str) -> str:
        """Best-effort sentence location for the judge's quote: find it verbatim among the object's
        fields (deterministic substring), report that field/sentence. A quote the model invented
        (not present in the text) falls back to object-level — the finding is NOT dropped, so the
        honest counters and this display aid stay decoupled from any quote-verification gate."""
        quote = (quote or "").strip()
        if quote:
            for field_path, field_text in row["fields"]:
                sentences = _sentences(field_text)
                for order, sentence in enumerate(sentences, start=1):
                    if quote in sentence:
                        return f"{field_path}（第{order}句）" if len(sentences) > 1 else field_path
        return "（对象整体·模型判定）"

    def _ref_of(self, object_id: str) -> str | None:
        if object_id in self.bundle.entities:
            return f"entity:{object_id}"
        if object_id in self.bundle.quests:
            return f"quest:{object_id}"
        if object_id in self.bundle.pois:
            return f"poi:{object_id}"
        if object_id in self.bundle.regions:
            return f"region:{object_id}"
        if object_id in self.bundle.dialogue_trees:
            return f"dialogue_tree:{object_id}"
        return None

    def _judge_batch(
        self, theme: str, terms: list[str], batch: list[dict[str, Any]]
    ) -> list[tuple[int, str, str]]:
        # one object = one numbered line: embedded newlines would detach an object's text
        # from its index and corrupt the judgement mapping
        listing = "\n".join(
            f"{index}. [{row['kind']}] {row['name']}："
            + row["text"][:_JUDGE_TEXT_CHARS].replace("\n", " ")
            for index, row in enumerate(batch)
        )
        synonyms = "、".join(terms[1:]) if len(terms) > 1 else ""
        user = (
            f"THEME：{theme}"
            + (f"（关联词：{synonyms}）" if synonyms else "")
            + f"\n\nOBJECTS:\n{listing}"
        )
        raw = self.gateway.complete(  # type: ignore[union-attr]
            task="theme_sweep", system=_JUDGE_SYSTEM_PROMPT, user=user
        )
        results: list[tuple[int, str, str]] = []
        # Kept off complete_structured: a batch judge — bad-index items are skipped per-item (not a
        # whole-batch reask) and an unparsable batch degrades to empty, never raised.
        try:
            # shared balanced-brace extractor (tolerates fences/prose), same as every other LLM
            # parse site — round 31's jsonio dedup had missed the sweep judge.
            payload = extract_json_object(raw)
        except ValueError:
            return results  # one bad batch must not sink the sweep; coverage stays in stats
        for item in payload.get("judgements") or []:
            if not isinstance(item, dict) or not item.get("related"):
                continue
            try:
                index = int(item.get("index", -1))
            except (TypeError, ValueError):
                continue
            if 0 <= index < len(batch):
                results.append(
                    (index, str(item.get("reason") or "相关"), str(item.get("quote") or ""))
                )
        return results


@dataclass
class SweepExclusion:
    """One "逐一排除" decision: a finding a reviewer judged a false positive, kept so the same
    sweep does not re-surface it. Carries an audit trail (operator/reason/at) in the spirit of the
    compliance case ledger, without the heavyweight case state machine."""

    finding_id: str
    theme: str
    ref: str
    locator: str
    layer: str
    operator: str
    reason: str = ""
    at: str = field(default_factory=_now)


class SweepExclusionStore:
    """Per-world persistence for sweep dismissals — a JSON ledger under ``<root>/.sweep/``.

    Deliberately NOT the compliance ``CaseStore``: that ledger only ingests ``report.hits`` and is
    keyed by (object, layer), so it structurally cannot dismiss the semantic / graph *review*
    findings where abstract-style matches actually land. This is the lightweight, review-layer-
    aware counterpart — a set of dismissed ``finding_id`` scoped by theme. It lives beside the
    world (zero canon pollution), mirroring where ``CaseStore`` keeps its JSON.

    Keyed by (theme, finding_id) on disk so a dismissal made while sweeping "赌博" does not hide the
    same sentence when sweeping "暴力". The ``finding_id`` itself is theme-independent and stable
    across re-runs (see ``_finding_id``), which is what makes the exclusion survive a re-sweep."""

    def __init__(self, world_root: str | Path) -> None:
        self.path = Path(world_root) / ".sweep" / "exclusions.json"

    def _record_key(self, theme: str, finding_id: str) -> str:
        return hashlib.sha256(f"{theme.strip()}\x1f{finding_id}".encode()).hexdigest()[:16]

    def _read(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {}
        raw = read_json_file(self.path)
        if not isinstance(raw, dict):
            raise ValueError(f"排除记录必须是 JSON object：{self.path.resolve()}")
        return raw

    def _write(self, records: dict[str, dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            self.path, json.dumps(records, ensure_ascii=False, indent=2, sort_keys=True)
        )

    def load(self) -> list[SweepExclusion]:
        fields = {f for f in SweepExclusion.__dataclass_fields__}  # tolerate schema drift on read
        return [
            SweepExclusion(**{k: v for k, v in rec.items() if k in fields})
            for rec in self._read().values()
        ]

    def dismissed_ids(self, theme: str) -> set[str]:
        """The finding_ids dismissed for this theme — pass straight to ``sweep(exclusions=...)``."""
        wanted = theme.strip()
        return {e.finding_id for e in self.load() if e.theme == wanted}

    def dismiss(
        self, finding: SweepFinding, theme: str, *, operator: str, reason: str = ""
    ) -> SweepExclusion:
        """Record a "逐一排除". Requires a signature (audit), like the compliance transition."""
        if not operator.strip():
            raise ValueError("请先填写署名")
        exclusion = SweepExclusion(
            finding_id=finding.finding_id,
            theme=theme.strip(),
            ref=finding.ref,
            locator=finding.locator,
            layer=finding.layer,
            operator=operator,
            reason=reason,
        )
        records = self._read()
        records[self._record_key(theme, finding.finding_id)] = asdict(exclusion)
        self._write(records)
        return exclusion

    def restore(self, theme: str, finding_id: str) -> bool:
        """Undo a dismissal (the finding re-surfaces next sweep). Returns whether one existed."""
        records = self._read()
        key = self._record_key(theme, finding_id)
        if key not in records:
            return False
        del records[key]
        self._write(records)
        return True


class OfflineSweepJudgeProvider:
    """Deterministic judge double. It relates an object when any single CJK character of
    the theme appears in the object text — deliberately LOOSER than the term layer's
    whole-phrase match, so tests can observe the judge catching rephrased content the
    lexical pass missed (the real judge's whole job). $0, no network."""

    def complete(self, *, system: str, user: str, model: str) -> tuple[str, int, int]:
        theme_line = user.split("\n", 1)[0]
        theme = theme_line.replace("THEME：", "").split("（", 1)[0].strip()
        theme_chars = [ch for ch in theme if not ch.isascii()]
        judgements = []
        for line in user.splitlines():
            stripped = line.strip()
            if not stripped or not stripped[0].isdigit() or "." not in stripped:
                continue
            index_text, _, body = stripped.partition(".")
            matched = next((ch for ch in theme_chars if ch in body), None)
            judgements.append(
                {
                    "index": int(index_text),
                    "related": matched is not None,
                    "reason": f"涉及主题要素「{matched}」" if matched else "",
                    "quote": matched or "",
                }
            )
        return json.dumps({"judgements": judgements}, ensure_ascii=False), 30, 20


def render_sweep_markdown(report: SweepReport) -> str:
    """The work order, as a portable checklist the team can tick off anywhere."""
    lines = [
        f"# 专项清查工作单：{report.theme}",
        "",
        f"- 检索词：{'、'.join(report.terms)}",
        f"- 扫描范围：全库 {report.scanned_total} 个对象（"
        + "，".join(f"{kind} {count}" for kind, count in sorted(report.scanned_by_kind.items()))
        + "）",
        "- 语义近似（向量）："
        + (
            f"已启用（bge-m3，阈值 {report.semantic_threshold:.2f}），按义召回 "
            f"{report.semantic_flagged} 个待查"
            if report.semantic_used
            else "未启用（需安装语义模型；当前仅词面）"
        ),
        "- 语义判定（模型逐项）："
        + (
            f"已启用，逐项判定 {report.judged_count} 个"
            if report.llm_used
            else "未启用（仅词面命中 + 关系扩散）"
        )
        + (
            f"；**有 {report.judge_skipped} 个对象未判定（超出上限）**"
            if report.judge_skipped
            else ""
        ),
        f"- 结论：直接命中 {len(report.hits)}，关联待查 {len(report.review_suggested)}",
    ]
    if report.dismissed_count:
        lines.append(f"- 已排除（逐一排除，本次抑制不再列出）：{report.dismissed_count} 条")
    lines += ["", "## 直接命中（需处理）", ""]
    if report.hits:
        for finding in report.hits:
            lines.append(_finding_line(finding))
    else:
        lines.append("（无）")
    lines += ["", "## 关联对象（建议复查）", ""]
    if report.review_suggested:
        for finding in report.review_suggested:
            lines.append(_finding_line(finding))
    else:
        lines.append("（无）")
    return "\n".join(lines) + "\n"


def _finding_line(finding: SweepFinding) -> str:
    # The trailing 排除码 is the finding_id — the handle a reviewer/agent uses to 逐一排除 this
    # exact match (and only this one) via SweepExclusionStore.dismiss.
    return (
        f"- [ ] `{finding.ref}` **{finding.name}** — {finding.evidence}"
        f" `[排除码 {finding.finding_id}]`"
    )


# ============================================================================================
# 风格核对级联：抽象标签 → 版本化二元元素清单(StyleRubric) → L1 词表/正则 → L2 概念分数 → L3 判官
# ============================================================================================
# The recall-first counterpart to ThemeSweepService. The driving scenario ("核对某系列内容是否含
# 『日式风格』并逐一定位") is the reverse of the other four stations: here MISSING a match is the
# expensive error (a rejected submission / a 監修 rework round of 1–3 weeks), while an over-report
# is a 3-second dismissal. So this cascade is high-recall, marks everything a CANDIDATE, and never
# asserts — the DNA's "错" ("assert an uncertain thing into canon") is untouched (design §5.1).
#
# The whole thing stands on the StyleRubric shape: each element is at once a retrieval query, a
# binary yes/no question and the hit reason. ``kind`` routes an element down the cascade — lexical
# tokens land on the $0 deterministic L1, a criterion that cannot be spelled as a word list lands on
# the embedding concept L2 and the LLM judge L3. This is "确定性优先" at element granularity.
#
# Two hard properties this class exists to guarantee (design §3.2 / §6.3):
#   * every level's contribution is separately inspectable (``RubricElementResult.l1/l2/l3_refs``),
#     so a labelled ground-truth set can compute recall@L1 and recall@L1∪L2 PER LEVEL and attribute
#     any miss to the right level — a cascade that hides its per-level recall loss is useless here;
#   * coverage is reported honestly: NOT a bare recall number (there is no full annotation to divide
#     by), but the scan surface + the frozen rubric fingerprint + an explicit line that the check
#     cannot see any element outside the checklist. That last line is the product's load-bearing
#     honesty: without it "we checked everything" is a false promise (design §W1).

# How much of an element's few-shot concept text the L2 layer embeds as the query (mirrors the
# object side's ``_EMBED_TEXT_CHARS`` cap so a runaway exemplar cannot blow up the encode).
_ELEMENT_QUERY_CHARS = 512

# Similarity provider seam for L2: ``(query_text, refs) -> {ref: cosine}``. ``VectorRetriever
# .similarities`` matches this exactly, so a caller can route L2 through the persisted, incremental
# ``content_vectors`` cache (keyed by text_hash, degrade-aware) instead of the in-memory embed —
# killing the O(全库 × 清查) re-embed across repeated sweeps. When absent, L2 embeds the corpus ONCE
# in memory (which already kills the O(元素 × 全库) re-embed WITHIN a single rubric sweep).
SimilarityFn = Callable[[str, list[str]], dict[str, float]]


def _rubric_finding_id(ref: str, element_id: str, location_key: str) -> str:
    """Dismissal id for a rubric finding, folding in ``element_id``.

    Deliberately element-aware: two elements matching the SAME sentence are two independent
    decisions (one may be a real hit, the other a false positive), so they must get distinct ids and
    dismiss independently. This is the fix for the compliance ``case_id = sha256(object_ref|layer)``
    collision the design flags (§1.7) — a rubric finding keys on (object, field/sentence, element),
    never on the layer name."""
    return (
        "sr_" + hashlib.sha256(f"{ref}\x1f{element_id}\x1f{location_key}".encode()).hexdigest()[:12]
    )


@dataclass
class RubricElementResult:
    """One rubric element's cascade outcome, with each level kept separable for miss-rate analysis.

    ``l1_refs`` / ``l2_refs`` / ``l3_refs`` are the object refs each level surfaced (L2 excludes
    what L1 caught; L3 ⊆ L2 candidates it confirmed). ``candidate_refs`` = L1 ∪ L2 is the recall
    ceiling for this element — L3, however accurate, can never recover a relevant object that never
    entered the candidate pool."""

    element_id: str
    element_text: str
    kind: str  # "lexical" | "semantic"
    polarity: str  # "include" | "exclude"
    l1_refs: list[str]
    l2_refs: list[str]
    l3_refs: list[str]
    findings: list[SweepFinding]

    @property
    def candidate_refs(self) -> list[str]:
        return sorted(set(self.l1_refs) | set(self.l2_refs))


@dataclass
class RubricSweepReport:
    """The rubric cascade's work order + honest coverage report.

    ``rubric_fingerprint`` is ``content_hash(rubric)`` — the frozen 清单指纹 that pins exactly which
    checklist version produced these findings (change one element and the fingerprint moves, so a
    stale report can never masquerade as current). Everything else is a transparency counter, not a
    quality claim."""

    rubric_id: str
    rubric_label: str
    rubric_version: str
    rubric_fingerprint: str
    abstract_tag: str
    scanned_total: int
    scanned_by_kind: dict[str, int]
    scanned_fields: int  # scan surface at field grain (the honest "扫描面")
    elements: list[RubricElementResult]
    semantic_used: bool
    semantic_threshold: float
    llm_used: bool
    judged_count: int
    judge_skipped: int  # candidates NOT shown to the judge (cap reached) — honesty counter
    dismissed_count: int = 0

    @property
    def findings(self) -> list[SweepFinding]:
        return [f for element in self.elements for f in element.findings]

    @property
    def hits(self) -> list[SweepFinding]:
        return [f for f in self.findings if f.verdict == "hit"]

    @property
    def review_suggested(self) -> list[SweepFinding]:
        return [f for f in self.findings if f.verdict == "review"]

    @property
    def l1_total(self) -> int:
        return len({r for e in self.elements for r in e.l1_refs})

    @property
    def l2_total(self) -> int:
        return len({r for e in self.elements for r in e.l2_refs})

    @property
    def l1_l2_overlap(self) -> int:
        """Objects surfaced by BOTH a lexical element and a concept element — a diagnostic only.

        Reported for transparency, NOT fed into a capture-recapture residual estimate: L1 and L2
        scan for DIFFERENT elements, so the two pipelines are not independent probes of one target
        and a Petersen estimate would be unsound (design §W5). Coverage is stated as scan-surface +
        checklist-completeness, never as an inferred total-relevant count."""
        l1 = {r for e in self.elements for r in e.l1_refs}
        l2 = {r for e in self.elements for r in e.l2_refs}
        return len(l1 & l2)


class RubricSweepService:
    """Cascade recall over a whole world against a versioned :class:`StyleRubric`.

    Reuses every ThemeSweepService primitive (``_corpus`` full enumeration, ``_term_matches``
    CJK/Latin split, the semantic embed + cosine, the batched ``_judge_batch`` with quote capture)
    — not a second scanner, but the same machinery re-aimed at "does this world contain the
    elements of THIS checklist" and instrumented for per-level recall."""

    def __init__(
        self,
        *,
        bundle: ContentBundle,
        gateway: LLMGateway | None = None,
        embedder: Embedder | None = None,
        similarity_fn: SimilarityFn | None = None,
    ) -> None:
        self.bundle = bundle
        self.gateway = gateway
        # Same gate as ThemeSweepService: a real neural embedder (``st:*``) turns L2 on; the hashing
        # stub is lexical noise, so it stays off — a loud, honest disable, not a silent degrade.
        self.embedder = embedder if (embedder is not None and _is_semantic(embedder)) else None
        # When provided, L2 scores through this instead of the in-memory embed — the seam that lets
        # a caller reuse the persisted ``content_vectors`` cache (``VectorRetriever.similarities``).
        self.similarity_fn = similarity_fn
        # Judge reuse: borrow ThemeSweepService._judge_batch verbatim (batched classify + json parse
        # + per-item bad-index skip + unparsable-batch degrade) instead of duplicating it.
        self._judge_host = ThemeSweepService(bundle=bundle, gateway=gateway, embedder=embedder)

    def sweep(
        self,
        rubric: StyleRubric,
        *,
        use_llm: bool = False,
        max_judge: int = 400,
        semantic_threshold: float = 0.5,
        exclusions: Iterable[str] | None = None,
        progress: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> RubricSweepReport:
        corpus = _corpus(self.bundle)
        scanned_fields = sum(len(row["fields"]) for row in corpus)
        by_kind: dict[str, int] = {}
        for row in corpus:
            by_kind[row["kind"]] = by_kind.get(row["kind"], 0) + 1

        # Embed the corpus ONCE (in-memory path only): every element then scores its few query texts
        # against this cached matrix, so the encode cost is O(全库 + Σ元素查询) not O(元素 × 全库).
        object_matrix, ref_order = self._embed_corpus_once(corpus)
        l2_ran = False

        dismissed = set(exclusions or ())
        dismissed_count = 0
        judged_count = 0
        judge_skipped = 0
        results: list[RubricElementResult] = []

        for element in rubric.elements:
            l1_findings, l1_refs = self._l1_lexical(element, corpus)
            l2_findings, l2_refs, l2_scores = self._l2_concept(
                element, corpus, object_matrix, ref_order, set(l1_refs), semantic_threshold
            )
            if l2_findings or (self._l2_active() and element.kind == StyleElementKind.SEMANTIC):
                l2_ran = l2_ran or self._l2_active()

            l3_findings: list[SweepFinding] = []
            l3_refs: list[str] = []
            if use_llm and self.gateway is not None and l2_findings:
                l3_findings, l3_refs, judged, skipped = self._l3_judge(
                    element, corpus, l2_findings, l2_scores, max_judge, progress
                )
                judged_count += judged
                judge_skipped += skipped
                # L3 upgrades a confirmed L2 review to a hit (same object-grain id) — drop the
                # superseded review so the object is not listed twice.
                confirmed = set(l3_refs)
                l2_findings = [f for f in l2_findings if f.ref not in confirmed]

            findings = l1_findings + l2_findings + l3_findings
            surviving = [f for f in findings if f.finding_id not in dismissed]
            dismissed_count += len(findings) - len(surviving)
            results.append(
                RubricElementResult(
                    element_id=element.id,
                    element_text=element.text,
                    kind=element.kind.value,
                    polarity=element.polarity.value,
                    l1_refs=sorted(set(l1_refs)),
                    l2_refs=sorted(set(l2_refs)),
                    l3_refs=sorted(set(l3_refs)),
                    findings=sorted(surviving, key=lambda f: (f.object_kind, f.ref, f.locator)),
                )
            )

        # Live re-read AFTER embedding: a model that degraded to hashing mid-run reports False here,
        # so the coverage report never claims bge-m3 while running on the stub (mirrors sweep()).
        semantic_live = self._l2_active()
        return RubricSweepReport(
            rubric_id=rubric.id,
            rubric_label=rubric.label,
            rubric_version=rubric.version,
            rubric_fingerprint=content_hash(rubric),
            abstract_tag=rubric.abstract_tag,
            scanned_total=len(corpus),
            scanned_by_kind=by_kind,
            scanned_fields=scanned_fields,
            elements=results,
            semantic_used=semantic_live and l2_ran,
            semantic_threshold=semantic_threshold if (semantic_live and l2_ran) else 0.0,
            llm_used=bool(use_llm and self.gateway is not None),
            judged_count=judged_count,
            judge_skipped=judge_skipped,
            dismissed_count=dismissed_count,
        )

    def _l2_active(self) -> bool:
        """Whether L2 can produce a real semantic signal: either a live neural embedder or an
        injected persistent similarity provider (the caller's responsibility to only pass a real
        one)."""
        return self.similarity_fn is not None or _is_semantic(self.embedder)

    def _embed_corpus_once(
        self, corpus: list[dict[str, Any]]
    ) -> tuple[np.ndarray | None, list[str]]:
        """Normalised object matrix + parallel ref order, embedded once for the whole sweep.

        Returns ``(None, [])`` when the in-memory path is inactive (no neural embedder, or an
        injected ``similarity_fn`` handles L2 instead) so no encode happens."""
        if self.embedder is None or self.similarity_fn is not None or not corpus:
            return None, []
        ref_order = [row["ref"] for row in corpus]
        matrix = _normalise_rows(
            np.asarray(
                self.embedder.embed_many([row["text"][:_EMBED_TEXT_CHARS] for row in corpus]),
                dtype=np.float32,
            )
        )
        return (matrix if matrix.size else None), ref_order

    def _l1_lexical(
        self, element: StyleRubricElement, corpus: list[dict[str, Any]]
    ) -> tuple[list[SweepFinding], list[str]]:
        """L1: deterministic $0 word/regex hit for an element's ``terms`` + ``patterns``, sentence
        by sentence so each finding locates to the exact field/sentence. Recall-first: any element
        listing tokens is scanned regardless of its ``kind`` (a semantic element may still carry a
        few concrete tokens, and dropping them would silently lose recall)."""
        findings: list[SweepFinding] = []
        refs: list[str] = []
        patterns = _compile_patterns(element.patterns)
        if not element.terms and not patterns:
            return findings, refs
        for row in corpus:
            hit_here = False
            for field_path, field_text in row["fields"]:
                sentences = _sentences(field_text)
                multi = len(sentences) > 1
                for order, sentence in enumerate(sentences, start=1):
                    trigger = _element_lexical_trigger(element.terms, patterns, sentence)
                    if trigger is None:
                        continue
                    locator = f"{field_path}（第{order}句）" if multi else field_path
                    fid = _rubric_finding_id(row["ref"], element.id, f"{field_path}\x1f{sentence}")
                    findings.append(
                        SweepFinding(
                            ref=row["ref"],
                            name=row["name"],
                            object_kind=row["kind"],
                            layer="term",
                            evidence=_element_evidence(element, f"命中「{trigger}」：")
                            + _snippet(sentence, trigger),
                            verdict="hit",
                            locator=locator,
                            finding_id=fid,
                        )
                    )
                    hit_here = True
            if hit_here:
                refs.append(row["ref"])
        return findings, refs

    def _l2_concept(
        self,
        element: StyleRubricElement,
        corpus: list[dict[str, Any]],
        object_matrix: np.ndarray | None,
        ref_order: list[str],
        l1_refs: set[str],
        threshold: float,
    ) -> tuple[list[SweepFinding], list[str], dict[str, float]]:
        """L2: concept score for a ``semantic`` element. Objects not already caught by L1 whose
        cosine to the element's concept text/exemplars clears ``threshold`` are surfaced as REVIEW
        (never a hit — the score is a weak, threshold-sensitive signal, design §5.3). Scores are
        returned so L3 can spend its cap on the likeliest first."""
        if element.kind != StyleElementKind.SEMANTIC or not self._l2_active():
            return [], [], {}
        queries = [element.text[:_ELEMENT_QUERY_CHARS]] + [
            ex[:_ELEMENT_QUERY_CHARS] for ex in element.exemplars_pos if ex.strip()
        ]
        scores = self._concept_scores(queries, corpus, object_matrix, ref_order)
        findings: list[SweepFinding] = []
        refs: list[str] = []
        names = {row["ref"]: (row["name"], row["kind"], row["text"]) for row in corpus}
        for ref, score in scores.items():
            if ref in l1_refs or score < threshold:
                continue
            name, kind, text = names[ref]
            fid = _rubric_finding_id(ref, element.id, _OBJECT_KEY)
            findings.append(
                SweepFinding(
                    ref=ref,
                    name=name,
                    object_kind=kind,
                    layer="semantic",
                    evidence=_element_evidence(element, f"语义相近（相似度 {score:.2f}）：")
                    + text[:_SNIPPET_CHARS],
                    verdict="review",
                    locator="（对象整体·语义近似）",
                    finding_id=fid,
                )
            )
            refs.append(ref)
        return findings, refs, scores

    def _concept_scores(
        self,
        queries: list[str],
        corpus: list[dict[str, Any]],
        object_matrix: np.ndarray | None,
        ref_order: list[str],
    ) -> dict[str, float]:
        """Max cosine of each object to any of the element's query texts.

        Persistent path (``similarity_fn`` injected): reuse ``VectorRetriever.similarities`` over
        the cached ``content_vectors`` — zero re-embed. In-memory path: score the pre-embedded
        corpus matrix (embedded once in ``_embed_corpus_once``) against the embedded queries."""
        if not queries:
            return {}
        if self.similarity_fn is not None:
            refs = [row["ref"] for row in corpus]
            best: dict[str, float] = {}
            for query in queries:
                for ref, score in self.similarity_fn(query, refs).items():
                    if score > best.get(ref, -2.0):
                        best[ref] = score
            return best
        if object_matrix is None or self.embedder is None:
            return {}
        query_matrix = _normalise_rows(
            np.asarray(self.embedder.embed_many(queries), dtype=np.float32)
        )
        if query_matrix.size == 0:
            return {}
        sims = object_matrix @ query_matrix.T  # (objects × queries) → each object's best query
        best_per_object = sims.max(axis=1)
        return {ref: float(best_per_object[index]) for index, ref in enumerate(ref_order)}

    def _l3_judge(
        self,
        element: StyleRubricElement,
        corpus: list[dict[str, Any]],
        candidates: list[SweepFinding],
        scores: dict[str, float],
        max_judge: int,
        progress: Callable[[str, dict[str, Any]], None] | None,
    ) -> tuple[list[SweepFinding], list[str], int, int]:
        """L3: per-element LLM yes/no over the L2 candidate pool only (the uncertain ones) — L1's
        逐字 hits are already fact and never spend judge budget (FrugalGPT cascade). A confirmed
        candidate upgrades to a hit carrying the model's reason and best-effort quote location.

        Judged by descending concept score so a per-element cap is spent on the likeliest. Reuses
        ThemeSweepService._judge_batch, with the element's ``text`` as the theme so the offline
        judge double and the real judge share one code path."""
        rows_by_ref = {row["ref"]: row for row in corpus}
        pool = sorted(candidates, key=lambda f: (-scores.get(f.ref, 0.0), f.ref))
        to_judge = pool[:max_judge]
        skipped = len(pool) - len(to_judge)
        judged = 0
        confirmed: list[SweepFinding] = []
        confirmed_refs: list[str] = []
        terms = [element.text] + [ex for ex in element.exemplars_pos if ex.strip()]
        for start in range(0, len(to_judge), _JUDGE_BATCH_SIZE):
            batch_findings = to_judge[start : start + _JUDGE_BATCH_SIZE]
            batch_rows = [rows_by_ref[f.ref] for f in batch_findings]
            judged += len(batch_rows)
            if progress is not None:
                progress("judge", {"element": element.id, "done": judged, "total": len(to_judge)})
            for index, reason, quote in self._judge_host._judge_batch(
                element.text, terms, batch_rows
            ):
                row = batch_rows[index]
                fid = _rubric_finding_id(row["ref"], element.id, _OBJECT_KEY)
                confirmed.append(
                    SweepFinding(
                        ref=row["ref"],
                        name=row["name"],
                        object_kind=row["kind"],
                        layer="judge",
                        evidence=_element_evidence(element, "模型判定：")
                        + reason
                        + (f"（原文：{quote}）" if quote else ""),
                        verdict="hit",
                        locator=self._judge_host._locate_quote(row, quote),
                        finding_id=fid,
                    )
                )
                confirmed_refs.append(row["ref"])
        return confirmed, confirmed_refs, judged, skipped


def _compile_patterns(patterns: list[str]) -> list[re.Pattern[str]]:
    """Compile an element's optional regexes, skipping any that do not compile (a bad pattern in one
    element must never sink the whole sweep). Case-insensitive, same as the lexical term rule."""
    compiled: list[re.Pattern[str]] = []
    for raw in patterns:
        try:
            compiled.append(re.compile(raw, re.IGNORECASE))
        except re.error:
            continue
    return compiled


def _element_lexical_trigger(
    terms: list[str], patterns: list[re.Pattern[str]], sentence: str
) -> str | None:
    """The first term or pattern firing in ``sentence`` (for the evidence snippet); else None."""
    for term in terms:
        if _term_matches(term, sentence):
            return term.strip()
    for pattern in patterns:
        match = pattern.search(sentence)
        if match:
            return match.group(0)
    return None


def _element_evidence(element: StyleRubricElement, prefix: str) -> str:
    """Evidence prefix carrying the element's binary criterion + its polarity, so a reviewer reads
    WHAT was checked, not just that something matched. ``exclude`` elements are taboos: a hit is a
    violation ("禁忌命中"), an ``include`` hit is a confirmed presence."""
    tag = "禁忌命中" if element.polarity == StyleElementPolarity.EXCLUDE else "应含要素"
    return f"[{tag}·{element.text}] {prefix}"


def render_rubric_markdown(report: RubricSweepReport) -> str:
    """The rubric cascade work order, a portable checklist grouped by element.

    The FIRST content line is the coverage ceiling (design §W1, non-negotiable): everything found is
    bounded by the checklist's completeness, and elements outside the list are structurally
    invisible — stated up front so "we checked everything" is never read as a false promise."""
    lines = [
        f"# 风格核对工作单：{report.rubric_label or report.rubric_id}"
        + (f"（{report.abstract_tag}）" if report.abstract_tag else ""),
        "",
        "> ⚠ 覆盖上限 = 清单完备性：本工作单只核对清单内的 "
        f"{len(report.elements)} 条元素；**清单外的未定义元素永远扫不到，无法用工程消除**。"
        "召回以本清单版本为定义域，不等于「检查了一切」。",
        "",
        f"- 清单：{report.rubric_id} v{report.rubric_version} · 冻结指纹 "
        f"`{report.rubric_fingerprint[:12]}`",
        f"- 扫描面：全库 {report.scanned_total} 个对象 / {report.scanned_fields} 个字段（"
        + "，".join(f"{kind} {count}" for kind, count in sorted(report.scanned_by_kind.items()))
        + "）",
        "- 概念层（L2 向量）："
        + (
            f"已启用（阈值 {report.semantic_threshold:.2f}）"
            if report.semantic_used
            else "未启用（需真实语义模型；当前仅词表/正则）"
        ),
        "- 判官层（L3 模型逐条）："
        + (
            f"已启用，逐条判定 {report.judged_count} 个候选"
            if report.llm_used
            else "未启用（仅 L1 词表 + L2 概念）"
        )
        + (
            f"；**有 {report.judge_skipped} 个候选未判定（超出上限）**"
            if report.judge_skipped
            else ""
        ),
        f"- 结论：直接命中 {len(report.hits)}，关联待查 {len(report.review_suggested)}"
        + (f"；已排除（逐一排除）{report.dismissed_count} 条" if report.dismissed_count else ""),
        "",
    ]
    for element in report.elements:
        polarity = "禁忌" if element.polarity == "exclude" else "应含"
        header = (
            f"## [{polarity}] {element.element_text} "
            f"`{element.element_id}`（L1 {len(element.l1_refs)} / "
            f"L2 {len(element.l2_refs)} / L3 {len(element.l3_refs)}）"
        )
        lines += [header, ""]
        if element.findings:
            lines += [_finding_line(f) for f in element.findings]
        else:
            lines.append("（无命中）")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
