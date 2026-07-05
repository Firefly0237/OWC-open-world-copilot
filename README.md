<div align="center">

# OWCopilot

**A local-first AI workbench for open-world game narrative and worldbuilding —
keep a sprawling world consistent, searchable, and ready to ship.**

English | [简体中文](README.zh-CN.md)

![tests](https://img.shields.io/badge/tests-1623_passing-brightgreen)
![python](https://img.shields.io/badge/python-3.11%2B-blue)
![frontend](https://img.shields.io/badge/frontend-Vue%203%20%2B%20Vite-42b883)
![license](https://img.shields.io/badge/license-MIT-lightgrey)

<img src="docs/images/overview.png" alt="OWCopilot workbench — world overview" width="920">

</div>

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
- ⚒️ **Repair loop** — every detected issue comes with fix candidates you can apply in one
  click and roll back just as easily.
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

## Tech stack

Python 3.11+ · FastAPI · Pydantic v2 · NetworkX · SQLite (WAL) — Vue 3 · Vite · TypeScript,
with hand-rolled deterministic SVG for all visual views. Works with any OpenAI-compatible LLM
provider.

## License

[MIT](LICENSE)
