"""Agentic retrieval (deepening L4): bounded retrieve→assess→re-query loop.

The decisive test is the multi-hop uplift: a question whose answer entity shares NO terms with
the question, reachable only by bridging through the first pass's evidence — single-pass misses
it, the agentic loop must surface it.
"""

from __future__ import annotations

import logging
from pathlib import Path

from owcopilot.content.models import ContentBundle, Entity, EntityType, Relation
from owcopilot.content.store import ContentStore
from owcopilot.llm.cache import NoOpCache
from owcopilot.llm.gateway import LLMGateway, LLMProvider
from owcopilot.llm.router import StaticRouter
from owcopilot.pipeline.project import ProjectContext
from owcopilot.retrieval.agentic import (
    AgenticRetriever,
    _agentic_user_prompt,
    _heuristic_followups,
    _pack_text,
    term_coverage,
)


class _ScriptedProvider:
    """Offline double: returns canned replies by call index, records each call's system+user.

    Assembles inputs and echoes scripted outputs only — it runs NO retrieval/judge business
    logic (that path stays in the production ``AgenticRetriever`` under test)."""

    def __init__(self, replies: list[str]) -> None:
        self._replies = replies
        self.calls = 0
        self.seen_systems: list[str] = []
        self.seen_users: list[str] = []

    def complete(self, *, system: str, user: str, model: str) -> tuple[str, int, int]:
        self.seen_systems.append(system)
        self.seen_users.append(user)
        reply = self._replies[min(self.calls, len(self._replies) - 1)]
        self.calls += 1
        return reply, 5, 5


def _gateway(providers: dict[str, LLMProvider], mapping: dict[str, str]) -> LLMGateway:
    return LLMGateway(providers=providers, router=StaticRouter(mapping=mapping), cache=NoOpCache())


def _two_hop_world(root: Path) -> None:
    """铁卫军团 --allied_with--> 雾隐盟, whose leader 沈青崖 is only named INSIDE 雾隐盟's
    description. Asking about 铁卫军团's ally's leader shares no terms with 沈青崖's row."""
    ContentStore(root).save(
        ContentBundle(
            entities={
                "fac_iron": Entity(
                    id="fac_iron",
                    name="铁卫军团",
                    type=EntityType.FACTION,
                    description="驻守北境的军团，与雾隐盟结为盟友。",
                ),
                "fac_mist": Entity(
                    id="fac_mist",
                    name="雾隐盟",
                    type=EntityType.FACTION,
                    description="雾隐盟由领袖沈青崖统领，行事隐秘。",
                ),
                "npc_shen": Entity(
                    id="npc_shen",
                    name="沈青崖",
                    type=EntityType.NPC,
                    # 真实 canon 惯例：领袖条目会提及所属阵营——这正是桥接查询能命中的互文
                    description="雾隐盟领袖沈青崖常驻于听雾崖，深居简出。",
                ),
                # noise so retrieval has something to rank against
                **{
                    f"npc_x{i}": Entity(
                        id=f"npc_x{i}",
                        name=f"路人{i}",
                        type=EntityType.NPC,
                        description=f"集市里的普通商贩{i}，与本案无关。",
                    )
                    for i in range(6)
                },
            },
            relations=[Relation(source="fac_iron", target="fac_mist", kind="allied_with")],
        )
    )


def _project(tmp_path: Path) -> ProjectContext:
    root = tmp_path / "content"
    _two_hop_world(root)
    return ProjectContext.open(root, sqlite_path=tmp_path / "p.sqlite")


def test_multi_hop_uplift_over_single_pass(tmp_path: Path) -> None:
    """单轮检索拿不到跨跳证据；agentic 循环通过'证据里的实体名 + 缺口词'的后续查询捞到。
    注意查询不点名答案实体（沈青崖）——这才是真多跳。"""
    project = _project(tmp_path)
    try:
        query = "铁卫军团的盟友的领袖驻在哪里"
        single = project.context_builder.build(query, budget_tokens=400)
        agentic = AgenticRetriever(project.context_builder, max_rounds=3).retrieve(
            query, budget_tokens=400
        )

        refs_agentic = {hit.ref for hit in agentic.pack.hits}
        # 目标证据（沈青崖的驻地行）必须被 agentic 捞到
        assert "entity:npc_shen" in refs_agentic
        assert agentic.rounds >= 2  # 真的跑了第二轮
        assert agentic.followup_queries  # 且是靠后续查询捞到的
        # uplift 断言：agentic 覆盖率不低于单轮（合并只加召回）
        assert term_coverage(query, agentic.pack) >= term_coverage(query, single)
    finally:
        project.close()


