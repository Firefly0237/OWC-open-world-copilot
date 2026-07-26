# 审计问题的来源定位：把"哪个对象的哪个字段"落到内容文件的相对路径与行号。
"""File + line location for audit issues.

行业报告 §15.9 要求每条错误携带六要素（文件、行号、任务 id、节点 id、原因、建议动作）。本模块
负责其中最难的两项——**文件**与**行号**：

* 文件：一对象一文件，从 ``Issue.target_ref``（``quest:hl_x``）机械推出 ``quests/hl_x.json``；
  写在聚合文件里的对象（术语/风格）与逐行文件里的对象（关系）各有自己的落法。
* 行号：标准 ``json`` 模块只还原值、丢掉位置，所以这里自带一个位置感知的扫描器，把
  ``Evidence.path`` 指的字段落到那一行。

全程确定性，不调模型：报告本身必须可信，用模型去解释模型的产物等于自我拆台。
"""

from __future__ import annotations

import json
import re
from bisect import bisect_left
from dataclasses import dataclass
from pathlib import Path

from .models import Issue

_WHITESPACE = " \t\n\r"

# 一对象一文件的类型：目录布局与 ``content/store.py`` 的读写路径一一对应。
_OBJECT_FILES: dict[str, str] = {
    "quest": "quests/{id}.json",
    "entity": "world/entities/{id}.json",
    "region": "regions/{id}.json",
    "poi": "pois/{id}.json",
    "dialogue": "dialogues/{id}.json",
    "dialogue_tree": "dialogues/trees/{id}.json",
    "localized_text": "localization/texts/{id}.json",
}

# 整类对象合写在一个 JSON 里：对象在文件内还有自己的路径前缀（列表下标或字典键）。
_AGGREGATE_FILES: dict[str, str] = {
    "term": "world/terms.json",
    "style_guide": "world/style_guides.json",
    "style_rubric": "world/style_rubrics.json",
}

# 一行一对象的文件：对象所在行就是它全部字段所在行。
_JSONL_FILES: dict[str, str] = {
    "relation": "world/relations.jsonl",
    "quest_event_ref": "quests/event_refs.jsonl",
}

# 字段路径的分隔符：规则里 ``/`` 与 ``.`` 两种写法都有（``nodes/n030/choices/0`` 与
# ``stages.0.location``），这里一并接受。
_SEGMENT_SPLIT = re.compile(r"[/.]")

_NODE_IN_PATH = re.compile(r"(?:^|/)nodes/([^/]+)")


@dataclass(frozen=True)
class JsonIndex:
    """一个 JSON 文本的位置索引。

    ``lines`` 是"字段路径 -> 1 起的行号"；``scalars`` 是标量字段的还原值，用来支持"把值当路径
    写"的那类字段路径（``prerequisites.hl_missing``），让它也能落到确切那一行。
    """

    lines: dict[str, int]
    scalars: dict[str, str]


@dataclass(frozen=True)
class SourceLocation:
    """一条问题在内容树里的落点。

    ``resolved_path`` 是**真正解析到**的字段路径，空串表示只定位到对象本身。报告只敢声称解析到
    的那一层：解析不到的段一律不猜，退回到最深的已解析层。
    """

    path: str
    line: int | None
    resolved_path: str


