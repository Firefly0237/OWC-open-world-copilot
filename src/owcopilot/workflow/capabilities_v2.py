"""Capability lifecycle with distinct supplied and sandbox-generated adapter paths.

Discovery is a pending proposal, not permission to execute. Only a supplied Python
adapter, configured local MCP client or independently validated container program
can become usable. This module itself does not synthesize or host-execute code. Dynamic JSON
Schema compilation supports a declared, strict subset and rejects everything else.
All model-driven calls use the existing Harness dispatch budget, trace and cancellation.
Production method reflection consumes this lifecycle and guarded execution facade.
"""

from __future__ import annotations

import builtins
import hashlib
import json
import re
import sqlite3
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
from types import GenericAlias
from typing import Annotated, Any, ClassVar, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    SerializationInfo,
    create_model,
    field_validator,
    model_serializer,
    model_validator,
)

from ..content.hash import content_hash
from .harness import BudgetOwner, Harness, ToolRegistry
from .models import now
from .store import WorkflowStore


def _copy(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


def _tokens(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (tuple, list, set, frozenset)) or any(
        not isinstance(item, str) or not item.strip() or len(item) > 256 for item in value
    ):
        raise ValueError("角色／作用域必须是明确的非空字符串集合")
    return tuple(sorted(set(value)))


class AdapterBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    kind: Literal["python", "configured_mcp", "generated_container"]
    binding_id: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=64)
    contract_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class CapabilityDescriptor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    project: str = Field(min_length=1, max_length=256)
    id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$")
    version: str = Field(default="1", pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
    tool_name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=4000)
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    schema_hash: str = ""
    read_scopes: tuple[str, ...] = ()
    write_scopes: tuple[str, ...] = ()
    roles: tuple[str, ...] = Field(min_length=1)
    adapter_binding: AdapterBinding | None = None
    status: Literal["discovered", "validated", "enabled", "revoked"] = "discovered"
    source_ref: dict[str, Any] = Field(default_factory=dict)

    _sets = field_validator("read_scopes", "write_scopes", "roles", mode="before")(_tokens)

    @model_validator(mode="after")
    def check_schema_hash(self) -> CapabilityDescriptor:
        expected = content_hash(_copy({"input": self.input_schema, "output": self.output_schema}))
        if self.schema_hash and self.schema_hash != expected:
            raise ValueError("能力声明schema_hash与实际schema不一致")
        object.__setattr__(self, "schema_hash", expected)
        _copy(self.source_ref)
        return self

    @property
    def descriptor_hash(self) -> str:
        return content_hash(self.model_dump(mode="json", exclude={"status"}))


