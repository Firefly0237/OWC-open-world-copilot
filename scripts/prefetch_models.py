"""Provision or verify the pinned Hugging Face artifacts used by OWCopilot.

Weights remain in the user's Hugging Face cache and are never copied into the repository. The
packaged ``owcopilot/model-revisions.json`` is the runtime source of truth; the repository-root
manifest is a human-visible mirror and this command fails if the two drift. ``--offline`` performs
no network access and fails when a requested artifact is absent or incomplete.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
import urllib.request
import zipfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from owcopilot.model_artifacts import model_artifacts  # noqa: E402

REVISION_FILE = REPOSITORY_ROOT / "model-revisions.json"

TOKENIZER_PATTERNS = (
    "config.json",
    "sentencepiece.bpe.model",
    "special_tokens_map.json",
    "tokenizer.json",
    "tokenizer_config.json",
)
WEIGHT_NAMES = ("model.safetensors", "pytorch_model.bin")


def _load_revisions() -> dict[str, dict[str, str]]:
    packaged = {
        name: {
            "repo_id": artifact.repo_id,
            "revision": artifact.revision,
            "purpose": artifact.purpose,
        }
        for name, artifact in model_artifacts().items()
    }
    payload: dict[str, Any] = json.loads(REVISION_FILE.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1 or payload.get("models") != packaged:
        raise ValueError(f"invalid model revision manifest: {REVISION_FILE}")
    for name in ("embedding", "reranker"):
        entry = packaged.get(name)
        if entry is None:
            raise ValueError(f"packaged model revision manifest lacks {name!r}")
        repo_id = entry.get("repo_id", "")
        revision = entry.get("revision", "")
        valid_revision = len(revision) == 40 and all(ch in "0123456789abcdef" for ch in revision)
        if not repo_id or not valid_revision:
            raise ValueError(f"invalid pinned model entry: {name!r}")
    return packaged


def _components(requested: str) -> tuple[str, ...]:
    if requested == "memory":
        return ("memory-tokenizer", "embedding")
    if requested == "all":
        return ("embedding", "reranker")
    if requested == "tokenizer":
        return ("tokenizer",)
    return (requested,)


def provision_memory_tokenizer(*, offline: bool) -> dict[str, str]:
    from owcopilot.workflow.memory_history import DEFAULT_TOKENIZER_PATH, TOKENIZER_SHA256

    target = DEFAULT_TOKENIZER_PATH.resolve()
    if target.is_file():
        if hashlib.sha256(target.read_bytes()).hexdigest() != TOKENIZER_SHA256:
            raise RuntimeError("memory-tokenizer: existing local artifact has an unexpected hash")
    elif offline:
        raise RuntimeError("memory-tokenizer: local artifact missing; provision without --offline")
    else:
        url = "https://cdn.deepseek.com/api-docs/deepseek_v4_tokenizer.zip"
        with urllib.request.urlopen(url, timeout=30) as response:
            archive = response.read(40_000_001)
        archive_hash = "e7310d1dafe0a86d8a5629fe78a7c763760f651db9b8682718a1781dcd6fe495"
        if len(archive) > 40_000_000 or hashlib.sha256(archive).hexdigest() != archive_hash:
            raise RuntimeError(
                "memory-tokenizer: official download differs from the pinned artifact"
            )
        with zipfile.ZipFile(io.BytesIO(archive)) as package:
            data = package.read("deepseek_v4_tokenizer/tokenizer.json")
        if hashlib.sha256(data).hexdigest() != TOKENIZER_SHA256:
            raise RuntimeError("memory-tokenizer: extracted tokenizer hash is invalid")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return {
        "component": "memory-tokenizer",
        "sha256": TOKENIZER_SHA256,
        "snapshot_path": str(target),
        "status": "verified" if offline else "provisioned",
    }


def _verify_snapshot(component: str, snapshot: Path, revision: str) -> None:
    if snapshot.name != revision:
        raise RuntimeError(
            f"{component}: resolved revision {snapshot.name!r}, expected pinned {revision!r}"
        )
    required = ("config.json", "tokenizer_config.json")
    missing = [name for name in required if not (snapshot / name).is_file()]
    tokenizer_names = ("tokenizer.json", "sentencepiece.bpe.model")
    if not any((snapshot / name).is_file() for name in tokenizer_names):
        missing.append("tokenizer.json|sentencepiece.bpe.model")
    if component != "tokenizer" and not any((snapshot / name).is_file() for name in WEIGHT_NAMES):
        missing.append("model.safetensors|pytorch_model.bin")
    if missing:
        raise RuntimeError(f"{component}: incomplete pinned snapshot; missing {', '.join(missing)}")


def provision(component: str, *, offline: bool) -> dict[str, str]:
    if component == "memory-tokenizer":
        return provision_memory_tokenizer(offline=offline)
    try:
        from huggingface_hub import snapshot_download
    except ImportError as exc:  # pragma: no cover - exercised by a clean environment
        raise RuntimeError(
            "huggingface-hub is required; install .[tokenizer] or .[semantic] first"
        ) from exc

    models = _load_revisions()
    model_key = "embedding" if component == "tokenizer" else component
    entry = models[model_key]
    kwargs: dict[str, Any] = {
        "repo_id": entry["repo_id"],
        "revision": entry["revision"],
        "local_files_only": offline,
        # Hugging Face's Windows symlink capability probe is not thread-safe: concurrent first
        # downloads can observe the optimistic cache value and raise WinError 1314 before the
        # probe records the copy fallback. Serial provisioning is a one-time cost and works on
        # non-admin Windows as well as POSIX.
        "max_workers": 1,
    }
    if component == "tokenizer":
        kwargs["allow_patterns"] = list(TOKENIZER_PATTERNS)
    try:
        snapshot = Path(snapshot_download(**kwargs)).resolve()
    except Exception as exc:  # noqa: BLE001 - turn provider/cache failures into one CLI contract
        mode = "offline verification" if offline else "download"
        raise RuntimeError(f"{component}: pinned snapshot {mode} failed: {exc}") from exc
    _verify_snapshot(component, snapshot, entry["revision"])
    return {
        "component": component,
        "repo_id": entry["repo_id"],
        "revision": entry["revision"],
        "snapshot_path": str(snapshot),
        "status": "verified" if offline else "provisioned",
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--component",
        choices=("tokenizer", "embedding", "reranker", "all", "memory-tokenizer", "memory"),
        default="tokenizer",
        help="artifact to provision; tokenizer avoids downloading model weights",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="forbid network access and verify the pinned artifact already exists locally",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        results = [provision(name, offline=args.offline) for name in _components(args.component)]
    except (OSError, RuntimeError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps({"ok": True, "artifacts": results}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