class _JsonScanner:
    """位置感知的 JSON 扫描器。

    对象成员记在**键**所在行，数组元素记在元素起始行；路径用 ``/`` 连接，数组下标写成十进制。
    字符串只在这里找边界（反斜杠转义下一个字符，对全部 JSON 转义都成立），字面值的还原交回
    ``json.loads``——转义与代理对那套容易写错的解码不自己重写一份。

    前提是输入为合法 JSON——内容树全部由 ``content/store.py`` 写出，非法 JSON 在加载阶段就已经
    炸掉，轮不到这里。字典键里含 ``/`` 会让路径产生歧义，而内容树的对象 id 在写盘边界就禁掉了
    路径分隔符，字段名本身也不含斜杠。
    """

    def __init__(self, text: str) -> None:
        self.text = text
        self.lines: dict[str, int] = {}
        self.scalars: dict[str, str] = {}
        self._newlines = [offset for offset, ch in enumerate(text) if ch == "\n"]

    def scan(self) -> JsonIndex:
        start = self._skip_ws(0)
        self.lines[""] = self._line_of(start)
        self._value(start, "")
        return JsonIndex(lines=self.lines, scalars=self.scalars)

    def _line_of(self, offset: int) -> int:
        return bisect_left(self._newlines, offset) + 1

    def _skip_ws(self, offset: int) -> int:
        text = self.text
        while offset < len(text) and text[offset] in _WHITESPACE:
            offset += 1
        return offset

    def _string(self, offset: int) -> tuple[str, int]:
        """``offset`` 指着开引号：返回 (还原后的字面值, 闭引号之后的位置)。"""
        end = offset + 1
        while self.text[end] != '"':
            end += 2 if self.text[end] == "\\" else 1
        end += 1
        value: str = json.loads(self.text[offset:end])
        return value, end

    def _value(self, offset: int, path: str) -> int:
        char = self.text[offset]
        if char == "{":
            return self._object(offset, path)
        if char == "[":
            return self._array(offset, path)
        if char == '"':
            value, end = self._string(offset)
            self.scalars[path] = value
            return end
        end = offset
        while end < len(self.text) and self.text[end] not in ",]}" + _WHITESPACE:
            end += 1
        self.scalars[path] = self.text[offset:end]
        return end

    def _object(self, offset: int, path: str) -> int:
        offset = self._skip_ws(offset + 1)
        if self.text[offset] == "}":
            return offset + 1
        while True:
            key_start = offset
            key, offset = self._string(offset)
            child = f"{path}/{key}" if path else key
            self.lines[child] = self._line_of(key_start)
            offset = self._skip_ws(self._skip_ws(offset) + 1)  # 跳过 ':'
            offset = self._skip_ws(self._value(offset, child))
            if self.text[offset] == ",":
                offset = self._skip_ws(offset + 1)
                continue
            return offset + 1

    def _array(self, offset: int, path: str) -> int:
        offset = self._skip_ws(offset + 1)
        if self.text[offset] == "]":
            return offset + 1
        index = 0
        while True:
            child = f"{path}/{index}" if path else str(index)
            self.lines[child] = self._line_of(offset)
            offset = self._skip_ws(self._value(offset, child))
            if self.text[offset] == ",":
                offset = self._skip_ws(offset + 1)
                index += 1
                continue
            return offset + 1


def build_json_index(text: str) -> JsonIndex:
    """扫描一段 JSON 文本，得到"字段路径 -> 行号"的索引。"""
    return _JsonScanner(text).scan()


def resolve_field(index: JsonIndex, field_path: str, *, base: str = "") -> tuple[int, str] | None:
    """把字段路径解析到行号，返回 ``(行号, 真正解析到的路径)``。

    逐段下潜：某一段既不是键也不是下标时，再试着在父级数组里按"元素的值等于这一段"匹配——规则里
    ``prerequisites.hl_missing`` 这种把值写进路径的用法，就靠这条落到确切那一行。**只在真正解析
    到时才往下走**，解析不到就停在已解析的最深一层，绝不假装定位成功。``base`` 不在索引里（对象
    根本不在这个文件里）时返回 ``None``。
    """
    if base not in index.lines:
        return None
    current = base
    for segment in _path_segments(index, base, field_path):
        candidate = f"{current}/{segment}" if current else segment
        if candidate in index.lines:
            current = candidate
            continue
        matched = _child_by_value(index, current, segment)
        if matched is None:
            break
        current = matched
    return index.lines[current], current


def _path_segments(index: JsonIndex, base: str, field_path: str) -> list[str]:
    """字段路径切成段。整条路径本身就能命中时不再切分——本地化键那种自带点号的值，切开反而找不到。"""
    if not field_path:
        return []
    whole = f"{base}/{field_path}" if base else field_path
    if whole in index.lines or _child_by_value(index, base, field_path) is not None:
        return [field_path]
    return [segment for segment in _SEGMENT_SPLIT.split(field_path) if segment]


