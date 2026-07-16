"""Prompt registry (deepening L1): every model-facing prompt is registered, resolvable, and
reviewable — and a drift test guarantees a new prompt constant cannot be added silently.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from owcopilot.prompts import PROMPT_SPECS, prompt_registry

SRC = Path("src/owcopilot")

#: Module-level prompt-constant naming pattern the drift scan enforces registration for.
_CONST_RE = re.compile(r"^(_[A-Z_]*(?:SYSTEM|PROMPT|SHAPE)[A-Z_]*) *=", re.M)


def test_names_unique_and_fields_complete() -> None:
    names = [spec.name for spec in PROMPT_SPECS]
    assert len(names) == len(set(names))
    for spec in PROMPT_SPECS:
        assert spec.purpose and spec.kind and spec.output_contract and spec.source
        assert spec.kind in {"static", "template", "fragment", "builder"}
        assert spec.version >= 1
        # every non-fragment prompt runs under a real router task label
        if spec.kind != "fragment":
            assert spec.task, f"{spec.name} has no task label"


def test_every_spec_resolves_to_a_live_object() -> None:
    """The registry references live objects — never copies. A renamed/deleted constant or
    builder breaks here immediately (anti-drift, direction: registry → code)."""
    for spec in PROMPT_SPECS:
        obj = spec.resolve()
        if spec.kind == "builder":
            assert callable(obj), f"{spec.name} builder is not callable"
        else:
            text = spec.text()
            assert text and len(text) > 20, f"{spec.name} text suspiciously short"


def test_template_slots_present() -> None:
    """Template prompts must actually contain their documented slot; the native prompt is
    static by design (the tool list travels in the tools schema, not in the prompt)."""
    reg = prompt_registry()
    assert "{manifest}" in (reg["agent.react.system.text"].text() or "")
    assert "{manifest}" not in (reg["agent.react.system.native"].text() or "")
    assert reg["agent.react.system.native"].kind == "static"


def test_all_prompt_constants_in_src_are_registered() -> None:
    """Anti-drift, direction: code → registry. Scan src for module-level *_SYSTEM/_PROMPT/_SHAPE
    constants; each must be referenced by some spec's source. Registering MORE than the scan
    finds is fine (builders, worldgen fragments); registering LESS fails this test."""
    registered_sources = {spec.source for spec in PROMPT_SPECS}
    missing: list[str] = []
    for path in SRC.rglob("*.py"):
        content = path.read_text(encoding="utf-8")
        for match in _CONST_RE.finditer(content):
            const = match.group(1)
            module = ".".join(path.relative_to("src").with_suffix("").parts)
            source = f"{module}:{const}"
            if source not in registered_sources:
                missing.append(source)
    assert not missing, (
        f"unregistered prompt constant(s) — add them to owcopilot.prompts.PROMPT_SPECS: {missing}"
    )


def test_cli_prompts_list_and_show(capsys) -> None:
    from owcopilot.cli.main import main

    assert main(["prompts"]) == 0
    listing = json.loads(capsys.readouterr().out)
    assert listing["count"] == len(PROMPT_SPECS)
    assert any(p["name"] == "qa.faithfulness.judge" for p in listing["prompts"])

    assert main(["prompts", "--show", "qa.faithfulness.judge"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["task"] == "faithfulness_judge"
    assert "supported" in shown["text"]  # the actual prompt text is rendered

    # unknown name → guided error (exit 2 via the CLI's error envelope)
    assert main(["prompts", "--show", "nope.nope"]) == 2
