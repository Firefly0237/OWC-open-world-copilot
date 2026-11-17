"""Portable skill documents and bounded, non-executable structured package import."""

from __future__ import annotations

import hashlib
import io
import json
import re
import stat
import zipfile

from ..content.hash import content_hash
from .skill_cards import SkillCards, SkillDefinition

_FORMAT = "owc-procedural-skill"
_MAX_ARCHIVE_BYTES = 512 * 1024
_MAX_TOTAL_BYTES = 256 * 1024
_MAX_FILE_BYTES = 128 * 1024
_REFERENCE = "references/owc-skill.json"
_PAYLOAD_FILES = {"SKILL.md", _REFERENCE}
_ALL_FILES = _PAYLOAD_FILES | {"manifest.json"}
_UNRECORDED = "not-recorded-in-package"


def _json(value):
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _portable_definition(definition):
    result = SkillDefinition.model_validate(definition).model_dump(mode="json")
    for step in result["steps"]:
        for slot in step["slots"]:
            slot.pop("source_value")
    return result


def _restore_definition(raw):
    if not isinstance(raw, dict) or not isinstance(raw.get("steps"), list):
        raise ValueError("技能包缺少结构化步骤定义")
    definition = json.loads(json.dumps(raw))
    for step in definition["steps"]:
        if not isinstance(step, dict):
            raise ValueError("技能包步骤格式错误")
        slots = step.get("slots", [])
        if not isinstance(slots, list):
            raise ValueError("技能包参数格式错误")
        for slot in slots:
            if not isinstance(slot, dict) or "source_value" in slot:
                raise ValueError("技能包不得携带旧实例槽值")
            slot["source_value"] = _UNRECORDED
    return SkillDefinition.model_validate(definition)


def _name(definition):
    return "owc-skill-" + content_hash(definition)[:16]


def _document(definition, name):
    description = (definition["purpose"] + " 适用：" + "；".join(definition["applies_to"]))[:1024]
    lines = [
        "---",
        "name: " + name,
        "description: " + json.dumps(description, ensure_ascii=False),
        'compatibility: "Readable procedural guidance; structured execution requires OWC."',
        "---",
        "",
        "# " + definition["name"],
        "",
        definition["purpose"],
        "",
        "## 适用范围",
        "",
        *["- " + value for value in definition["applies_to"]],
    ]
    if definition["does_not_apply_to"]:
        lines += ["", "## 不适用范围", ""]
        lines += ["- " + value for value in definition["does_not_apply_to"]]
    lines += ["", "## 操作步骤", ""]
    for index, step in enumerate(definition["steps"], 1):
        lines += [f"### {index}. {step['title']}", "", step["instruction"], ""]
        if step["operation"]:
            lines += ["执行步骤：`" + step["operation"] + "`。", ""]
        for slot in step["slots"]:
            lines.append(
                "- 参数 `"
                + slot["name"]
                + "`：按当前任务的 `"
                + slot["kind"]
                + "/"
                + slot["binding_key"]
                + "` 重新绑定。"
            )
        if step["slots"]:
            lines.append("")
    lines += [
        "## 使用边界",
        "",
        "本包提供制作指导，不授予工具、脚本或模型调用权限。导入后作为待选用的方法保存，"
        "不代表经过实际任务验证或作者认可。参数必须按当前任务重新绑定。",
        "",
        "结构化定义见 [技能定义](references/owc-skill.json)。",
        "",
    ]
    return "\n".join(lines)


def _validate_files(files):
    if not isinstance(files, dict) or len(files) != 3:
        raise ValueError("仅支持含三个声明文件的OWC技能包")
    roots, relative, total = set(), {}, 0
    for path, text in files.items():
        if not isinstance(path, str) or not isinstance(text, str):
            raise ValueError("技能包路径和内容必须是文本")
        parts = path.split("/")
        if (
            len(parts) < 2
            or "\\" in path
            or any(part in {"", ".", ".."} for part in parts)
            or not re.fullmatch(r"owc-skill-[a-f0-9]{16}", parts[0])
        ):
            raise ValueError("技能包路径不在允许范围内")
        root, child = parts[0], "/".join(parts[1:])
        if child not in _ALL_FILES:
            raise ValueError("技能包包含未声明文件，不执行或安装额外资源")
        size = len(text.encode("utf-8"))
        if size > _MAX_FILE_BYTES:
            raise ValueError("技能包单文件超出大小限制")
        total += size
        roots.add(root)
        relative[child] = text
    if len(roots) != 1 or set(relative) != _ALL_FILES or total > _MAX_TOTAL_BYTES:
        raise ValueError("技能包目录、文件集合或总大小不符合合同")
    root = next(iter(roots))
    try:
        manifest = json.loads(relative["manifest.json"])
        payload = json.loads(relative[_REFERENCE])
    except (ValueError, TypeError) as error:
        raise ValueError("技能包JSON格式错误") from error
    if not isinstance(manifest, dict) or set(manifest) != {
        "schema_version",
        "package_format",
        "files",
        "manifest_hash",
    }:
        raise ValueError("技能包清单格式不受支持")
    material = {key: value for key, value in manifest.items() if key != "manifest_hash"}
    if (
        manifest["schema_version"] != 1
        or manifest["package_format"] != _FORMAT
        or manifest["manifest_hash"] != content_hash(material)
        or manifest["files"] != {name: _sha256(relative[name]) for name in _PAYLOAD_FILES}
    ):
        raise ValueError("技能包清单或文件完整性校验失败")
    if (
        not isinstance(payload, dict)
        or set(payload) != {"schema_version", "package_format", "version_definition"}
        or payload["schema_version"] != 1
        or payload["package_format"] != _FORMAT
    ):
        raise ValueError("仅支持本系统的结构化技能包，不能自动执行外部技能")
    definition = _restore_definition(payload["version_definition"])
    portable = _portable_definition(definition)
    if portable != payload["version_definition"] or root != _name(portable):
        raise ValueError("技能包定义或目录身份不一致")
    if relative["SKILL.md"] != _document(portable, root):
        raise ValueError("技能文档与结构化定义不一致")
    return root, manifest, definition


