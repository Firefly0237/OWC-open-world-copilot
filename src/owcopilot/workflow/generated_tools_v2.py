"""Generated programs: actual synthesis, isolated feedback, guarded activation.

Programs retain validation feedback and guarded activation. Existing adapter
registration remains a different capability kind.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..content.hash import content_hash
from .capabilities_v2 import (
    AdapterCatalog,
    CapabilityDescriptor,
    CapabilityPolicy,
    CapabilityStore,
    compile_schema,
)
from .generated_sandbox_v2 import DockerSandbox, inspect_program
from .harness import BudgetOwner, Harness
from .models import new_id, now
from .store import WorkflowStore


class GenerationProviderAdmissionError(ValueError):
    """Local SDK/configuration failed before a dispatch was admitted."""

    category = "config"
    outbound_started = False


def admit_generation_provider(harness: Harness, *, node_id: str) -> dict[str, Any]:
    """Use the existing provider's public local construction API, never invoke.

    This is optional for explicitly supplied provider fixtures. No key value,
    arbitrary provider repr or SDK validation input is exported in this event.
    """
    provider = harness.provider
    seen: set[int] = set()
    for _ in range(5):
        if id(provider) in seen:
            raise GenerationProviderAdmissionError("provider包装存在循环，未派发")
        seen.add(id(provider))
        constructor = getattr(provider, "chat_model", None)
        if callable(constructor):
            try:
                model = constructor()
            except Exception as exc:
                harness.store.append(
                    harness.run_id,
                    "provider/local_admission_failed",
                    {
                        "stage": "local_sdk_construction",
                        "outbound_started": False,
                        "exception_type": type(exc).__name__,
                        "error_fingerprint": content_hash(str(exc)),
                        "provider_class": type(provider).__name__,
                    },
                    node_id=node_id,
                )
                raise GenerationProviderAdmissionError(
                    "本地模型配置／SDK构造未通过；没有派发模型请求，请检查既有配置加载"
                ) from exc
            record = {
                "status": "validated_local_construction",
                "outbound_started": False,
                "provider_class": type(provider).__name__,
                "model_class": type(model).__name__,
            }
            harness.store.append(
                harness.run_id, "provider/local_admission", record, node_id=node_id
            )
            return record
        inner = getattr(provider, "inner", None)
        if inner is None:
            break
        provider = inner
    record = {
        "status": "not_offered_by_supplied_provider",
        "outbound_started": False,
        "provider_class": type(provider).__name__,
    }
    harness.store.append(harness.run_id, "provider/local_admission", record, node_id=node_id)
    return record


class GeneratedToolSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    project: str = Field(min_length=1)
    id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_.-]{0,100}$")
    tool_name: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
    purpose: str = Field(min_length=1, max_length=5000)
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    roles: tuple[str, ...] = Field(min_length=1)

    @field_validator("roles", mode="before")
    @classmethod
    def normalize_roles(cls, value: Any) -> tuple[str, ...]:
        if not isinstance(value, (list, tuple)) or any(
            not isinstance(item, str) or not item.strip() for item in value
        ):
            raise ValueError("生成工具需要明确的宿主角色列表")
        return tuple(sorted(set(value)))


class ProgramProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    description: str = Field(min_length=1, max_length=4000)
    source: str = Field(min_length=1, max_length=32768)


class ToolValidationCase(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str = Field(min_length=1, max_length=100)
    arguments: dict[str, Any]
    expected: dict[str, Any]


def _sealed(record: dict[str, Any], field: str) -> dict[str, Any]:
    return {**record, field: content_hash(record)}


def _decode(raw: str, field: str) -> dict[str, Any]:
    value = json.loads(raw)
    if value.get(field) != content_hash({key: item for key, item in value.items() if key != field}):
        raise ValueError("生成工具不可变记录hash不匹配")
    return value


class GeneratedToolStore:
    def __init__(self, workflow: WorkflowStore, sandbox: DockerSandbox):
        self.workflow, self.sandbox = workflow, sandbox
        with workflow.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS generated_tool_versions_v2(
                    id TEXT PRIMARY KEY,project TEXT NOT NULL,tool_id TEXT NOT NULL,
                    payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS generated_tool_validations_v2(
                    id TEXT PRIMARY KEY,project TEXT NOT NULL,version_id TEXT NOT NULL,
                    payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS generated_tool_activations_v2(
                    id TEXT PRIMARY KEY,project TEXT NOT NULL,tool_id TEXT NOT NULL,
                    payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS generated_tool_events_v2(
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,project TEXT NOT NULL,
                    tool_id TEXT NOT NULL,payload TEXT NOT NULL);
            """)

    def _event(
        self, project: str, tool_id: str, action: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        event = _sealed(
            {
                "project": project,
                "tool_id": tool_id,
                "action": action,
                "payload": payload,
                "created_at": now(),
            },
            "event_hash",
        )
        with self.workflow.connection() as db:
            db.execute(
                "INSERT INTO generated_tool_events_v2(project,tool_id,payload) VALUES(?,?,?)",
                (project, tool_id, json.dumps(event, ensure_ascii=False)),
            )
        return event

    def get(self, project: str, version_id: str) -> dict[str, Any]:
        with self.workflow.connection() as db:
            row = db.execute(
                "SELECT payload FROM generated_tool_versions_v2 WHERE id=? AND project=?",
                (version_id, project),
            ).fetchone()
        if row is None:
            raise KeyError("生成程序版本不存在于当前项目")
        return _decode(row[0], "version_hash")

    def versions(self, project: str, tool_id: str) -> list[dict[str, Any]]:
        with self.workflow.connection() as db:
            rows = db.execute(
                "SELECT payload FROM generated_tool_versions_v2 "
                "WHERE project=? AND tool_id=? ORDER BY rowid",
                (project, tool_id),
            ).fetchall()
        return [_decode(row[0], "version_hash") for row in rows]

    def events(self, project: str, tool_id: str) -> list[dict[str, Any]]:
        with self.workflow.connection() as db:
            rows = db.execute(
                "SELECT payload FROM generated_tool_events_v2 "
                "WHERE project=? AND tool_id=? ORDER BY seq",
                (project, tool_id),
            ).fetchall()
        return [_decode(row[0], "event_hash") for row in rows]

    def _save_version(
        self,
        spec: GeneratedToolSpec,
        proposal: ProgramProposal,
        source_ref: dict[str, Any],
        *,
        prior_version_id: str | None = None,
    ) -> dict[str, Any]:
        compile_schema(spec.input_schema, name="ProgramInput")
        compile_schema(spec.output_schema, name="ProgramOutput")
        static = inspect_program(proposal.source)
        if prior_version_id:
            parent = self.get(spec.project, prior_version_id)
            if parent["spec"] != spec.model_dump(mode="json"):
                raise ValueError("改码不能扩大原工具schema、角色或用途合同")
        payload = {
            "id": new_id(),
            "project": spec.project,
            "tool_id": spec.id,
            "spec": spec.model_dump(mode="json"),
            "description": proposal.description,
            "source": proposal.source,
            "source_hash": content_hash(proposal.source),
            "schema_hash": content_hash([spec.input_schema, spec.output_schema]),
            "runtime": self.sandbox.policy,
            "runtime_hash": content_hash(self.sandbox.policy),
            "static_analysis": static,
            "source_ref": source_ref,
            "prior_version_id": prior_version_id,
            "created_at": now(),
        }
        record = _sealed(payload, "version_hash")
        with self.workflow.connection() as db:
            db.execute(
                "INSERT INTO generated_tool_versions_v2 VALUES(?,?,?,?)",
                (record["id"], spec.project, spec.id, json.dumps(record, ensure_ascii=False)),
            )
        self._event(
            spec.project,
            spec.id,
            "program_proposed",
            {
                "version_id": record["id"],
                "version_hash": record["version_hash"],
                "source_ref": source_ref,
            },
        )
        return record

    def record_engineering_fixture(
        self,
        spec: GeneratedToolSpec,
        source: str,
        *,
        label: str,
        prior_version_id: str | None = None,
    ) -> dict[str, Any]:
        """Explicitly labelled test path; never reported as LLM code generation."""
        if not label.strip():
            raise ValueError("工程fixture必须明确标注来源")
        return self._save_version(
            spec,
            ProgramProposal(description=label, source=source),
            {"kind": "engineering_fixture", "label": label},
            prior_version_id=prior_version_id,
        )

    def propose(
        self,
        harness: Harness,
        *,
        owner: BudgetOwner,
        node_id: str,
        spec: GeneratedToolSpec,
        prior_version_id: str | None = None,
        feedback_validation_id: str | None = None,
    ) -> dict[str, Any]:
        if (
            owner.project != spec.project
            or harness.store.workflow.path.resolve() != self.workflow.path.resolve()
        ):
            raise ValueError("代码生成必须使用同一项目、原Harness预算和工作库")
        compile_schema(spec.input_schema)
        compile_schema(spec.output_schema)
        prior = self.get(spec.project, prior_version_id) if prior_version_id else None
        feedback = (
            self.validation(spec.project, feedback_validation_id)
            if feedback_validation_id
            else None
        )
        if feedback and (not prior or feedback["version_id"] != prior["id"]):
            raise ValueError("改码反馈必须绑定明确的前一版本真实执行记录")
        if prior and prior["spec"] != spec.model_dump(mode="json"):
            raise ValueError("修订必须沿原工具冻结合同")
        admit_generation_provider(harness, node_id=node_id)
        before = {item["id"] for item in harness.store.requests(harness.run_id)}
        prompt = {
            "frozen_spec": spec.model_dump(mode="json"),
            "prior_program": prior,
            "execution_feedback": feedback,
            "response_schema": ProgramProposal.model_json_schema(),
            "runtime": self.sandbox.policy,
        }
        response = harness.complete(
            owner=owner,
            node_id=node_id,
            role_id="program_synthesizer",
            system=(
                "生成一个通用、无副作用的Python纯JSON工具。只返回符合response_schema的JSON。"
                "必须定义def run(payload)，返回符合冻结output_schema的对象；不得改变宿主合同。"
                "不访问文件、网络、进程、环境、私有/dunder属性，不安装依赖，不使用eval/exec/反射。"
                "只可导入math,re,json,collections,itertools,functools,statistics；只在函数内处理数据。"
                "真实execution_feedback仅是该版本执行证据，若有失败请定向改码。"
                "不要声称已运行、已通过、获作者接受或已注册。"
            ),
            user=json.dumps(prompt, ensure_ascii=False),
            context_manifest={
                "tool_spec_hash": content_hash(spec.model_dump(mode="json")),
                "prior_version_id": prior_version_id,
                "feedback_validation_id": feedback_validation_id,
                "generated_code_execution": "container_only",
            },
        )
        text = response.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        proposal = ProgramProposal.model_validate(json.loads(text))
        requests = [
            item
            for item in harness.store.requests(harness.run_id)
            if item["id"] not in before and item["node_id"] == node_id
        ]
        if not requests:
            raise ValueError("代码生成缺少实际Harness请求证据")
        return self._save_version(
            spec,
            proposal,
            {
                "kind": "model_generated",
                "trace_run_id": harness.run_id,
                "request_ids": [item["id"] for item in requests],
                "response_hash": content_hash(response),
                "budget_owner": owner.model_dump(),
            },
            prior_version_id=prior_version_id,
        )

    def validation(self, project: str, validation_id: str) -> dict[str, Any]:
        with self.workflow.connection() as db:
            row = db.execute(
                "SELECT payload FROM generated_tool_validations_v2 WHERE id=? AND project=?",
                (validation_id, project),
            ).fetchone()
        if row is None:
            raise KeyError("生成工具验证回执不存在")
        return _decode(row[0], "validation_hash")

    def validate(
        self,
        project: str,
        version_id: str,
        cases: list[ToolValidationCase],
        *,
        cancelled: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        version = self.get(project, version_id)
        if not 1 <= len(cases) <= 10 or len({case.id for case in cases}) != len(cases):
            raise ValueError("工具验证需要1至10个独立编号的宿主样例")
        if version["runtime_hash"] != content_hash(self.sandbox.policy):
            raise ValueError("sandbox策略已改变，应建立新程序版本再验证")
        spec = GeneratedToolSpec.model_validate(version["spec"])
        input_model, output_model = (
            compile_schema(spec.input_schema),
            compile_schema(spec.output_schema),
        )
        rows = []
        # Bind expected examples before executing the candidate. No model-written
        # expected result or self-reported 'passed' is trusted as this verdict.
        cases_hash = content_hash([case.model_dump(mode="json") for case in cases])
        for case in cases:
            arguments = input_model.model_validate(case.arguments, strict=True).model_dump(
                mode="json", by_alias=True, exclude_unset=True
            )
            expected = output_model.model_validate(case.expected, strict=True).model_dump(
                mode="json", by_alias=True, exclude_unset=True
            )
            execution = self.sandbox.run(version["source"], arguments, cancelled=cancelled)
            passed, error, observed = False, execution["error"], None
            if execution["status"] == "completed":
                try:
                    observed = output_model.model_validate(
                        execution["value"], strict=True
                    ).model_dump(mode="json", by_alias=True, exclude_unset=True)
                    passed = observed == expected
                    if not passed:
                        error = "actual output differs from host-frozen expected example"
                except ValueError as exc:
                    error = "strict output schema rejected: " + str(exc)
            rows.append(
                {
                    "case_id": case.id,
                    "arguments": arguments,
                    "expected": expected,
                    "observed": observed,
                    "passed": passed,
                    "error": error,
                    "execution": execution,
                }
            )
            if execution["status"] in {"cancelled", "infrastructure_error"}:
                break
        report = _sealed(
            {
                "id": new_id(),
                "project": project,
                "version_id": version_id,
                "version_hash": version["version_hash"],
                "cases_hash": cases_hash,
                "cases": rows,
                "status": "passed"
                if len(rows) == len(cases) and all(row["passed"] for row in rows)
                else "failed",
                "limits": "only supplied host examples; "
                "no general semantic/author-satisfaction claim",
                "created_at": now(),
            },
            "validation_hash",
        )
        with self.workflow.connection() as db:
            db.execute(
                "INSERT INTO generated_tool_validations_v2 VALUES(?,?,?,?)",
                (report["id"], project, version_id, json.dumps(report, ensure_ascii=False)),
            )
        self._event(
            project,
            version["tool_id"],
            "program_validated",
            {
                "validation_id": report["id"],
                "validation_hash": report["validation_hash"],
                "status": report["status"],
            },
        )
        return report

    def _activation(self, project: str, activation_id: str) -> dict[str, Any]:
        with self.workflow.connection() as db:
            row = db.execute(
                "SELECT payload FROM generated_tool_activations_v2 WHERE id=? AND project=?",
                (activation_id, project),
            ).fetchone()
        if row is None:
            raise KeyError("生成工具激活记录不存在")
        return _decode(row[0], "activation_hash")

    def _save_activation(self, record: dict[str, Any]) -> dict[str, Any]:
        record = _sealed(
            {key: value for key, value in record.items() if key != "activation_hash"},
            "activation_hash",
        )
        with self.workflow.connection() as db:
            db.execute(
                "INSERT OR REPLACE INTO generated_tool_activations_v2 VALUES(?,?,?,?)",
                (
                    record["id"],
                    record["project"],
                    record["tool_id"],
                    json.dumps(record, ensure_ascii=False),
                ),
            )
        self._event(record["project"], record["tool_id"], "activation_" + record["status"], record)
        return record

    def _executor(
        self,
        project: str,
        activation_id: str,
        version: dict[str, Any],
    ) -> Callable[[dict[str, Any]], dict[str, Any]]:
        spec = GeneratedToolSpec.model_validate(version["spec"])

        def execute(arguments: dict[str, Any]) -> dict[str, Any]:
            from .capabilities_v2 import _ACTIVE

            fresh = self._activation(project, activation_id)
            if fresh["status"] != "enabled":
                raise PermissionError("生成程序激活已撤销，未进入容器")
            invocation = _ACTIVE.get()
            if invocation is None:
                raise PermissionError("生成工具只能通过实际Harness能力调用")

            def cancelled() -> bool:
                try:
                    invocation.harness._check_current(invocation.owner)
                    active = self._activation(project, activation_id)
                    capability = invocation.store.get(
                        project, active["capability_id"], active["capability_version"]
                    )
                    return (
                        active["status"] != "enabled"
                        or capability["descriptor"]["status"] != "enabled"
                    )
                except (ValueError, KeyError):
                    return True

            receipt = self.sandbox.run(version["source"], arguments, cancelled=cancelled)
            payload = {
                "activation_id": activation_id,
                "version_id": version["id"],
                "execution": receipt,
            }
            self._event(project, spec.id, "program_executed", payload)
            invocation.harness.store.append(
                invocation.harness.run_id, "generated_tool/execution", payload
            )
            if receipt["status"] != "completed":
                raise RuntimeError(
                    "生成工具隔离执行失败：" + receipt["status"] + ": " + receipt["error"]
                )
            return receipt["value"]

        return execute

    def restore_bindings(
        self,
        project: str,
        *,
        catalog: AdapterCatalog,
        capabilities: CapabilityStore,
        policy: CapabilityPolicy,
    ) -> list[dict[str, Any]]:
        """Rebind persisted activations after restart, without granting permission."""
        if capabilities.catalog is not catalog:
            raise ValueError("恢复必须使用能力库当前catalog")
        with self.workflow.connection() as db:
            rows = db.execute(
                "SELECT payload FROM generated_tool_activations_v2 WHERE project=? ORDER BY rowid",
                (project,),
            ).fetchall()
        restored = []
        for row in rows:
            activation = _decode(row[0], "activation_hash")
            if activation["status"] not in {"prepared", "enabled"}:
                continue
            version = self.get(project, activation["version_id"])
            validation = self.validation(project, activation["validation_id"])
            if (
                validation["status"] != "passed"
                or validation["version_hash"] != version["version_hash"]
                or version["runtime_hash"] != content_hash(self.sandbox.policy)
            ):
                raise ValueError("历史程序的验证或当前隔离策略不一致，未恢复")
            capability = capabilities.get(
                project, activation["capability_id"], activation["capability_version"]
            )
            descriptor = CapabilityDescriptor.model_validate(capability["descriptor"])
            if descriptor.status != activation["status"].replace("prepared", "validated"):
                raise ValueError("程序激活与能力权限状态不同，未恢复")
            assert descriptor.adapter_binding is not None
            try:
                catalog.get(project, descriptor.adapter_binding)
            except ValueError:
                spec = GeneratedToolSpec.model_validate(version["spec"])
                binding = catalog.register_generated_container(
                    descriptor.adapter_binding.binding_id,
                    project=project,
                    version=descriptor.adapter_binding.version,
                    input_schema=spec.input_schema,
                    output_schema=spec.output_schema,
                    roles=spec.roles,
                    execute=self._executor(project, activation["id"], version),
                )
                if binding != descriptor.adapter_binding:
                    raise ValueError("恢复程序binding hash与原权限合同不一致") from None
            capabilities._check(descriptor, policy)
            restored.append(
                {
                    "activation_id": activation["id"],
                    "version_id": version["id"],
                    "status": activation["status"],
                }
            )
        return restored

    def prepare_capability(
        self,
        project: str,
        version_id: str,
        validation_id: str,
        *,
        catalog: AdapterCatalog,
        capabilities: CapabilityStore,
        policy: CapabilityPolicy,
        rollback_from_activation_id: str | None = None,
    ) -> dict[str, Any]:
        version, validation = self.get(project, version_id), self.validation(project, validation_id)
        if (
            validation["version_id"] != version_id
            or validation["version_hash"] != version["version_hash"]
            or validation["status"] != "passed"
        ):
            raise ValueError("只有此不可变版本的真实容器验证passed后才能注册")
        if (
            capabilities.workflow.path.resolve() != self.workflow.path.resolve()
            or capabilities.catalog is not catalog
        ):
            raise ValueError("生成工具、能力库和catalog必须使用同一实际工作库")
        if version["runtime_hash"] != content_hash(self.sandbox.policy):
            raise ValueError("运行时隔离策略与已验证版本不同")
        spec = GeneratedToolSpec.model_validate(version["spec"])
        activation_id = new_id()
        binding_version = activation_id[:24]
        capability_id = "generated." + spec.id

        binding = catalog.register_generated_container(
            "generated:" + activation_id,
            project=project,
            version=binding_version,
            input_schema=spec.input_schema,
            output_schema=spec.output_schema,
            execute=self._executor(project, activation_id, version),
            roles=spec.roles,
        )
        descriptor = CapabilityDescriptor(
            project=project,
            id=capability_id,
            version=binding_version,
            tool_name=spec.tool_name,
            description=version["description"],
            input_schema=spec.input_schema,
            output_schema=spec.output_schema,
            roles=spec.roles,
            read_scopes=("generated:pure_json",),
            write_scopes=(),
            adapter_binding=binding,
            source_ref={
                "kind": "generated_container",
                "version_id": version_id,
                "version_hash": version["version_hash"],
                "source_kind": version["source_ref"]["kind"],
                "validation_id": validation_id,
                "validation_hash": validation["validation_hash"],
                "activation_id": activation_id,
            },
        )
        discovered = capabilities.discover(project, descriptor)
        checked = capabilities.validate(
            project,
            descriptor.id,
            descriptor.version,
            expected_hash=discovered["record_hash"],
            policy=policy,
        )
        if checked["descriptor"]["status"] != "validated":
            raise ValueError("生成工具能力合同／权限核验未通过")
        activation = self._save_activation(
            {
                "id": activation_id,
                "project": project,
                "tool_id": spec.id,
                "version_id": version_id,
                "validation_id": validation_id,
                "capability_id": descriptor.id,
                "capability_version": descriptor.version,
                "status": "prepared",
                "rollback_from_activation_id": rollback_from_activation_id,
                "created_at": now(),
            }
        )
        return {"activation": activation, "capability": checked}

    def enable(
        self,
        project: str,
        activation_id: str,
        *,
        capabilities: CapabilityStore,
        policy: CapabilityPolicy,
        authority: Literal["explicit_author", "preauthorized"],
    ) -> dict[str, Any]:
        activation = self._activation(project, activation_id)
        if activation["status"] != "prepared":
            raise ValueError("仅新准备的激活能启用；撤销版本需显式建立新activation")
        capability = capabilities.get(
            project, activation["capability_id"], activation["capability_version"]
        )
        enabled = capabilities.enable(
            project,
            activation["capability_id"],
            activation["capability_version"],
            expected_hash=capability["record_hash"],
            policy=policy,
            authority=authority,
        )
        active = self._save_activation({**activation, "status": "enabled", "authority": authority})
        return {"activation": active, "capability": enabled}

    def revoke(
        self,
        project: str,
        activation_id: str,
        *,
        capabilities: CapabilityStore,
        reason: str,
        actor_kind: Literal["author", "development_operator"] = "author",
    ) -> dict[str, Any]:
        activation = self._activation(project, activation_id)
        if not reason.strip() or actor_kind not in {"author", "development_operator"}:
            raise ValueError("撤销需要明确理由")
        capability = capabilities.get(
            project, activation["capability_id"], activation["capability_version"]
        )
        if activation["status"] == "revoked":
            return {"activation": activation, "capability": capability, "idempotent_replay": True}
        revoked = capabilities.revoke(
            project,
            activation["capability_id"],
            activation["capability_version"],
            expected_hash=capability["record_hash"],
            reason=reason,
            actor_kind=actor_kind,
        )
        record = self._save_activation(
            {**activation, "status": "revoked", "reason": reason, "actor_kind": actor_kind}
        )
        return {"activation": record, "capability": revoked}

    def rollback(
        self,
        project: str,
        from_activation_id: str,
        to_version_id: str,
        validation_id: str,
        *,
        capabilities: CapabilityStore,
        catalog: AdapterCatalog,
        policy: CapabilityPolicy,
        reason: str,
        actor_kind: Literal["author", "development_operator"] = "author",
    ) -> dict[str, Any]:
        if not reason.strip() or actor_kind not in {"author", "development_operator"}:
            raise ValueError("回退需要明确原因和执行主体")
        current, target = (
            self._activation(project, from_activation_id),
            self.get(project, to_version_id),
        )
        if current["tool_id"] != target["tool_id"]:
            raise ValueError("回退不能换工具身份")
        versions = [version["id"] for version in self.versions(project, target["tool_id"])]
        if versions.index(to_version_id) >= versions.index(current["version_id"]):
            raise ValueError("回退目标必须是同一工具真实更早的不可变版本")
        # Prepare/check the historical program first. Invalid historical proof or
        # changed permission must not revoke a currently usable activation.
        prepared = self.prepare_capability(
            project,
            to_version_id,
            validation_id,
            capabilities=capabilities,
            catalog=catalog,
            policy=policy,
            rollback_from_activation_id=from_activation_id,
        )
        self.revoke(
            project,
            from_activation_id,
            capabilities=capabilities,
            reason=reason,
            actor_kind=actor_kind,
        )
        prepared["activation"] = self._save_activation(
            {**prepared["activation"], "rollback_actor_kind": actor_kind}
        )
        return prepared
