from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from scripts import prefetch_models

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    ("dockerfile", "cache_contract"),
    (
        ("Dockerfile", "COPY --from=builder --chown=10001:10001 /opt/huggingface"),
        ("infra/team/Dockerfile", "chown -R appuser:appuser /opt/huggingface"),
    ),
)
def test_runtime_images_ship_only_the_pinned_tokenizer(
    dockerfile: str,
    cache_contract: str,
) -> None:
    text = (REPOSITORY_ROOT / dockerfile).read_text(encoding="utf-8")
    install_lines = [line for line in text.splitlines() if "pip install" in line and ".[" in line]
    prefetch_lines = [
        line for line in text.splitlines() if "prefetch_models.py" in line and "COPY" not in line
    ]

    assert len(install_lines) == 1
    assert "tokenizer" in install_lines[0]
    assert len(prefetch_lines) == 2
    assert all("--component tokenizer" in line for line in prefetch_lines)
    assert all(
        forbidden not in " ".join(prefetch_lines)
        for forbidden in ("--component all", "--component embedding", "--component reranker")
    )
    assert any("--offline" in line for line in prefetch_lines)
    assert "HF_HOME=/opt/huggingface" in text
    assert "HF_HUB_OFFLINE=1" in text
    assert "TRANSFORMERS_OFFLINE=1" in text
    assert "chmod -R a-w /opt/huggingface" in text
    assert cache_contract in text


def test_tokenizer_prefetch_allowlist_excludes_model_weights(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    revision = prefetch_models._load_revisions()["embedding"]["revision"]
    snapshot = tmp_path / revision
    snapshot.mkdir()
    for name in ("config.json", "tokenizer_config.json", "tokenizer.json"):
        (snapshot / name).write_text("{}", encoding="utf-8")

    captured: dict[str, Any] = {}

    def fake_snapshot_download(**kwargs: Any) -> str:
        captured.update(kwargs)
        return str(snapshot)

    fake_hub = ModuleType("huggingface_hub")
    fake_hub.snapshot_download = fake_snapshot_download  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "huggingface_hub", fake_hub)

    result = prefetch_models.provision("tokenizer", offline=False)

    assert result["revision"] == revision
    assert captured["revision"] == revision
    assert captured["max_workers"] == 1
    assert captured["allow_patterns"] == list(prefetch_models.TOKENIZER_PATTERNS)
    assert not set(prefetch_models.WEIGHT_NAMES) & set(prefetch_models.TOKENIZER_PATTERNS)
