"""草稿链路接素材（N4.2/N4.3，B1）：任务草稿 / 人设 / 对白树 / bark / flavor 五条链路。

契约见 ``src/owcopilot/inspiration/grounding.py``。每条链路四件事各钉一条：

1. **开关关 = 现行为逐字节一致** —— 接了检索器但开关关掉，与"根本没接检索器"产出同一个 prompt、
   同一份产物（这是接线前的形态）。
2. **开关开 + 有素材** —— 系统提示词里多出一段**独立的**「灵感参考」围栏段（素材绝不混进正典事实
   那一段），产物 metadata 记下 ``inspiration_refs``。
3. **开关开 + 无素材** —— 优雅降级：照常出稿，不报错，不留半个空段。
4. **泄漏检测对新产物生效** —— 模型把素材原文抄进草稿会被重算出来；改写转述不会。

另外钉住用途词表：只允许 ``inspiration`` 的素材不会被草稿链路召回（``use="drafting"``）——这一条
只在任务草稿一处重复验（其余链路复用同一个 ``retrieve_drafting_references``，逻辑共享，不逐链路
重复）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from owcopilot.app.actions import (
    add_reference_action,
    run_barks_action,
    run_character_action,
    run_dialogue_tree_action,
    run_draft_action,
    run_flavor_action,
)
from owcopilot.assist.barks import BarkBatchService
from owcopilot.assist.characters import CharacterBrief, CharacterProfileService
from owcopilot.assist.dialogue_trees import DialogueTreeService
from owcopilot.assist.drafts import QuestDraftService
from owcopilot.assist.flavor import FlavorBatchService
from owcopilot.cli.main import main
from owcopilot.content.models import ContentBundle, Entity, EntityType
from owcopilot.content.store import ContentStore
from owcopilot.inspiration.models import USE_DRAFTING, USE_INSPIRATION
from owcopilot.llm.cache import NoOpCache
from owcopilot.llm.gateway import LLMGateway
from owcopilot.llm.router import StaticRouter
from owcopilot.pipeline.project import ProjectContext

# 素材原文。第二句是"会被抄"的那句：连续汉字远超严格档（第三方参考素材：连续 8 字）。
_MATERIAL = (
    "枯井镇的商会围绕盐道彼此设局，账册与船期都是筹码。"
    "夜里盐车压过石桥，桥下的水声盖住了驮队里最后一声咳嗽。"
)
_COPIED_SENTENCE = "夜里盐车压过石桥，桥下的水声盖住了驮队里最后一声咳嗽。"
# 素材第一句：不含任何实体显示名，可以直接放进 bark/flavor 的产物文本而不触发实体引用 lint
# （bark/flavor 的接受门会拒绝提到未在 allowed_entity_ids 里的实体名）。
_SAFE_COPIED_SENTENCE = "枯井镇的商会围绕盐道彼此设局，账册与船期都是筹码。"
_BRIEF = "让押运队把盐车送过石桥，路上要有商会设的局"
_CHARACTER_CONCEPT = "在盐道上替商会押船的掮客"
_BARK_TOPIC = "过桥前的最后叮嘱"

# 围栏标记来自 llm/spotlight.py：素材在 prompt 里必须是被围起来的数据，不是指令。
_FENCE = "〘UNTRUSTED REFERENCE MATERIAL — START〙"
_CANON_SECTION = "Content context:"


class _RecordingProvider:
    """记下每次调用看到的 system prompt，然后返回一份固定草稿。"""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.systems: list[str] = []

    def complete(self, *, system: str, user: str, model: str) -> tuple[str, int, int]:
        self.systems.append(system)
        text = json.dumps(self.payload, ensure_ascii=False)
        return text, 20, 10


def _gateway(provider: object, task: str) -> LLMGateway:
    return LLMGateway(
        providers={"cheap": provider},
        router=StaticRouter(mapping={task: "cheap"}),
        cache=NoOpCache(),
    )


def _world(root: Path) -> None:
    ContentStore(root).save(
        ContentBundle(
            entities={
                "npc_shen": Entity(
                    id="npc_shen", name="沈押司", type=EntityType.NPC, description="押运队的头目。"
                ),
                "npc_wei": Entity(
                    id="npc_wei", name="魏账房", type=EntityType.NPC, description="商会的账房先生。"
                ),
                "loc_bridge": Entity(
                    id="loc_bridge", name="石桥", type=EntityType.LOCATION, description="盐道要冲。"
                ),
            }
        )
    )


def _project(tmp_path: Path, *, material: str | None, allowed_use: str = USE_DRAFTING) -> Path:
    root = tmp_path / "content"
    _world(root)
    if material is not None:
        add_reference_action(root, title="盐道旧闻", text=material, allowed_uses=[allowed_use])
    return root


def _open(root: Path, tmp_path: Path) -> ProjectContext:
    return ProjectContext.open(root, sqlite_path=tmp_path / "runtime.sqlite")


# --- 任务草稿 --------------------------------------------------------------------------------

_QUEST_PAYLOAD: dict[str, Any] = {
    "title": "过桥的盐车",
    "giver_npc": "npc_shen",
    "location": "loc_bridge",
    "objective": "把盐车押过石桥。",
    "stages": [{"id": "stage_1", "summary": "在桥头点清账册。"}],
}


def _draft_service(
    project: ProjectContext, provider: _RecordingProvider, *, wire_references: bool
) -> QuestDraftService:
    return QuestDraftService(
        gateway=_gateway(provider, "quest_draft"),
        context_builder=project.context_builder,
        audit_runner=project.audit_runner,
        bundle=project.bundle,
        reference_context_builder=(project.reference_context_builder if wire_references else None),
    )


def test_quest_draft_switch_off_is_byte_identical_to_not_wiring_references(tmp_path) -> None:
    """哨兵：接了检索器但开关关着 == 根本没接检索器。prompt 与产物都必须逐字节相同。"""
    root = _project(tmp_path, material=_MATERIAL)
    project = _open(root, tmp_path)
    try:
        unwired = _RecordingProvider(_QUEST_PAYLOAD)
        wired_off = _RecordingProvider(_QUEST_PAYLOAD)
        before = _draft_service(project, unwired, wire_references=False).draft_quest(_BRIEF)
        after = _draft_service(project, wired_off, wire_references=True).draft_quest(_BRIEF)

        assert wired_off.systems == unwired.systems
        assert _FENCE not in wired_off.systems[0]
        assert after.quest.model_dump(mode="json") == before.quest.model_dump(mode="json")
        assert "inspiration_refs" not in after.quest.metadata
        assert after.inspiration_refs == [] and after.reference_report == []
    finally:
        project.close()


def test_quest_draft_with_references_adds_a_separate_fenced_section(tmp_path) -> None:
    root = _project(tmp_path, material=_MATERIAL)
    project = _open(root, tmp_path)
    try:
        provider = _RecordingProvider(_QUEST_PAYLOAD)
        result = _draft_service(project, provider, wire_references=True).draft_quest(
            _BRIEF, use_references=True
        )

        system = provider.systems[0]
        assert _FENCE in system
        assert _COPIED_SENTENCE in system
        # 素材是独立的一段，接在正典事实之后——两者绝不混进同一个 context 列表。
        canon_section = system.split(_CANON_SECTION, 1)[1].split(_FENCE, 1)[0]
        assert _COPIED_SENTENCE not in canon_section

        assert result.inspiration_refs
        # 逗号连接的字符串，与 worldgen 同格式（悬空引用检查按 split(",") 读）。
        assert result.quest.metadata["inspiration_refs"] == ",".join(result.inspiration_refs)
        assert [row.source_title for row in result.reference_report] == ["盐道旧闻"]
        assert result.reference_report[0].leakage is not None
        assert result.reference_report[0].leakage.checked is True
    finally:
        project.close()


def test_quest_draft_with_references_degrades_when_no_material_is_allowed(tmp_path) -> None:
    """开关开着、库里那本只允许用于创世取灵感：草稿链路召不到它，照常出稿。"""
    root = _project(tmp_path, material=_MATERIAL, allowed_use=USE_INSPIRATION)
    project = _open(root, tmp_path)
    try:
        provider = _RecordingProvider(_QUEST_PAYLOAD)
        result = _draft_service(project, provider, wire_references=True).draft_quest(
            _BRIEF, use_references=True
        )

        assert _FENCE not in provider.systems[0]
        assert result.quest.title == "过桥的盐车"
        assert result.inspiration_refs == [] and result.reference_report == []
        assert "inspiration_refs" not in result.quest.metadata
    finally:
        project.close()


def test_quest_draft_with_references_degrades_on_an_empty_library(tmp_path) -> None:
    root = _project(tmp_path, material=None)
    project = _open(root, tmp_path)
    try:
        provider = _RecordingProvider(_QUEST_PAYLOAD)
        result = _draft_service(project, provider, wire_references=True).draft_quest(
            _BRIEF, use_references=True
        )

        assert _FENCE not in provider.systems[0]
        assert result.quest.title == "过桥的盐车"
        assert result.reference_report == []
    finally:
        project.close()


def test_quest_draft_leakage_is_recomputed_on_the_new_artifact(tmp_path) -> None:
    """种子：模型把素材原文抄进任务目标 → 重算命中；改写转述 → 不命中。"""
    root = _project(tmp_path, material=_MATERIAL)
    project = _open(root, tmp_path)
    try:
        copied = dict(_QUEST_PAYLOAD, objective=_COPIED_SENTENCE)
        leaked = _draft_service(
            project, _RecordingProvider(copied), wire_references=True
        ).draft_quest(_BRIEF, use_references=True)
        leakage = leaked.reference_report[0].leakage
        assert leakage is not None and leakage.detected is True
        assert leakage.verbatim_spans
        assert "请把这些地方改写成自己的表述再提交" in leakage.message

        paraphrased = dict(_QUEST_PAYLOAD, objective="盐车趁夜过桥，水声掩住了随行者的动静。")
        clean = _draft_service(
            project, _RecordingProvider(paraphrased), wire_references=True
        ).draft_quest(_BRIEF, use_references=True)
        clean_leakage = clean.reference_report[0].leakage
        assert clean_leakage is not None
        assert clean_leakage.checked is True and clean_leakage.detected is False
    finally:
        project.close()


# --- 人设 ------------------------------------------------------------------------------------

_CHARACTER_PAYLOAD: dict[str, Any] = {
    "name": "白盐",
    "summary": "替商会押船的掮客。",
    "appearance": "袖口常年沾着盐粒。",
    "personality": "沉静，算得比说得多。",
    "backstory": "从账房小吏做到押船人。",
    "motivation": "赎回被扣下的船契。",
    "abilities": "认得每一处暗礁。",
    "weakness": "见不得旧账。",
    "voice": "话短，句尾常带一个「罢」。",
    "relationships": [],
}


def _character_service(
    project: ProjectContext, provider: _RecordingProvider, *, wire_references: bool
) -> CharacterProfileService:
    return CharacterProfileService(
        gateway=_gateway(provider, "character_profile"),
        bundle=project.bundle,
        context_builder=project.context_builder,
        reference_context_builder=(project.reference_context_builder if wire_references else None),
    )


def _character_brief(*, use_references: bool) -> CharacterBrief:
    return CharacterBrief(name="白盐", concept=_CHARACTER_CONCEPT, use_references=use_references)


def test_character_switch_off_is_byte_identical_to_not_wiring_references(tmp_path) -> None:
    root = _project(tmp_path, material=_MATERIAL)
    project = _open(root, tmp_path)
    try:
        unwired = _RecordingProvider(_CHARACTER_PAYLOAD)
        wired_off = _RecordingProvider(_CHARACTER_PAYLOAD)
        before = _character_service(project, unwired, wire_references=False).generate(
            _character_brief(use_references=False)
        )
        after = _character_service(project, wired_off, wire_references=True).generate(
            _character_brief(use_references=False)
        )

        assert wired_off.systems == unwired.systems
        assert _FENCE not in wired_off.systems[0]
        assert after.entity.model_dump(mode="json") == before.entity.model_dump(mode="json")
        assert "inspiration_refs" not in after.entity.metadata
        assert after.inspiration_refs == [] and after.reference_report == []
    finally:
        project.close()


def test_character_with_references_adds_a_separate_fenced_section(tmp_path) -> None:
    root = _project(tmp_path, material=_MATERIAL)
    project = _open(root, tmp_path)
    try:
        provider = _RecordingProvider(_CHARACTER_PAYLOAD)
        draft = _character_service(project, provider, wire_references=True).generate(
            _character_brief(use_references=True)
        )

        system = provider.systems[0]
        assert _FENCE in system
        assert _COPIED_SENTENCE in system
        world_section = system.split("World facts context:", 1)[1].split(_FENCE, 1)[0]
        assert _COPIED_SENTENCE not in world_section

        assert draft.inspiration_refs
        assert draft.entity.metadata["inspiration_refs"] == ",".join(draft.inspiration_refs)
        assert [row.source_title for row in draft.reference_report] == ["盐道旧闻"]
        assert draft.reference_report[0].used_for == "人设草稿的灵感参考"
    finally:
        project.close()


def test_character_with_references_degrades_on_an_empty_library(tmp_path) -> None:
    root = _project(tmp_path, material=None)
    project = _open(root, tmp_path)
    try:
        provider = _RecordingProvider(_CHARACTER_PAYLOAD)
        draft = _character_service(project, provider, wire_references=True).generate(
            _character_brief(use_references=True)
        )

        assert _FENCE not in provider.systems[0]
        assert draft.entity.name == "白盐"
        assert draft.reference_report == []
        assert "inspiration_refs" not in draft.entity.metadata
    finally:
        project.close()


def test_character_leakage_covers_the_profile_sections(tmp_path) -> None:
    """人设的主体文字住在 metadata["profile"] 里：抄在背景故事里同样要被查出来。"""
    root = _project(tmp_path, material=_MATERIAL)
    project = _open(root, tmp_path)
    try:
        copied = dict(_CHARACTER_PAYLOAD, backstory=_COPIED_SENTENCE)
        draft = _character_service(
            project, _RecordingProvider(copied), wire_references=True
        ).generate(_character_brief(use_references=True))
        leakage = draft.reference_report[0].leakage
        assert leakage is not None and leakage.detected is True
        assert leakage.verbatim_spans
    finally:
        project.close()


# --- 对白树（N4.3）----------------------------------------------------------------------------

_DIALOGUE_PARTICIPANTS = ["npc_shen", "npc_wei"]
_DIALOGUE_PAYLOAD: dict[str, Any] = {
    "title": "桥头对质",
    "root": "n1",
    "nodes": [
        {"id": "n1", "speaker": "npc_shen", "text": "账册对得上吗？", "next": "n2"},
        {"id": "n2", "speaker": "npc_wei", "text": "差着三本，得连夜补。"},
    ],
}


def _dialogue_service(
    project: ProjectContext, provider: _RecordingProvider, *, wire_references: bool
) -> DialogueTreeService:
    return DialogueTreeService(
        gateway=_gateway(provider, "dialogue_tree"),
        bundle=project.bundle,
        reference_context_builder=(project.reference_context_builder if wire_references else None),
    )


def test_dialogue_tree_switch_off_is_byte_identical_to_not_wiring_references(tmp_path) -> None:
    root = _project(tmp_path, material=_MATERIAL)
    project = _open(root, tmp_path)
    try:
        unwired = _RecordingProvider(_DIALOGUE_PAYLOAD)
        wired_off = _RecordingProvider(_DIALOGUE_PAYLOAD)
        before = _dialogue_service(project, unwired, wire_references=False).generate(
            participant_ids=_DIALOGUE_PARTICIPANTS, brief=_BRIEF
        )
        after = _dialogue_service(project, wired_off, wire_references=True).generate(
            participant_ids=_DIALOGUE_PARTICIPANTS, brief=_BRIEF
        )

        assert wired_off.systems == unwired.systems
        assert _FENCE not in wired_off.systems[0]
        assert after.tree.model_dump(mode="json") == before.tree.model_dump(mode="json")
        assert "inspiration_refs" not in after.tree.metadata
        assert after.inspiration_refs == [] and after.reference_report == []
    finally:
        project.close()


def test_dialogue_tree_with_references_adds_a_separate_fenced_section(tmp_path) -> None:
    root = _project(tmp_path, material=_MATERIAL)
    project = _open(root, tmp_path)
    try:
        provider = _RecordingProvider(_DIALOGUE_PAYLOAD)
        result = _dialogue_service(project, provider, wire_references=True).generate(
            participant_ids=_DIALOGUE_PARTICIPANTS, brief=_BRIEF, use_references=True
        )

        system = provider.systems[0]
        assert _FENCE in system
        assert _COPIED_SENTENCE in system
        # 素材是独立的一段，接在 voice cards 之后——两者绝不混进同一段。
        voice_section = system.split("Voice cards:", 1)[1].split(_FENCE, 1)[0]
        assert _COPIED_SENTENCE not in voice_section

        assert result.inspiration_refs
        assert result.tree.metadata["inspiration_refs"] == ",".join(result.inspiration_refs)
        assert [row.source_title for row in result.reference_report] == ["盐道旧闻"]
        assert result.reference_report[0].leakage is not None
        assert result.reference_report[0].leakage.checked is True
    finally:
        project.close()


def test_dialogue_tree_with_references_degrades_on_an_empty_library(tmp_path) -> None:
    root = _project(tmp_path, material=None)
    project = _open(root, tmp_path)
    try:
        provider = _RecordingProvider(_DIALOGUE_PAYLOAD)
        result = _dialogue_service(project, provider, wire_references=True).generate(
            participant_ids=_DIALOGUE_PARTICIPANTS, brief=_BRIEF, use_references=True
        )

        assert _FENCE not in provider.systems[0]
        assert result.tree.title == "桥头对质"
        assert result.inspiration_refs == [] and result.reference_report == []
        assert "inspiration_refs" not in result.tree.metadata
    finally:
        project.close()


def test_dialogue_tree_leakage_is_recomputed_on_the_new_artifact(tmp_path) -> None:
    """种子：模型把素材原文抄进节点台词 → 重算命中；改写转述 → 不命中。node 台词已经在
    ``draft_prose`` 里覆盖，不需要 extra_prose。"""
    root = _project(tmp_path, material=_MATERIAL)
    project = _open(root, tmp_path)
    try:
        leaked_payload = dict(
            _DIALOGUE_PAYLOAD,
            nodes=[{"id": "n1", "speaker": "npc_shen", "text": _COPIED_SENTENCE}],
        )
        leaked = _dialogue_service(
            project, _RecordingProvider(leaked_payload), wire_references=True
        ).generate(participant_ids=_DIALOGUE_PARTICIPANTS, brief=_BRIEF, use_references=True)
        leakage = leaked.reference_report[0].leakage
        assert leakage is not None and leakage.detected is True
        assert leakage.verbatim_spans

        paraphrased_payload = dict(
            _DIALOGUE_PAYLOAD,
            nodes=[
                {
                    "id": "n1",
                    "speaker": "npc_shen",
                    "text": "盐车趁夜过桥，水声掩住了随行者的动静。",
                }
            ],
        )
        clean = _dialogue_service(
            project, _RecordingProvider(paraphrased_payload), wire_references=True
        ).generate(participant_ids=_DIALOGUE_PARTICIPANTS, brief=_BRIEF, use_references=True)
        clean_leakage = clean.reference_report[0].leakage
        assert clean_leakage is not None
        assert clean_leakage.checked is True and clean_leakage.detected is False
    finally:
        project.close()


# --- bark（N4.3）------------------------------------------------------------------------------

_BARK_PAYLOAD: dict[str, Any] = {"variants": ["慢着点，别惊了车。"]}


def _bark_service(
    project: ProjectContext, provider: _RecordingProvider, *, wire_references: bool
) -> BarkBatchService:
    return BarkBatchService(
        gateway=_gateway(provider, "barks_batch"),
        bundle=project.bundle,
        reference_context_builder=(project.reference_context_builder if wire_references else None),
    )


def test_bark_switch_off_is_byte_identical_to_not_wiring_references(tmp_path) -> None:
    root = _project(tmp_path, material=_MATERIAL)
    project = _open(root, tmp_path)
    try:
        unwired = _RecordingProvider(_BARK_PAYLOAD)
        wired_off = _RecordingProvider(_BARK_PAYLOAD)
        before = _bark_service(project, unwired, wire_references=False).generate(
            speaker_ids=["npc_shen"], topic=_BARK_TOPIC, variants_per_speaker=1, max_chars=40
        )
        after = _bark_service(project, wired_off, wire_references=True).generate(
            speaker_ids=["npc_shen"], topic=_BARK_TOPIC, variants_per_speaker=1, max_chars=40
        )

        assert wired_off.systems == unwired.systems
        assert _FENCE not in wired_off.systems[0]
        assert [v.text for v in after.accepted] == [v.text for v in before.accepted]
        assert all(variant.metadata == {} for variant in after.accepted)
        assert after.inspiration_refs == [] and after.reference_report == []
    finally:
        project.close()


def test_bark_with_references_adds_a_separate_fenced_section(tmp_path) -> None:
    root = _project(tmp_path, material=_MATERIAL)
    project = _open(root, tmp_path)
    try:
        provider = _RecordingProvider(_BARK_PAYLOAD)
        result = _bark_service(project, provider, wire_references=True).generate(
            speaker_ids=["npc_shen"],
            topic=_BARK_TOPIC,
            variants_per_speaker=1,
            max_chars=40,
            use_references=True,
        )

        system = provider.systems[0]
        assert _FENCE in system
        assert _COPIED_SENTENCE in system
        voice_section = system.split("Stay within this voice card:", 1)[1].split(_FENCE, 1)[0]
        assert _COPIED_SENTENCE not in voice_section

        assert result.inspiration_refs
        variant = result.accepted[0]
        assert variant.metadata["inspiration_refs"] == ",".join(result.inspiration_refs)
        assert [row.source_title for row in result.reference_report] == ["盐道旧闻"]
        assert result.reference_report[0].leakage is not None
        assert result.reference_report[0].leakage.checked is True
    finally:
        project.close()


def test_bark_with_references_degrades_on_an_empty_library(tmp_path) -> None:
    root = _project(tmp_path, material=None)
    project = _open(root, tmp_path)
    try:
        provider = _RecordingProvider(_BARK_PAYLOAD)
        result = _bark_service(project, provider, wire_references=True).generate(
            speaker_ids=["npc_shen"],
            topic=_BARK_TOPIC,
            variants_per_speaker=1,
            max_chars=40,
            use_references=True,
        )

        assert _FENCE not in provider.systems[0]
        assert [v.text for v in result.accepted] == _BARK_PAYLOAD["variants"]
        assert result.inspiration_refs == [] and result.reference_report == []
        assert all(variant.metadata == {} for variant in result.accepted)
    finally:
        project.close()


def test_bark_leakage_is_recomputed_on_the_new_artifact(tmp_path) -> None:
    """种子：模型把素材原文抄进 bark 台词 → 重算命中；改写转述 → 不命中。BarkVariant 不是 content
    模型，draft_prose 查不到 bark 文本，必须走 extra_prose——这条顺带验证 extra_prose 真被喂了。"""
    root = _project(tmp_path, material=_MATERIAL)
    project = _open(root, tmp_path)
    try:
        leaked = _bark_service(
            project, _RecordingProvider({"variants": [_SAFE_COPIED_SENTENCE]}), wire_references=True
        ).generate(
            speaker_ids=["npc_shen"],
            topic=_BARK_TOPIC,
            variants_per_speaker=1,
            max_chars=60,
            use_references=True,
        )
        assert [v.text for v in leaked.accepted] == [_SAFE_COPIED_SENTENCE]  # 确认没被 lint 拒收
        leakage = leaked.reference_report[0].leakage
        assert leakage is not None and leakage.detected is True
        assert leakage.verbatim_spans

        clean = _bark_service(
            project, _RecordingProvider(_BARK_PAYLOAD), wire_references=True
        ).generate(
            speaker_ids=["npc_shen"],
            topic=_BARK_TOPIC,
            variants_per_speaker=1,
            max_chars=40,
            use_references=True,
        )
        clean_leakage = clean.reference_report[0].leakage
        assert clean_leakage is not None
        assert clean_leakage.checked is True and clean_leakage.detected is False
    finally:
        project.close()


# --- flavor（N4.3）----------------------------------------------------------------------------

_FLAVOR_PAYLOAD: dict[str, Any] = {
    "entries": [
        {"name": "盐引", "description": "证明缴过盐税的凭证。", "flavor": "边角还带着潮气。"}
    ]
}


def _flavor_service(
    project: ProjectContext, provider: _RecordingProvider, *, wire_references: bool
) -> FlavorBatchService:
    return FlavorBatchService(
        gateway=_gateway(provider, "flavor_batch"),
        bundle=project.bundle,
        reference_context_builder=(project.reference_context_builder if wire_references else None),
    )


def test_flavor_switch_off_is_byte_identical_to_not_wiring_references(tmp_path) -> None:
    root = _project(tmp_path, material=_MATERIAL)
    project = _open(root, tmp_path)
    try:
        unwired = _RecordingProvider(_FLAVOR_PAYLOAD)
        wired_off = _RecordingProvider(_FLAVOR_PAYLOAD)
        before = _flavor_service(project, unwired, wire_references=False).generate(
            category="item", names=["盐引"]
        )
        after = _flavor_service(project, wired_off, wire_references=True).generate(
            category="item", names=["盐引"]
        )

        assert wired_off.systems == unwired.systems
        assert _FENCE not in wired_off.systems[0]
        assert [e.model_dump(mode="json") for e in after.entities] == [
            e.model_dump(mode="json") for e in before.entities
        ]
        assert "inspiration_refs" not in after.entities[0].metadata
        assert after.inspiration_refs == [] and after.reference_report == []
    finally:
        project.close()


def test_flavor_with_references_adds_a_separate_fenced_section(tmp_path) -> None:
    root = _project(tmp_path, material=_MATERIAL)
    project = _open(root, tmp_path)
    try:
        provider = _RecordingProvider(_FLAVOR_PAYLOAD)
        result = _flavor_service(project, provider, wire_references=True).generate(
            category="item", names=["盐引"], use_references=True
        )

        system = provider.systems[0]
        assert _FENCE in system
        assert _COPIED_SENTENCE in system
        style_section = system.split("Style guide:", 1)[1].split(_FENCE, 1)[0]
        assert _COPIED_SENTENCE not in style_section

        assert result.inspiration_refs
        assert result.entities[0].metadata["inspiration_refs"] == ",".join(result.inspiration_refs)
        assert [row.source_title for row in result.reference_report] == ["盐道旧闻"]
        assert result.reference_report[0].leakage is not None
        assert result.reference_report[0].leakage.checked is True
    finally:
        project.close()


def test_flavor_with_references_degrades_on_an_empty_library(tmp_path) -> None:
    root = _project(tmp_path, material=None)
    project = _open(root, tmp_path)
    try:
        provider = _RecordingProvider(_FLAVOR_PAYLOAD)
        result = _flavor_service(project, provider, wire_references=True).generate(
            category="item", names=["盐引"], use_references=True
        )

        assert _FENCE not in provider.systems[0]
        assert result.entities[0].name == "盐引"
        assert result.inspiration_refs == [] and result.reference_report == []
        assert "inspiration_refs" not in result.entities[0].metadata
    finally:
        project.close()


def test_flavor_leakage_is_recomputed_on_the_new_artifact(tmp_path) -> None:
    """种子：模型把素材原文抄进 flavor 文本 → 重算命中；改写转述 → 不命中。flavor_text 住在
    metadata 里，draft_prose 查不到，必须走 extra_prose。"""
    root = _project(tmp_path, material=_MATERIAL)
    project = _open(root, tmp_path)
    try:
        leaked_payload = {
            "entries": [
                {
                    "name": "盐引",
                    "description": "证明缴过盐税的凭证。",
                    "flavor": _SAFE_COPIED_SENTENCE,
                }
            ]
        }
        leaked = _flavor_service(
            project, _RecordingProvider(leaked_payload), wire_references=True
        ).generate(category="item", names=["盐引"], use_references=True)
        assert leaked.entities[0].metadata["flavor_text"] == _SAFE_COPIED_SENTENCE  # 没被拒收
        leakage = leaked.reference_report[0].leakage
        assert leakage is not None and leakage.detected is True
        assert leakage.verbatim_spans

        clean = _flavor_service(
            project, _RecordingProvider(_FLAVOR_PAYLOAD), wire_references=True
        ).generate(category="item", names=["盐引"], use_references=True)
        clean_leakage = clean.reference_report[0].leakage
        assert clean_leakage is not None
        assert clean_leakage.checked is True and clean_leakage.detected is False
    finally:
        project.close()


# --- CLI -------------------------------------------------------------------------------------


def _run_cli(capsys, *argv: str) -> tuple[int, dict[str, Any]]:
    code = main(list(argv))
    out = capsys.readouterr().out.strip().splitlines()
    payload: dict[str, Any] = json.loads(out[-1]) if out else {}
    return code, payload


def test_cli_draft_use_references_flag_carries_the_switch(tmp_path, capsys) -> None:
    root = str(_project(tmp_path, material=_MATERIAL))

    code, plain = _run_cli(capsys, "draft", "--content-root", root, "--brief", _BRIEF)
    assert code == 0
    assert "inspiration_refs" not in plain and "reference_report" not in plain

    code, grounded = _run_cli(
        capsys, "draft", "--content-root", root, "--brief", _BRIEF, "--use-references"
    )
    assert code == 0
    assert grounded["inspiration_refs"]
    assert grounded["reference_report"][0]["source_title"] == "盐道旧闻"
    assert grounded["reference_report"][0]["leakage"]["checked"] is True
    assert grounded["quest"]["metadata"]["inspiration_refs"] == ",".join(
        grounded["inspiration_refs"]
    )


def test_cli_barks_use_references_flag_carries_the_switch(tmp_path, capsys) -> None:
    """对白树/flavor 没有 CLI 子命令（实查过 cli/main.py，只有 barks 有）；

    N4.3 的 CLI 面就这一条。"""
    root = str(_project(tmp_path, material=_MATERIAL))

    code, plain = _run_cli(
        capsys, "barks", "--content-root", root, "--speakers", "npc_shen", "--topic", _BARK_TOPIC
    )
    assert code == 0
    assert "inspiration_refs" not in plain and "reference_report" not in plain

    code, grounded = _run_cli(
        capsys,
        "barks",
        "--content-root",
        root,
        "--speakers",
        "npc_shen",
        "--topic",
        _BARK_TOPIC,
        "--use-references",
    )
    assert code == 0
    assert grounded["inspiration_refs"]
    assert grounded["reference_report"][0]["source_title"] == "盐道旧闻"
    assert grounded["reference_report"][0]["leakage"]["checked"] is True


# --- app action 层（CLI 之外的另一条调用面：REST / 前端走这里）--------------------------------


def test_draft_action_carries_the_switch_and_reports_an_empty_recall_honestly(tmp_path) -> None:
    """开着但一条素材都没召回 ≠ 没开：前者照样返回两个（空）字段，后者一个键都不多。"""
    root = _project(tmp_path, material=_MATERIAL)
    assert "inspiration_refs" not in run_draft_action(root, brief=_BRIEF, refine_rounds=0)

    grounded = run_draft_action(root, brief=_BRIEF, refine_rounds=0, use_references=True)
    assert grounded["inspiration_refs"]
    assert grounded["reference_report"][0]["source_title"] == "盐道旧闻"

    barren = _project(tmp_path / "barren", material=None)
    empty = run_draft_action(barren, brief=_BRIEF, refine_rounds=0, use_references=True)
    assert empty["inspiration_refs"] == [] and empty["reference_report"] == []


def test_character_action_reads_the_switch_off_the_brief(tmp_path) -> None:
    root = _project(tmp_path, material=_MATERIAL)
    brief = {"name": "白盐", "concept": _CHARACTER_CONCEPT}
    assert "inspiration_refs" not in run_character_action(root, brief=brief, refine_rounds=0)

    runtime = tmp_path / "character-review.sqlite"
    grounded = run_character_action(
        root,
        brief={**brief, "use_references": True},
        sqlite_path=str(runtime),
        refine_rounds=0,
    )
    assert grounded["inspiration_refs"]
    assert grounded["entity"]["metadata"]["inspiration_refs"] == ",".join(
        grounded["inspiration_refs"]
    )
    project = ProjectContext.open(root, sqlite_path=runtime)
    try:
        review = project.sqlite_store.get_review_item(grounded["review_item_id"])
        assert review is not None
        assert review["payload"]["inspiration_refs"] == grounded["inspiration_refs"]
        assert review["payload"]["reference_report"] == grounded["reference_report"]
    finally:
        project.close()


def test_dialogue_tree_action_carries_the_switch_and_reports_an_empty_recall_honestly(
    tmp_path,
) -> None:
    root = _project(tmp_path, material=_MATERIAL)
    plain = run_dialogue_tree_action(
        root, participant_ids=_DIALOGUE_PARTICIPANTS, brief=_BRIEF, refine_rounds=0
    )
    assert "inspiration_refs" not in plain

    grounded = run_dialogue_tree_action(
        root,
        participant_ids=_DIALOGUE_PARTICIPANTS,
        brief=_BRIEF,
        refine_rounds=0,
        use_references=True,
    )
    assert grounded["inspiration_refs"]
    assert grounded["reference_report"][0]["source_title"] == "盐道旧闻"

    barren = _project(tmp_path / "barren", material=None)
    empty = run_dialogue_tree_action(
        barren,
        participant_ids=_DIALOGUE_PARTICIPANTS,
        brief=_BRIEF,
        refine_rounds=0,
        use_references=True,
    )
    assert empty["inspiration_refs"] == [] and empty["reference_report"] == []


def test_barks_action_carries_the_switch_and_reports_an_empty_recall_honestly(tmp_path) -> None:
    root = _project(tmp_path, material=_MATERIAL)
    plain = run_barks_action(root, speaker_ids=["npc_shen"], topic=_BARK_TOPIC, refine_rounds=0)
    assert "inspiration_refs" not in plain

    grounded = run_barks_action(
        root, speaker_ids=["npc_shen"], topic=_BARK_TOPIC, refine_rounds=0, use_references=True
    )
    assert grounded["inspiration_refs"]
    assert grounded["reference_report"][0]["source_title"] == "盐道旧闻"

    barren = _project(tmp_path / "barren", material=None)
    empty = run_barks_action(
        barren, speaker_ids=["npc_shen"], topic=_BARK_TOPIC, refine_rounds=0, use_references=True
    )
    assert empty["inspiration_refs"] == [] and empty["reference_report"] == []


def test_flavor_action_carries_the_switch_and_reports_an_empty_recall_honestly(tmp_path) -> None:
    root = _project(tmp_path, material=_MATERIAL)
    plain = run_flavor_action(root, category="item", names=["盐引"], refine_rounds=0)
    assert "inspiration_refs" not in plain

    grounded = run_flavor_action(
        root, category="item", names=["盐引"], refine_rounds=0, use_references=True
    )
    assert grounded["inspiration_refs"]
    assert grounded["reference_report"][0]["source_title"] == "盐道旧闻"

    barren = _project(tmp_path / "barren", material=None)
    empty = run_flavor_action(
        barren, category="item", names=["盐引"], refine_rounds=0, use_references=True
    )
    assert empty["inspiration_refs"] == [] and empty["reference_report"] == []
