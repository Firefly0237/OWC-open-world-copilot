"""Revision-pinned model artifact manifest shipped inside the Python package."""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files
from typing import Any


@dataclass(frozen=True)
class ModelArtifact:
    component: str
    repo_id: str
    revision: str
    purpose: str


def is_immutable_revision(value: str | None) -> bool:
    """Return whether ``value`` is a full lowercase Git commit SHA."""
    return bool(
        value
        and len(value) == 40
        and all(char in "0123456789abcdef" for char in value)
    )


@lru_cache(maxsize=1)
def model_artifacts() -> dict[str, ModelArtifact]:
    resource = files("owcopilot").joinpath("model-revisions.json")
    payload: dict[str, Any] = json.loads(resource.read_text(encoding="utf-8"))
    raw_models = payload.get("models")
    if payload.get("schema_version") != 1 or not isinstance(raw_models, dict):
        raise ValueError("invalid packaged model revision manifest")
    result: dict[str, ModelArtifact] = {}
    for component, raw in raw_models.items():
        if not isinstance(component, str) or not isinstance(raw, dict):
            raise ValueError("invalid packaged model entry")
        repo_id = raw.get("repo_id")
        revision = raw.get("revision")
        purpose = raw.get("purpose", "")
        if not isinstance(repo_id, str) or not repo_id:
            raise ValueError(f"model artifact {component!r} has no repo_id")
        if not isinstance(revision, str) or not is_immutable_revision(revision):
            raise ValueError(f"model artifact {component!r} has no immutable SHA revision")
        result[component] = ModelArtifact(
            component=component,
            repo_id=repo_id,
            revision=revision,
            purpose=str(purpose),
        )
    return result


def model_artifact(component: str) -> ModelArtifact:
    try:
        return model_artifacts()[component]
    except KeyError:
        raise KeyError(f"no pinned model artifact for component {component!r}") from None


def model_artifact_for_repo(repo_id: str) -> ModelArtifact | None:
    return next(
        (artifact for artifact in model_artifacts().values() if artifact.repo_id == repo_id),
        None,
    )


__all__ = [
    "ModelArtifact",
    "is_immutable_revision",
    "model_artifact",
    "model_artifact_for_repo",
    "model_artifacts",
]
