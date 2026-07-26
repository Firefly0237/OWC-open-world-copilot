# Changelog

All notable changes to this package are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow SemVer.

## [Unreleased]

### Added

- `DialogueTreeSO` runtime type mirroring the OWCopilot `DialogueTree` contract: the branching
  conversation graph (nodes/choices/effects) stored in one asset, nodes serialized by managed
  reference (`[SerializeReference]`) so choice/next links can share instances and form cycles. The
  two-pass importer now materializes dialogue trees, wires per-node speaker + owning-quest + intra-tree
  choice/next references, adds them to `ContentRegistrySO`, and counts them in the import report.
  `SoToRunner.FromTree` bridges an imported tree into the Core `DialogueWalker`, and the Quest Runner
  window can walk one in-editor.
- Opt-in orphan cleanup (default off, two-tiered): **orphan scan** reports assets under the tool's
  output folders whose id is no longer in the bundle; **orphan delete** additionally offers to remove
  them, gated by a confirmation dialog (never silent) and scoped strictly to the tool's own folders.

## [0.1.0] - 2026-07-11

### Added

- Initial content-import UPM package (`com.owcopilot.content-import`).
- Runtime ScriptableObject data types mirroring the OWCopilot content contract: `EntitySO`,
  `QuestSO` (with the native `QuestLogic` layer — variables, preconditions, effects, branches),
  `DialogueSO`, `RelationSO`, and a `ContentRegistrySO` (serialized id → asset lookup).
- Engine-free `Core` assembly: Newtonsoft DTOs, `ContentBundleParser`, and the two-pass
  `ContentBundleResolver` (string-id → target resolution with honest "kept for review" warnings).
- Editor importer (`Tools > OWCopilot > Import Content Bundle`): materializes assets, wires object
  references, rebuilds the registry, preserves GUIDs on re-import (in-place overwrite), and an
  opt-in `AssetPostprocessor` re-import watcher (default off).
- NUnit contract tests over a fixture that is byte-identical to the real Python exporter output.