def test_sufficient_first_pass_stops_at_one_round(tmp_path: Path) -> None:
    """首轮证据已充分（直问某实体）→ 一轮即停，不多花检索。"""
    project = _project(tmp_path)
    try:
        result = AgenticRetriever(project.context_builder, max_rounds=3).retrieve(
            "雾隐盟是什么", budget_tokens=400
        )
        assert result.rounds == 1
        assert result.sufficient is True
        assert result.followup_queries == []
    finally:
        project.close()


def test_loop_is_bounded_and_auditable(tmp_path: Path) -> None:
    """无法满足的查询也必须在 max_rounds 内停，且决策记录完整。"""
    project = _project(tmp_path)
    try:
        result = AgenticRetriever(project.context_builder, max_rounds=2).retrieve(
            "不存在的圣物赫尔墨斯之杖的下落", budget_tokens=400
        )
        assert result.rounds <= 2
        assert result.sufficient is False
        assert isinstance(result.coverage, float)
    finally:
        project.close()


def test_heuristic_followups_bridge_with_top_evidence_title(tmp_path: Path) -> None:
    """$0 后续查询把'未覆盖词'与首轮最相关实体名桥接（多跳的关键一步）。"""
    project = _project(tmp_path)
    try:
        query = "铁卫军团的盟友的领袖驻在哪里"
        pack = project.context_builder.build(query, budget_tokens=400)
        followups = _heuristic_followups(query, pack)
        if followups:  # 首轮若已全覆盖则无后续（此世界不会，防御断言）
            assert any(" " in f for f in followups)  # 存在桥接式查询
    finally:
        project.close()


def test_llm_followups_bridge_by_extracting_entity_from_evidence(tmp_path: Path) -> None:
    """LLM 后续查询的桥接能力（从证据文本抽出 query 未含的实体名）——scripted gateway 模拟
    真实模型行为：读到雾隐盟证据后直接查'沈青崖'。这是 live 路径的语义，$0 可测。"""
    from owcopilot.llm.cache import NoOpCache
    from owcopilot.llm.gateway import LLMGateway
    from owcopilot.llm.router import StaticRouter

    class _BridgeProvider:
        def complete(self, *, system: str, user: str, model: str):
            return '["沈青崖 驻地"]', 5, 5

    project = _project(tmp_path)
    try:
        gateway = LLMGateway(
            providers={"cheap": _BridgeProvider()},
            router=StaticRouter(mapping={"agentic_rag_followup": "cheap"}),
            cache=NoOpCache(),
        )
        result = AgenticRetriever(project.context_builder, gateway=gateway, max_rounds=2).retrieve(
            "铁卫军团的盟友的领袖驻在哪里", budget_tokens=400
        )
        assert result.rounds == 2
        assert result.followup_queries == ["沈青崖 驻地"]
        assert "entity:npc_shen" in {hit.ref for hit in result.pack.hits}
    finally:
        project.close()


def test_qa_service_agentic_opt_in_answers_multi_hop(tmp_path: Path) -> None:
    """LoreQAService(agentic=True) 端到端：多跳问题的答案由捞到的证据支撑（离线 $0 路径）。"""
    from owcopilot.llm.cache import NoOpCache
    from owcopilot.llm.gateway import LLMGateway
    from owcopilot.llm.router import StaticRouter
    from owcopilot.qa.offline import OfflineQAProvider
    from owcopilot.qa.service import LoreQAService

    project = _project(tmp_path)
    try:
        gateway = LLMGateway(
            providers={"cheap": OfflineQAProvider()},
            router=StaticRouter(mapping={"qa_answer": "cheap", "agentic_rag_followup": "cheap"}),
            cache=NoOpCache(),
        )
        service = LoreQAService(
            gateway=gateway,
            context_builder=project.qa_context_builder(),
            bundle=project.bundle,
            agentic=True,
        )
        answer = service.ask("铁卫军团的盟友的领袖驻在哪里", budget_tokens=400)
        assert not answer.refused
        # 跨跳证据行进了答案的引用（听雾崖所在的 npc_shen 行）
        assert any(c.ref == "entity:npc_shen" for c in answer.citations)
    finally:
        project.close()


