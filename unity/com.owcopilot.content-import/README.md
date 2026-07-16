# OWCopilot Content Import (com.owcopilot.content-import)

Editor tooling that turns OWCopilot's engine-agnostic `content_bundle.json` handoff into **native
Unity ScriptableObject assets** — entities, quests (with the native quest-logic layer), dialogues,
dialogue trees (the branching-conversation graph), and relations — with all string-id references
wired into real object references in a two-pass import.

Text-only UPM package. No vendored DLLs (Newtonsoft is a declared dependency), no committed
`.uasset`, no Unity project. Drops into any project via `Packages/` or a git URL.

## Install

- **Embedded:** copy `com.owcopilot.content-import/` into your project's `Packages/` folder.
- **Git URL** (from this monorepo subfolder — note `?path=` must precede `#`):

  ```
  "com.owcopilot.content-import": "https://github.com/<owner>/openworld.git?path=/unity/com.owcopilot.content-import#v0.1.0"
  ```

`com.unity.nuget.newtonsoft-json` is pulled automatically as a dependency.

## Use

`Tools > OWCopilot > Import Content Bundle` opens a window: pick a `content_bundle.json`, pick an
output folder under `Assets/`, click **Import**. Assets are written under
`Assets/OWCopilot/Content/{Entities,Quests,Dialogues,DialogueTrees,Relations}` plus a
`ContentRegistry.asset` (id → asset lookup). Re-importing overwrites assets **in place**, so GUIDs —
and every scene/prefab reference to them — survive.

A `DialogueTreeSO` keeps the whole conversation graph inside one asset (nodes serialized by managed
reference so a choice can point back at a sibling node, cycles included). `Tools > OWCopilot > Quest
Runner (Playtest)` can walk an imported tree through the same engine-free Core runner the quests use.

Unresolved references (an id with no matching object) are **kept on the asset and reported**, never
fabricated — the same honest behaviour as OWCopilot's `recognize/engine_data.py`.

Optional toggles, all **off** by default so engine imports stay human-gated:

- **Auto-reimport** re-runs the import when an in-project `content_bundle.json` changes (via
  `AssetPostprocessor`).
- **Orphan scan** lists assets under the output folders whose id is no longer in the bundle (a
  source-side deletion) into the import report — report-only.
- **Orphan delete** (only with scan on) additionally offers to delete those orphans, but **never
  silently**: a confirmation dialog gates every deletion, and only the tool's own output folders are
  ever scanned.

## Layout

| Folder | Assembly | Ships in build? | Purpose |
| --- | --- | --- | --- |
| `Runtime/` | `com.owcopilot.content-import` | yes | ScriptableObject data types |
| `Core/` | `…​.Core` (`noEngineReferences`) | yes | Newtonsoft parse + two-pass resolve (engine-free, off-engine testable) |
| `Editor/` | `…​.Editor` (Editor-only) | no | importer, window/menu, AssetPostprocessor |
| `Tests/Editor/` | `…​.Editor.Tests` | no | NUnit contract tests over the real-exporter fixture |

See `../../docs/UNITY_INTEGRATION.md` for the field mapping, the verification tiers, and the honest
"needs a real Unity/dotnet toolchain to confirm" checklist.
