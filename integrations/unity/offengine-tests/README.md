# Off-engine tests (no Unity required)

`dotnet test` harness for the **engine-free Core** of `com.owcopilot.content-import`. It compiles the
Core sources (which the UPM package marks `noEngineReferences: true`) plus the UnityEngine-free NUnit
tests, so the exact same test source that runs in Unity EditMode also runs on a machine with no
editor — the honest, machine-independent verification path for the cross-language quest-runner
conformance (differential test against the Python `simulate_quest` / `walk_dialogue_tree` oracle).

```
dotnet test integrations/unity/offengine-tests/OWCopilot.ContentImport.OffEngine.Tests.csproj
```

Requires the **.NET 8 SDK**. This repository's environment has the .NET *runtime* but no SDK, so the
project is written and documented here but **not** compiled or run in-repo — no claim is made that the
C# built or the tests passed. The package [README](../com.owcopilot.content-import/README.md) records
the runtime/editor split and verification boundary.

Kept OUTSIDE the UPM package on purpose: building it here would drop `obj/`/`bin/` next to the
sources, and the package gate (`tests/test_unity_package.py`) requires the package tree to stay
text-only with no build scaffolding.
