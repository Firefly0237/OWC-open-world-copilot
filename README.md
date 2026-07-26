<div align="center">

# OWCopilot

**A local-first AI workbench for open-world game narrative and worldbuilding —
keep a sprawling world consistent, searchable, and ready to ship.**

English | [简体中文](README.zh-CN.md)

![tests](https://img.shields.io/badge/tests-passing-brightgreen)
![python](https://img.shields.io/badge/python-3.11%2B-blue)
![frontend](https://img.shields.io/badge/frontend-Vue%203%20%2B%20Vite-42b883)
![license](https://img.shields.io/badge/license-MIT-lightgrey)

<img src="docs/images/overview.png" alt="OWCopilot workbench — world overview" width="920">

</div>

## The idea in one line

**An AI assistant whose every suggestion is checked by a machine before it can land.**

Narrative teams fear two things above all: *continuity breaks* (a character contradicts an earlier
scene) and *dead links* (a branch that can never be entered, or a state the player cannot leave).
At tens of thousands of lines, across many versions, with several writers in parallel, neither is
catchable by eye. Existing narrative tools help you **write** content; none of them tell you
**what your last change broke**.

So OWCopilot is built as two halves plus one feedback edge:

```
     AI side                          Deterministic side
 (reads, drafts, edits,   ──────►   (34 rules · logic simulation ·
  proposes repairs)                  branch exhaustion · temporal checks)
        ▲                                       │
        └────────── verdict feeds back ─────────┘
```

The right half never lies, so it doubles as a **reward signal for the left half** — no human
labelling, no learned reward model. A proposal that would add new errors is dropped before a human
ever sees it, and a tree search uses the same signal to find the repair sequence that clears the
most errors.

## Features

- 🗂️ **World content management** — factions, characters, locations, quests, events and their
  relations live as plain, versionable files. Import from JSON / CSV / XLSX (Chinese headers
  supported), Markdown, or full manuscripts.
- 🛡️ **Consistency checking** — reference integrity, timeline order, quest logic (deadlocks,
  unreachable stages, undefined variables), dialogue conditions, localization pre-flight and
  more. Every finding comes with its location and evidence, ready to jump to and fix.
- 🕸️ **Impact analysis** — before you change a faction, character or event, see exactly which
  quests, dialogues and regions are affected.
- 🔮 **World Q&A** — ask about a single fact or the shape of the whole world and get answers
  with source citations. When the world doesn't contain the answer, it tells you what's missing
  instead of guessing.
- 🎭 **Grounded generation** — draft a whole starting world from one sentence, or expand
  regions, quests, characters, barks and dialogue trees. Generated content only references
  entities that exist in your world and is refined through an automatic critique pass before it
  reaches your review queue.
- 🤖 **Agent loop** — a reason-and-act agent diagnoses a world through a read-only/propose-only
  tool surface: permissions converge at a single adjudication point, failures come back as
  machine-readable error codes it can recover from, oversized observations are paged rather than
  truncated, and it **can never write canon** (a structural ban, not a convention). Checkpointed,
  resumable, and pausable for human approval.
- ⚒️ **Repair loop** — every detected issue comes with fix candidates you can apply in one
  click and roll back just as easily. Candidates are **shadow-validated**: applied to a copy,
  re-audited, and dropped if they would introduce new errors. A Monte-Carlo tree search over the
  fix space uses "errors cleared" as its reward — deterministic, reproducible, and free.
- ✦ **Visual editing** — a draggable relationship star-graph, a chronology timeline, a dialogue
  flow editor, and snapshot diffs between world versions.
- 📦 **Delivery** — export the world as a checksummed data bundle plus CSV / XLIFF 1.2
  localization files; quest rows edited on the engine side sync back for review.

## Quick start

```bash
git clone <repo> && cd openworld
python -m venv .venv && .venv/Scripts/pip install -e ".[dev,serve]"   # Windows
# source .venv/bin/activate && pip install -e ".[dev,serve]"          # macOS/Linux
# optional: ".[semantic]" adds multilingual semantic retrieval (downloads a model once)

# sanity check: build the built-in bilingual sample world and run the quality gates
.venv/Scripts/python -m owcopilot.cli.main eval-acceptance --workspace .tmp/demo
```

### Launch the workbench

```bash
npm --prefix frontend install && npm --prefix frontend run build   # first time only
.venv/Scripts/python -m uvicorn owcopilot.service.api:create_app --factory --port 8000
# open http://localhost:8000 — one process; worlds live in ~/.owcopilot/worlds/
```

A guided tour walks you through every area on first launch. If you're not comfortable with a
terminal, have a teammate run the commands above once — after that, everything happens in the
browser.

**First run starts empty.** Populate your world by *importing* what you already have
(manuscripts, spreadsheets, Markdown — under **Import**) or *generating* from a sentence
(under **Create**, needs a connected model).

**Bring your own model:** open Settings, pick a provider (DeepSeek / OpenAI / Anthropic / Kimi /
Zhipu / Qwen / Doubao / custom), paste your API key, and test the connection. The key stays in
local process memory and calls the provider directly. Browsing, checking, reviewing and
exporting all work without any model connected.

## Using OWCopilot

- **Web workbench** — the main experience: create, import, check, ask, review and export from
  the browser.
- **CLI** — `audit`, `impact`, `ask`, `draft` / `expand`, `export`, `eval-acceptance` and more;
  JSON output, non-zero exit on unresolved errors — drops straight into CI.
- **REST API** — resource-oriented endpoints per project, with server-sent events for
  long-running jobs.
- **MCP server** — exposes the diagnosis / retrieval / proposal toolset to AI assistants and
  agent frameworks.

## How it works

```
  existing material · new ideas · engine-side edits
              ↓
  content hub — structured, versioned, searchable
              ↓
  quality layer — consistency audit · impact analysis · timeline & quest-logic checks
              ↓
  assistance layer — cited world Q&A · grounded drafts · repair proposals
              ↓
  review — approve, edit or reject; every change traceable and reversible
              ↓
  delivery — world data bundle · localization files · engine round-trip
```

## What is actually verified

This project distinguishes **"implemented"** from **"verified"**, and says which is which.

| Area | Status |
|---|---|
| Deterministic checks | 34 rules, 7 of them with automatic fixers. Full offline test suite (~2380 tests). |
| Agent diagnosis loop | Verified against a real model: 5 steps, US$0.004, completed — it called the audit itself and translated the machine output into planner-readable findings. |
| Repair search | Verified: one seeded error → cleared in one iteration, US$0.00 (fully deterministic path, no model call). |
| **Unreal Engine data layer** | **Verified on real UE 5.7.2** — tables filled and read back with zero drift, pinned by a regression test that runs the headless editor. Condition expressions are compiled to structured clauses at export time, so the engine side needs no expression parser and no C++. |
| **The value of the deterministic layer** | **Measured, not asserted.** A single-variable controlled experiment (72 real runs, 12 seeded worlds, k=3): with the checker in the agent's toolset it finds **100%** of real defects and fabricates **0**; with the checker removed it finds **11.6%** and makes **279** unsupported claims — and reports false alarms on a clean world **3 times out of 3**. Confidence intervals do not overlap. |
| Unity package | Code complete (importer, runtime runner, cross-language differential fixtures) but **never compiled or executed** — treated as unverified. |

**Known limits, stated on purpose:** no large-scale benchmark exists, so no throughput numbers are
claimed; multi-agent orchestration has architecture tests but **no end-to-end evaluation**; the
agent is reachable from the CLI only (no REST/UI entry yet); context compression and replanning
exist but were not triggered in the recorded live run.

Design notes, verification records and the industry-alignment audit live under [`docs/`](docs/).

## Tech stack

Python 3.11+ · FastAPI · Pydantic v2 · NetworkX · SQLite (WAL) — Vue 3 · Vite · TypeScript,
with hand-rolled deterministic SVG for all visual views. Works with any OpenAI-compatible LLM
provider.

## License

[MIT](LICENSE)