def _child_by_value(index: JsonIndex, parent: str, value: str) -> str | None:
    """父级数组里第一个字面值等于 ``value`` 的元素路径。"""
    position = 0
    while True:
        child = f"{parent}/{position}" if parent else str(position)
        if child not in index.lines:
            return None
        if index.scalars.get(child) == value:
            return child
        position += 1


def node_ref_from_path(field_path: str) -> str:
    """从字段路径里取出对话节点 id（``nodes/n030/choices/0`` -> ``n030``），没有则空串。"""
    match = _NODE_IN_PATH.search(field_path or "")
    return match.group(1) if match else ""


def content_file_for(target_ref: str) -> str:
    """从 ``target_ref`` 机械推出内容文件的相对路径；推不出来返回空串。

    推不出来的两类：``dialogue_key:<键>``（一个本地化键横跨多个文件，没有单一归属）与
    ``quest_prerequisites``（整张前置图的环，不属于任何单一文件）。
    """
    kind, separator, rest = target_ref.partition(":")
    if not separator or not rest:
        return ""
    if kind in _OBJECT_FILES:
        return _OBJECT_FILES[kind].format(id=rest)
    if kind in _AGGREGATE_FILES:
        return _AGGREGATE_FILES[kind]
    return _JSONL_FILES.get(kind, "")


