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
import re
import zipfile
from pathlib import Path
from typing import Any

from ..content.models import ContentBundle
from ..content.store import ContentStore

# Runtime state and local-only history are rebuildable / machine-local and stay out of world packs.
_PACK_EXCLUDE_DIRS = {".owcopilot", ".git", ".snapshots"}
_NAME_MAX_LEN = 48
# Windows reserves these device names: a directory called CON/NUL/COM1 fails or behaves
# pathologically (this app runs on win32), so a world can never be named one.
_RESERVED_WIN_NAMES = (
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)
# A world is JSON/text and realistically a few MB; cap decompressed pack size so a zip bomb
# (tiny compressed, gigabytes inflated) can't exhaust memory/disk before any content check.
_MAX_PACK_UNCOMPRESSED = 500 * 1024 * 1024


def _default_path() -> Path:
    return Path.home() / ".owcopilot" / "recent_workspaces.json"


def worlds_home(base: str | Path | None = None) -> Path:
    return Path(base) if base is not None else Path.home() / ".owcopilot" / "worlds"


def sanitize_world_name(name: str) -> str:
    """A world name doubles as its directory name: strip path-hostile characters but keep
    CJK (Windows and POSIX are both fine with it)."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", str(name)).strip().strip(".")
    cleaned = re.sub(r"\s+", " ", cleaned)[:_NAME_MAX_LEN].strip()
    if not cleaned:
        raise ValueError("世界名称不能为空（或全是非法字符）。")
    # Windows reserves device names regardless of extension (CON, CON.txt, ...), so check the stem.
    if cleaned.split(".")[0].upper() in _RESERVED_WIN_NAMES:
        raise ValueError(f"「{cleaned}」是系统保留名称，请换一个。")
    return cleaned


def list_managed_worlds(base: str | Path | None = None) -> list[dict[str, Any]]:
    """Managed worlds, most recently modified first."""
    home = worlds_home(base)
    if not home.exists():
        return []
    worlds = [
        {"name": child.name, "path": str(child)} for child in home.iterdir() if child.is_dir()
    ]
    worlds.sort(key=lambda w: Path(w["path"]).stat().st_mtime, reverse=True)
    return worlds


def create_managed_world(name: str, *, base: str | Path | None = None) -> Path:
    target = worlds_home(base) / sanitize_world_name(name)
    if target.exists() and any(target.iterdir()):
        raise ValueError(f"世界「{target.name}」已存在。换个名字，或直接在列表里选中它。")
    target.mkdir(parents=True, exist_ok=True)
    ContentStore(target).save(ContentBundle())
    return target


def export_world_zip(root: str | Path) -> bytes:
    """Pack a content root into a portable world pack (runtime/.git excluded)."""
    source = Path(root)
    if not source.exists():
        raise FileNotFoundError(f"content root does not exist: {source}")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as pack:
        for path in sorted(source.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(source)
            if relative.parts and relative.parts[0] in _PACK_EXCLUDE_DIRS:
                continue
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
    target = worlds_home(base) / sanitize_world_name(name)
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
    home = worlds_home(base).resolve()
    target = (worlds_home(base) / sanitize_world_name(name)).resolve()
    if target.parent != home:
        raise ValueError("非法的世界名称，已拒绝删除。")
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
