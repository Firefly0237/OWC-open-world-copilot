# OWCopilot Content Import — package documentation

This folder is hidden from the Unity Project window (the trailing `~`), so nothing here is imported
as an asset and it carries no `.meta` files.

The package [README](../README.md) and this quick reference define the field mapping, two-pass import
contract, Runtime/Core/Editor/Tests assembly split, and honest verification tiers (what is locked
offline versus what still needs a real Unity or dotnet toolchain).

Quick reference:

- Import UI: `Tools > OWCopilot > Import Content Bundle`.
- Output: `Assets/OWCopilot/Content/{Entities,Quests,Dialogues,Relations}` + `ContentRegistry.asset`.
- Dependency: `com.unity.nuget.newtonsoft-json` (declared in `package.json`, never vendored).
- Re-import preserves GUIDs by overwriting assets in place.
- Unresolved id references are kept on the asset and reported, never fabricated.
