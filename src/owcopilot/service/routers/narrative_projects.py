"""No-code project authoring and real cross-scene preview, under creative/v2."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from ...workflow.project_v2 import ClaimInput, NarrativeProjectStore, ProjectOutline, SourceInput
from .workflows import _call, _store

router = APIRouter(prefix="/narrative-projects")


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Create(_Input):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=16000)


class Change(_Input):
    expected_project_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class Outline(Change):
    outline: ProjectOutline


class Source(Change):
    source: SourceInput
    source_id: str | None = None
    expected_source_hash: str | None = None


class Claim(Change):
    claim: ClaimInput


class ClaimDecision(Change):
    accept: StrictBool
    reject_claim_ids: list[str] = Field(default_factory=list, max_length=100)


class Bind(Change):
    artifact_id: str
    expected_artifact_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class Preview(Change):
    initial_state: dict[str, Any] | None = None


class Rebase(Change):
    expected_preview_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class Restore(Change):
    operation_id: str | None = Field(default=None, min_length=1, max_length=200)


class Step(_Input):
    action_id: str = Field(min_length=1)
    expected_seq: int = Field(ge=0)
    choice_id: str | None = None
    transition_id: str | None = None


class Archive(_Input):
    name: str = Field(min_length=1, max_length=200)
    expected_seq: int = Field(ge=0)


def _project_store(project):
    return NarrativeProjectStore(_store(project))


@router.post("/{project_id}/previews/{preview_id}/compatibility")
def preview_compatibility(project: str, project_id: str, preview_id: str, req: Rebase):
    return _call(
        _project_store(project).preview_compatibility,
        project,
        project_id,
        preview_id,
        **req.model_dump(),
    )


@router.post("/{project_id}/previews/{preview_id}/continue-original")
def continue_original(project: str, project_id: str, preview_id: str, req: Rebase):
    return _call(
        _project_store(project).continue_original_preview,
        project,
        project_id,
        preview_id,
        **req.model_dump(),
    )


@router.get("")
def projects(project: str):
    return {"projects": _call(_project_store(project).list, project)}


@router.post("", status_code=201)
def create(project: str, req: Create):
    return {"project": _call(_project_store(project).create, project, req.name, req.description)}


@router.get("/{project_id}")
def detail(project: str, project_id: str):
    store = _project_store(project)
    return {
        "project": _call(store.get, project, project_id),
        "sources": _call(store.sources, project, project_id),
        "claims": _call(store.claims, project, project_id),
    }


@router.post("/{project_id}/outline")
def outline(project: str, project_id: str, req: Outline):
    return {
        "project": _call(
            _project_store(project).save_outline,
            project,
            project_id,
            req.outline,
            expected_project_hash=req.expected_project_hash,
        )
    }


@router.post("/{project_id}/sources", status_code=201)
def source(project: str, project_id: str, req: Source):
    return _call(
        _project_store(project).add_source,
        project,
        project_id,
        req.source,
        expected_project_hash=req.expected_project_hash,
        source_id=req.source_id,
        expected_source_hash=req.expected_source_hash,
    )


@router.post("/{project_id}/claims", status_code=201)
def claim(project: str, project_id: str, req: Claim):
    return _call(
        _project_store(project).record_claim,
        project,
        project_id,
        req.claim,
        expected_project_hash=req.expected_project_hash,
    )


@router.post("/{project_id}/claims/{claim_id}/decision")
def decide(project: str, project_id: str, claim_id: str, req: ClaimDecision):
    return _call(
        _project_store(project).decide_claim,
        project,
        project_id,
        claim_id,
        accept=req.accept,
        reject_claim_ids=req.reject_claim_ids,
        expected_project_hash=req.expected_project_hash,
    )


@router.post("/{project_id}/scenes/{scene_id}/artifact")
def bind(project: str, project_id: str, scene_id: str, req: Bind):
    return _call(
        _project_store(project).bind_scene_artifact,
        project,
        project_id,
        scene_id,
        req.artifact_id,
        expected_project_hash=req.expected_project_hash,
        expected_artifact_hash=req.expected_artifact_hash,
    )


@router.post("/{project_id}/previews", status_code=201)
def preview(project: str, project_id: str, req: Preview):
    return _call(
        _project_store(project).create_preview,
        project,
        project_id,
        expected_project_hash=req.expected_project_hash,
        initial_state=req.initial_state,
    )


@router.post("/{project_id}/previews/{preview_id}/step")
def step(project: str, project_id: str, preview_id: str, req: Step):
    return _call(
        _project_store(project).step_preview, project, project_id, preview_id, **req.model_dump()
    )


@router.get("/{project_id}/previews/{preview_id}")
def get_preview(project: str, project_id: str, preview_id: str):
    return _call(_project_store(project).get_preview, project, project_id, preview_id)


@router.post("/{project_id}/previews/{preview_id}/rebase")
def rebase(project: str, project_id: str, preview_id: str, req: Rebase):
    return _call(
        _project_store(project).rebase_preview, project, project_id, preview_id, **req.model_dump()
    )


@router.get("/{project_id}/archives")
def archives(project: str, project_id: str):
    return {"archives": _call(_project_store(project).archives, project, project_id)}


@router.post("/{project_id}/previews/{preview_id}/archives", status_code=201)
def archive(project: str, project_id: str, preview_id: str, req: Archive):
    return _call(
        _project_store(project).save_archive, project, project_id, preview_id, **req.model_dump()
    )


@router.post("/{project_id}/archives/{archive_id}/restore")
def restore(project: str, project_id: str, archive_id: str, req: Restore):
    return _call(
        _project_store(project).restore_archive,
        project,
        project_id,
        archive_id,
        expected_project_hash=req.expected_project_hash,
        operation_id=req.operation_id,
    )


@router.get("/{project_id}/export")
def export(project: str, project_id: str):
    return _call(_project_store(project).export_project, project, project_id)