def test_llm_empty_reply_is_a_sufficiency_verdict_not_a_failure(tmp_path: Path) -> None:
    """两级充分性：词法 coverage 不达标但 LLM 明确回空（{'queries': []}）→ 判定充分、
    优雅停止（stop_reason=llm_no_gap），绝不落入启发式兜底继续空转。"""
    from owcopilot.llm.cache import NoOpCache
    from owcopilot.llm.gateway import LLMGateway
    from owcopilot.llm.router import StaticRouter

    class _NoGapProvider:
        def complete(self, *, system: str, user: str, model: str):
            return '{"queries": []}', 5, 5

    project = _project(tmp_path)
    try:
        gateway = LLMGateway(
            providers={"cheap": _NoGapProvider()},
            router=StaticRouter(mapping={"agentic_rag_followup": "cheap"}),
            cache=NoOpCache(),
        )
        result = AgenticRetriever(project.context_builder, gateway=gateway, max_rounds=3).retrieve(
            "铁卫军团的盟友的领袖驻在哪里", budget_tokens=400
        )
        assert result.stop_reason == "llm_no_gap"
        assert result.sufficient is True
        assert result.rounds == 1  # 未再空转
        assert result.followup_queries == []
    finally:
        project.close()


# ------------------------------------------------------------------------ level-2 LLM judge (T10)
def test_level2_judge_accepts_sufficient_evidence(tmp_path: Path) -> None:
    """词法 coverage 达标后，显式 LLM 判官判 sufficient=true（CRAG 'Correct'）→ 停，
    stop_reason=llm_sufficient，一轮即止，判官仅被咨询一次。"""
    project = _project(tmp_path)
    try:
        judge = _ScriptedProvider(['{"sufficient": true, "missing": ""}'])
        gateway = _gateway({"judge": judge}, {"agentic_rag_sufficiency": "judge"})
        result = AgenticRetriever(project.context_builder, gateway=gateway, max_rounds=2).retrieve(
            "雾隐盟是什么", budget_tokens=400
        )
        assert result.stop_reason == "llm_sufficient"
        assert result.sufficient is True
        assert result.rounds == 1
        assert result.followup_queries == []
        assert judge.calls == 1  # level-1 passed → judge consulted exactly once
        assert "sufficiency judge" in judge.seen_systems[0]  # the sufficiency prompt fired
    finally:
        project.close()


def test_level2_judge_rejection_triggers_corrective_round_steered_by_missing(
    tmp_path: Path,
) -> None:
    """词法达标但判官判 sufficient=false（CRAG 'Ambiguous'）→ 覆写词法判据、触发一轮纠正检索，
    且该后续查询被判官的 `missing` 短语显式引导。"""
    project = _project(tmp_path)
    try:
        judge = _ScriptedProvider(['{"sufficient": false, "missing": "沈青崖 的驻地"}'])
        rewriter = _ScriptedProvider(['{"queries": ["沈青崖 驻地"]}'])
        gateway = _gateway(
            {"judge": judge, "rewriter": rewriter},
            {"agentic_rag_sufficiency": "judge", "agentic_rag_followup": "rewriter"},
        )
        result = AgenticRetriever(project.context_builder, gateway=gateway, max_rounds=2).retrieve(
            "雾隐盟是什么", budget_tokens=400
        )
        assert result.rounds == 2  # judge overrode lexical → one corrective round fired
        assert result.followup_queries == ["沈青崖 驻地"]
        assert judge.calls == 1  # consulted once (round 2 has no budget to act on)
        assert "The evidence is still MISSING: 沈青崖 的驻地" in rewriter.seen_users[0]
    finally:
        project.close()


