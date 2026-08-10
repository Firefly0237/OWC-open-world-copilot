"""报告可执行率量表的离线测试（``evaluation/report_actionability.py``）。

全部 $0：世界与审计都是确定性的，修复者用 LangChain 标准假模型脚本驱动。这里要守的是**判分逻辑**
——一个判分写错的量表只会给出自信的错数字，比没有量表更糟。所以每种结局都单独钉一遍：
真修好、没动手、修坏文件、修掉了却带出新问题、以及**把出问题的东西删掉冒充修好**。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from owcopilot.audit.baseline import issue_fingerprint
from owcopilot.audit.models import Issue
from owcopilot.audit.report import render_issue_markdown
from owcopilot.content.models import ContentBundle
from owcopilot.content.store import ContentStore
from owcopilot.evaluation.report_actionability import (
    FIXER_SYSTEM_PROMPT,
    FIXER_SYSTEM_PROMPT_NO_LISTING,
    ContentCensus,
    FixJudgement,
    _ContentFileTools,
    build_actionability_worlds,
    finding_report_text,
    judge_fix,
    load_report_texts,
    open_issues_of,
    render_actionability_markdown,
    run_report_actionability,
    take_census,
    write_actionability_artifacts,
)
from owcopilot.fakes import scripted_chat_model

TREE_FILE = "dialogues/trees/dtree_r1_q1.json"


@dataclass
class _World:
    """一个写到磁盘上的种子世界，外加判分要用的三样：问题、修复前指纹表、修复前清点。"""

    bundle: ContentBundle
    root: Path
    issues: dict[str, Issue]
    before: dict[str, str]
    census: ContentCensus

    def judge(self, rule_code: str) -> FixJudgement:
        issue = self.issues[rule_code]
        return judge_fix(
            self.root,
            issue=issue,
            fingerprint=issue_fingerprint(issue),
            before=self.before,
            census=self.census,
        )

    def write_tree(self, tree_id: str, tree: dict) -> None:
        path = self.root / "dialogues" / "trees" / f"{tree_id}.json"
        path.write_text(json.dumps(tree, ensure_ascii=False), encoding="utf-8")


def _world(world_id: str, root: Path) -> _World:
    """种子世界走一次磁盘往返——判分两侧必须取自同一次往返（见 ``_trial_plan`` 的说明）。"""
    bundle = dict(build_actionability_worlds())[world_id]
    ContentStore(root).save(bundle)
    loaded = ContentStore(root).load()
    issues = open_issues_of(loaded, root)
    return _World(
        bundle=loaded,
        root=root,
        issues={issue.rule_code: issue for issue in issues},
        before={
            issue_fingerprint(issue): f"{issue.rule_code} @ {issue.target_ref}" for issue in issues
        },
        census=take_census(loaded),
    )


@pytest.fixture
def t01(tmp_path: Path) -> _World:
    """T01（悬空跳转 + 不可达节点）。"""
    return _world("T01", tmp_path / "w")


def _tree_json(bundle: ContentBundle, tree_id: str) -> dict:
    return bundle.dialogue_trees[tree_id].model_dump(mode="json", exclude_none=True)


# --- 语料 -------------------------------------------------------------------


def test_corpus_drops_the_clean_world_and_every_world_has_findings() -> None:
    worlds = build_actionability_worlds()
    assert "T00" not in dict(worlds), "阴性对照世界没有可修的东西，不该进可执行率语料"
    assert len(worlds) >= 10
    for world_id, bundle in worlds:
        assert open_issues_of(bundle), f"{world_id} 没有开放问题，不该出现在语料里"


def test_finding_text_is_the_shipping_renderers_own_output(t01: _World) -> None:
    """修复者看到的就是报告本身的那段文字——不是量表另写的一份。"""
    issue = next(iter(t01.issues.values()))
    assert finding_report_text(issue) == "\n".join(render_issue_markdown(issue))


# --- 修复者能碰到的东西 ------------------------------------------------------


def test_fixer_only_gets_file_access_and_cannot_escape_the_content_root(tmp_path: Path) -> None:
    tools = _ContentFileTools(tmp_path)
    assert {tool.name for tool in tools.as_tools()} == {
        "list_content_files",
        "read_content_file",
        "write_content_file",
    }
    # 越界与猜错路径都必须回成一条观察，而不是抛出去把整条 trial 判死。
    assert "只能读写内容目录里的文件" in tools.read_file("../../secrets.json")
    assert "只能读写内容目录里的文件" in tools.write_file("../../secrets.json", "x")
    assert not (tmp_path.parent / "secrets.json").exists()
    assert tools.written == []
    assert "没有这个文件" in tools.read_file("quests/nope.json")
    tools.write_file("quests/new.json", "{}")
    assert tools.written == ["quests/new.json"]
    assert (tmp_path / "quests" / "new.json").read_text(encoding="utf-8") == "{}"


def test_no_file_listing_variant_removes_exactly_one_capability(tmp_path: Path) -> None:
    """加严对照:只拿掉"翻目录",读写照旧,提示词同步换成不提目录清单的那一份。"""
    tools = _ContentFileTools(tmp_path, file_listing=False)
    assert {tool.name for tool in tools.as_tools()} == {
        "read_content_file",
        "write_content_file",
    }
    assert "list_content_files" not in FIXER_SYSTEM_PROMPT_NO_LISTING
    assert "list_content_files" in FIXER_SYSTEM_PROMPT
    tools.read_file("quests/guessed_wrong.json")
    assert tools.missed_reads == ["quests/guessed_wrong.json"]


def test_fixer_brief_names_no_rule_and_no_defect_family() -> None:
    """提示词里一旦漏出规则名或缺陷家族，量到的就是提示词而不是报告。"""
    rule_codes = {code for _, bundle in build_actionability_worlds() for code in _codes(bundle)}
    assert not [code for code in rule_codes if code.lower() in FIXER_SYSTEM_PROMPT.lower()]
    assert "审计" not in FIXER_SYSTEM_PROMPT


def _codes(bundle: ContentBundle) -> set[str]:
    return {issue.rule_code for issue in open_issues_of(bundle)}


# --- 判分 -------------------------------------------------------------------


def test_untouched_world_counts_as_not_fixed(t01: _World) -> None:
    judgement = t01.judge("DIALOGUE_TREE_BROKEN_LINK")
    assert (judgement.target_gone, judgement.introduced) == (False, [])
    assert judgement.verdict == "没修掉(问题原样还在)"
    assert judgement.edit.touched_nothing


def test_real_repair_counts_as_fixed(t01: _World) -> None:
    tree = _tree_json(t01.bundle, "dtree_r1_q1")
    tree["nodes"]["n_lie"]["next_node"] = "n_close"
    t01.write_tree("dtree_r1_q1", tree)
    judgement = t01.judge("DIALOGUE_TREE_BROKEN_LINK")
    assert (judgement.target_gone, judgement.introduced) == (True, [])
    assert (judgement.fixed, judgement.verdict) == (True, "修好了")
    # 真修好的那次也要留下"改了什么"的账：改了一个对象、一个节点，没删任何东西。
    assert judgement.edit.changed_objects == ["dialogue_tree:dtree_r1_q1"]
    assert judgement.edit.changed_nodes == ["dialogue_tree:dtree_r1_q1/n_lie"]
    assert judgement.edit.removed_objects == judgement.edit.removed_nodes == []


def test_pointing_at_another_missing_node_is_still_not_fixed(t01: _World) -> None:
    """把悬空跳转"修"成另一个同样不存在的节点：换了个名字，洞还在，判定必须是没修掉。

    指纹按"规则 + 对象 + 证据路径"算，与那个不存在的节点叫什么无关——这正是判定要的语义：
    同一处字段上的同一种毛病就是同一条问题。
    """
    tree = _tree_json(t01.bundle, "dtree_r1_q1")
    tree["nodes"]["n_lie"]["next_node"] = "n_also_missing"
    t01.write_tree("dtree_r1_q1", tree)
    judgement = t01.judge("DIALOGUE_TREE_BROKEN_LINK")
    assert (judgement.target_gone, judgement.introduced) == (False, [])
    assert judgement.verdict == "没修掉(问题原样还在)"


def test_repair_that_opens_a_new_hole_does_not_count(t01: _World) -> None:
    """这条修好了，却在别处捅了个新窟窿：不算修好，且新问题要如实列出来。"""
    tree = _tree_json(t01.bundle, "dtree_r1_q1")
    tree["nodes"]["n_lie"]["next_node"] = "n_close"
    tree["nodes"]["n_truth"]["next_node"] = "n_nowhere"
    t01.write_tree("dtree_r1_q1", tree)
    judgement = t01.judge("DIALOGUE_TREE_BROKEN_LINK")
    assert judgement.target_gone is True
    assert any("BROKEN_LINK" in item for item in judgement.introduced)
    assert judgement.verdict.startswith("这条消失了,但引入了新问题")


def test_corrupted_content_file_counts_as_not_fixed(t01: _World) -> None:
    (t01.root / TREE_FILE).write_text("{ 这不是 JSON", encoding="utf-8")
    judgement = t01.judge("DIALOGUE_TREE_BROKEN_LINK")
    assert (judgement.target_gone, judgement.introduced) == (False, [])
    assert "加载失败" in judgement.verdict


# --- 内容保全：修掉的，还是删掉的？ -------------------------------------------


def test_deleting_the_offending_node_is_not_a_fix(t01: _World) -> None:
    """阴性对照（判分层）：把悬空跳转的那个节点整个删掉。

    指纹会消失、也不引入新问题——旧判据下这就是一次"修好了"。内容保全检查必须拦住它，否则
    整份可执行率里混着的是"删出来的通过"。
    """
    tree = _tree_json(t01.bundle, "dtree_r1_q1")
    del tree["nodes"]["n_lie"]
    tree["nodes"]["n_open"]["choices"][0]["next_node"] = "n_truth"
    tree["nodes"]["n_open"]["choices"][1]["next_node"] = "n_truth"
    t01.write_tree("dtree_r1_q1", tree)
    judgement = t01.judge("DIALOGUE_TREE_BROKEN_LINK")
    assert judgement.fingerprint_ok is True, "旧判据下这次删除确实会被算成修好"
    assert judgement.fixed is False
    assert judgement.preserved is False
    assert judgement.edit.removed_nodes == ["dialogue_tree:dtree_r1_q1/n_lie"]
    assert any("删掉了对话节点" in reason for reason in judgement.damage)
    assert judgement.verdict.startswith("这条消失了,但内容被毁")


def test_deleting_the_whole_target_object_is_not_a_fix(t01: _World) -> None:
    """更粗暴的一种：直接把出问题的对话树文件删掉。"""
    (t01.root / TREE_FILE).unlink()
    judgement = t01.judge("DIALOGUE_TREE_BROKEN_LINK")
    assert judgement.fingerprint_ok is True
    assert judgement.fixed is False
    assert judgement.edit.removed_objects == ["dialogue_tree:dtree_r1_q1"]
    assert any("整个删了" in reason for reason in judgement.damage)


def test_blanking_the_lines_instead_of_fixing_is_not_a_fix(t01: _World) -> None:
    """节点还在、对象还在，但台词被清空——保全检查看的是正文，不只是结构。"""
    tree = _tree_json(t01.bundle, "dtree_r1_q1")
    tree["nodes"]["n_lie"]["next_node"] = "n_close"
    for node in tree["nodes"].values():
        node["text"] = ""
    t01.write_tree("dtree_r1_q1", tree)
    judgement = t01.judge("DIALOGUE_TREE_BROKEN_LINK")
    assert judgement.fingerprint_ok is True
    assert judgement.fixed is False
    assert any("掏到 0 字" in reason for reason in judgement.damage)


def test_deleting_the_unreachable_node_the_report_named_is_allowed(tmp_path: Path) -> None:
    """反过来也要成立：规则说的就是"这个节点没人走得到"，删掉它是正规修法之一，不能判失败。"""
    world = _world("T01", tmp_path / "w")
    issue = world.issues["DIALOGUE_TREE_UNREACHABLE_NODE"]
    tree_id = issue.target_ref.split(":", 1)[1]
    tree = _tree_json(world.bundle, tree_id)
    assert issue.node_ref in tree["nodes"], "报告得指名是哪个节点，豁免才有落点"
    del tree["nodes"][issue.node_ref]
    world.write_tree(tree_id, tree)
    judgement = world.judge("DIALOGUE_TREE_UNREACHABLE_NODE")
    assert (judgement.fixed, judgement.damage) == (True, [])


def test_deleting_the_whole_tree_is_never_allowed_even_for_the_unreachable_node_rule(
    tmp_path: Path,
) -> None:
    """豁免落在"报告点名的那个节点"上，不是落在它所在的那棵树上。

    否则这条规则会变成一张免死金牌：报告说"这个节点没人走得到"，修复者把整棵对话树删掉，
    指纹一样消失，还能算通过。
    """
    world = _world("T01", tmp_path / "w")
    issue = world.issues["DIALOGUE_TREE_UNREACHABLE_NODE"]
    (world.root / "dialogues" / "trees" / f"{issue.target_ref.split(':', 1)[1]}.json").unlink()
    judgement = world.judge("DIALOGUE_TREE_UNREACHABLE_NODE")
    assert judgement.fingerprint_ok is True
    assert judgement.fixed is False
    assert any("整个删了" in reason for reason in judgement.damage)


def test_deleting_the_duplicate_relation_the_report_named_is_allowed(tmp_path: Path) -> None:
    """同理：重复关系删掉副本正是规则要的修法，事实还在，不算毁内容。"""
    world = _world("T09", tmp_path / "w")
    issue = world.issues["DUPLICATE_RELATION"]
    _, source, kind, target = issue.target_ref.split(":")
    path = world.root / "world" / "relations.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    duplicates = [
        row for row in rows if (row["source"], row["kind"], row["target"]) == (source, kind, target)
    ]
    assert len(duplicates) == 2
    rows.remove(duplicates[0])
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8"
    )
    judgement = world.judge("DUPLICATE_RELATION")
    assert judgement.edit.removed_objects == [issue.target_ref]
    assert (judgement.fixed, judgement.damage) == (True, [])


def test_deleting_either_half_of_a_named_conflicting_pair_is_allowed(tmp_path: Path) -> None:
    """RELATION_CONFLICT 的建议动作就是"这两条只能留一条"，删掉哪一条都算照做。

    ``target_ref`` 在这条规则上是合成出来的 ``relation:<a>:conflict:<b>``，磁盘上并不存在；
    可删集合必须取自这条问题**自己的证据**，否则修复者照着建议删掉结盟那条，会被判成毁内容。
    """
    world = _world("T09", tmp_path / "w")
    issue = world.issues["RELATION_CONFLICT"]
    pairs = [evidence.relation for evidence in issue.evidence if evidence.relation]
    assert len(pairs) == 2, "这条问题的证据里点名了互相矛盾的两条关系"
    source, kind, target = pairs[0]
    path = world.root / "world" / "relations.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    kept = [
        row for row in rows if (row["source"], row["kind"], row["target"]) != (source, kind, target)
    ]
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in kept) + "\n", encoding="utf-8"
    )
    judgement = world.judge("RELATION_CONFLICT")
    assert judgement.edit.removed_objects == [f"relation:{source}:{kind}:{target}"]
    assert (judgement.fixed, judgement.damage) == (True, [])


def test_deleting_a_second_relation_beyond_the_named_one_is_not_allowed(tmp_path: Path) -> None:
    """豁免只给报告指名的那一个元素：顺手多删一条关系，照样判失败。"""
    world = _world("T09", tmp_path / "w")
    issue = world.issues["DUPLICATE_RELATION"]
    _, source, kind, target = issue.target_ref.split(":")
    path = world.root / "world" / "relations.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    triple = (source, kind, target)
    kept = [row for row in rows if (row["source"], row["kind"], row["target"]) != triple]
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in kept) + "\n", encoding="utf-8"
    )
    judgement = world.judge("DUPLICATE_RELATION")
    assert judgement.edit.removed_objects == [issue.target_ref] * 2, "两条同名关系都被删了"
    assert judgement.fixed is False
    # 豁免只兑掉其中一条，另一条照记：这条关系本身彻底没了，事实也就没了。
    assert judgement.damage == [f"把这条问题指向的对象 {issue.target_ref} 整个删了"]


def test_shortening_is_allowed_only_for_the_rule_that_asks_for_it(tmp_path: Path) -> None:
    """TEXT_TOO_LONG_FOR_UI 的正规修法就是把这段文本改短，不能拿"正文变少"判它失败。"""
    world = _world("T03", tmp_path / "w")
    issue = world.issues["TEXT_TOO_LONG_FOR_UI"]
    dialogue_id = issue.target_ref.split(":", 1)[1]
    path = world.root / "dialogues" / f"{dialogue_id}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["text"] = "这条路上的盐车被劫了三次。"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    judgement = world.judge("TEXT_TOO_LONG_FOR_UI")
    assert judgement.edit.gutted_texts, "这次改动确实把正文砍到三成以下，豁免要在知情的前提下给"
    assert (judgement.fixed, judgement.damage) == (True, [])


# --- 端到端（假模型） --------------------------------------------------------


def test_run_scores_a_scripted_fixer_end_to_end(tmp_path: Path) -> None:
    """一个只会修悬空跳转的脚本修复者：该修好的修好，另一条如实记成没修好。"""
    bundle = dict(build_actionability_worlds())["T01"]
    tree = _tree_json(bundle, "dtree_r1_q1")
    tree["nodes"]["n_lie"]["next_node"] = "n_close"
    payload = json.dumps(tree, ensure_ascii=False)

    def factory():
        return scripted_chat_model(
            [
                ("list_content_files", {}),
                ("write_content_file", {"path": TREE_FILE, "content": payload}),
                "把 n_lie 的跳转改回 n_close。",
            ]
        )

    report = run_report_actionability(
        tmp_path / "ws", chat_model_factory=factory, world_ids={"T01"}, label="离线自检"
    )
    assert report.total_findings == 2
    assert report.fixed_count == 1
    assert report.actionable_rate == 0.5
    assert (report.fingerprint_fixed_count, report.damaged_count) == (1, 0)
    assert report.total_cost_usd == 0.0
    by_rule = {trial.rule_code: trial for trial in report.trials}
    assert by_rule["DIALOGUE_TREE_BROKEN_LINK"].fixed is True
    assert by_rule["DIALOGUE_TREE_UNREACHABLE_NODE"].fixed is False
    for trial in report.trials:
        assert trial.report_text.startswith(f"- `{trial.rule_code}`")
        assert trial.files_written == [TREE_FILE]


def test_run_flags_a_fixer_that_deletes_instead_of_repairing(tmp_path: Path) -> None:
    """阴性对照（整轮层）：一个"专删不修"的修复者，整轮跑下来必须一条都不算通过。

    它删掉出问题的那个节点：旧判据（指纹消失且不引入新问题）会给它一个 ✅，加了内容保全
    检查之后必须翻成 ❌，并且翻转要被记进 ``damaged_count``。零翻转的结论只有在这条对照
    成立时才可信。
    """
    bundle = dict(build_actionability_worlds())["T01"]
    tree = _tree_json(bundle, "dtree_r1_q1")
    del tree["nodes"]["n_lie"]
    tree["nodes"]["n_open"]["choices"][0]["next_node"] = "n_truth"
    tree["nodes"]["n_open"]["choices"][1]["next_node"] = "n_truth"
    payload = json.dumps(tree, ensure_ascii=False)

    report = run_report_actionability(
        tmp_path / "ws",
        chat_model_factory=lambda: scripted_chat_model(
            [
                ("write_content_file", {"path": TREE_FILE, "content": payload}),
                "把出问题的节点删了。",
            ]
        ),
        world_ids={"T01"},
        label="离线自检",
    )
    broken = next(t for t in report.trials if t.rule_code == "DIALOGUE_TREE_BROKEN_LINK")
    assert broken.fingerprint_ok is True, "旧判据下这确实会被算成一次修好"
    assert (broken.fixed, broken.preserved) == (False, False)
    assert broken.edit.removed_nodes == ["dialogue_tree:dtree_r1_q1/n_lie"]
    assert report.fixed_count == 0
    assert report.fingerprint_fixed_count == 1
    assert report.damaged_count == 1


def test_run_records_a_fixer_that_never_touched_a_file(tmp_path: Path) -> None:
    report = run_report_actionability(
        tmp_path / "ws",
        chat_model_factory=lambda: scripted_chat_model(["这条我看不明白，没动。"]),
        world_ids={"T01"},
        label="离线自检",
    )
    assert report.fixed_count == 0
    assert report.actionable_rate == 0.0
    for trial in report.trials:
        assert trial.files_written == []
        assert trial.verdict == "修复者一个文件都没改"


def test_trials_are_isolated_from_each_other(tmp_path: Path) -> None:
    """每条 finding 一份全新的世界拷贝：上一条改坏了，下一条也得从干净的世界开始。"""
    report = run_report_actionability(
        tmp_path / "ws",
        chat_model_factory=lambda: scripted_chat_model(
            [("write_content_file", {"path": TREE_FILE, "content": "坏掉的内容"}), "改完了。"]
        ),
        world_ids={"T01"},
        label="离线自检",
    )
    assert [trial.verdict for trial in report.trials] == [
        "内容文件写坏了,加载失败(CorruptJSONError)"
    ] * 2
    roots = sorted(p.name for p in (tmp_path / "ws").iterdir() if p.is_dir())
    assert roots == ["T01_00", "T01_01", "T01_pristine"], "两条 finding 各一份世界,外加一份原样"


def test_a_fixer_that_changes_nothing_never_looks_like_a_regression(tmp_path: Path) -> None:
    """没动手的修复者不许被记成"引入了新问题"。

    T04 是这条的守门世界:占位符规则把"先遇到的那个语言"当基准,而世界在内存里的插入顺序与
    从磁盘读回来的顺序不同 —— 一旦"修复前"取自内存、"修复后"取自磁盘,同一条没人碰过的问题
    就会凭空变成一条新问题。两边必须走同一次磁盘往返。
    """
    report = run_report_actionability(
        tmp_path / "ws",
        chat_model_factory=lambda: scripted_chat_model(["这条我不动。"]),
        world_ids={"T04"},
        label="离线自检",
    )
    assert report.total_findings == 3
    for trial in report.trials:
        assert trial.introduced == [], f"{trial.rule_code} 无人改动却被判成引入新问题"
        assert trial.verdict == "修复者一个文件都没改"


def test_replayed_report_text_is_what_the_fixer_sees(tmp_path: Path) -> None:
    """重放旧报告：修复者看到的必须是记录里那段旧文字，而不是今天渲染出来的这段。

    这是"判据变了、旧报告要用新判据重测一遍"的通路——否则只能把旧的报告渲染器找回来重跑。
    """
    first = run_report_actionability(
        tmp_path / "ws1",
        chat_model_factory=lambda: scripted_chat_model(["不改。"]),
        world_ids={"T01"},
        label="旧一代",
    )
    write_actionability_artifacts(first, tmp_path / "out" / "old")
    replay = load_report_texts(tmp_path / "out" / "old.json")
    replay = {key: f"（这是旧报告的文字）{text}" for key, text in replay.items()}

    second = run_report_actionability(
        tmp_path / "ws2",
        chat_model_factory=lambda: scripted_chat_model(["不改。"]),
        world_ids={"T01"},
        label="重放",
        report_texts=replay,
    )
    assert [trial.report_text for trial in second.trials] == [replay[key] for key in replay]

    with pytest.raises(KeyError, match="没有这一条"):
        run_report_actionability(
            tmp_path / "ws3",
            chat_model_factory=lambda: scripted_chat_model(["不改。"]),
            world_ids={"T01"},
            label="缺条的重放",
            report_texts={},
        )


def test_budget_stop_aborts_before_spending_more(tmp_path: Path) -> None:
    report = run_report_actionability(
        tmp_path / "ws",
        chat_model_factory=lambda: scripted_chat_model(["不改。"]),
        budget_usd=0.0,
        label="离线自检",
    )
    assert report.budget_stopped is True
    assert report.total_findings == 0
    assert report.actionable_rate == 0.0


# --- 产出 -------------------------------------------------------------------


def test_markdown_and_json_artifacts_list_every_finding(tmp_path: Path) -> None:
    report = run_report_actionability(
        tmp_path / "ws",
        chat_model_factory=lambda: scripted_chat_model(["不改。"]),
        world_ids={"T01"},
        label="离线自检",
    )
    markdown = render_actionability_markdown(report)
    assert "可执行率:0.0%" in markdown
    for trial in report.trials:
        assert f"`{trial.rule_code}`" in markdown
        assert trial.verdict in markdown

    write_actionability_artifacts(report, tmp_path / "out" / "baseline")
    restored = json.loads((tmp_path / "out" / "baseline.json").read_text(encoding="utf-8"))
    assert restored["label"] == "离线自检"
    assert len(restored["trials"]) == len(report.trials)
    assert (tmp_path / "out" / "baseline.md").read_text(encoding="utf-8") == markdown
