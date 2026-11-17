"""Versioned story projects, source ledgers and real cross-scene narrative sessions.

Story branches are content, never production-agent edges. Each playable session
holds one path's state; a merge never unions facts from mutually exclusive paths.
Foreign script adapters preserve originals and report extraction limits, without
claiming that an ink/Yarn/articy engine has executed the imported project.
"""

from __future__ import annotations

import builtins
import json
from collections.abc import Mapping, Sequence
from typing import Any, Literal

import networkx as nx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..content.hash import content_hash
from ..content.models import DialogueTree, Effect, Quest
from ..logic.expr import parse_expr, refs_in
from ..logic.semantics import WorldState, evaluate, type_errors
from ..logic.stepper import advance_preview, apply_effects, preview_view, start_preview
from .models import new_id, now
from .project_snapshot_v2 import compatible_contexts, compatible_edit_projection, protected_contexts
from .reference_guard_v2 import bound_reference_snapshot, check_reference_snapshot
from .store import WorkflowStore


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class SourceScope(_Model):
    chapter_ids: list[str] = Field(default_factory=list)
    scene_ids: list[str] = Field(default_factory=list)
    branch_ids: list[str] = Field(default_factory=list)
    start_order: int | None = Field(default=None, ge=0, strict=True)
    end_order: int | None = Field(default=None, ge=0, strict=True)

    @model_validator(mode="after")
    def ordered(self) -> SourceScope:
        if (
            self.start_order is not None
            and self.end_order is not None
            and self.start_order > self.end_order
        ):
            raise ValueError("来源／事实的时间范围顺序无效")
        return self


class SourceInput(_Model):
    title: str = Field(min_length=1, max_length=300)
    format: Literal["text", "markdown", "ink", "yarn", "articy_json", "json", "csv", "tsv"]
    text: str = Field(min_length=1, max_length=4_000_000)
    status: Literal["inspiration", "draft", "canon", "retired"] = "inspiration"
    allowed_uses: list[Literal["inspiration", "drafting", "adaptation", "facts"]] = Field(
        default=["inspiration"]
    )
    scope: SourceScope = Field(default_factory=SourceScope)
    original_locator: str = Field(default="", max_length=2000)


class SourceAnchor(_Model):
    source_version_id: str = Field(min_length=1)
    start: int = Field(ge=0, strict=True)
    end: int = Field(gt=0, strict=True)
    quote: str = Field(min_length=1, max_length=16000)

    @model_validator(mode="after")
    def nonempty(self) -> SourceAnchor:
        if self.start >= self.end:
            raise ValueError("来源锚点字符范围必须非空")
        return self


class ClaimInput(_Model):
    subject: str = Field(min_length=1, max_length=300)
    predicate: str = Field(min_length=1, max_length=200)
    value: Any
    anchors: list[SourceAnchor] = Field(min_length=1, max_length=30)
    scope: SourceScope = Field(default_factory=SourceScope)
    interpretation: Literal["author_statement", "deterministic_extraction", "model_interpretation"]


class ProjectVariable(_Model):
    id: str = Field(min_length=1, max_length=200)
    type: Literal["bool", "int", "enum"]
    default: Any
    enum_values: list[str] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def typed_default(self) -> ProjectVariable:
        _typed(self, self.default)
        if self.type == "enum" and (
            not self.enum_values or len(set(self.enum_values)) != len(self.enum_values)
        ):
            raise ValueError("枚举项目状态必须有不重复的声明值")
        return self


class StoryChapter(_Model):
    id: str = Field(min_length=1, max_length=120)
    title: str = Field(min_length=1, max_length=300)
    summary: str = Field(default="", max_length=8000)


class StoryScene(_Model):
    id: str = Field(min_length=1, max_length=120)
    chapter_id: str = Field(min_length=1, max_length=120)
    title: str = Field(min_length=1, max_length=300)
    brief: str = Field(default="", max_length=16000)
    order: int = Field(default=0, ge=0, strict=True)
    dependencies: list[str] = Field(default_factory=list, max_length=100)
    sources: list[SourceAnchor] = Field(default_factory=list, max_length=50)
    precondition: str = Field(default="", max_length=4000)
    postcondition: str = Field(default="", max_length=4000)
    forbidden: list[str] = Field(default_factory=list, max_length=50)
    merge_policy: Literal["preserve_path_state", "explicit_assignments"] | None = None
    merge_effects: list[Effect] = Field(default_factory=list, max_length=100)
    requirements: dict[str, Any] | None = None


class StoryBranch(_Model):
    id: str = Field(min_length=1, max_length=120)
    source_scene: str = Field(min_length=1)
    target_scene: str | None = None
    label: str = Field(min_length=1, max_length=1000)
    condition: str = Field(default="", max_length=4000)
    effects: list[Effect] = Field(default_factory=list, max_length=100)
    ending_text: str = Field(default="", max_length=16000)

    @model_validator(mode="after")
    def visible_ending(self) -> StoryBranch:
        if self.target_scene is None and not self.ending_text.strip():
            raise ValueError("终结分支必须声明作者可见的结局文本")
        if self.target_scene is not None and self.ending_text:
            raise ValueError("普通跨场景分支不能同时声称已终结")
        return self


class ProjectOutline(_Model):
    schema_version: Literal[1] = 1
    chapters: list[StoryChapter] = Field(min_length=1, max_length=500)
    scenes: list[StoryScene] = Field(min_length=1, max_length=5000)
    branches: list[StoryBranch] = Field(default_factory=list, max_length=20000)
    variables: list[ProjectVariable] = Field(default_factory=list, max_length=500)
    entry_scene_id: str

    @model_validator(mode="after")
    def structural_contract(self) -> ProjectOutline:
        for rows in (self.chapters, self.scenes, self.branches, self.variables):
            ids = [row.id for row in rows]
            if len(set(ids)) != len(ids):
                raise ValueError("章节／场景／分支／变量ID分别须稳定且唯一")
        scenes = {scene.id: scene for scene in self.scenes}
        chapters = {chapter.id for chapter in self.chapters}
        if self.entry_scene_id not in scenes:
            raise ValueError("项目入口场景不存在")
        dependency = nx.DiGraph()
        dependency.add_nodes_from(scenes)
        symbols = {variable.id: variable for variable in self.variables}
        defaults = {key: row.default for key, row in symbols.items()}
        incoming = {scene_id: 0 for scene_id in scenes}
        for branch in self.branches:
            if (
                branch.source_scene not in scenes
                or branch.target_scene is not None
                and branch.target_scene not in scenes
            ):
                raise ValueError("故事分支引用不存在的场景")
            if branch.target_scene is not None:
                incoming[branch.target_scene] += 1
            _condition(branch.condition, symbols, defaults)
            _validate_effects(branch.effects, symbols)
        for scene in self.scenes:
            if scene.chapter_id not in chapters or set(scene.dependencies) - scenes.keys():
                raise ValueError("场景章节或前史依赖不存在")
            dependency.add_edges_from((parent, scene.id) for parent in scene.dependencies)
            if incoming[scene.id] > 1 and scene.merge_policy is None:
                raise ValueError("多分支汇流必须显式声明状态合并政策，不能自动并集")
            if scene.merge_effects and scene.merge_policy != "explicit_assignments":
                raise ValueError("汇流重设只能在明确explicit_assignments政策中执行")
            _validate_effects(scene.merge_effects, symbols)
            if any(not expression.strip() for expression in scene.forbidden):
                raise ValueError("Forbid条件不能留空，否则会无条件禁止场景")
            if scene.requirements is not None:
                from .requirements_v2 import RequirementContract

                RequirementContract.model_validate(scene.requirements)
            for expression in (scene.precondition, scene.postcondition, *scene.forbidden):
                _condition(expression, symbols, defaults)
        if not nx.is_directed_acyclic_graph(dependency):
            raise ValueError("制作依赖必须无环；故事循环由有界运行游标另行处理")
        return self


def _typed(variable: ProjectVariable, value: Any) -> None:
    if (
        variable.type == "bool"
        and type(value) is not bool
        or variable.type == "int"
        and type(value) is not int
    ):
        raise ValueError("项目状态类型不匹配：" + variable.id)
    if variable.type == "enum" and (type(value) is not str or value not in variable.enum_values):
        raise ValueError("项目枚举状态不在声明范围：" + variable.id)


