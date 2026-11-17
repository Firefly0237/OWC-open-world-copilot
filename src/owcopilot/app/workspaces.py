# 工作台的工作区簿记（多世界切换）。
"""Workspace bookkeeping for the Workbench.

Two ways to hold a world, mirroring how shipped tools (LegendKeeper, NovelAI lorebooks)
handle user content:

* Managed worlds — app-owned storage under `~/.owcopilot/worlds/<name>/`. The default.
  Users pick a NAME, never type a path; worlds travel as zip "world packs" (import or
  export the whole content root). This is the only mode that survives a future move to a
  hosted deployment, where the server cannot read client paths at all.
* Custom paths — point the Workbench at any directory (git checkouts, network drives).
  Kept as the advanced option; the recent-paths list belongs to this mode.
"""

from __future__ import annotations

import io
import json
import os
import zipfile
from pathlib import Path
from typing import Any

from ..content.models import ContentBundle
from ..content.store import ContentStore
from ..trust.identifiers import resolve_path_segment, validate_path_segment
from ..trust.security import PathSecurityError, resolve_under_root

# Runtime state and local-only history are rebuildable / machine-local and stay out of world packs.
_PACK_EXCLUDE_DIRS = {".owcopilot", ".git", ".snapshots"}
_NAME_MAX_LEN = 64
# A world is JSON/text and realistically a few MB; cap decompressed pack size so a zip bomb
# (tiny compressed, gigabytes inflated) can't exhaust memory/disk before any content check.
_MAX_PACK_UNCOMPRESSED = 500 * 1024 * 1024


def _default_path() -> Path:
    return Path.home() / ".owcopilot" / "recent_workspaces.json"


def worlds_home(base: str | Path | None = None) -> Path:
    """The managed-worlds root: an explicit ``base`` wins, then ``OWCOPILOT_WORLDS_HOME``
    (so a container can mount a data volume there), then the local per-user default."""
    if base is not None:
        return Path(base)
    env_home = os.environ.get("OWCOPILOT_WORLDS_HOME")
    if env_home:
        return Path(env_home)
    return Path.home() / ".owcopilot" / "worlds"


def sanitize_world_name(name: str) -> str:
    """Validate a world directory name and return it byte-for-byte unchanged.

    The public name is also its identifier.  Silently deleting hostile characters used to create
    aliases (``../evil`` and ``evil`` became the same world), so invalid input now fails closed.
    """
    return validate_path_segment(name, label="世界名", max_length=_NAME_MAX_LEN)


def managed_world_path(name: str, *, base: str | Path | None = None) -> Path:
    """Resolve one managed-world name as a direct, non-symlink child of ``worlds_home``."""
    return resolve_path_segment(worlds_home(base), name, label="世界名", max_length=_NAME_MAX_LEN)


def list_managed_worlds(base: str | Path | None = None) -> list[dict[str, Any]]:
    """Managed worlds, most recently modified first."""
    home = worlds_home(base).expanduser().resolve()
    if not home.exists():
        return []
    worlds: list[dict[str, Any]] = []
    for child in home.iterdir():
        try:
            resolved = managed_world_path(child.name, base=home)
        except ValueError:
            # A symlink/junction is not a managed world and must not turn listing into an external
            # directory oracle. Invalid legacy entries remain on disk for manual recovery.
            continue
        if resolved.is_dir():
            worlds.append({"name": child.name, "path": str(resolved)})
    worlds.sort(key=lambda w: Path(w["path"]).stat().st_mtime, reverse=True)
    return worlds


def create_managed_world(name: str, *, base: str | Path | None = None) -> Path:
    target = managed_world_path(name, base=base)
    if target.exists() and any(target.iterdir()):
        raise ValueError(f"世界「{target.name}」已存在。换个名字，或直接在列表里选中它。")
    target.mkdir(parents=True, exist_ok=True)
    ContentStore(target).save(ContentBundle())
    return target


def open_managed_world(
    name: str,
    *,
    version: str = "v1",
    base: str | Path | None = None,
    sqlite_path: str | Path = ":memory:",
    embedder: Any = None,
) -> Any:
    """Open a managed world by NAME as a scoped ProjectContext -- the multi-world "switch".

    Managed worlds are already separate content roots under ``worlds_home``
    (create/list/delete/import/export above), so their content is isolated by construction. This
    ties that identity to the ``(world_id, version)`` scope: the world's sanitized name becomes the
    ProjectContext's ``world_id`` and ``version`` selects the copy-on-write line, so the scope
    dimension reflects the actual world+version -- self-describing, and correct even if a future
    deployment shares one runtime DB across worlds (the ``world_id`` column keeps them apart)."""
    from ..pipeline.project import ProjectContext

    world_name = sanitize_world_name(name)
    world_dir = managed_world_path(world_name, base=base)
    if not world_dir.exists() or not world_dir.is_dir():
        raise ValueError(f"世界「{world_name}」不存在。请先创建或导入它。")
    return ProjectContext.open(
        world_dir,
        world_id=world_name,
        version=version,
        sqlite_path=sqlite_path,
        embedder=embedder,
    )


