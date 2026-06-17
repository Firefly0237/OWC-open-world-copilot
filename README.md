<div align="center">

# OWCopilot

**A local-first AI workbench for open-world game narrative & worldbuilding.**

Keep a sprawling world consistent, searchable, and trustworthy — with a deterministic
consistency engine in front, the LLM behind it, and a human signing off at the end.

English | [简体中文](README.zh-CN.md)

![tests](https://img.shields.io/badge/tests-passing-brightgreen)
![python](https://img.shields.io/badge/python-3.11%2B-blue)
![frontend](https://img.shields.io/badge/frontend-Vue%203%20%2B%20Vite-42b883)
![license](https://img.shields.io/badge/license-MIT-lightgrey)

</div>

---

## What it is

Modern open-world game scripts routinely reach into the millions of words across many locales.
Past a certain size, *keeping the lore from contradicting itself is no longer something a human can
hold in their head* — one "faction flipped the wrong way" gets multiplied into N rounds of rework
across every localized language.

OWCopilot is not "AI that writes your game for you." It is **AI that helps you check, retrieve, and
organize** what you've written:

- A **content graph** — every faction, character, location, quest, event and relation, stored as
  plain files you can diff and version in git.
- A **deterministic consistency audit** that proves "this references something that doesn't exist"
  or "this prerequisite happens after the quest that needs it" — with structured evidence, no LLM.
- **Cited retrieval Q&A** over the world: every answer carries its sources, and *if the world
  doesn't say, it refuses to make something up.*
- **Constrained generation** (quests, characters, dialogue trees — or a grounded first-draft world
  from one sentence, as a *starting point*, never a substitute for your authorship) that may only
  reference entities already in the graph, runs through a critique→refine loop, and lands in a
  **review queue** — a human's approval is the only way AI content reaches canon.
- **Editable visual views** — a star-map relationship graph, a chronology timeline, a Detroit-style
  branching dialogue editor, and a snapshot diff. Edit on the canvas and it writes straight to your
  files, through the same pipeline as editing by hand.
- **Data + localization export** — the world as a checksummed `content_bundle.json` plus
  localization in **XLIFF 1.2 / CSV** (UI length caps carried as `maxwidth`) — and **engine
  import** that reads edited quest rows back into the human review queue.

It runs as **one local process** (FastAPI serving a Vue frontend), a **CLI** for CI gates, a
**REST API** for services, and an **MCP server** for the agent ecosystem.

## What it focuses on

OWCopilot is built around a narrow, practical loop: **structured content in files → deterministic
checks → grounded AI suggestions → human approval → audited delivery.** It is designed to help teams
keep large narrative worlds reviewable, explainable, and ready to hand off.

| Focus | What OWCopilot does |
|---|---|
| Structured canon | Stores lore as a content graph backed by plain, git-friendly files. |
| Deterministic QA | Finds broken references, timeline conflicts, quest-logic issues, dialogue-scope problems, localization risks, and injection patterns with structured evidence. |
| Grounded assistance | Answers with citations, refuses unsupported claims, and keeps generated drafts constrained to known entities. |
| Human control | Routes AI drafts and repairs through review, provenance, shadow re-audit, operator logs, and rollback. |
| Delivery | Exports checksummed data bundles plus CSV/XLIFF localization, and can read edited quest rows back into review. |

The core discipline is intentionally simple: **deterministic checks → LLM suggestion → human
confirmation → write to canon → re-audit.** Every object carries its `origin` (human / ai_draft /
ai_patch) and `review_status`, so an AI-participation report is one command away.

**Where it fits.** OWCopilot is a consistency, retrieval, review, and delivery layer for narrative
pipelines. Use it when your world needs to live in version control, when teams need to understand
the impact of changes before editing, when AI assistance must stay grounded in approved canon, and
when delivery requires clean data plus standard localization files. It layers over your existing
writing and engine workflow instead of trying to become the place where every creative decision is
made.

## Features

| Pillar | What it does |
|---|---|
| 🗂️ **Content hub** | Import from JSON / CSV / XLSX (incl. Chinese headers) / Markdown, dry-run by default. Content *is* files; SQLite only holds rebuildable runtime state. |
| 📖 **Catches any input** | Manuscript extraction covers a whole novel via **adaptive full-coverage chunking** (granularity coarsens to stay within a bounded budget — never a silent truncation; truly enormous docs report the uncovered tail honestly). Automatic **language detection** answers in the source language and keeps proper nouns verbatim; the inspiration library indexes a whole book as RAG chunks. Language and granularity are the system's job, not a knob for the user. |
| 🛡️ **Consistency audit** | Deterministic rules (reference integrity, graph relations, world-lore, region/level, **quest logic — deadlock / unreachable stage / undefined variable / faction-reputation ref**, dialogue-condition scope, localization pre-flight, injection scan, AI-trust) with structured evidence. Audits the content **authored in OWCopilot** (the source of truth); it is not an importer that lints external narrative-tool files. A baseline ratchet lets a legacy project adopt the CI gate the same day. |
| 🕸️ **Impact analysis** | Before you change a row, see the blast radius: a pure graph walk yields a "must change / suggest check" list — zero cost, zero hallucination. |
| 🔮 **Retrieval Q&A** | Two-stage hybrid RAG: three-way recall — BM25 + **real multilingual semantic vectors** (bge-m3, local; downloaded once on first run, then $0/offline) + entity-anchored graph expansion — and RRF fusion, then a **hybrid reranker** (lexical signals + query↔document cosine) lifts the on-topic document to the top so even a tight context window contains the answer, then a token budgeter. Cross-lingual and paraphrase queries that share no words with the canon still retrieve. Answers cite their sources; **no source, no answer.** |
| ⚒️ **Repair loop** | issue → deterministic fixer + LLM candidates → **re-audit on a shadow copy** (a candidate that adds new errors is discarded) → human apply (operator logged) → one-click rollback. |
| 🎭 **Constrained generation** | Staged grounded world creation, expansion, quests, characters, dialogue trees — each referencing only in-graph entities, each running a critique→refine loop, each entering the review queue. World creation can be **grounded directly in what you brought in** (draw on the inspiration library + your approved canon) and generates in the source language. |
| ✦ **Visual views (editable)** | Relationship star-graph (drag, connect, focus, ripple-preview), chronology timeline (reorder, flag violations), dialogue flow editor, and canon snapshot diff. Layout is computed deterministically; edits go to canon through the normal pipeline. |
| 📦 **Data + localization delivery** | The world as a checksummed `content_bundle.json` (universal handoff any importer reads) + localization as **CSV and XLIFF 1.2** (the CAT/TMS standard; UI char-caps carried as `maxwidth`). Every artifact in a `sha256` manifest. **Engine import** back-syncs quest rows edited engine-side into the review queue. (Per-engine *code* generation was dropped: engine schemas differ project to project, so there is no portable one-click paradigm — the value is clean data + the standard localization format.) |
| 💰 **Cost engineering** | Every model call goes through one gateway: two-tier cache, cascade routing, output caps, per-action cost readout and budget guards. Offline default is **$0**. |

## Quick start

```bash
git clone <repo> && cd openworld
python -m venv .venv && .venv/Scripts/pip install -e ".[dev,serve]"   # Windows
# source .venv/bin/activate && pip install -e ".[dev,serve]"          # macOS/Linux
# add ".[semantic]" for real multilingual semantic retrieval — the first run downloads bge-m3 once
# (needs network); after that it runs fully local at $0. Without it, retrieval falls back to BM25 + graph.

# 60-second sanity check: build the built-in bilingual sample world and run every quality gate
.venv/Scripts/python -m owcopilot.cli.main eval-acceptance --workspace .tmp/demo
```

`eval-acceptance` builds a 65-entity / 10-region / 36-quest bilingual world and verifies five gates
(zero false positives on a clean world, 25/25 seeded errors caught, 100% impact recall, 30/30
retrieval hits, grounded-or-refuse Q&A). It needs no API key — the offline deterministic doubles
keep the whole thing **$0**.

### Launch the workbench

```bash
npm --prefix frontend install && npm --prefix frontend run build   # first time only
.venv/Scripts/python -m uvicorn owcopilot.service.api:create_app --factory --port 8000
# open http://localhost:8000 — one process, zero config; worlds live in ~/.owcopilot/worlds/
```

The sidebar is organized by stage: **Overview** (world summary / archive) · **Create**
(world genesis / characters / quests · dialogue · barks · flavor) · **Import** (manuscript
extraction / spreadsheets / inspiration) · **Analyze** (audit & repair / impact / sweep /
**timeline** / **relationship star-graph**) · **Q&A & Deliver** (world Q&A / review queue / export) ·
**Manage** (workspaces / **change history** / settings). A guided tour walks every area on first run.

**First run starts empty** — there is no bundled sample world. Populate yours either by *importing*
what you already have (manuscript / CSV / XLSX / Markdown, under **Import**) or *generating* from a
sentence (under **Create**, needs a connected model). The empty Overview points you to **Manage · Worlds**
to create or open one.

**Bring your own model:** open Settings, pick a provider (DeepSeek / OpenAI / Anthropic / Kimi /
Zhipu / Qwen / Doubao / custom), paste your API key, choose a model, and Test Connection. Your key
lives only in the local process memory and calls the provider directly — there is no middle server.
You can browse, review, and export with no model connected at all.

## Architecture

```
  Workbench UI       CLI            REST           MCP (read-only tools)
        └───────────────┴──────┬───────┴───────────────┘
                       pipeline/  fixed workflows (audit · patches · review · ingest)
         ┌───────────────────────┼───────────────────────┐
   audit/   deterministic    patches/  suggest+shadow    assist/  drafts · dialogue · lint
   impact/  graph propagation qa/       cited Q&A · refuse retrieval/ recall · rerank · budget
   graph/   content graph + views (timeline · star-graph · dialogue flow · diff)
         └───────────────────────┼───────────────────────┐
   content/  files are the source of truth    storage/  SQLite (WAL) runtime cache
                       llm/  the one gateway: cache · routing · telemetry · retries · token caps
```

**Tech stack:** Python 3.11+, FastAPI, Pydantic v2, NetworkX, SQLite (WAL); Vue 3 + Vite + TypeScript
(zero graph-viz dependencies — the visual views are hand-rolled, deterministic SVG); OpenAI-compatible
LLM gateway. Offline deterministic doubles make the entire test suite run at **$0**.

## Surfaces

- **Web workbench** — the main path; one process, Vue served by FastAPI.
- **CLI** — `audit` (CI gate, exits non-zero on unresolved errors), `impact`, `suggest`/`apply`/
  `rollback`, `ask`, `draft`/`expand`, `export`, `eval-acceptance`. Every command prints JSON.
- **REST API** — resource-oriented per project, SSE for long jobs; `OWCOPILOT_API_KEY` gates
  paid `llm_mode=real` calls (fail-closed for non-local clients).
- **MCP server** — read-only tools for the agent ecosystem (write actions are deliberately not
  exposed).

## Documentation

- [Design report](project_docs/设计报告.md) — architecture, key design decisions, and milestones (zh)
- [Interview prep handbook](project_docs/面试准备手册.md) — 490 big-tech AI-agent interview Q&A mapped to this project (zh)
- [Agent learning handbook](project_docs/Agent学习手册.md) — agent engineering taught through this codebase (zh)
- [CHANGELOG](CHANGELOG.md)

## License

[MIT](LICENSE)
