from __future__ import annotations

import json
from importlib.resources import files
from pathlib import Path

from owcopilot.model_artifacts import model_artifact, model_artifact_for_repo, model_artifacts


def test_repository_manifest_is_an_exact_mirror_of_packaged_manifest() -> None:
    repository_root = Path(__file__).resolve().parents[2]
    root_payload = json.loads(
        (repository_root / "model-revisions.json").read_text(encoding="utf-8")
    )
    packaged_payload = json.loads(
        files("owcopilot").joinpath("model-revisions.json").read_text(encoding="utf-8")
    )
    assert root_payload == packaged_payload


def test_model_artifact_lookup_is_revision_pinned_and_unambiguous() -> None:
    artifacts = model_artifacts()
    assert set(artifacts) == {"embedding", "reranker"}
    for component, artifact in artifacts.items():
        assert model_artifact(component) is artifact
        assert model_artifact_for_repo(artifact.repo_id) is artifact
        assert len(artifact.revision) == 40
        assert set(artifact.revision) <= set("0123456789abcdef")
