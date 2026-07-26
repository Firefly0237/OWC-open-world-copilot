"""Export manifest models for engine-friendly file bundles."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum

from pydantic import BaseModel, Field


class EngineTarget(str, Enum):
    # GENERIC = the engine-agnostic data + localization handoff (content_bundle.json + XLIFF/CSV).
    # UNREAL additionally emits UE DataTable row-array JSON + StringTable CSVs + a row-struct header
    # under an ``unreal/`` subdir (a superset of GENERIC — the generic bundle is still written).
    # UNITY additionally emits ``unity/unity_import_manifest.json`` (bundle-relative path + per-
    # category content counts + schema version), also a superset of GENERIC: the
    # com.owcopilot.content-import UPM importer reads the SAME generic content_bundle.json, and the
    # sidecar manifest is what that importer and a human reviewer cross-check the import against.
    GENERIC = "generic"
    UNREAL = "unreal"
    UNITY = "unity"


class ExportedFile(BaseModel):
    path: str
    kind: str
    sha256: str


class ExportManifest(BaseModel):
    target_engine: EngineTarget = EngineTarget.GENERIC
    content_hash: str
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    files: list[ExportedFile] = Field(default_factory=list)
