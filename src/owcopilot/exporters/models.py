"""Export manifest models for engine-friendly file bundles."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum

from pydantic import BaseModel, Field


class EngineTarget(str, Enum):
    GENERIC = "generic"
    UNITY = "unity"
    UNREAL = "unreal"


class ExportedFile(BaseModel):
    path: str
    kind: str
    sha256: str


class ExportManifest(BaseModel):
    target_engine: EngineTarget = EngineTarget.GENERIC
    content_hash: str
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    files: list[ExportedFile] = Field(default_factory=list)