def _condition(
    text: str, variables: Mapping[str, ProjectVariable], values: Mapping[str, Any]
) -> bool:
    if not text.strip():
        return True
    expression = parse_expr(text)
    symbols = {key: variable.type for key, variable in variables.items()}
    if refs_in(expression) - symbols.keys() or type_errors(expression, symbols):
        raise ValueError("项目条件包含未声明变量或类型错误")
    result = evaluate(expression, dict(values))
    if type(result) is not bool:
        raise ValueError("项目条件必须产生布尔值")
    return result


def _validate_effects(effects: Sequence[Effect], variables: Mapping[str, ProjectVariable]) -> None:
    for effect in effects:
        variable = variables.get(effect.var)
        if variable is None:
            raise ValueError("跨场景效果引用未声明的项目变量")
        if effect.op == "set":
            _typed(variable, effect.value)
        elif variable.type != "int" or type(effect.value) is not int:
            raise ValueError("项目inc／dec只支持整数变量和整数增量")


def _seal(value: dict, key: str) -> dict:
    return {**value, key: content_hash(value)}


def _decode(payload: str, key: str) -> dict:
    value = json.loads(payload)
    if value.get(key) != content_hash({name: item for name, item in value.items() if name != key}):
        raise ValueError("项目／来源／存档hash校验失败")
    return value


def adapt_narrative_source(source: SourceInput) -> dict:
    """Real existing adapters, preserving their warnings and raw-content hash."""
    text = source.text
    if source.format == "ink":
        from ..recognize.ink import recognize_ink

        result = recognize_ink(text, source_file=source.original_locator)
    elif source.format == "yarn":
        from ..recognize.yarn import recognize_yarn

        result = recognize_yarn(text, source_file=source.original_locator)
    elif source.format == "articy_json":
        from ..recognize.articy import recognize_articy

        payload = json.loads(text)
        if not isinstance(payload, dict) or not isinstance(payload.get("Packages"), list):
            raise ValueError("articy来源须为含Packages的实际JSON导出，不猜测其他布局")
        result = recognize_articy(payload, source_file=source.original_locator)
    elif source.format in {"csv", "tsv"}:
        import csv
        import io

        reader = csv.DictReader(
            io.StringIO(text), delimiter="," if source.format == "csv" else "\t"
        )
        if not reader.fieldnames or len(set(reader.fieldnames)) != len(reader.fieldnames):
            raise ValueError("表格来源须有非重复表头")
        return {
            "format": source.format,
            "columns": reader.fieldnames,
            "rows": list(reader),
            "scope": "literal_table_rows_no_inferred_entity_mapping",
            "engine_executed": False,
        }
    elif source.format == "json":
        return {
            "format": "json",
            "value": json.loads(text),
            "scope": "literal_json_no_inferred_world_facts",
            "engine_executed": False,
        }
    else:
        return {
            "format": source.format,
            "paragraphs": [
                {"line": index, "text": line}
                for index, line in enumerate(text.splitlines(), 1)
                if line.strip()
            ],
            "scope": "literal_text_no_automatic_fact_confirmation",
            "engine_executed": False,
        }
    return {
        "format": source.format,
        "recognition": result.model_dump(mode="json"),
        "scope": "structural_source_recognition_not_engine_execution",
        "engine_executed": False,
        "original_preserved": True,
        "original_hash": content_hash(text),
    }