def export_skill_package(store, project, card_id, version_id=None):
    """Export the selected definition; private source records and instance values stay local."""
    cards = SkillCards(store)
    card = cards.detail(project, card_id)
    version_id = version_id or card["current_version_id"]
    version = next((item for item in card["versions"] if item["id"] == version_id), None)
    if version is None:
        raise ValueError("导出版本不属于当前Skill")
    definition = _portable_definition(version["definition"])
    name = _name(definition)
    payload = {"schema_version": 1, "package_format": _FORMAT, "version_definition": definition}
    relative = {"SKILL.md": _document(definition, name), _REFERENCE: _json(payload)}
    material = {
        "schema_version": 1,
        "package_format": _FORMAT,
        "files": {path: _sha256(text) for path, text in relative.items()},
    }
    manifest = {**material, "manifest_hash": content_hash(material)}
    relative["manifest.json"] = _json(manifest)
    files = {name + "/" + path: text for path, text in relative.items()}
    _validate_files(files)
    return {
        "name": name,
        "filename": name + ".zip",
        "manifest_hash": manifest["manifest_hash"],
        "files": files,
    }


def package_zip(files):
    """Build a bounded archive in memory; no package path is written to disk."""
    _validate_files(files)
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, text in sorted(files.items()):
            info = zipfile.ZipInfo(path)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (stat.S_IFREG | 0o600) << 16
            archive.writestr(info, text.encode("utf-8"))
    result = stream.getvalue()
    if len(result) > _MAX_ARCHIVE_BYTES:
        raise ValueError("技能包压缩文件超出大小限制")
    return result


def read_package_zip(data):
    """Read this package format only, rejecting links, extra resources and oversized entries."""
    if not isinstance(data, bytes) or len(data) > _MAX_ARCHIVE_BYTES:
        raise ValueError("技能包压缩文件类型或大小不符合合同")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if len(entries) != 3 or len({entry.filename for entry in entries}) != 3:
                raise ValueError("技能包包含额外或重复文件")
            if sum(entry.file_size for entry in entries) > _MAX_TOTAL_BYTES:
                raise ValueError("技能包解压大小超出限制")
            files = {}
            for entry in entries:
                if (
                    entry.file_size > _MAX_FILE_BYTES
                    or entry.flag_bits & 1
                    or stat.S_ISLNK(entry.external_attr >> 16)
                    or entry.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
                ):
                    raise ValueError("技能包包含不允许的文件或压缩格式")
                files[entry.filename] = archive.read(entry).decode("utf-8")
    except (zipfile.BadZipFile, UnicodeError, OSError) as error:
        raise ValueError("技能包压缩文件无法读取") from error
    _validate_files(files)
    return files


def import_skill_package(store, project, files):
    """Import as a candidate, without trusting supplied adoption or execution claims."""
    _name_value, manifest, definition = _validate_files(files)
    source = {
        "kind": "import",
        "package_format": _FORMAT,
        "manifest_hash": manifest["manifest_hash"],
        "execution_status": "not_observed",
        "source_value_status": "not_recorded_in_package",
        "author_quality": "unknown",
    }
    card, created = SkillCards(store).add_candidate(
        project,
        definition.model_dump(mode="json"),
        source,
        changes=["导入技能定义；旧实例参数未记录，执行时按当前任务重新绑定"],
    )
    return {
        "card": card,
        "created": created,
        "manifest_hash": manifest["manifest_hash"],
        "source_status": "imported_unverified",
    }
