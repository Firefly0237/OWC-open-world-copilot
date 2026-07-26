# 可执行报告改造·接线验收：六要素从审计执行器一路到达每个出口。
"""六要素（文件、行号、任务 id、节点 id、原因、建议动作）的**接线**验收。

规则那一侧（中文消息 + 实例化建议）与定位器那一侧（文件/行号）各自已有测试；这一份只盯一件事：
**产出真的到达使用者**。本项目的历史顽疾是"实现了没接到产品路径"，所以这里逐个出口断言：

* 审计执行器：给了内容根就补齐定位，没给就一个字段都不编；
* Markdown 报告渲染（命令行 ``--markdown-report`` / 桌面动作 / 接口都读它）；
* 落库再读回（"列出已存问题"这条出口是从库里读的）；
* 桌面动作 / 命令行 / 接口共用的那份 JSON（``Issue.model_dump``）；
* 发布门的阻断文案。

测试只组装输入、断言产出，审计与渲染全部走生产代码。
"""

from __future__ import annotations

import json

import pytest

from owcopilot.app import run_project_audit_action
from owcopilot.audit.context import AuditContext
from owcopilot.audit.default_rules import build_default_rule_registry
from owcopilot.audit.report import render_issue_markdown
from owcopilot.audit.runner import AuditRunner
from owcopilot.content.models import (
    ContentBundle,
    DialogueNode,
    DialogueTree,
    Entity,
    EntityType,
    Quest,
)
from owcopilot.content.store import ContentStore
from owcopilot.pipeline.export_gate import export_blockers
from owcopilot.pipeline.project import ProjectContext
from owcopilot.storage import SQLiteStore


def _defective_bundle() -> ContentBundle:
    """一个刚好踩中两类问题的小世界：任务指向不存在的发布人；对话树跳向不存在的节点。"""
    return ContentBundle(
        entities={
            "npc_aldric": Entity(
                id="npc_aldric", name="Aldric", type=EntityType.NPC, description="Caravan master"
            )
        },
        quests={"q1": Quest(id="q1", title="Q1", giver_npc="npc_missing", objective="Do it.")},
        dialogue_trees={
            "dt1": DialogueTree(
                id="dt1",
                root_node="n1",
                nodes={
                    "n1": DialogueNode(
                        id="n1", speaker="npc_aldric", text="hi", next_node="n_gone"
                    )
                },
            )
        },
    )


@pytest.fixture
def content_root(tmp_path):
    root = tmp_path / "content"
    ContentStore(root).save(_defective_bundle())
    return root


def _issue_by_rule(issues, rule_code):
    return next(issue for issue in issues if issue.rule_code == rule_code)


# -- 出口 0：审计执行器本身 -----------------------------------------------------------------------


def test_runner_annotates_only_when_given_a_content_root(content_root) -> None:
    runner = AuditRunner(build_default_rule_registry())
    bundle = ContentStore(content_root).load()

    without_root = runner.run(AuditContext.from_bundle(bundle)).issues
    with_root = runner.run(
        AuditContext.from_bundle(bundle, content_root=content_root)
    ).issues

    # 不给根目录就一个字段都不编——候选 bundle（打完补丁的、请求体里传进来的）在磁盘上并不存在，
    # 拿磁盘文件标行号只会指错地方。
    assert {issue.source_path for issue in without_root} == {""}
    assert {issue.source_line for issue in without_root} == {None}

    quest_issue = _issue_by_rule(with_root, "UNKNOWN_ENTITY_REF")
    assert quest_issue.source_path == "quests/q1.json"
    assert quest_issue.source_line is not None
    tree_issue = _issue_by_rule(with_root, "DIALOGUE_TREE_BROKEN_LINK")
    assert tree_issue.source_path == "dialogues/trees/dt1.json"
    assert tree_issue.node_ref == "n1"


def test_annotating_does_not_move_the_fingerprint(content_root) -> None:
    """指纹只由规则码、目标与证据算出，所以补定位不会让基线比对失效。"""
    runner = AuditRunner(build_default_rule_registry())
    bundle = ContentStore(content_root).load()

    bare = {issue.fingerprint for issue in runner.run(AuditContext.from_bundle(bundle)).issues}
    located = {
        issue.fingerprint
        for issue in runner.run(
            AuditContext.from_bundle(bundle, content_root=content_root)
        ).issues
    }

    assert bare == located