def test_level2_judge_failure_degrades_to_lexical_verdict_and_logs(tmp_path: Path, caplog) -> None:
    """判官回复不可解析（修复预算耗尽 → StructuredOutputError）→ 优雅降级为词法判据，
    不崩不空转，且留下告警痕迹（非静默吞异常）。"""
    project = _project(tmp_path)
    try:
        judge = _ScriptedProvider(["not json at all"])  # unparseable every attempt
        gateway = _gateway({"judge": judge}, {"agentic_rag_sufficiency": "judge"})
        with caplog.at_level(logging.WARNING, logger="owcopilot.retrieval.agentic"):
            result = AgenticRetriever(
                project.context_builder, gateway=gateway, max_rounds=2
            ).retrieve("雾隐盟是什么", budget_tokens=400)
        assert result.sufficient is True  # lexical 'sufficient' stands
        assert result.stop_reason == "coverage"
        assert result.rounds == 1
        assert result.degraded is True
        assert any(
            reason.startswith("agentic_sufficiency_judge_failed:")
            for reason in result.degradation_reasons
        )
        assert result.pack.degradation_reasons == result.degradation_reasons
        assert judge.calls == 2  # original + one repair reask, both unparseable
        assert "sufficiency judge failed" in caplog.text  # degrade left a trace
    finally:
        project.close()


def test_level1_lexical_gap_never_consults_the_judge(tmp_path: Path) -> None:
    """词法 coverage 不达标（CRAG 'Incorrect'）→ 直接判欠检索、绝不调用判官（省一次调用，
    也保证 gateway 存在时的既有行为不被判官打断）。"""
    project = _project(tmp_path)
    try:
        judge = _ScriptedProvider(['{"sufficient": true, "missing": ""}'])
        rewriter = _ScriptedProvider(['{"queries": ["赫尔墨斯之杖 下落"]}'])
        gateway = _gateway(
            {"judge": judge, "rewriter": rewriter},
            {"agentic_rag_sufficiency": "judge", "agentic_rag_followup": "rewriter"},
        )
        result = AgenticRetriever(project.context_builder, gateway=gateway, max_rounds=2).retrieve(
            "不存在的圣物赫尔墨斯之杖的下落", budget_tokens=400
        )
        assert judge.calls == 0  # coverage<threshold short-circuits before any judge call
        assert result.sufficient is False
        assert result.stop_reason in {"max_rounds", "no_new_queries"}
    finally:
        project.close()


def test_followup_prompt_uses_inert_boundary_when_no_missing_hint(tmp_path: Path) -> None:
    """The no-hint path still omits MISSING while treating retrieved text as inert JSON data."""
    project = _project(tmp_path)
    try:
        query = "铁卫军团的盟友的领袖驻在哪里"
        rewriter = _ScriptedProvider(['{"queries": []}'])  # empty → llm_no_gap, single round
        gateway = _gateway({"rewriter": rewriter}, {"agentic_rag_followup": "rewriter"})
        AgenticRetriever(project.context_builder, gateway=gateway, max_rounds=3).retrieve(
            query, budget_tokens=400
        )
        pack = project.context_builder.build(query, budget_tokens=400)  # same deterministic pack
        evidence = _pack_text(pack) or "(no evidence retrieved)"
        expected = _agentic_user_prompt(query, evidence, label="EVIDENCE SO FAR")
        assert rewriter.seen_users[0] == expected
        assert "UNTRUSTED DATA JSON" in rewriter.seen_users[0]
        assert "MISSING" not in rewriter.seen_users[0]
    finally:
        project.close()


def test_sufficiency_verdict_missing_field_triggers_repair(tmp_path: Path) -> None:
    """`sufficient` 为必填：缺该字段的回复校验失败 → 触发 complete_structured 的有界修复重试
    （而非编造默认值），修复后的判据被采纳。"""
    project = _project(tmp_path)
    try:
        judge = _ScriptedProvider(
            ['{"missing": "the leader location"}', '{"sufficient": true, "missing": ""}']
        )
        gateway = _gateway({"judge": judge}, {"agentic_rag_sufficiency": "judge"})
        result = AgenticRetriever(project.context_builder, gateway=gateway, max_rounds=2).retrieve(
            "雾隐盟是什么", budget_tokens=400
        )
        assert judge.calls == 2  # first reply lacked `sufficient` → one repair reask
        assert "sufficient" in judge.seen_users[1]  # repair prompt carries the specific error
        assert result.stop_reason == "llm_sufficient"  # the repaired verdict was honored
        assert result.sufficient is True
    finally:
        project.close()
