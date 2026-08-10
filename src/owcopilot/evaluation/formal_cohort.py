"""Canonical completion manifest for one four-suite formal-evaluation cohort.

The cohort is the publication boundary for the unified runner.  Suite reports and traces may be
written incrementally, but they are not a complete run until ``cohort.json`` is written last.  The
helpers in this module intentionally depend on relative paths and file bytes only, so an
independently copied formal-governance tree has the same digest.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Final, Literal, Self, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

FORMAL_COHORT_SCHEMA_VERSION: Final[Literal["formal-eval-cohort/v1"]] = "formal-eval-cohort/v1"
FORMAL_COHORT_SUITE_IDS: Final = (
    "game_planner_vertical_e2e",
    "agent_trust_gold",
    "failure_isolation_matrix",
    "semantic_shadow_eval",
)

FormalCohortSuiteId: TypeAlias = Literal[
    "game_planner_vertical_e2e",
    "agent_trust_gold",
    "failure_isolation_matrix",
    "semantic_shadow_eval",
]
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
_BASE_RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,95}$")
_GOVERNANCE_DIGEST_DOMAIN = b"owcopilot-formal-governance-tree/v1\x00"


class FormalCohortError(ValueError):
    """A cohort or its frozen-governance digest is malformed."""


class FormalCohortCanonicalError(FormalCohortError):
    """Serialized cohort bytes are valid JSON but not the canonical representation."""


class _DuplicateKeyError(ValueError):
    pass


class _CohortModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class FormalCohortSuiteEntry(_CohortModel):
    """Hashes for one suite directory within a completed cohort."""

    suite_id: FormalCohortSuiteId
    run_id: str = Field(min_length=1)
    report_sha256: Sha256
    trace_sha256s: tuple[Sha256, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def require_unique_trace_hashes(self) -> Self:
        if len(self.trace_sha256s) != len(set(self.trace_sha256s)):
            raise ValueError("FORMAL_COHORT_TRACE_HASHES_MUST_BE_UNIQUE")
        return self


class FormalEvalCohort(_CohortModel):
    """Completion record for exactly one invocation of all four frozen suites."""

    schema_version: Literal["formal-eval-cohort/v1"]
    base_run_id: str = Field(min_length=1, max_length=96)
    generated_at: datetime
    governance_sha256: Sha256
    suites: tuple[FormalCohortSuiteEntry, ...] = Field(min_length=4, max_length=4)

    @model_validator(mode="after")
    def require_exact_ordered_suite_cohort(self) -> Self:
        if _BASE_RUN_ID_PATTERN.fullmatch(self.base_run_id) is None:
            raise ValueError("FORMAL_COHORT_BASE_RUN_ID_INVALID")
        if self.generated_at.tzinfo is None or self.generated_at.utcoffset() is None:
            raise ValueError("FORMAL_COHORT_GENERATED_AT_REQUIRES_OFFSET")
        actual_ids = tuple(entry.suite_id for entry in self.suites)
        if actual_ids != FORMAL_COHORT_SUITE_IDS:
            raise ValueError("FORMAL_COHORT_REQUIRES_EXACT_ORDERED_FOUR_SUITES")
        for entry in self.suites:
            expected_run_id = f"{self.base_run_id}.{entry.suite_id}"
            if entry.run_id != expected_run_id:
                raise ValueError(f"FORMAL_COHORT_SUITE_RUN_ID_MISMATCH:{entry.suite_id}")
        return self


def sha256_bytes(content: bytes) -> str:
    """Return the lowercase SHA-256 digest used by cohort and verifier code."""

    return hashlib.sha256(content).hexdigest()


def render_formal_cohort(cohort: FormalEvalCohort) -> bytes:
    """Render the one canonical UTF-8 JSON representation of a cohort."""

    return (
        json.dumps(
            cohort.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateKeyError(f"duplicate JSON key: {key!r}")
        value[key] = item
    return value


def _reject_non_finite(value: str) -> None:
    raise ValueError(f"non-finite JSON number: {value}")


def parse_formal_cohort_bytes(content: bytes) -> FormalEvalCohort:
    """Parse final bytes and require strict JSON, model validity, and canonical encoding."""

    try:
        payload = json.loads(
            content.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_finite,
        )
        cohort = FormalEvalCohort.model_validate(payload)
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        _DuplicateKeyError,
        ValueError,
        ValidationError,
    ) as exc:
        raise FormalCohortError(f"invalid formal cohort: {exc}") from exc
    if render_formal_cohort(cohort) != content:
        raise FormalCohortCanonicalError("formal cohort bytes are not canonical")
    return cohort


def _is_link_like(path: Path) -> bool:
    junction_check = getattr(path, "is_junction", None)
    return path.is_symlink() or bool(junction_check and junction_check())


def _governance_files(formal_root: str | Path) -> tuple[tuple[str, bytes], ...]:
    root_path = Path(formal_root)
    try:
        root = root_path.resolve(strict=True)
    except OSError as exc:
        raise FormalCohortError(
            f"formal governance root is unavailable: {root_path}: {exc}"
        ) from exc
    if not root.is_dir():
        raise FormalCohortError(f"formal governance root is not a directory: {root}")

    artifacts: list[tuple[str, bytes]] = []
    try:
        for current_text, directory_names, file_names in os.walk(
            root, topdown=True, followlinks=False
        ):
            current = Path(current_text)
            relative_directory = current.relative_to(root)
            directory_names.sort()
            if not relative_directory.parts:
                # Generated run cohorts are outputs, never governance inputs.
                directory_names[:] = [name for name in directory_names if name != "runs"]
            for directory_name in directory_names:
                directory = current / directory_name
                if _is_link_like(directory):
                    relative = directory.relative_to(root).as_posix()
                    raise FormalCohortError(
                        f"formal governance directory symlink is forbidden: {relative}"
                    )
                try:
                    directory.resolve(strict=True).relative_to(root)
                except (OSError, ValueError) as exc:
                    relative = directory.relative_to(root).as_posix()
                    raise FormalCohortError(
                        f"formal governance directory escapes root: {relative}"
                    ) from exc
            for file_name in sorted(file_names):
                path = current / file_name
                relative = path.relative_to(root).as_posix()
                if _is_link_like(path) or not path.is_file():
                    raise FormalCohortError(
                        f"formal governance artifact must be a regular file: {relative}"
                    )
                try:
                    path.resolve(strict=True).relative_to(root)
                except (OSError, ValueError) as exc:
                    raise FormalCohortError(
                        f"formal governance artifact escapes root: {relative}"
                    ) from exc
                artifacts.append((relative, path.read_bytes()))
    except OSError as exc:
        raise FormalCohortError(f"cannot read formal governance tree {root}: {exc}") from exc
    artifacts.sort(key=lambda item: item[0])
    if not artifacts:
        raise FormalCohortError("formal governance tree contains no files")
    return tuple(artifacts)


def compute_formal_governance_sha256(formal_root: str | Path) -> str:
    """Hash sorted repository-relative paths and bytes, excluding only generated ``runs``."""

    digest = hashlib.sha256()
    digest.update(_GOVERNANCE_DIGEST_DOMAIN)
    for relative, content in _governance_files(formal_root):
        path_bytes = relative.encode("utf-8")
        digest.update(len(path_bytes).to_bytes(8, byteorder="big", signed=False))
        digest.update(path_bytes)
        digest.update(len(content).to_bytes(8, byteorder="big", signed=False))
        digest.update(content)
    return digest.hexdigest()


__all__ = [
    "FORMAL_COHORT_SCHEMA_VERSION",
    "FORMAL_COHORT_SUITE_IDS",
    "FormalCohortCanonicalError",
    "FormalCohortError",
    "FormalCohortSuiteEntry",
    "FormalEvalCohort",
    "compute_formal_governance_sha256",
    "parse_formal_cohort_bytes",
    "render_formal_cohort",
    "sha256_bytes",
]