# -- 出口 1：Markdown 报告 -----------------------------------------------------------------------


def test_markdown_report_carries_location_and_suggested_action(content_root) -> None:
    runner = AuditRunner(build_default_rule_registry())
    issue = _issue_by_rule(
        runner.run(
            AuditContext.from_bundle(
                ContentStore(content_root).load(), content_root=content_root
            )
        ).issues,
        "DIALOGUE_TREE_BROKEN_LINK",
    )

    block = "\n".join(render_issue_markdown(issue))

    assert "dialogues/trees/dt1.json" in block
    assert f"第 {issue.source_line} 行" in block
    assert "n1" in block  # 节点 id
    assert issue.suggested_action and issue.suggested_action in block


def test_markdown_report_omits_the_location_line_when_nothing_resolved() -> None:
    """定位不出来就整行不写——报告只声称真定位到的那部分。"""
    runner = AuditRunner(build_default_rule_registry())
    issue = _issue_by_rule(
        runner.run(AuditContext.from_bundle(_defective_bundle())).issues, "UNKNOWN_ENTITY_REF"
    )

    assert "位置" not in "\n".join(render_issue_markdown(issue))


# -- 出口 2：落库再读回（"列出已存问题"） ---------------------------------------------------------


def test_persisted_issue_round_trips_the_six_elements(content_root, tmp_path) -> None:
    project = ProjectContext.open(content_root, sqlite_path=str(tmp_path / "runtime.db"))
    try:
        from owcopilot.pipeline.audit import run_full_audit

        run_full_audit(project, persist=True)
        stored = _issue_by_rule(project.sqlite_store.list_issues(), "DIALOGUE_TREE_BROKEN_LINK")
    finally:
        project.close()

    assert stored.source_path == "dialogues/trees/dt1.json"
    assert stored.source_line is not None
    assert stored.node_ref == "n1"
    assert stored.suggested_action


def test_legacy_runtime_db_without_the_new_columns_still_reads(tmp_path) -> None:
    """旧运行库缺这四列时读回来是默认值，而不是炸掉。"""
    store = SQLiteStore(str(tmp_path / "legacy.db"))
    try:
        store.conn.execute("DROP TABLE issues")
        store.conn.execute(
            """
            CREATE TABLE issues (
                id TEXT PRIMARY KEY, rule_code TEXT NOT NULL, severity TEXT NOT NULL,
                category TEXT NOT NULL, target_ref TEXT NOT NULL, message TEXT NOT NULL,
                evidence_json TEXT NOT NULL, fingerprint TEXT, audit_run_id TEXT,
                status TEXT NOT NULL
            )
            """
        )
        store.conn.execute(
            "INSERT INTO issues VALUES ('i1','R','error','graph','quest:q1','旧消息','[]',"
            "'fp1',NULL,'open')"
        )
        store.conn.commit()
        restored = store.list_issues()
    finally:
        store.close()

    assert len(restored) == 1
    assert restored[0].source_path == ""
    assert restored[0].source_line is None
    assert restored[0].suggested_action == ""


# -- 出口 3：桌面动作 / 命令行 / 接口共用的那份 JSON -----------------------------------------------


def test_audit_action_json_carries_the_six_elements(content_root) -> None:
    result = run_project_audit_action(content_root)

    payload = next(
        issue for issue in result["issues"] if issue["rule_code"] == "DIALOGUE_TREE_BROKEN_LINK"
    )
    assert payload["source_path"] == "dialogues/trees/dt1.json"
    assert payload["source_line"] >= 1
    assert payload["node_ref"] == "n1"
    assert payload["suggested_action"]
    # 同一份 JSON 就是命令行 `owcopilot audit` 与 REST 审计端点下发的那一份。
    assert json.loads(json.dumps(payload)) == payload
    assert "dialogues/trees/dt1.json" in result["markdown_report"]


# -- 出口 4：发布门的阻断文案 ---------------------------------------------------------------------


def test_export_blocker_carries_location_and_advice(content_root, tmp_path) -> None:
    project = ProjectContext.open(content_root, sqlite_path=str(tmp_path / "runtime.db"))
    try:
        blocker = next(b for b in export_blockers(project) if b.code == "UNKNOWN_ENTITY_REF")
    finally:
        project.close()

    assert blocker.source_path == "quests/q1.json"
    assert blocker.source_line is not None
    assert blocker.suggested_action