class NarrativeProjectStore:
    def __init__(self, workflow: WorkflowStore):
        self.workflow = workflow
        with workflow.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS narrative_projects_v2(
                    id TEXT PRIMARY KEY,
                    project TEXT NOT NULL,
                    payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS narrative_project_versions_v2(
                    project_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY(project_id,
                    version));
                CREATE TABLE IF NOT EXISTS narrative_sources_v2(
                    id TEXT PRIMARY KEY,
                    project TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS narrative_claims_v2(
                    id TEXT PRIMARY KEY,
                    project TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS narrative_claim_decisions_v2(
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    claim_id TEXT NOT NULL,
                    project TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS narrative_previews_v2(
                    id TEXT PRIMARY KEY,
                    project TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS narrative_archives_v2(
                    id TEXT PRIMARY KEY,
                    project TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS narrative_preview_migrations_v2(
                    id TEXT PRIMARY KEY,
                    project TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    source_hash TEXT NOT NULL,
                    target_project_hash TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    UNIQUE(project,project_id,source_hash,target_project_hash));
                CREATE TABLE IF NOT EXISTS narrative_preview_actions_v2(
                    preview_id TEXT NOT NULL,
                    action_id TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY(preview_id,action_id));
            """)

    @staticmethod
    def _project(db, project: str, project_id: str) -> dict:
        row = db.execute(
            "SELECT payload FROM narrative_projects_v2 WHERE id=? AND project=?",
            (project_id, project),
        ).fetchone()
        if row is None:
            raise KeyError("叙事项目不属于当前项目或不存在")
        value = _decode(row[0], "project_hash")
        if value["id"] != project_id or value["project"] != project:
            raise ValueError("叙事项目索引与内容不一致")
        return value

    @staticmethod
    def _token(record: dict, expected: str) -> None:
        if record["project_hash"] != expected:
            raise ValueError("项目版本已改变，请读取当前版本")

    @staticmethod
    def _update(db, record: dict, **changes) -> dict:
        plain = {key: value for key, value in record.items() if key != "project_hash"}
        plain.update(
            changes,
            version=record["version"] + 1,
            parent_hash=record["project_hash"],
            updated_at=now(),
        )
        saved = _seal(plain, "project_hash")
        payload = json.dumps(saved, ensure_ascii=False)
        db.execute("UPDATE narrative_projects_v2 SET payload=? WHERE id=?", (payload, record["id"]))
        db.execute(
            "INSERT INTO narrative_project_versions_v2 VALUES(?,?,?)",
            (record["id"], saved["version"], payload),
        )
        return saved

    def create(self, project: str, name: str, description: str = "") -> dict:
        if not project.strip() or not name.strip():
            raise ValueError("项目名称和作用域必填")
        record = _seal(
            {
                "id": new_id(),
                "project": project,
                "name": name,
                "description": description,
                "version": 1,
                "parent_hash": None,
                "source_heads": {},
                "outline": None,
                "world_id": None,
                "base_version": None,
                "base_hash": None,
                "scene_bindings": {},
                "created_at": now(),
                "updated_at": now(),
            },
            "project_hash",
        )
        with self.workflow.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            payload = json.dumps(record, ensure_ascii=False)
            db.execute(
                "INSERT INTO narrative_projects_v2 VALUES(?,?,?)", (record["id"], project, payload)
            )
            db.execute(
                "INSERT INTO narrative_project_versions_v2 VALUES(?,?,?)",
                (record["id"], 1, payload),
            )
        return record

    def get(self, project: str, project_id: str) -> dict:
        with self.workflow.connection() as db:
            return self._project(db, project, project_id)

    def list(self, project: str) -> builtins.list[dict]:
        with self.workflow.connection() as db:
            rows = db.execute(
                "SELECT id FROM narrative_projects_v2 WHERE project=? ORDER BY rowid DESC",
                (project,),
            ).fetchall()
            return [self._project(db, project, row[0]) for row in rows]

    def _source(self, db, project: str, project_id: str, version_id: str) -> dict:
        row = db.execute(
            "SELECT payload FROM narrative_sources_v2 WHERE id=? AND project=? AND project_id=?",
            (version_id, project, project_id),
        ).fetchone()
        if row is None:
            raise KeyError("来源版本不属于当前叙事项目")
        value = _decode(row[0], "source_hash")
        if value["id"] != version_id or value["text_hash"] != content_hash(value["source"]["text"]):
            raise ValueError("来源索引／原文hash不一致")
        return value

    def add_source(
        self,
        project: str,
        project_id: str,
        source: SourceInput | dict,
        *,
        expected_project_hash: str,
        source_id: str | None = None,
        expected_source_hash: str | None = None,
    ) -> dict:
        validated_source: SourceInput = SourceInput.model_validate(source)
        adaptation = adapt_narrative_source(validated_source)
        with self.workflow.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            current = self._project(db, project, project_id)
            self._token(current, expected_project_hash)
            old = None
            if source_id is not None:
                if source_id not in current["source_heads"]:
                    raise KeyError("来源家族不存在")
                old = self._source(db, project, project_id, current["source_heads"][source_id])
                if expected_source_hash != old["source_hash"]:
                    raise ValueError("来源版本已改变")
            record = _seal(
                {
                    "id": new_id(),
                    "source_id": source_id or new_id(),
                    "project": project,
                    "project_id": project_id,
                    "version": old["version"] + 1 if old else 1,
                    "parent_version_id": old["id"] if old else None,
                    "source": validated_source.model_dump(mode="json"),
                    "text_hash": content_hash(validated_source.text),
                    "adaptation": adaptation,
                    "created_at": now(),
                },
                "source_hash",
            )
            db.execute(
                "INSERT INTO narrative_sources_v2 VALUES(?,?,?,?)",
                (record["id"], project, project_id, json.dumps(record, ensure_ascii=False)),
            )
            heads = {**current["source_heads"], record["source_id"]: record["id"]}
            saved = self._update(db, current, source_heads=heads)
            return {"source": record, "project": saved}

    def sources(self, project: str, project_id: str) -> builtins.list[dict]:
        with self.workflow.connection() as db:
            current = self._project(db, project, project_id)
            return [
                self._source(db, project, project_id, value)
                for value in current["source_heads"].values()
            ]

    def _anchor(self, db, current: dict, anchor: SourceAnchor, *, use: str | None = None) -> dict:
        source = self._source(db, current["project"], current["id"], anchor.source_version_id)
        if current["source_heads"].get(source["source_id"]) != source["id"]:
            raise ValueError("引用来源已被新版本替代，须明确重新绑定")
        if (
            source["source"]["status"] == "retired"
            or use is not None
            and use not in source["source"]["allowed_uses"]
        ):
            raise ValueError("来源已停用或不许可本次用途")
        if (
            anchor.end > len(source["source"]["text"])
            or source["source"]["text"][anchor.start : anchor.end] != anchor.quote
        ):
            raise ValueError("来源原文锚点不匹配")
        return source

    def record_claim(
        self, project: str, project_id: str, claim: ClaimInput | dict, *, expected_project_hash: str
    ) -> dict:
        validated_claim: ClaimInput = ClaimInput.model_validate(claim)
        with self.workflow.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            current = self._project(db, project, project_id)
            self._token(current, expected_project_hash)
            for anchor in validated_claim.anchors:
                source = self._anchor(db, current, anchor)
                declared = source["source"]["scope"]
                scope = validated_claim.scope.model_dump(mode="json")
                for key in ("chapter_ids", "scene_ids", "branch_ids"):
                    if declared[key] and (
                        not scope[key] or not set(scope[key]).issubset(declared[key])
                    ):
                        raise ValueError("提取事实不能超出来源的章节／场景／互斥分支范围")
                if (
                    declared["start_order"] is not None
                    and (
                        scope["start_order"] is None
                        or scope["start_order"] < declared["start_order"]
                    )
                    or declared["end_order"] is not None
                    and (scope["end_order"] is None or scope["end_order"] > declared["end_order"])
                ):
                    raise ValueError("提取事实不能扩大来源的有效时间范围")
            record = _seal(
                {
                    "id": new_id(),
                    "project": project,
                    "project_id": project_id,
                    "claim": validated_claim.model_dump(mode="json"),
                    "created_at": now(),
                },
                "claim_hash",
            )
            db.execute(
                "INSERT INTO narrative_claims_v2 VALUES(?,?,?,?)",
                (record["id"], project, project_id, json.dumps(record, ensure_ascii=False)),
            )
            saved = self._update(db, current)
            return {"claim": record, "project": saved}

    def claims(self, project: str, project_id: str) -> builtins.list[dict]:
        with self.workflow.connection() as db:
            self._project(db, project, project_id)
            rows = db.execute(
                "SELECT payload FROM narrative_claims_v2 WHERE project=? AND "
                "project_id=? ORDER BY rowid",
                (project, project_id),
            ).fetchall()
            result = []
            for row in rows:
                claim = _decode(row[0], "claim_hash")
                decision = db.execute(
                    "SELECT payload FROM narrative_claim_decisions_v2 WHERE "
                    "claim_id=? AND project=? AND project_id=? ORDER BY seq DESC LIMIT 1",
                    (claim["id"], project, project_id),
                ).fetchone()
                result.append(
                    {
                        **claim,
                        "decision": _decode(decision[0], "decision_hash") if decision else None,
                    }
                )
            return result

    @staticmethod
    def _overlap(left: dict, right: dict) -> bool:
        a, b = left["scope"], right["scope"]
        for key in ("chapter_ids", "scene_ids", "branch_ids"):
            if a[key] and b[key] and not set(a[key]) & set(b[key]):
                return False
        return not (
            (
                a["end_order"] is not None
                and b["start_order"] is not None
                and a["end_order"] < b["start_order"]
            )
            or (
                b["end_order"] is not None
                and a["start_order"] is not None
                and b["end_order"] < a["start_order"]
            )
        )

    def decide_claim(
        self,
        project: str,
        project_id: str,
        claim_id: str,
        *,
        accept: bool,
        expected_project_hash: str,
        reject_claim_ids: Sequence[str] = (),
    ) -> dict:
        with self.workflow.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            current = self._project(db, project, project_id)
            self._token(current, expected_project_hash)
            claims = {row["id"]: row for row in self.claims(project, project_id)}
            if (
                claim_id not in claims
                or set(reject_claim_ids) - claims.keys()
                or claim_id in reject_claim_ids
            ):
                raise KeyError("裁决引用了不存在或自相冲突的事实")
            claim = claims[claim_id]["claim"]
            if accept:
                for anchor in claim["anchors"]:
                    source = self._anchor(
                        db, current, SourceAnchor.model_validate(anchor), use="facts"
                    )
                    if source["source"]["status"] != "canon":
                        raise ValueError(
                            "灵感／草稿不能自动晋升为已采用世界事实；先明确来源用途与状态"
                        )
                for other in claims.values():
                    previous = other["claim"]
                    if (
                        other["id"] != claim_id
                        and other["id"] not in reject_claim_ids
                        and other["decision"]
                        and other["decision"]["accepted"]
                        and (previous["subject"], previous["predicate"])
                        == (claim["subject"], claim["predicate"])
                        and content_hash(previous["value"]) != content_hash(claim["value"])
                        and self._overlap(previous, claim)
                    ):
                        raise ValueError(
                            "与已有采用事实冲突，须显式列出被拒绝的旧事实，不能自动以新覆盖旧"
                        )
            decisions = []
            for target, accepted in [(item, False) for item in reject_claim_ids] + [
                (claim_id, accept)
            ]:
                decision = _seal(
                    {
                        "claim_id": target,
                        "accepted": accepted,
                        "authority": "explicit_author_action",
                        "created_at": now(),
                    },
                    "decision_hash",
                )
                db.execute(
                    "INSERT INTO "
                    "narrative_claim_decisions_v2(claim_id,project,project_id,payload) "
                    "VALUES(?,?,?,?)",
                    (target, project, project_id, json.dumps(decision, ensure_ascii=False)),
                )
                decisions.append(decision)
            return {"decisions": decisions, "project": self._update(db, current)}

    def save_outline(
        self,
        project: str,
        project_id: str,
        outline: ProjectOutline | dict,
        *,
        expected_project_hash: str,
    ) -> dict:
        validated_outline: ProjectOutline = ProjectOutline.model_validate(outline)
        with self.workflow.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            current = self._project(db, project, project_id)
            self._token(current, expected_project_hash)
            for scene in validated_outline.scenes:
                for anchor in scene.sources:
                    self._anchor(db, current, anchor, use="drafting")
            return self._update(db, current, outline=validated_outline.model_dump(mode="json"))

    def _accepted_claim_signatures(
        self, db, current: dict, scene: StoryScene
    ) -> builtins.list[dict]:
        """Bind adopted facts and current permissions, conservatively across branch scope.

        Pending/rejected claims never become generation facts. A fact change only
        invalidates scenes to which its chapter/scene/order scope can apply.
        """
        result = []
        rows = db.execute(
            "SELECT payload FROM narrative_claims_v2 WHERE project=? "
            "AND project_id=? ORDER BY rowid",
            (current["project"], current["id"]),
        ).fetchall()
        for row in rows:
            claim = _decode(row[0], "claim_hash")
            scope = claim["claim"]["scope"]
            if (
                scope["chapter_ids"]
                and scene.chapter_id not in scope["chapter_ids"]
                or scope["scene_ids"]
                and scene.id not in scope["scene_ids"]
                or scope["start_order"] is not None
                and scene.order < scope["start_order"]
                or scope["end_order"] is not None
                and scene.order > scope["end_order"]
            ):
                continue
            decision_row = db.execute(
                "SELECT payload FROM narrative_claim_decisions_v2 WHERE claim_id=? "
                "AND project=? AND project_id=? ORDER BY seq DESC LIMIT 1",
                (claim["id"], current["project"], current["id"]),
            ).fetchone()
            if decision_row is None:
                continue
            decision = _decode(decision_row[0], "decision_hash")
            if not decision["accepted"]:
                continue
            try:
                sources = [
                    self._anchor(db, current, SourceAnchor.model_validate(anchor), use="facts")[
                        "source_hash"
                    ]
                    for anchor in claim["claim"]["anchors"]
                ]
                available = True
            except ValueError:
                sources, available = [], False
            result.append(
                {
                    "claim_hash": claim["claim_hash"],
                    "decision_hash": decision["decision_hash"],
                    "source_hashes": sources,
                    "available": available,
                }
            )
        return result

    def _signature(
        self, db, current: dict, scene_id: str, *, check_binding_references: bool = True
    ) -> str:
        existing_binding = current["scene_bindings"].get(scene_id)
        if check_binding_references and existing_binding is not None:
            check_reference_snapshot(
                self.workflow.path.parent.parent, bound_reference_snapshot(db, existing_binding)
            )
        outline = ProjectOutline.model_validate(current["outline"])
        scene = next((row for row in outline.scenes if row.id == scene_id), None)
        if scene is None:
            raise KeyError("场景不存在")
        sources = [
            self._anchor(db, current, anchor, use="drafting")["source_hash"]
            for anchor in scene.sources
        ]
        dependencies = {}
        for key in scene.dependencies:
            binding = current["scene_bindings"].get(key)
            signature = self._signature(db, current, key)
            if binding is None or binding["input_signature"] != signature:
                raise ValueError("前史依赖尚未生成或已因相关修改失效：" + key)
            dependencies[key] = {
                "artifact_hash": binding["artifact_hash"],
                "input_signature": signature,
            }
        inputs = {
            "scene": scene,
            "variables": outline.variables,
            "sources": sources,
            "dependencies": dependencies,
        }
        facts = self._accepted_claim_signatures(db, current, scene)
        if facts:
            # Empty legacy contexts retain their original signature; a formerly
            # unbound adopted fact deliberately invalidates the old artifact.
            inputs["accepted_facts"] = facts
        return content_hash(inputs)

    def bind_scene_artifact(
        self,
        project: str,
        project_id: str,
        scene_id: str,
        artifact_id: str,
        *,
        expected_project_hash: str,
        expected_artifact_hash: str,
    ) -> dict:
        from ..app.actions.workflow import artifact_is_stale

        artifact = self.workflow.get_artifact(artifact_id)
        task = self.workflow.get_task(artifact.task_id)
        if (
            task.project != project
            or artifact.content_hash != expected_artifact_hash
            or content_hash(artifact.payload) != artifact.content_hash
        ):
            raise ValueError("场景产物作用域或内容hash不一致")
        if artifact_is_stale(task, artifact):
            raise ValueError("场景绑定不能采用已失效的旧任务产物")
        if artifact.kind not in {"bound_dialogue", "quest_with_logic", "quest", "dialogue_tree"}:
            raise ValueError("该产物不是可运行叙事内容")
        snapshot = self.get(project, project_id)
        self._token(snapshot, expected_project_hash)
        outline = ProjectOutline.model_validate(snapshot["outline"])
        scene = next((row for row in outline.scenes if row.id == scene_id), None)
        if scene is None:
            raise KeyError("场景不存在")
        from .edit_continuity import application_chain

        prior_binding = snapshot["scene_bindings"].get(scene_id)
        derived_chain = None
        with self.workflow.connection() as check_db:
            if prior_binding and prior_binding["artifact_id"] == artifact.id:
                if prior_binding["artifact_hash"] != artifact.content_hash or prior_binding[
                    "input_signature"
                ] != self._signature(check_db, snapshot, scene_id):
                    raise ValueError("已绑定产物的来源或内容已改变")
                return snapshot
            if prior_binding:
                derived_chain = application_chain(
                    check_db, project, prior_binding["artifact_id"], artifact.id
                )
                if derived_chain and prior_binding["input_signature"] != self._signature(
                    check_db, snapshot, scene_id
                ):
                    raise ValueError("正文应用的场景生成来源已改变，不能继承原绑定")
        requirement_report = None
        if scene.requirements is not None:
            from .requirements_v2 import check_requirement_outputs, verify_requirement_binding
            from .v2_service import v2_detail

            requirement_task = (
                task.model_copy(update={"revision": scene.requirements["task_revision"]})
                if derived_chain
                else task
            )
            contract = verify_requirement_binding(scene.requirements, requirement_task)
            state = v2_detail(self.workflow, project, artifact.run_id)
            ports: dict[str, Any] = {}
            for outcome in (state.get("result") or {}).get("outcomes", {}).values():
                if outcome.get("status") in {"succeeded", "reused"}:
                    ports.update(
                        {name: value["value"] for name, value in outcome.get("outputs", {}).items()}
                    )
            quest_value = artifact.payload.get("quest")
            claims = (
                (quest_value or {})
                .get("metadata", {})
                .get("requirements_v2", {})
                .get("business_role_claims")
            )
            report = check_requirement_outputs(
                contract,
                plan=ports.get("plan"),
                quest=quest_value,
                tree=artifact.payload.get("tree"),
                decisions=ports.get("decisions"),
                choices=ports.get("choices"),
                business_claims=claims,
            )
            if not report.can_continue:
                raise ValueError("场景产物未满足绑定需求合同：" + report.model_dump_json())
            requirement_report = report.model_dump(mode="json")
        with self.workflow.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            current = self._project(db, project, project_id)
            self._token(current, expected_project_hash)
            latest = db.execute("SELECT payload FROM tasks WHERE id=?", (task.id,)).fetchone()
            if latest is None or json.loads(latest[0])["revision"] != task.revision:
                raise ValueError("绑定时任务版本已经改变")
            world_binding = {
                "world_id": task.request.world_id,
                "base_version": task.base_version,
                "base_hash": task.base_hash,
            }
            if current["base_hash"] is not None and any(
                current[key] != value for key, value in world_binding.items()
            ):
                raise ValueError("不能将不同world／冻结基线的任务产物混入同一叙事项目")
            signature = self._signature(db, current, scene_id, check_binding_references=False)
            generation_scope = None
            state_row = (
                db.execute(
                    "SELECT payload FROM creative_v2_runs WHERE run_id=? AND project=?",
                    (artifact.run_id, project),
                ).fetchone()
                if db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='creative_v2_runs'"
                ).fetchone()
                else None
            )
            if state_row is not None:
                state = json.loads(state_row[0])
                check_reference_snapshot(
                    self.workflow.path.parent.parent, state.get("references", [])
                )
                if derived_chain:
                    # The new draft was explicitly derived from this exact scene's
                    # current binding, rather than generated against a new context.
                    generation_scope = prior_binding["generation_scope"]
                elif state.get("narrative_project_id"):
                    context = state["narrative_context"]
                    context_plain = {
                        key: value
                        for key, value in context.items()
                        if key != "generation_context_hash"
                    }
                    if content_hash(context_plain) != context.get("generation_context_hash"):
                        raise ValueError("产物生成上下文hash不一致")
                    if (
                        state["narrative_project_id"] != project_id
                        or state["narrative_scene_id"] != scene_id
                        or context["project_hash"] != current["project_hash"]
                        or context["generation_binding"]["input_signature"] != signature
                    ):
                        raise ValueError("产物不属于当前场景／来源快照；不能重新标记旧生成内容")
                    generation_scope = {
                        "context_hash": context["generation_context_hash"],
                        "generation_binding": context["generation_binding"],
                        "path_state": context["path_state"],
                        "branch_history": context["branch_history"],
                        "policy": "exact_frozen_path_and_project_state",
                    }
            binding = _seal(
                {
                    "artifact_id": artifact_id,
                    "artifact_hash": artifact.content_hash,
                    "task_id": task.id,
                    "task_revision": artifact.revision,
                    "input_signature": signature,
                    "payload": artifact.payload,
                    "requirement_report": requirement_report,
                    "generation_scope": generation_scope,
                    "reference_snapshot": state.get("references", [])
                    if state_row is not None
                    else [],
                    **(
                        {"application_chain": [row["id"] for row in derived_chain]}
                        if derived_chain
                        else {}
                    ),
                },
                "binding_hash",
            )
            return self._update(
                db,
                current,
                scene_bindings={**current["scene_bindings"], scene_id: binding},
                **world_binding,
            )

    def project_context(
        self,
        project: str,
        project_id: str,
        scene_id: str,
        *,
        branch_history: Sequence[str] = (),
        values: Mapping[str, Any] | None = None,
    ) -> dict:
        current = self.get(project, project_id)
        outline = ProjectOutline.model_validate(current["outline"])
        scene = next((row for row in outline.scenes if row.id == scene_id), None)
        if scene is None:
            raise KeyError("场景不存在")
        history = set(branch_history)

        def applies(scope):
            return (
                (not scope["chapter_ids"] or scene.chapter_id in scope["chapter_ids"])
                and (not scope["scene_ids"] or scene_id in scope["scene_ids"])
                and (not scope["branch_ids"] or bool(history & set(scope["branch_ids"])))
                and (scope["start_order"] is None or scene.order >= scope["start_order"])
                and (scope["end_order"] is None or scene.order <= scope["end_order"])
            )

        facts: builtins.list[dict] = []
        pending: builtins.list[dict] = []
        rejected: builtins.list[dict] = []

        def unresolved(row: dict, status: str, reason: str) -> dict:
            return {
                "claim_id": row["id"],
                "subject": row["claim"]["subject"],
                "predicate": row["claim"]["predicate"],
                "status": status,
                "reason": reason,
                "usable_as_world_fact": False,
            }

        with self.workflow.connection() as db:
            sources = [
                self._anchor(db, current, anchor, use="drafting") for anchor in scene.sources
            ]
            if any(not applies(source["source"]["scope"]) for source in sources):
                raise ValueError("场景材料超出来源允许的章节／分支／时间范围")
            for row in self.claims(project, project_id):
                if not applies(row["claim"]["scope"]):
                    continue
                try:
                    for anchor in row["claim"]["anchors"]:
                        self._anchor(
                            db,
                            current,
                            SourceAnchor.model_validate(anchor),
                            use="facts"
                            if row["decision"] and row["decision"]["accepted"]
                            else None,
                        )
                except ValueError:
                    pending.append(unresolved(row, "unavailable", "source_changed_or_revoked"))
                    continue
                if row["decision"] and not row["decision"]["accepted"]:
                    rejected.append(unresolved(row, "rejected", "explicit_author_rejection"))
                elif row["decision"] and row["decision"]["accepted"]:
                    facts.append(
                        {
                            **row,
                            "epistemic_status": "explicitly_author_accepted",
                            "usable_as_world_fact": True,
                        }
                    )
                else:
                    pending.append(unresolved(row, "proposed", "not_author_accepted"))
        return {
            "project_id": project_id,
            "project_hash": current["project_hash"],
            "chapter": next(
                row.model_dump(mode="json")
                for row in outline.chapters
                if row.id == scene.chapter_id
            ),
            "scene": scene.model_dump(mode="json"),
            "sources": sources,
            "accepted_facts": facts,
            "proposed_or_unresolved": pending,
            "rejected_claims": rejected,
            "path_state": dict(values or {}),
            "branch_history": list(branch_history),
            "variables": [variable.model_dump(mode="json") for variable in outline.variables],
            "evidence_policy": {
                "accepted_facts": "only_current_source_bound_explicit_author_decisions",
                "source_excerpts": "authorized_context_not_automatic_world_facts",
                "unresolved_and_rejected": "summaries_only_not_generation_facts",
            },
            "semantic_status": "unknown",
        }

    def generation_context(
        self,
        project: str,
        project_id: str,
        scene_id: str,
        *,
        preview_id: str | None = None,
        transition_id: str | None = None,
    ) -> dict:
        """Freeze real path evidence without requiring an ungenerated target artifact.

        Prospective transitions apply actual effects to a detached saved state;
        they do not move the player's cursor or invent a played branch history.
        """
        if transition_id is not None and preview_id is None:
            raise ValueError("前瞻分支生成必须绑定真实试玩会话，不能凭空填写分支历史")
        with self.workflow.connection() as db:
            current = self._project(db, project, project_id)
            outline = ProjectOutline.model_validate(current["outline"])
            scene = next((row for row in outline.scenes if row.id == scene_id), None)
            if scene is None:
                raise KeyError("生成目标场景不存在")
            signature = self._signature(db, current, scene_id, check_binding_references=False)
            dependencies = []
            for dependency_id in scene.dependencies:
                binding = current["scene_bindings"][dependency_id]
                if binding["binding_hash"] != content_hash(
                    {key: value for key, value in binding.items() if key != "binding_hash"}
                ):
                    raise ValueError("前史依赖的真实绑定hash不一致")
                dependency_scene = next(row for row in outline.scenes if row.id == dependency_id)
                dependencies.append(
                    {
                        "scene_id": dependency_id,
                        "artifact_id": binding["artifact_id"],
                        "artifact_hash": binding["artifact_hash"],
                        "binding_hash": binding["binding_hash"],
                        "input_signature": binding["input_signature"],
                        "payload": binding["payload"],
                        "sources": [
                            self._anchor(db, current, anchor, use="drafting")
                            for anchor in dependency_scene.sources
                        ],
                    }
                )
            values = {row.id: row.default for row in outline.variables}
            history: builtins.list[str] = []
            evidence: dict[str, Any] = {
                "kind": "declared_default_state",
                "project_hash": current["project_hash"],
                "scene_id": scene_id,
                "input_signature": signature,
                "preview_id": None,
                "preview_hash": None,
                "action_seq": None,
                "transition_id": None,
                "prospective": False,
            }
            if preview_id is not None:
                row = db.execute(
                    "SELECT payload FROM narrative_previews_v2 WHERE id=? AND project=? "
                    "AND project_id=?",
                    (preview_id, project, project_id),
                ).fetchone()
                if row is None:
                    raise KeyError("生成来源试玩不属于当前叙事项目")
                session = _decode(row[0], "preview_hash")
                if session.get("version_mode") == "fixed_historical":
                    raise ValueError("旧版隔离试玩不能作为当前创作的生成输入")
                self._token(current, session["project_hash"])
                values = dict(session["values"])
                history = list(session["branch_history"])
                evidence.update(
                    kind="saved_preview",
                    preview_id=preview_id,
                    preview_hash=session["preview_hash"],
                    action_seq=session["action_seq"],
                    source_scene_id=session["scene_id"],
                )
                if transition_id is None:
                    if session["scene_id"] != scene_id:
                        raise ValueError("试玩当前位置与生成场景不一致；下一场景须选择实际可走分支")
                else:
                    view = self._view(current, session)["view"]
                    if view["status"] != "waiting_transition" or transition_id not in {
                        item["id"] for item in view["choices"]
                    }:
                        raise ValueError("前瞻生成的分支不是试玩当前实际可走分支")
                    branch = next(item for item in outline.branches if item.id == transition_id)
                    if branch.target_scene != scene_id:
                        raise ValueError("前瞻分支的真实目标不是所请求生成场景")
                    state = WorldState(values)
                    apply_effects(state, branch.effects)
                    values = state.as_mapping()
                    history.append(branch.id)
                    evidence.update(
                        kind="prospective_saved_preview_transition",
                        transition_id=transition_id,
                        prospective=True,
                        transition_hash=content_hash(branch),
                    )
            symbols = {row.id: row for row in outline.variables}
            values = {key: values.get(key, variable.default) for key, variable in symbols.items()}
            for key, variable in symbols.items():
                _typed(variable, values[key])
            if evidence["kind"] != "saved_preview":
                if scene.merge_policy == "explicit_assignments":
                    state = WorldState(values)
                    apply_effects(state, scene.merge_effects)
                    values = state.as_mapping()
                if not _condition(scene.precondition, symbols, values):
                    raise ValueError("生成来源状态不满足场景前置，不能编造或重置前史状态")
            context = self.project_context(
                project, project_id, scene_id, branch_history=history, values=values
            )
            if context["project_hash"] != current["project_hash"]:
                raise ValueError("装配生成上下文时项目已改变")
            context.update(history_dependencies=dependencies, generation_binding=evidence)
            return {**context, "generation_context_hash": content_hash(context)}

    def _enter(self, db, current: dict, session: dict, scene_id: str) -> None:
        outline = ProjectOutline.model_validate(current["outline"])
        scene = next(row for row in outline.scenes if row.id == scene_id)
        symbols = {row.id: row for row in outline.variables}
        session["values"] = {
            key: session["values"].get(key, row.default) for key, row in symbols.items()
        }
        # Local selectors are seeded anew; only explicitly declared project state crosses scenes.
        context = self.project_context(
            current["project"],
            current["id"],
            scene_id,
            branch_history=session["branch_history"],
            values=session["values"],
        )
        if context["project_hash"] != current["project_hash"]:
            raise ValueError("进入场景时项目版本已改变")
        binding = current["scene_bindings"].get(scene_id)
        if binding is None or binding["input_signature"] != self._signature(db, current, scene_id):
            raise ValueError("场景尚无与当前来源／依赖匹配的真实产物")
        if binding["binding_hash"] != content_hash(
            {key: value for key, value in binding.items() if key != "binding_hash"}
        ):
            raise ValueError("场景绑定hash不一致")
        if scene.merge_policy == "explicit_assignments":
            state = WorldState(session["values"])
            apply_effects(state, scene.merge_effects)
            session["values"] = state.as_mapping()
        if not _condition(scene.precondition, symbols, session["values"]):
            raise ValueError("场景前置状态不满足，不能自动重置跨章状态")
        generation_scope = binding.get("generation_scope")
        if generation_scope is not None and (
            generation_scope["branch_history"] != session["branch_history"]
            or generation_scope["path_state"] != session["values"]
        ):
            raise ValueError("续写产物依据另一条路径／状态生成；请从当前路线定向生成，不能混用前史")
        payload = binding["payload"]
        quest = Quest.model_validate(payload["quest"]) if "quest" in payload else None
        tree = DialogueTree.model_validate(payload["tree"]) if "tree" in payload else None
        if quest and quest.logic:
            for local in quest.logic.variables:
                if local.id in symbols and (
                    local.type.value != symbols[local.id].type
                    or local.type.value == "enum"
                    and set(local.enum_values) != set(symbols[local.id].enum_values)
                ):
                    raise ValueError("场景逻辑与项目同名状态类型／枚举冲突")
        domain = start_preview(
            quest=quest, dialogue_tree=tree, initial_state=session["values"], presentation="player"
        )
        session.update(
            scene_id=scene_id,
            domain=domain.model_dump(mode="json"),
            values=domain.values,
            phase="scene",
        )
        session["visited_scenes"].append(scene_id)
        session["protected_contexts"] = protected_contexts(
            current,
            session["visited_scenes"],
            lambda key: self._signature(db, current, key),
        )

    def create_preview(
        self,
        project: str,
        project_id: str,
        *,
        expected_project_hash: str,
        initial_state: Mapping[str, Any] | None = None,
        max_scene_steps: int = 200,
    ) -> dict:
        if not 1 <= max_scene_steps <= 10000:
            raise ValueError("项目预演场景步数应在1至10000之间")
        with self.workflow.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            current = self._project(db, project, project_id)
            self._token(current, expected_project_hash)
            outline = ProjectOutline.model_validate(current["outline"])
            values = {row.id: row.default for row in outline.variables}
            symbols = {row.id: row for row in outline.variables}
            for key, value in (initial_state or {}).items():
                if key not in symbols:
                    raise ValueError("初态包含未声明项目变量")
                _typed(symbols[key], value)
                values[key] = value
            session = {
                "id": new_id(),
                "project": project,
                "project_id": project_id,
                "project_hash": current["project_hash"],
                "project_version": current["version"],
                "action_seq": 0,
                "actions": {},
                "values": values,
                "branch_history": [],
                "visited_scenes": [],
                "phase": "scene",
                "max_scene_steps": max_scene_steps,
                "ending_text": "",
                "created_at": now(),
            }
            self._enter(db, current, session, outline.entry_scene_id)
            saved = _seal(session, "preview_hash")
            db.execute(
                "INSERT INTO narrative_previews_v2 VALUES(?,?,?,?)",
                (session["id"], project, project_id, json.dumps(saved, ensure_ascii=False)),
            )
            return self._view(current, saved)

    @staticmethod
    def _view(current: dict, session: dict) -> dict:
        outline = ProjectOutline.model_validate(current["outline"])
        variables = {row.id: row for row in outline.variables}
        view = preview_view(session["domain"])
        if view["status"] == "completed" and session["phase"] != "completed":
            scene = next(row for row in outline.scenes if row.id == session["scene_id"])
            if not _condition(scene.postcondition, variables, session["values"]) or any(
                _condition(rule, variables, session["values"]) for rule in scene.forbidden
            ):
                view.update(status="blocked", choices=[], message="场景Post／Forbid合同不满足")
            else:
                choices = [
                    {"id": edge.id, "label": edge.label, "target_scene": edge.target_scene}
                    for edge in outline.branches
                    if edge.source_scene == session["scene_id"]
                    and _condition(edge.condition, variables, session["values"])
                ]
                view.update(
                    status="waiting_transition" if choices else "blocked",
                    choices=choices,
                    can_advance=bool(choices),
                    message="选择下一场景／结局"
                    if choices
                    else "场景已结束，但无满足条件的项目分支",
                )
        if session["phase"] == "completed":
            view.update(
                status="completed", text=session["ending_text"], choices=[], can_advance=False
            )
        view["action_seq"] = session["action_seq"]
        return {
            "preview_id": session["id"],
            "project_id": current["id"],
            "project_hash": current["project_hash"],
            "action_seq": session["action_seq"],
            "preview_hash": session["preview_hash"],
            "scene_id": session["scene_id"],
            "branch_history": session["branch_history"],
            "visited_scenes": session["visited_scenes"],
            "state": session["values"],
            "version_mode": session.get("version_mode", "current"),
            "view": view,
            "coverage": {
                "visited_scene_count": len(set(session["visited_scenes"])),
                "declared_scene_count": len(outline.scenes),
                "scope": "one_played_path_not_all_paths",
            },
        }

    def step_preview(
        self,
        project: str,
        project_id: str,
        preview_id: str,
        *,
        action_id: str,
        expected_seq: int,
        choice_id: str | None = None,
        transition_id: str | None = None,
    ) -> dict:
        if not action_id.strip() or choice_id is not None and transition_id is not None:
            raise ValueError("动作ID必填；场景选择与项目跳转不能在同一动作混用")
        with self.workflow.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            current = self._project(db, project, project_id)
            row = db.execute(
                "SELECT payload FROM narrative_previews_v2 WHERE id=? AND project=? "
                "AND project_id=?",
                (preview_id, project, project_id),
            ).fetchone()
            if row is None:
                raise KeyError("预演不属于当前叙事项目")
            session = _decode(row[0], "preview_hash")
            request_hash = content_hash(
                {
                    "expected_seq": expected_seq,
                    "choice_id": choice_id,
                    "transition_id": transition_id,
                }
            )
            receipt_row = db.execute(
                "SELECT request_hash,payload FROM narrative_preview_actions_v2 "
                "WHERE preview_id=? AND action_id=?",
                (preview_id, action_id),
            ).fetchone()
            if receipt_row is not None:
                if receipt_row[0] != request_hash:
                    raise ValueError("同一动作ID不能执行不同参数")
                receipt = _decode(receipt_row[1], "action_receipt_hash")
                return {**receipt["result"], "idempotent_replay": True}
            signature = content_hash([choice_id, transition_id])
            if action_id in session["actions"]:
                if session["actions"][action_id] != signature:
                    raise ValueError("同一动作ID不能执行不同选择")
                historical = self._historical_project(db, current, session)
                return {**self._view(historical, session), "idempotent_replay": True}
            if session.get("version_mode") == "fixed_historical":
                historical = self._historical_project(db, current, session)
                self._historical_baseline(current, historical)
                current = historical
            self._token(current, session["project_hash"])
            actual_contexts = protected_contexts(
                current, session["visited_scenes"], lambda key: self._signature(db, current, key)
            )
            if (
                session.get("protected_contexts")
                and actual_contexts != session["protected_contexts"]
            ):
                raise ValueError("已玩依赖或来源已改变，不能继续旧预演")
            if expected_seq != session["action_seq"] or session["phase"] == "completed":
                raise ValueError("预演动作版本过期或项目已经终结")
            outline = ProjectOutline.model_validate(current["outline"])
            if transition_id is None:
                domain = advance_preview(
                    session["domain"],
                    action_id=action_id,
                    expected_seq=session["domain"]["action_seq"],
                    choice_id=choice_id,
                )
                session.update(domain=domain.model_dump(mode="json"), values=domain.values)
            else:
                view = self._view(current, session)["view"]
                if view["status"] != "waiting_transition" or transition_id not in {
                    item["id"] for item in view["choices"]
                }:
                    raise ValueError("当前不可执行该项目分支")
                if len(session["branch_history"]) >= session["max_scene_steps"]:
                    raise ValueError("项目预演已达场景步数边界；未覆盖路径仍未知")
                branch = next(row for row in outline.branches if row.id == transition_id)
                state = WorldState(session["values"])
                apply_effects(state, branch.effects)
                session["values"] = state.as_mapping()
                session["branch_history"].append(branch.id)
                if branch.target_scene is None:
                    session.update(phase="completed", ending_text=branch.ending_text)
                else:
                    self._enter(db, current, session, branch.target_scene)
            session.pop("preview_hash", None)
            session["actions"][action_id] = signature
            session["action_seq"] += 1
            saved = _seal(session, "preview_hash")
            result = self._view(current, saved)
            db.execute(
                "UPDATE narrative_previews_v2 SET payload=? WHERE id=?",
                (json.dumps(saved, ensure_ascii=False), preview_id),
            )
            receipt = _seal(
                {
                    "preview_id": preview_id,
                    "action_id": action_id,
                    "request_hash": request_hash,
                    "result": result,
                },
                "action_receipt_hash",
            )
            db.execute(
                "INSERT INTO narrative_preview_actions_v2 VALUES(?,?,?,?)",
                (preview_id, action_id, request_hash, json.dumps(receipt, ensure_ascii=False)),
            )
            return result

    @staticmethod
    def _historical_project(db, current: dict, session: dict) -> dict:
        if current["project_hash"] == session["project_hash"]:
            return current
        rows = db.execute(
            "SELECT payload FROM narrative_project_versions_v2 WHERE project_id=?",
            (current["id"],),
        ).fetchall()
        for row in rows:
            version = _decode(row[0], "project_hash")
            if version["project_hash"] == session["project_hash"]:
                if version["project"] != current["project"]:
                    raise ValueError("项目历史作用域不一致")
                return version
        raise ValueError("预演引用的不可变项目版本不存在")

    def _fork_preview(
        self, db, current: dict, session: dict, *, source_hash: str, source_ref: dict
    ) -> dict:
        existing = db.execute(
            "SELECT payload FROM narrative_preview_migrations_v2 WHERE project=? "
            "AND project_id=? AND source_hash=? AND target_project_hash=?",
            (current["project"], current["id"], source_hash, current["project_hash"]),
        ).fetchone()
        if existing is not None:
            receipt = _decode(existing[0], "migration_hash")
            return {**receipt["result"], "snapshot_migration": receipt, "idempotent_replay": True}
        previous = self._historical_project(db, current, session)
        contexts = session.get("protected_contexts", {})
        domain = None
        edit_evidence = None
        if previous["project_hash"] != current["project_hash"]:
            contexts, domain, edit_evidence = self._continuity_projection(db, current, session)
        else:
            actual = protected_contexts(
                current, session["visited_scenes"], lambda key: self._signature(db, current, key)
            )
            if contexts and actual != contexts:
                raise ValueError("恢复时已玩依赖／来源已改变")
            contexts = actual
        migration_id = new_id()
        plain = {key: value for key, value in session.items() if key != "preview_hash"}
        # Action IDs are scoped to the new preview. Preserve state/cursor/history
        # and sequence, while clearing both layers' idempotency namespaces.
        plain["domain"] = domain or {**session["domain"], "applied_actions": {}}
        plain.update(
            id=new_id(),
            project_hash=current["project_hash"],
            project_version=current["version"],
            parent_preview_id=session["id"],
            parent_preview_hash=session["preview_hash"],
            migration_id=migration_id,
            protected_contexts=contexts,
            actions={},
            created_at=now(),
        )
        if source_ref["kind"] == "archive":
            plain["restored_from"] = source_ref["id"]
        saved = _seal(plain, "preview_hash")
        result = self._view(current, saved)
        receipt = _seal(
            {
                "id": migration_id,
                "source_ref": source_ref,
                "source_preview_hash": session["preview_hash"],
                "from_project_hash": previous["project_hash"],
                "to_project_hash": current["project_hash"],
                "protected_contexts": contexts,
                "policy": "fixed_historical_snapshot_v1"
                if session.get("version_mode") == "fixed_historical"
                else edit_evidence["policy"]
                if edit_evidence
                else "unchanged_played_content_dependencies_transitions_and_variables_v1",
                "edit_projection": edit_evidence,
                "result": result,
                "created_at": now(),
            },
            "migration_hash",
        )
        db.execute(
            "INSERT INTO narrative_previews_v2 VALUES(?,?,?,?)",
            (saved["id"], current["project"], current["id"], json.dumps(saved, ensure_ascii=False)),
        )
        db.execute(
            "INSERT INTO narrative_preview_migrations_v2 VALUES(?,?,?,?,?,?)",
            (
                migration_id,
                current["project"],
                current["id"],
                source_hash,
                current["project_hash"],
                json.dumps(receipt, ensure_ascii=False),
            ),
        )
        return {**result, "snapshot_migration": receipt}

    def _continuity_projection(self, db, current: dict, session: dict):
        if session.get("version_mode") == "fixed_historical":
            raise ValueError("旧版隔离试玩不能迁移为当前创作输入")
        previous = self._historical_project(db, current, session)

        def signature(key):
            return self._signature(db, current, key)

        try:
            contexts = compatible_contexts(previous, current, session, signature)
            return contexts, None, None
        except ValueError as original:
            try:
                return compatible_edit_projection(previous, current, session, signature, db)
            except ValueError as narrowed:
                raise ValueError(str(original) + "；正文继续检查：" + str(narrowed)) from narrowed

    def preview_compatibility(
        self,
        project: str,
        project_id: str,
        preview_id: str,
        *,
        expected_project_hash: str,
        expected_preview_hash: str,
    ) -> dict:
        """Read an exact compatibility result without forking or advancing a session."""
        with self.workflow.connection() as db:
            current = self._project(db, project, project_id)
            self._token(current, expected_project_hash)
            row = db.execute(
                "SELECT payload FROM narrative_previews_v2 "
                "WHERE id=? AND project=? AND project_id=?",
                (preview_id, project, project_id),
            ).fetchone()
            if row is None:
                raise KeyError("原项目试玩不存在")
            session = _decode(row[0], "preview_hash")
            if session["preview_hash"] != expected_preview_hash:
                raise ValueError("检查前试玩位置已改变")
            evidence = {
                "source_project_hash": session["project_hash"],
                "target_project_hash": current["project_hash"],
                "preview_hash": session["preview_hash"],
            }
            try:
                contexts, _, edited = self._continuity_projection(db, current, session)
                evidence.update(protected_scenes=sorted(contexts), edit_projection=edited)
                return {
                    "status": "compatible",
                    "reasons": [],
                    "evidence": evidence,
                    "recovery_actions": ["fork_at_current_position", "keep_original"],
                }
            except ValueError as error:
                return {
                    "status": "incompatible",
                    "reasons": [str(error)],
                    "evidence": evidence,
                    "recovery_actions": ["keep_original", "restart_from_project_entry"],
                }

    def rebase_preview(
        self,
        project: str,
        project_id: str,
        preview_id: str,
        *,
        expected_project_hash: str,
        expected_preview_hash: str,
    ) -> dict:
        """Explicit compatible fork; never edits the source preview or archive."""
        with self.workflow.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            current = self._project(db, project, project_id)
            self._token(current, expected_project_hash)
            row = db.execute(
                "SELECT payload FROM narrative_previews_v2 WHERE id=? AND project=? "
                "AND project_id=?",
                (preview_id, project, project_id),
            ).fetchone()
            if row is None:
                raise KeyError("预演不属于当前叙事项目")
            session = _decode(row[0], "preview_hash")
            if session.get("version_mode") == "fixed_historical":
                raise ValueError("旧版隔离试玩不迁移到当前创作版本，请从新版入口开始")
            if session["preview_hash"] != expected_preview_hash:
                raise ValueError("迁移前预演已改变")
            if current["project_hash"] == session["project_hash"]:
                actual = protected_contexts(
                    current,
                    session["visited_scenes"],
                    lambda key: self._signature(db, current, key),
                )
                if session.get("protected_contexts") and actual != session["protected_contexts"]:
                    raise ValueError("已玩依赖／来源已改变，不能继续旧预演")
                return self._view(current, session)
            return self._fork_preview(
                db,
                current,
                session,
                source_hash=session["preview_hash"],
                source_ref={"kind": "preview", "id": preview_id},
            )

    @staticmethod
    def _historical_baseline(current: dict, historical: dict) -> None:
        for field in ("id", "project", "world_id", "base_version", "base_hash"):
            if current.get(field) != historical.get(field):
                raise ValueError("作品或世界基线已改变，旧版试玩不可继续：" + field)

    def continue_original_preview(
        self,
        project: str,
        project_id: str,
        preview_id: str,
        *,
        expected_project_hash: str,
        expected_preview_hash: str,
    ) -> dict:
        with self.workflow.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            current = self._project(db, project, project_id)
            self._token(current, expected_project_hash)
            row = db.execute(
                "SELECT payload FROM narrative_previews_v2 "
                "WHERE id=? AND project=? AND project_id=?",
                (preview_id, project, project_id),
            ).fetchone()
            if row is None:
                raise KeyError("原项目试玩不存在")
            session = _decode(row[0], "preview_hash")
            if session["preview_hash"] != expected_preview_hash:
                raise ValueError("原试玩位置已改变")
            historical = self._historical_project(db, current, session)
            self._historical_baseline(current, historical)
            return self._fork_preview(
                db,
                historical,
                {**session, "version_mode": "fixed_historical"},
                source_hash=content_hash(["continue_original", session["preview_hash"]]),
                source_ref={"kind": "original_preview", "id": preview_id},
            )

    def save_archive(
        self, project: str, project_id: str, preview_id: str, *, name: str, expected_seq: int
    ) -> dict:
        if not name.strip():
            raise ValueError("存档名称必填")
        with self.workflow.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            current = self._project(db, project, project_id)
            row = db.execute(
                "SELECT payload FROM narrative_previews_v2 WHERE id=? AND project=? "
                "AND project_id=?",
                (preview_id, project, project_id),
            ).fetchone()
            if row is None:
                raise KeyError("预演不属于当前叙事项目")
            preview = _decode(row[0], "preview_hash")
            if preview.get("version_mode") == "fixed_historical":
                raise ValueError("旧版隔离试玩保留独立记录，不能保存为当前作品存档")
            self._token(current, preview["project_hash"])
            if preview["action_seq"] != expected_seq:
                raise ValueError("保存存档时预演已改变")
            record = _seal(
                {
                    "id": new_id(),
                    "project": project,
                    "project_id": project_id,
                    "name": name,
                    "project_hash": current["project_hash"],
                    "preview": preview,
                    "created_at": now(),
                },
                "archive_hash",
            )
            db.execute(
                "INSERT INTO narrative_archives_v2 VALUES(?,?,?,?)",
                (record["id"], project, project_id, json.dumps(record, ensure_ascii=False)),
            )
            return record

    def restore_archive(
        self,
        project: str,
        project_id: str,
        archive_id: str,
        *,
        expected_project_hash: str,
        operation_id: str | None = None,
    ) -> dict:
        with self.workflow.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            current = self._project(db, project, project_id)
            self._token(current, expected_project_hash)
            row = db.execute(
                "SELECT payload FROM narrative_archives_v2 WHERE id=? AND project=? "
                "AND project_id=?",
                (archive_id, project, project_id),
            ).fetchone()
            if row is None:
                raise KeyError("存档不属于当前叙事项目")
            archive = _decode(row[0], "archive_hash")
            preview = _decode(json.dumps(archive["preview"], ensure_ascii=False), "preview_hash")
            return self._fork_preview(
                db,
                current,
                preview,
                source_hash=content_hash(
                    {
                        "archive_hash": archive["archive_hash"],
                        "operation_id": operation_id or new_id(),
                    }
                ),
                source_ref={"kind": "archive", "id": archive_id},
            )

    def get_preview(self, project: str, project_id: str, preview_id: str) -> dict:
        with self.workflow.connection() as db:
            current = self._project(db, project, project_id)
            row = db.execute(
                "SELECT payload FROM narrative_previews_v2 WHERE id=? AND project=? "
                "AND project_id=?",
                (preview_id, project, project_id),
            ).fetchone()
            if row is None:
                raise KeyError("预演不属于当前叙事项目")
            session = _decode(row[0], "preview_hash")
            historical = self._historical_project(db, current, session)
            return {
                **self._view(historical, session),
                "snapshot_status": "fixed_historical"
                if session.get("version_mode") == "fixed_historical"
                else "current"
                if historical == current
                else "historical_requires_rebase",
                "current_project_hash": current["project_hash"],
            }

    def archives(self, project: str, project_id: str) -> builtins.list[dict]:
        with self.workflow.connection() as db:
            self._project(db, project, project_id)
            return [
                _decode(row[0], "archive_hash")
                for row in db.execute(
                    "SELECT payload FROM narrative_archives_v2 WHERE project=? AND "
                    "project_id=? ORDER BY rowid DESC",
                    (project, project_id),
                ).fetchall()
            ]

    def source_versions(self, project: str, project_id: str) -> builtins.list[dict]:
        with self.workflow.connection() as db:
            self._project(db, project, project_id)
            rows = db.execute(
                "SELECT id FROM narrative_sources_v2 WHERE project=? AND project_id=? "
                "ORDER BY rowid",
                (project, project_id),
            ).fetchall()
            return [self._source(db, project, project_id, row[0]) for row in rows]

    def export_project(self, project: str, project_id: str) -> dict:
        current = self.get(project, project_id)
        with self.workflow.connection() as db:
            migrations = [
                _decode(row[0], "migration_hash")
                for row in db.execute(
                    "SELECT payload FROM narrative_preview_migrations_v2 "
                    "WHERE project=? AND project_id=? ORDER BY rowid",
                    (project, project_id),
                ).fetchall()
            ]
            archives = [
                _decode(row[0], "archive_hash")
                for row in db.execute(
                    "SELECT payload FROM narrative_archives_v2 WHERE project=? AND "
                    "project_id=? ORDER BY rowid",
                    (project, project_id),
                ).fetchall()
            ]
        result = {
            "schema_version": 1,
            "project": current,
            "sources": self.source_versions(project, project_id),
            "claims": self.claims(project, project_id),
            "archives": archives,
            "snapshot_migrations": migrations,
            "runtime": "owc_domain_stepper_cross_scene",
            "external_engine_executed": False,
            "limits": [
                "recognition is not foreign-engine execution",
                "played paths are not exhaustive coverage",
                "textual claim semantics remain unknown",
            ],
        }
        return {**result, "export_hash": content_hash(result)}
