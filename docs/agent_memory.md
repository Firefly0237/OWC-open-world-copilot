# Agent Memory & the data closed-loop (数据闭环)

OWCopilot ships an **Agent Memory / data closed-loop** subsystem — the pieces were built
incrementally and named per-module, so this document names the *whole* as one subsystem and maps it
to the capability a game-Agent role calls "记忆与数据建设" (JD 职责4) and "长期记忆系统"
(加分4). Scope, stated up front: this is **project-level, review-derived** memory (a queue +
calibration + lesson archive + retrieval index), **not** a per-agent learned/episodic memory model —
every write still passes human review. No new logic is introduced here; every stage below is
existing, tested code.

Inspect it live on any workspace with:

```
owcopilot memory-report --content-root <path>
```

## The loop

```
                 ┌─────────────────────────────────────────────────────────────┐
                 │                                                             ▼
   generation / critique          ┌──────────────┐        ┌───────────────────────────┐
   (worldgen, assist) ───drafts──▶│ review_queue │──────▶ │ human accept / reject      │
                                   │ (only canon- │        │ decision (HITL)            │
   ┌───────────────────────────┐  │  write path) │        └──────────────┬────────────┘
   │ retrieval over the        │  └──────────────┘                       │
   │ versioned content store   │◀────accepted canon────┐                 ▼
   │ (bm25 + vector + graph)   │                        │   ┌────────────────────────────┐
   └───────────────────────────┘                        │   │ calibration                │
                 ▲                                       │   │ confusion matrix + Wilson  │
                 │                                       │   │ 95% false-pass interval    │
                 │                                       │   └──────────────┬─────────────┘
                 │                                       │                  │
                 │                              (canon)  │                  ▼
                 │                                       │   ┌────────────────────────────┐
   ┌─────────────┴──────────────┐                       │   │ lessons                    │
   │ critic / generation prompts│◀──────fed back────────┼───│ cross-session lesson       │
   │ (dimension-aware)          │                       │   │ archive (fixed-template)   │
   └────────────────────────────┘                       │   └────────────────────────────┘
                                                         │
                              preference-data export ────┘
                              (dpo_export, handoff data only)
```

## Stages, with the code that implements each

| Stage | What it does | Implementation |
|-------|--------------|----------------|
| **Write path** | Every AI-authored artifact lands in a review queue keyed by kind (quest / dialogue-tree / character-profile / world-seed / …); it is the *only* path by which content becomes canon. | `assist/review_queue.py::ReviewQueue` (persisted in `storage/sqlite.py`) |
| **Human decision** | A reviewer accepts or rejects each item; the decision plus the critic's verdict are stored together. | `ReviewQueue.mark`, `app/actions.py` |
| **Calibration** | Over resolved items, build a confusion matrix (critic verdict × human decision) and the **false-pass rate** — critic said "pass" but the human rejected — with a **95% Wilson interval** and a `sufficient_sample` guard so small samples don't over-claim. | `assist/calibration.py::build_calibration_report`, `_wilson_interval` |
| **Lessons** | Distil recurring false-passes into a cross-session **lesson archive** (fixed-template text — no LLM, no hallucination; min false-pass count, ≤3 lessons/prompt, 90-day deprioritisation). Lessons feed forward two ways: the quest **generation** prompt (drafts.py) and the quest **critic** (same archive, stricter on human-rejected dimensions). Write triggers: the REST calibration endpoint and `owcopilot memory-report --write-lessons` (CLI-only workflows). Other content types (character/dialogue/flavor/bark) do not read lessons yet — an honest scope note, not an oversight. | `assist/lessons.py`, consumed by `assist/drafts.py` (generation + critic injection) |
| **Preference-data export** | Export review/refine traces as JSONL — a genuine **data closed-loop** artifact. Some rows are weak signals; newer snapshot-backed `complete_pairs` are DPO-format handoff data for downstream experiments. This repo does **not** train, fine-tune, produce weights, or switch runtime models. | `assist/dpo_export.py` |
| **Retrieval memory** | Accepted canon is indexed and recalled by the hybrid retriever (lexical + dense + graph) over the **versioned** content store, so later generation grounds on prior accepted state. | `retrieval/`, `content/store.py`, `storage/sqlite.py` |

## How it maps to the JD

- **职责4 记忆与数据建设 — Agent Memory 体系 + 数据闭环**: the loop above *is* a data closed-loop:
  drafts → human decision → calibration → lessons fed back → export → retrieval memory. NPC/quest
  state, character relations and progress live in the versioned content store and are recalled by
  the retriever.
- **加分4 长期记忆系统 / Eval / 自动化测试**: the lesson archive is a cross-session, review-derived
  long-term memory (Reflexion-style feed-forward, not a learned memory model); calibration is the
  eval feedback signal; both are covered by the `$0` deterministic test suite.

## Honesty notes

- The lesson text is **fixed-template**, generated from calibration counts — it never invents
  content, so it cannot hallucinate a "lesson" that was not observed.
- `dpo_export` is a **preference-data export** layer, not a training layer. It can write
  DPO-format `complete_pairs.jsonl` when both sides are recoverable, but model training/alignment
  lives in a **separate** project, not here (this repo does not train models, produce weights, or
  claim a fine-tuned runtime model).
- `memory-report` reads already-persisted state; it runs no model and costs nothing.