def export_world_zip(root: str | Path) -> bytes:
    """Pack a content root into a portable world pack (runtime/.git excluded)."""
    source = Path(root).expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(f"content root does not exist: {source}")
    # Resolve and validate the full read set before opening the archive writer. A file/directory
    # symlink inside a world must not turn a pack download into an outside-root file disclosure.
    portable_files: list[tuple[Path, Path]] = []
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        if relative.parts and relative.parts[0] in _PACK_EXCLUDE_DIRS:
            continue
        resolved = resolve_under_root(source, path)
        if resolved != path:
            raise PathSecurityError(f"world pack entry {relative} is a symlink or path alias")
        if resolved.is_file():
            portable_files.append((relative, resolved))

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as pack:
        for relative, path in portable_files:
            pack.writestr(str(relative).replace("\\", "/"), path.read_bytes())
    return buffer.getvalue()


def _strip_common_top(names: list[str]) -> str:
    """Foreign packs often wrap everything in one top folder; detect it so
    `myworld/quests/...` lands as `quests/...`."""
    tops = {name.split("/", 1)[0] for name in names}
    if len(tops) == 1 and all("/" in name for name in names):
        return next(iter(tops)) + "/"
    return ""


def import_world_zip(data: bytes, name: str, *, base: str | Path | None = None) -> Path:
    """Restore a world pack into a new managed world.

    Defenses: zip-slip members are rejected outright; after extraction the directory must
    load as a content bundle (and contain at least one object) or it is rolled back.
    """
    target = managed_world_path(name, base=base)
    if target.exists() and any(target.iterdir()):
        raise ValueError(f"世界「{target.name}」已存在。换个名字再导入。")
    try:
        pack = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise ValueError("这不是一个有效的 zip 文件。") from e
    member_names = [m.filename for m in pack.infolist() if not m.is_dir()]
    if not member_names:
        raise ValueError("世界包是空的。")
    # Reject a decompression bomb BEFORE writing anything: a world pack is small text, so a pack
    # that inflates to hundreds of MB is corrupt or hostile.
    total_uncompressed = sum(m.file_size for m in pack.infolist() if not m.is_dir())
    if total_uncompressed > _MAX_PACK_UNCOMPRESSED:
        raise ValueError("世界包解压后体积异常巨大，已拒绝导入（疑似损坏或恶意压缩包）。")
    # Traversal scan happens on the RAW names, before any top-folder stripping —
    # otherwise "../evil" reads as a common top folder and the slip sails through.
    for raw_name in member_names:
        normalized = raw_name.replace("\\", "/")
        if normalized.startswith("/") or any(part == ".." for part in normalized.split("/")):
            raise ValueError(f"世界包内含非法路径，已拒绝导入：{raw_name}")
    prefix = _strip_common_top(member_names)
    target.mkdir(parents=True, exist_ok=True)
    target_resolved = target.resolve()
    try:
        for member in pack.infolist():
            if member.is_dir():
                continue
            relative = member.filename[len(prefix) :] if prefix else member.filename
            if not relative:
                continue
            destination = (target / relative).resolve()
            if target_resolved not in destination.parents:
                raise ValueError(f"世界包内含非法路径，已拒绝导入：{member.filename}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(pack.read(member))
        bundle = ContentStore(target).load()
        if not _bundle_has_content(bundle):
            raise ValueError(
                "解包成功但没有读到任何世界内容——这可能不是 OWCopilot 世界包。"
                "（世界包应包含 world/entities、quests 等目录）"
            )
    except Exception:
        _remove_tree(target)
        raise
    return target


def delete_managed_world(name: str, *, base: str | Path | None = None) -> None:
    """Delete a managed world's directory (and its rebuildable runtime/snapshots with it).

    Destructive and irreversible, so it is fenced hard: the resolved target must be a DIRECT child
    of ``worlds_home`` — belt-and-suspenders over name sanitization, so no crafted name can ever
    point ``rmtree`` outside the managed-worlds folder."""
    target = managed_world_path(name, base=base)
    if not target.exists() or not target.is_dir():
        raise ValueError(f"世界「{target.name}」不存在。")
    _remove_tree(target)


def _bundle_has_content(bundle: ContentBundle) -> bool:
    # every content collection counts — a pack of only localized strings or only event refs is still
    # a real world (the old check omitted localized_texts + quest_event_refs and rejected them).
    return bool(
        bundle.entities
        or bundle.quests
        or bundle.regions
        or bundle.pois
        or bundle.dialogues
        or bundle.dialogue_trees
        or bundle.terms
        or bundle.relations
        or bundle.style_guides
        or bundle.localized_texts
        or bundle.quest_event_refs
    )


def _remove_tree(root: Path) -> None:
    import shutil

    shutil.rmtree(root, ignore_errors=True)


def load_recent_workspaces(path: str | Path | None = None) -> list[str]:
    target = Path(path) if path is not None else _default_path()
    if not target.exists():
        return []
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(data, list):
        return []
    return [str(item) for item in data if str(item).strip()]


def remember_workspace(
    root: str | Path,
    *,
    path: str | Path | None = None,
    limit: int = 8,
) -> list[str]:
    """Push `root` to the front of the recent list (deduped, capped) and persist it."""
    target = Path(path) if path is not None else _default_path()
    entry = str(Path(root).resolve())
    recent = [item for item in load_recent_workspaces(target) if item != entry]
    recent.insert(0, entry)
    recent = recent[:limit]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(recent, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return recent