class CapabilityPolicy(BaseModel):
    """Trusted host policy; do not accept this object from model/tool request arguments."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    project: str = Field(min_length=1)
    allowed_roles: tuple[str, ...]
    allowed_read_scopes: tuple[str, ...] = ()
    allowed_write_scopes: tuple[str, ...] = ()
    preauthorized_descriptor_hashes: tuple[str, ...] = ()

    _sets = field_validator(
        "allowed_roles",
        "allowed_read_scopes",
        "allowed_write_scopes",
        "preauthorized_descriptor_hashes",
        mode="before",
    )(_tokens)


class _SchemaModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    _schema_document: ClassVar[dict[str, Any] | None] = None

    @classmethod
    def model_json_schema(cls, *args: Any, **kwargs: Any) -> dict[str, Any]:
        if cls._schema_document is not None:
            return _copy(cls._schema_document)
        return super().model_json_schema(*args, **kwargs)

    @model_serializer(mode="wrap")
    def serialize_present(self, handler: Any, info: SerializationInfo) -> dict[str, Any]:
        # JSON Schema's optional/non-null field may be absent but must reject an
        # explicitly supplied null. Missing defaults are not materialized as null.
        raw = handler(self)
        return {
            field.alias or name: raw[(field.alias or name) if info.by_alias else name]
            for name, field in type(self).model_fields.items()
            if name in self.model_fields_set
        }


class UnsupportedSchema(ValueError):
    """The declared schema is outside the supported subset; no permissive fallback."""


def compile_schema(schema: dict[str, Any], *, name: str = "CapabilityValue") -> type[BaseModel]:
    """Compile closed object JSON Schemas using Pydantic's strict create_model API.

    Supported: closed objects, required/optional fields, nested arrays, strict JSON
    primitives, scalar enum, nullable anyOf/type, local acyclic $defs refs, numeric
    bounds/multipleOf, string length/pattern and array length. Annotation keywords
    title/description/default/examples are preserved but do not add defaults.
    Open maps, recursion, combinators other than nullable anyOf, formats, tuple
    arrays, arbitrary validators and unknown assertion keywords are rejected.
    """
    document = _copy(schema)
    if len(json.dumps(document)) > 128_000:
        raise UnsupportedSchema("schema超过128KB")
    definitions = document.get("$defs", {})
    if not isinstance(definitions, dict):
        raise UnsupportedSchema("$defs必须是本地对象")
    if (
        "$schema" in document
        and document["$schema"] != "https://json-schema.org/draft/2020-12/schema"
    ):
        raise UnsupportedSchema("只支持JSON Schema 2020-12声明")
    annotations = {"title", "description", "default", "examples"}
    visited_nodes = 0

    def annotation(part: Any, trail: tuple[str, ...] = (), depth: int = 0) -> Any:
        nonlocal visited_nodes
        visited_nodes += 1
        if depth > 16 or visited_nodes > 1000 or not isinstance(part, dict):
            raise UnsupportedSchema("schema结构无效或超过有限编译范围")
        extra_root = {"$defs", "$schema"} if depth == 0 else set()
        if "$ref" in part:
            if set(part) - annotations - extra_root - {"$ref"}:
                raise UnsupportedSchema("不支持$ref附加断言")
            ref = part["$ref"]
            if not isinstance(ref, str) or not ref.startswith("#/$defs/"):
                raise UnsupportedSchema("只支持本地$defs引用")
            suffix = ref[len("#/$defs/") :]
            if "/" in suffix or re.search(r"~(?![01])", suffix):
                raise UnsupportedSchema("$ref必须准确引用一个转义后的本地定义名")
            key = suffix.replace("~1", "/").replace("~0", "~")
            if key not in definitions or key in trail:
                raise UnsupportedSchema("$ref不存在或包含递归")
            return annotation(definitions[key], (*trail, key), depth + 1)
        if "anyOf" in part:
            if set(part) - annotations - extra_root - {"anyOf"}:
                raise UnsupportedSchema("nullable anyOf不能附带其他断言")
            variants = part["anyOf"]
            if not isinstance(variants, list) or len(variants) != 2:
                raise UnsupportedSchema("anyOf仅支持一个具体类型与null")
            nulls = [
                item for item in variants if isinstance(item, dict) and item == {"type": "null"}
            ]
            if len(nulls) != 1:
                raise UnsupportedSchema("anyOf仅支持一个具体类型与null")
            concrete = next(item for item in variants if item != {"type": "null"})
            return annotation(concrete, trail, depth + 1) | None
        kind = part.get("type")
        if isinstance(kind, list):
            if len(kind) != 2 or "null" not in kind or len(set(kind)) != 2:
                raise UnsupportedSchema("type数组仅支持nullable类型")
            if "enum" in part:
                raise UnsupportedSchema("nullable type数组与enum组合暂不支持")
            concrete = next(item for item in kind if item != "null")
            return annotation({**part, "type": concrete}, trail, depth + 1) | None
        common = annotations | extra_root | {"type", "enum"}
        constraints: dict[str, Any] = {}
        if kind == "object":
            if set(part) - common - {"properties", "required", "additionalProperties"}:
                raise UnsupportedSchema("对象包含不支持的断言")
            properties = part.get("properties", {})
            required = part.get("required", [])
            if part.get("additionalProperties") is not False or not isinstance(properties, dict):
                raise UnsupportedSchema("对象必须明确additionalProperties=false并声明properties")
            if (
                len(properties) > 200
                or not isinstance(required, list)
                or any(not isinstance(key, str) for key in required)
                or len(set(required)) != len(required)
                or set(required) - set(properties)
            ):
                raise UnsupportedSchema("对象required或字段数量无效")
            fields: dict[str, Any] = {
                f"field_{index}": (
                    annotation(child, trail, depth + 1),
                    Field(... if key in required else None, alias=key),
                )
                for index, (key, child) in enumerate(properties.items())
            }
            model = create_model(f"{name}{visited_nodes}", __base__=_SchemaModel, **fields)
            model._schema_document = _copy(part)
            result: Any = model
        elif kind == "array":
            if set(part) - common - {"items", "minItems", "maxItems"} or "items" not in part:
                raise UnsupportedSchema("数组必须声明单一items；不支持其他数组断言")
            result = GenericAlias(list, annotation(part["items"], trail, depth + 1))
            for key, target in (("minItems", "min_length"), ("maxItems", "max_length")):
                if key in part:
                    if type(part[key]) is not int or part[key] < 0:
                        raise UnsupportedSchema("数组长度必须是非负整数")
                    constraints[target] = part[key]
        elif kind == "string":
            if set(part) - common - {"minLength", "maxLength", "pattern"}:
                raise UnsupportedSchema("字符串包含不支持的断言")
            result = str
            for key, target in (("minLength", "min_length"), ("maxLength", "max_length")):
                if key in part:
                    if type(part[key]) is not int or part[key] < 0:
                        raise UnsupportedSchema("字符串长度必须是非负整数")
                    constraints[target] = part[key]
            if "pattern" in part:
                if not isinstance(part["pattern"], str):
                    raise UnsupportedSchema("pattern必须是字符串")
                constraints["pattern"] = part["pattern"]
        elif kind in {"integer", "number"}:
            if (
                set(part)
                - common
                - {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf"}
            ):
                raise UnsupportedSchema("数字包含不支持的断言")
            result = int if kind == "integer" else float
            for key, target in (
                ("minimum", "ge"),
                ("maximum", "le"),
                ("exclusiveMinimum", "gt"),
                ("exclusiveMaximum", "lt"),
                ("multipleOf", "multiple_of"),
            ):
                if key in part:
                    if type(part[key]) not in {int, float} or (
                        key == "multipleOf" and part[key] <= 0
                    ):
                        raise UnsupportedSchema("数字约束必须是有效JSON数字")
                    constraints[target] = part[key]
        elif kind in {"boolean", "null"}:
            if set(part) - common:
                raise UnsupportedSchema("布尔／null包含不支持的断言")
            result = bool if kind == "boolean" else type(None)
        else:
            raise UnsupportedSchema("必须显式声明受支持的JSON类型")
        if constraints:
            result = Annotated[result, Field(**constraints)]
        if "enum" in part:
            values = part["enum"]
            if (
                not isinstance(values, list)
                or not values
                or any(type(item) not in {str, int, float, bool, type(None)} for item in values)
            ):
                raise UnsupportedSchema("enum仅支持非空标量列表")

            def check_enum(value: Any) -> Any:
                if not any(
                    value == item
                    and (
                        type(value) is type(item)
                        or (type(value) in {int, float} and type(item) in {int, float})
                    )
                    for item in values
                ):
                    raise ValueError("值不属于声明的enum")
                return value

            result = Annotated[result, AfterValidator(check_enum)]
        return result

    try:
        compiled = annotation(document)
        if not isinstance(compiled, type) or not issubclass(compiled, BaseModel):
            raise UnsupportedSchema("工具输入／输出根必须是封闭对象")
        return compiled
    except UnsupportedSchema:
        raise
    except (ValueError, TypeError, KeyError) as exc:
        raise UnsupportedSchema(f"Pydantic不支持该schema：{exc}") from exc


@dataclass(frozen=True)
class _ProvidedAdapter:
    project: str
    binding_id: str
    version: str
    kind: Literal["python", "configured_mcp", "generated_container"]
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    execute: Callable[[dict[str, Any]], Any]
    roles: tuple[str, ...]
    read_scopes: tuple[str, ...]
    write_scopes: tuple[str, ...]
    remote_schema_fingerprint: str | None = None

    @property
    def binding(self) -> AdapterBinding:
        surface = {key: value for key, value in self.__dict__.items() if key != "execute"}
        return AdapterBinding(
            kind=self.kind,
            binding_id=self.binding_id,
            version=self.version,
            contract_hash=content_hash(surface),
        )


class AdapterCatalog:
    """In-memory bindings supplied by trusted application code, not import/module strings."""

    def __init__(self) -> None:
        self._adapters: dict[tuple[str, str, str], _ProvidedAdapter] = {}

    def _add(self, adapter: _ProvidedAdapter) -> AdapterBinding:
        key = (adapter.project, adapter.binding_id, adapter.version)
        if key in self._adapters:
            raise ValueError("adapter绑定版本已存在，不能热替换其实现")
        self._adapters[key] = adapter
        return adapter.binding

    def register_python(
        self,
        binding_id: str,
        *,
        project: str,
        input_model: type[BaseModel],
        output_model: type[BaseModel],
        execute: Callable[[dict[str, Any]], Any],
        roles: tuple[str, ...] | set[str] | list[str],
        read_scopes: tuple[str, ...] | set[str] | list[str] = (),
        write_scopes: tuple[str, ...] | set[str] | list[str] = (),
        version: str = "1",
    ) -> AdapterBinding:
        def call(arguments: dict[str, Any]) -> Any:
            typed = input_model.model_validate(arguments, strict=True)
            value = execute(typed.model_dump(mode="json", by_alias=True, exclude_unset=True))
            if isinstance(value, BaseModel):
                value = value.model_dump(mode="json", by_alias=True)
            return output_model.model_validate(value, strict=True).model_dump(
                mode="json", by_alias=True, exclude_unset=True
            )

        return self._add(
            _ProvidedAdapter(
                project=project,
                binding_id=binding_id,
                version=version,
                kind="python",
                input_schema=_copy(input_model.model_json_schema()),
                output_schema=_copy(output_model.model_json_schema()),
                execute=call,
                roles=_tokens(roles),
                read_scopes=_tokens(read_scopes),
                write_scopes=_tokens(write_scopes),
            )
        )

    def register_generated_container(
        self,
        binding_id: str,
        *,
        project: str,
        version: str,
        input_schema: dict[str, Any],
        output_schema: dict[str, Any],
        execute: Callable[[dict[str, Any]], Any],
        roles: tuple[str, ...],
    ) -> AdapterBinding:
        """Trusted lifecycle facade; generated source is never a host callback.

        The callback is the fixed Docker executor from generated_tools_v2, with
        validated immutable program identity. It is deliberately a distinct kind
        from an existing Python implementation or configured remote MCP tool.
        """
        input_model = compile_schema(input_schema, name="GeneratedInput")
        output_model = compile_schema(output_schema, name="GeneratedOutput")

        def call(arguments: dict[str, Any]) -> Any:
            typed = input_model.model_validate(arguments, strict=True)
            value = execute(typed.model_dump(mode="json", by_alias=True, exclude_unset=True))
            return output_model.model_validate(value, strict=True).model_dump(
                mode="json",
                by_alias=True,
                exclude_unset=True,
            )

        return self._add(
            _ProvidedAdapter(
                project=project,
                binding_id=binding_id,
                version=version,
                kind="generated_container",
                input_schema=_copy(input_schema),
                output_schema=_copy(output_schema),
                execute=call,
                roles=_tokens(roles),
                read_scopes=("generated:pure_json",),
                write_scopes=(),
            )
        )

    def register_configured_mcp(
        self,
        binding_id: str,
        *,
        project: str,
        client: Any,
        discovered_tool: Any,
        roles: tuple[str, ...] | set[str] | list[str],
        read_scopes: tuple[str, ...] | set[str] | list[str],
        write_scopes: tuple[str, ...] | set[str] | list[str] = (),
        version: str = "1",
    ) -> AdapterBinding:
        """Bind existing local client + SDK discovery metadata; never creates a transport/config.

        The existing consumer re-discovers and checks its allowlisted contract before
        each real call. The complete discovery fingerprint is also checked on the
        returned MCPObservation; unknown remote outcomes are never retried here.
        """
        import anyio

        from ..mcp_client.consumer import LocalStdioMCPClient
        from ..mcp_client.skill_adapter import MCPInvocationError
        from ..mcp_client.types import MCPCallError, MCPObservation

        if not isinstance(client, LocalStdioMCPClient):
            raise ValueError("仅支持已配置的LocalStdioMCPClient，不接受任意命令／URL")
        try:
            from mcp.types import Tool
        except ImportError as exc:
            raise ValueError("MCP SDK未安装；只记录缺能力，不自动安装") from exc
        tool = Tool.model_validate(discovered_tool)
        contract = client.config.contracts.get(tool.name)
        if contract is None or tool.outputSchema is None:
            raise ValueError("工具不在现有MCP配置allowlist中或没有outputSchema")
        annotations = {
            key: getattr(tool.annotations, key, None)
            for key in (
                "readOnlyHint",
                "destructiveHint",
                "idempotentHint",
                "openWorldHint",
            )
        }
        expected = dict(
            zip(
                annotations,
                (
                    contract.read_only,
                    contract.destructive,
                    contract.idempotent,
                    contract.open_world,
                ),
                strict=True,
            )
        )
        if annotations != expected or (not contract.read_only and not write_scopes):
            raise ValueError("MCP副作用声明改变，或写工具没有明确写作用域")
        if contract.open_world and "network:external" not in {*read_scopes, *write_scopes}:
            raise ValueError("开放世界MCP工具必须明确network:external作用域")
        surface = {
            "name": tool.name,
            "inputSchema": tool.inputSchema,
            "outputSchema": tool.outputSchema,
            "annotations": annotations,
        }
        fingerprint = hashlib.sha256(
            json.dumps(
                surface,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode()
        ).hexdigest()
        properties = tool.inputSchema.get("properties", {})
        if not isinstance(properties, dict) or set(contract.caller_arguments) - set(properties):
            raise ValueError("MCP caller参数没有对应的发现schema")
        projected = {
            "type": "object",
            "additionalProperties": False,
            "properties": {key: properties[key] for key in sorted(contract.caller_arguments)},
            "required": [
                key
                for key in tool.inputSchema.get("required", [])
                if key in contract.caller_arguments
            ],
            **({"$defs": tool.inputSchema["$defs"]} if "$defs" in tool.inputSchema else {}),
        }
        # Keep checked remote strings visibly untrusted rather than promoting them
        # into system instructions or silently dropping protocol provenance.
        output = {
            "type": "object",
            "additionalProperties": False,
            "required": ["data", "untrusted_input", "schema_fingerprint"],
            "properties": {
                "data": {key: value for key, value in tool.outputSchema.items() if key != "$defs"},
                "untrusted_input": {"type": "boolean", "enum": [True]},
                "schema_fingerprint": {"type": "string"},
            },
        }
        if "$defs" in tool.outputSchema:
            output["$defs"] = tool.outputSchema["$defs"]

        def call(arguments: dict[str, Any]) -> dict[str, Any]:
            outcome = anyio.run(client.call_tool, tool.name, arguments)
            if isinstance(outcome, MCPCallError):
                raise MCPInvocationError(outcome)
            if not isinstance(outcome, MCPObservation) or outcome.schema_fingerprint != fingerprint:
                raise ValueError("MCP发现schema已改变；结果不作为成功，无自动重放")
            return {
                "data": dict(outcome.data),
                "untrusted_input": True,
                "schema_fingerprint": outcome.schema_fingerprint,
            }

        return self._add(
            _ProvidedAdapter(
                project=project,
                binding_id=binding_id,
                version=version,
                kind="configured_mcp",
                input_schema=_copy(projected),
                output_schema=_copy(output),
                execute=call,
                roles=_tokens(roles),
                read_scopes=_tokens(read_scopes),
                write_scopes=_tokens(write_scopes),
                remote_schema_fingerprint=fingerprint,
            )
        )

    def get(self, project: str, binding: AdapterBinding) -> _ProvidedAdapter:
        adapter = self._adapters.get((project, binding.binding_id, binding.version))
        if adapter is None or adapter.binding != binding:
            raise ValueError("已有adapter不存在或其合同／权限／版本已改变")
        return adapter

    def describe(
        self,
        binding_id: str,
        capability_id: str,
        *,
        project: str,
        version: str = "1",
        adapter_version: str = "1",
        tool_name: str | None = None,
        description: str = "",
    ) -> CapabilityDescriptor:
        adapter = self._adapters[(project, binding_id, adapter_version)]
        return CapabilityDescriptor(
            project=project,
            id=capability_id,
            version=version,
            tool_name=tool_name or capability_id,
            description=description,
            input_schema=_copy(adapter.input_schema),
            output_schema=_copy(adapter.output_schema),
            roles=adapter.roles,
            read_scopes=adapter.read_scopes,
            write_scopes=adapter.write_scopes,
            adapter_binding=adapter.binding,
        )


@dataclass(frozen=True)
class _Invocation:
    store: CapabilityStore
    harness: Harness
    owner: BudgetOwner
    role_id: str


_ACTIVE: ContextVar[_Invocation | None] = ContextVar("owc_capability_harness", default=None)


class CapabilityStore:
    def __init__(self, workflow: WorkflowStore, catalog: AdapterCatalog):
        self.workflow, self.catalog = workflow, catalog
        self._models: dict[str, tuple[type[BaseModel], type[BaseModel]]] = {}
        with workflow.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS creative_capabilities_v2(
                    project TEXT NOT NULL, id TEXT NOT NULL, version TEXT NOT NULL,
                    payload TEXT NOT NULL, PRIMARY KEY(project,id,version));
                CREATE TABLE IF NOT EXISTS creative_capability_events_v2(
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, project TEXT NOT NULL,
                    id TEXT NOT NULL, version TEXT NOT NULL, payload TEXT NOT NULL);
            """)

    @staticmethod
    def _read(
        db: sqlite3.Connection, project: str, capability_id: str, version: str
    ) -> dict[str, Any]:
        row = db.execute(
            "SELECT payload FROM creative_capabilities_v2 WHERE project=? AND id=? AND version=?",
            (project, capability_id, version),
        ).fetchone()
        if row is None:
            raise KeyError("当前项目没有该能力版本")
        record = json.loads(row[0])
        descriptor = CapabilityDescriptor.model_validate(record["descriptor"])
        if (descriptor.project, descriptor.id, descriptor.version) != (
            project,
            capability_id,
            version,
        ):
            raise ValueError("能力索引与来源不一致")
        if (
            record["record_hash"]
            != content_hash({key: value for key, value in record.items() if key != "record_hash"})
            or record["descriptor_hash"] != descriptor.descriptor_hash
        ):
            raise ValueError("能力记录hash校验失败")
        return record

    @staticmethod
    def _save(
        db: sqlite3.Connection,
        descriptor: CapabilityDescriptor,
        *,
        previous: dict[str, Any] | None,
        action: str,
        errors: list[str] | None = None,
        authority: str = "host",
        reason: str = "",
        actor_kind: Literal["author", "development_operator"] | None = None,
    ) -> dict[str, Any]:
        record = {
            "descriptor": descriptor.model_dump(mode="json"),
            "descriptor_hash": descriptor.descriptor_hash,
            "revision": previous["revision"] + 1 if previous else 1,
            "validation_errors": errors or [],
            "authority": authority,
            "reason": reason,
            "created_at": previous["created_at"] if previous else now(),
            "updated_at": now(),
        }
        if actor_kind is not None:
            record["actor_kind"] = actor_kind
        record["record_hash"] = content_hash(record)
        db.execute(
            "INSERT INTO creative_capabilities_v2 VALUES(?,?,?,?) ON "
            "CONFLICT(project,id,version) DO UPDATE SET payload=excluded.payload",
            (
                descriptor.project,
                descriptor.id,
                descriptor.version,
                json.dumps(record, ensure_ascii=False),
            ),
        )
        event = {
            "action": action,
            "descriptor_hash": descriptor.descriptor_hash,
            "record_hash": record["record_hash"],
            "previous_record_hash": previous["record_hash"] if previous else None,
            "status": descriptor.status,
            "authority": authority,
            "reason": reason,
            "errors": errors or [],
            "created_at": now(),
        }
        if actor_kind is not None:
            event["actor_kind"] = actor_kind
        event["event_hash"] = content_hash(event)
        db.execute(
            "INSERT INTO creative_capability_events_v2(project,id,version,payload) VALUES(?,?,?,?)",
            (
                descriptor.project,
                descriptor.id,
                descriptor.version,
                json.dumps(event, ensure_ascii=False),
            ),
        )
        return record

    def discover(
        self, project: str, descriptor: CapabilityDescriptor | dict[str, Any]
    ) -> dict[str, Any]:
        item = CapabilityDescriptor.model_validate(descriptor)
        if item.project != project or item.status != "discovered":
            raise ValueError("发现只能建立当前项目的待审能力，不授予权限")
        with self.workflow.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                existing = self._read(db, project, item.id, item.version)
            except KeyError:
                return self._save(db, item, previous=None, action="discovered")
            if existing["descriptor_hash"] != item.descriptor_hash:
                raise ValueError("同一能力版本合同不可变；schema／权限改动须新版本")
            return existing

    def discover_missing(
        self,
        project: str,
        tool_name: str,
        description: str,
        *,
        roles: list[str] | tuple[str, ...],
        source_ref: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        capability_id = "missing-" + content_hash([project, tool_name])[:24]
        try:
            return self.get(project, capability_id, "1")
        except KeyError:
            try:
                return self.discover(
                    project,
                    CapabilityDescriptor(
                        project=project,
                        id=capability_id,
                        tool_name=tool_name,
                        description=description,
                        input_schema={},
                        output_schema={},
                        roles=_tokens(roles),
                        source_ref=source_ref or {},
                    ),
                )
            except ValueError:
                # Another host thread may have recorded the same missing name;
                # keep the first immutable source reference rather than overwriting it.
                return self.get(project, capability_id, "1")

    def get(self, project: str, capability_id: str, version: str = "1") -> dict[str, Any]:
        with self.workflow.connection() as db:
            return self._read(db, project, capability_id, version)

    def list(self, project: str) -> builtins.list[dict[str, Any]]:
        with self.workflow.connection() as db:
            rows = db.execute(
                "SELECT id,version FROM creative_capabilities_v2 WHERE project=? ORDER BY rowid",
                (project,),
            ).fetchall()
            return [self._read(db, project, row["id"], row["version"]) for row in rows]

    def events(
        self, project: str, *, after_seq: int = 0, limit: int = 200
    ) -> builtins.list[dict[str, Any]]:
        if after_seq < 0 or not 1 <= limit <= 1000:
            raise ValueError("能力事件分页无效")
        with self.workflow.connection() as db:
            rows = db.execute(
                "SELECT seq,id,version,payload FROM creative_capability_events_v2 "
                "WHERE project=? AND seq>? ORDER BY seq LIMIT ?",
                (project, after_seq, limit),
            ).fetchall()
        result = []
        for row in rows:
            item = json.loads(row["payload"])
            if item["event_hash"] != content_hash(
                {key: value for key, value in item.items() if key != "event_hash"}
            ):
                raise ValueError("能力事件hash校验失败")
            result.append({"seq": row["seq"], "id": row["id"], "version": row["version"], **item})
        return result

    @staticmethod
    def _cas(record: dict[str, Any], expected_hash: str) -> CapabilityDescriptor:
        if record["record_hash"] != expected_hash:
            raise ValueError("能力状态已改变，请读取实际版本")
        return CapabilityDescriptor.model_validate(record["descriptor"])

    def _check(
        self, descriptor: CapabilityDescriptor, policy: CapabilityPolicy
    ) -> tuple[type[BaseModel], type[BaseModel]]:
        if descriptor.project != policy.project or not set(descriptor.roles) <= set(
            policy.allowed_roles
        ):
            raise ValueError("能力的项目或角色超出宿主授权")
        if not set(descriptor.read_scopes) <= set(policy.allowed_read_scopes) or not set(
            descriptor.write_scopes
        ) <= set(policy.allowed_write_scopes):
            raise ValueError("能力读写作用域超出宿主授权")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", descriptor.tool_name):
            raise ValueError("工具名不符合当前模型工具协议")
        if descriptor.adapter_binding is None:
            raise ValueError("缺少已有adapter；发现不代表工具已实现")
        adapter = self.catalog.get(descriptor.project, descriptor.adapter_binding)
        if (
            descriptor.read_scopes != adapter.read_scopes
            or descriptor.write_scopes != adapter.write_scopes
            or not set(descriptor.roles) <= set(adapter.roles)
        ):
            raise ValueError("能力声明与adapter真实权限合同不一致")
        if (
            descriptor.input_schema != adapter.input_schema
            or descriptor.output_schema != adapter.output_schema
        ):
            raise ValueError("能力schema与已有adapter绑定不一致")
        if descriptor.schema_hash not in self._models:
            self._models[descriptor.schema_hash] = (
                compile_schema(descriptor.input_schema, name="CapabilityInput"),
                compile_schema(descriptor.output_schema, name="CapabilityOutput"),
            )
        return self._models[descriptor.schema_hash]

    def validate(
        self,
        project: str,
        capability_id: str,
        version: str,
        *,
        expected_hash: str,
        policy: CapabilityPolicy,
    ) -> dict[str, Any]:
        with self.workflow.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            record = self._read(db, project, capability_id, version)
            item = self._cas(record, expected_hash)
            if item.status not in {"discovered", "validated"}:
                raise ValueError("只能核验待审或已验证的能力；撤销版本保持不可用")
            try:
                self._check(item, policy)
            except (ValueError, TypeError) as exc:
                return self._save(
                    db,
                    item.model_copy(update={"status": "discovered"}),
                    previous=record,
                    action="validation_failed",
                    errors=[str(exc)],
                )
            return self._save(
                db,
                item.model_copy(update={"status": "validated"}),
                previous=record,
                action="validated",
            )

    def enable(
        self,
        project: str,
        capability_id: str,
        version: str,
        *,
        expected_hash: str,
        policy: CapabilityPolicy,
        authority: Literal["explicit_author", "preauthorized"],
    ) -> dict[str, Any]:
        with self.workflow.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            record = self._read(db, project, capability_id, version)
            item = self._cas(record, expected_hash)
            if item.status != "validated":
                raise ValueError("启用前必须完成结构／权限核验；撤销版本不能重新自启")
            self._check(item, policy)
            if authority not in {"explicit_author", "preauthorized"} or (
                authority == "preauthorized"
                and item.descriptor_hash not in policy.preauthorized_descriptor_hashes
            ):
                raise ValueError("没有该精确能力合同的作者选择或宿主预授权")
            rows = db.execute(
                "SELECT payload FROM creative_capabilities_v2 WHERE project=?", (project,)
            ).fetchall()
            if any(
                json.loads(row[0])["descriptor"]["tool_name"] == item.tool_name
                and json.loads(row[0])["descriptor"]["status"] == "enabled"
                for row in rows
            ):
                raise ValueError("同名工具已有启用版本；请明确撤销旧版本后切换")
            return self._save(
                db,
                item.model_copy(update={"status": "enabled"}),
                previous=record,
                action="enabled",
                authority=authority,
            )

    def revoke(
        self,
        project: str,
        capability_id: str,
        version: str,
        *,
        expected_hash: str,
        reason: str,
        actor_kind: Literal["author", "development_operator"] = "author",
    ) -> dict[str, Any]:
        if not reason.strip() or actor_kind not in {"author", "development_operator"}:
            raise ValueError("撤销需要明确原因")
        with self.workflow.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            record = self._read(db, project, capability_id, version)
            item = self._cas(record, expected_hash)
            if item.status == "revoked":
                return record
            return self._save(
                db,
                item.model_copy(update={"status": "revoked"}),
                previous=record,
                action="revoked",
                authority="explicit_author" if actor_kind == "author" else "development_operator",
                reason=reason,
                actor_kind=actor_kind,
            )

    def inject(
        self, project: str, role_id: str, registry: ToolRegistry, policy: CapabilityPolicy
    ) -> dict[str, Any]:
        if project != policy.project or role_id not in policy.allowed_roles:
            raise ValueError("当前角色没有宿主能力授权")
        injected, missing = [], []
        for record in self.list(project):
            item = CapabilityDescriptor.model_validate(record["descriptor"])
            if role_id not in item.roles:
                continue
            reference = {
                "id": item.id,
                "version": item.version,
                "tool_name": item.tool_name,
                "descriptor_hash": item.descriptor_hash,
                "schema_hash": item.schema_hash,
                "adapter_binding": item.adapter_binding.model_dump()
                if item.adapter_binding
                else None,
            }
            if item.status != "enabled":
                missing.append({**reference, "reason": item.status})
                continue
            try:
                input_model, output_model = self._check(item, policy)

                def guarded(arguments: dict[str, Any], frozen: CapabilityDescriptor = item) -> Any:
                    invocation = _ACTIVE.get()
                    if (
                        invocation is None
                        or invocation.store is not self
                        or invocation.role_id != role_id
                        or invocation.owner.project != project
                    ):
                        raise PermissionError("动态能力只能在本轮Harness工具循环中执行")
                    invocation.harness._check_current(invocation.owner)
                    fresh = CapabilityDescriptor.model_validate(
                        self.get(project, frozen.id, frozen.version)["descriptor"]
                    )
                    if fresh.status != "enabled" or fresh.descriptor_hash != frozen.descriptor_hash:
                        raise PermissionError("能力已撤销或合同改变，工具正文未执行")
                    self._check(fresh, policy)
                    assert fresh.adapter_binding is not None
                    return self.catalog.get(project, fresh.adapter_binding).execute(arguments)

                registry.register(
                    item.tool_name,
                    input_model,
                    output_model,
                    guarded,
                    roles={role_id},
                    description=item.description,
                    version=f"{item.version}:{item.descriptor_hash}",
                )
                injected.append(reference)
            except (ValueError, TypeError) as exc:
                missing.append({**reference, "reason": str(exc)})
        return {"injected": injected, "missing": missing}

    def run_tools(
        self,
        harness: Harness,
        *,
        owner: BudgetOwner,
        node_id: str,
        role_id: str,
        messages: builtins.list[dict[str, Any]],
        policy: CapabilityPolicy,
        registry: ToolRegistry | None = None,
        context_manifest: dict[str, Any] | None = None,
        max_dispatches: int | None = None,
    ) -> dict[str, Any]:
        if (
            owner.project != policy.project
            or harness.store.workflow.path.resolve() != self.workflow.path.resolve()
        ):
            raise ValueError("能力库、Harness与预算必须属于相同项目和工作库")
        tools = registry or ToolRegistry()
        inventory = self.inject(owner.project, role_id, tools, policy)
        manifest = {**(context_manifest or {}), "capabilities": inventory}
        harness.store.append(harness.run_id, "capability/injected", inventory, node_id=node_id)
        with self.workflow.connection() as db:
            after = db.execute(
                "SELECT COALESCE(MAX(seq),0) FROM creative_harness_events WHERE run_id=?",
                (harness.run_id,),
            ).fetchone()[0]
        token = _ACTIVE.set(_Invocation(self, harness, owner, role_id))
        try:
            return harness.run_tools(
                owner=owner,
                node_id=node_id,
                role_id=role_id,
                messages=messages,
                registry=tools,
                context_manifest=manifest,
                max_dispatches=max_dispatches,
            )
        finally:
            _ACTIVE.reset(token)
            known = {record["descriptor"]["tool_name"] for record in self.list(owner.project)}
            for event in harness.store.events(harness.run_id, after_seq=after, limit=1000):
                payload = event["payload"]
                name = payload.get("name")
                if (
                    event["type"] == "tool/result"
                    and payload.get("code") == "tool_not_allowed"
                    and isinstance(name, str)
                    and name not in known
                ):
                    try:
                        self.discover_missing(
                            owner.project,
                            name,
                            "当前角色的工具调用被拒绝；未证明已有实现或获得权限，需宿主核验。",
                            roles=[role_id],
                            source_ref={"run_id": harness.run_id, "event_seq": event["seq"]},
                        )
                    except (ValueError, KeyError) as exc:
                        harness.store.append(
                            harness.run_id,
                            "capability/discovery_failed",
                            {"source_event_seq": event["seq"], "error": str(exc)},
                            node_id=node_id,
                        )
                    known.add(name)


__all__ = [
    "AdapterBinding",
    "AdapterCatalog",
    "CapabilityDescriptor",
    "CapabilityPolicy",
    "CapabilityStore",
    "UnsupportedSchema",
    "compile_schema",
]