class ContentSourceLocator:
    """给定内容树根目录，把审计问题落到"文件 + 行号 + 节点 id"。

    按文件缓存解析结果：一次审计动辄上百条问题挤在同几个文件上，重复扫描纯属浪费。
    """

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self._index_cache: dict[str, JsonIndex | None] = {}
        self._jsonl_cache: dict[str, list[str]] = {}

    def locate(self, target_ref: str, field_path: str = "") -> SourceLocation | None:
        relative = content_file_for(target_ref)
        if not relative:
            return None
        kind, _, rest = target_ref.partition(":")
        if kind in _JSONL_FILES:
            return self._locate_in_jsonl(relative, kind, rest, field_path)
        index = self._index_of(relative)
        if index is None:
            return SourceLocation(path=relative, line=None, resolved_path="")
        base = self._object_base(index, kind, rest)
        if base is None:
            return SourceLocation(path=relative, line=None, resolved_path="")
        resolved = resolve_field(index, field_path, base=base)
        if resolved is None:
            return SourceLocation(path=relative, line=None, resolved_path="")
        line, resolved_path = resolved
        return SourceLocation(path=relative, line=line, resolved_path=resolved_path)

    def annotate(self, issue: Issue) -> Issue:
        """给一条问题补上文件、行号、节点 id。

        原有字段一律不动，``suggested_action`` 由各条规则自己填（那是下一阶段的事）。
        """
        field_path = _first_field_path(issue)
        location = self.locate(issue.target_ref, field_path)
        if location is None or location.line is None:
            location = (
                self._locate_via_object_evidence(issue)
                or self._locate_via_relation_evidence(issue)
                or location
            )
        update: dict[str, object] = {"node_ref": node_ref_from_path(field_path)}
        if location is not None:
            update["source_path"] = location.path
            update["source_line"] = location.line
        return issue.model_copy(update=update)

    def _locate_via_object_evidence(self, issue: Issue) -> SourceLocation | None:
        """``target_ref`` 本身推不出文件时，退到证据自己指的那个对象。

        两类问题的 ``target_ref`` 天然不是某个对象：本地化键（``dialogue_key:<键>``，一个键横跨
        多个语言版本、散在多个对象里）与整张前置图的环（``quest_prerequisites``）。这两条规则各自
        在证据里留了"这一条真正落在哪个对象上"，顺着它就能给出文件与行号——而不是让报告在最需要
        定位的两类问题上恰好交白卷。
        """
        for evidence in issue.evidence:
            if not evidence.target_ref or evidence.target_ref == issue.target_ref:
                continue
            location = self.locate(evidence.target_ref, evidence.path or "")
            if location is not None and location.line is not None:
                return location
        return None

    def _locate_via_relation_evidence(self, issue: Issue) -> SourceLocation | None:
        """关系冲突这类问题，target_ref 里的三元组是合成的（``甲:conflict:乙``），文件里根本没有这
        一行；但证据里带着真实存在的那几条关系，落到第一条上，策划打开就能看到冲突的另一半。"""
        for evidence in issue.evidence:
            if evidence.relation is None:
                continue
            source, kind, target = evidence.relation
            location = self.locate(f"relation:{source}:{kind}:{target}")
            if location is not None and location.line is not None:
                return location
        return None

    def _object_base(self, index: JsonIndex, kind: str, object_id: str) -> str | None:
        """对象在文件内的路径前缀：独立文件是根（空串），聚合文件是列表下标或字典键。"""
        if kind not in _AGGREGATE_FILES:
            return ""
        if object_id in index.lines:  # 以 id 为键的字典（风格指南 / 风格量表）
            return object_id
        position = 0
        while f"{position}" in index.lines:  # 列表（术语表）
            if index.scalars.get(f"{position}/id") == object_id:
                return str(position)
            position += 1
        return None

    def _locate_in_jsonl(
        self, relative: str, kind: str, rest: str, field_path: str
    ) -> SourceLocation:
        lines = self._jsonl_of(relative)
        # 关系有两种 ref 写法：图规则写三元组，信任规则写它在 bundle 里的序号——而 bundle 的关系
        # 顺序就是文件的行顺序（``store._load_relations`` 逐行读入），所以序号直接就是行号。
        if kind == "relation" and rest.isdigit():
            position = int(rest)
            if position >= len(lines):
                return SourceLocation(path=relative, line=None, resolved_path="")
            index = build_json_index(lines[position])
            resolved = resolve_field(index, field_path)
            return SourceLocation(
                path=relative,
                line=position + 1,
                resolved_path=resolved[1] if resolved is not None else "",
            )
        for number, raw in enumerate(lines, start=1):
            if not _jsonl_line_matches(kind, raw, rest):
                continue
            index = build_json_index(raw)
            resolved = resolve_field(index, field_path)
            resolved_path = resolved[1] if resolved is not None else ""
            return SourceLocation(path=relative, line=number, resolved_path=resolved_path)
        return SourceLocation(path=relative, line=None, resolved_path="")

    def _index_of(self, relative: str) -> JsonIndex | None:
        if relative in self._index_cache:
            return self._index_cache[relative]
        path = self.root / relative
        index = build_json_index(path.read_text(encoding="utf-8")) if path.is_file() else None
        self._index_cache[relative] = index
        return index

    def _jsonl_of(self, relative: str) -> list[str]:
        if relative in self._jsonl_cache:
            return self._jsonl_cache[relative]
        path = self.root / relative
        lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
        self._jsonl_cache[relative] = lines
        return lines


def annotate_issues(issues: list[Issue], root: Path | str) -> list[Issue]:
    """给一批问题统一补上文件/行号/节点 id（共用一个定位器，文件只解析一遍）。"""
    locator = ContentSourceLocator(root)
    return [locator.annotate(issue) for issue in issues]


def _first_field_path(issue: Issue) -> str:
    for evidence in issue.evidence:
        if evidence.path:
            return evidence.path
    return ""


def _jsonl_line_matches(kind: str, raw: str, rest: str) -> bool:
    """逐行文件里认对象：关系认 ``来源:类型:目标`` 三元组（信任规则用的是行序号），其余认 id。"""
    if not raw.strip():
        return False
    payload = json.loads(raw)
    if kind != "relation":
        return bool(payload.get("id") == rest)
    triple = f"{payload.get('source')}:{payload.get('kind')}:{payload.get('target')}"
    return triple == rest
