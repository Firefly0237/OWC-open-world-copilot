"""critic→refine loop extended to barks and flavor text — proving "add a content kind = write a
critic prompt". Offline / $0: the offline doubles drive both halves of each loop.
"""

from __future__ import annotations

import json

from owcopilot.assist.barks import BarkBatchService
from owcopilot.assist.critic import BARK_CRITIQUE_MARKER, BarkCritic, FlavorCritic
from owcopilot.assist.flavor import FlavorBatchService, OfflineFlavorProvider
from owcopilot.assist.offline import OfflineBarksProvider, _offline_quality_critique
from owcopilot.content.models import ContentBundle, Entity, EntityType
from owcopilot.llm.cache import NoOpCache
from owcopilot.llm.gateway import LLMGateway
from owcopilot.llm.router import StaticRouter


def _bundle() -> ContentBundle:
    return ContentBundle(
        entities={
            "npc_lin": Entity(
                id="npc_lin", name="林潮生", type=EntityType.NPC, description="领航员。"
            ),
        }
    )


def _gateway(provider: object, task: str) -> LLMGateway:
    return LLMGateway(
        providers={"cheap": provider},
        router=StaticRouter(mapping={task: "cheap"}),
        cache=NoOpCache(),
    )


def test_bark_refine_loop_runs_and_converges() -> None:
    gateway = _gateway(OfflineBarksProvider(), "barks_batch")
    result = BarkBatchService(
        gateway=gateway,
        bundle=_bundle(),
        critic=BarkCritic(gateway=gateway),
        max_refine_rounds=2,
    ).generate(
        speaker_ids=["npc_lin"],
        topic="海上的传闻",
        variants_per_speaker=2,
        max_chars=40,
        allowed_entity_ids={"npc_lin"},
    )
    # clean offline variants → critic passes at round 0; the loop ran and recorded it
    assert result.accepted
    assert result.accepted[0].refine_rounds == 1
    assert result.accepted[0].auto_review_incomplete is False


def test_flavor_refine_loop_runs_and_converges() -> None:
    gateway = _gateway(OfflineFlavorProvider(), "flavor_batch")
    result = FlavorBatchService(
        gateway=gateway,
        bundle=_bundle(),
        critic=FlavorCritic(gateway=gateway),
        max_refine_rounds=2,
    ).generate(category="item", names=["盐风护符"], theme="海港")
    assert result.accepted
    assert result.refine_rounds == 1
    assert result.auto_review_incomplete is False


class _LongThenShortBarkProvider:
    """First draft has an over-length variant (a lint blocker); the refine reply fixes it. The
    critic passes only once lint is clean, so a converged pass proves the loop drove a real fix."""

    def complete(self, *, system: str, user: str, model: str) -> tuple[str, int, int]:
        if BARK_CRITIQUE_MARKER in system:
            text = _offline_quality_critique(user)
        elif "[REFINE]" in user:
            text = json.dumps({"variants": ["短句一", "短句二"]})
        else:
            too_long = "这是一条远远超过长度上限的冗长台词用于触发长度lint"
            text = json.dumps({"variants": [too_long, "短句二"]})
        return text, 10, 10


def test_bark_refine_loop_fixes_a_lint_blocker() -> None:
    gateway = _gateway(_LongThenShortBarkProvider(), "barks_batch")
    result = BarkBatchService(
        gateway=gateway,
        bundle=_bundle(),
        critic=BarkCritic(gateway=gateway),
        max_refine_rounds=2,
    ).generate(
        speaker_ids=["npc_lin"],
        topic="海上的传闻",
        variants_per_speaker=2,
        max_chars=12,
        allowed_entity_ids={"npc_lin"},
    )
    # the over-length first draft was revised away; both final variants pass lint and land accepted
    assert len(result.accepted) == 2
    assert all(len(v.text) <= 12 for v in result.accepted)
    assert result.accepted[0].refine_rounds == 2  # round 0 revise → round 1 pass
    assert result.accepted[0].auto_review_incomplete is False


class _JunkBarkCriticProvider:
    """Valid variants for generation, unparsable text for every critique — proves no fake pass."""

    def complete(self, *, system: str, user: str, model: str) -> tuple[str, int, int]:
        if BARK_CRITIQUE_MARKER in system:
            return "looks fine, ship it", 10, 10
        return json.dumps({"variants": ["短句一", "短句二"]}), 10, 10


def test_bark_unparsable_critic_flags_for_human_not_silent_pass() -> None:
    gateway = _gateway(_JunkBarkCriticProvider(), "barks_batch")
    result = BarkBatchService(
        gateway=gateway,
        bundle=_bundle(),
        critic=BarkCritic(gateway=gateway),
        max_refine_rounds=2,
    ).generate(
        speaker_ids=["npc_lin"],
        topic="海上的传闻",
        variants_per_speaker=2,
        max_chars=40,
        allowed_entity_ids={"npc_lin"},
    )
    assert result.accepted
    assert result.accepted[0].auto_review_incomplete is True


def test_without_critic_barks_and_flavor_are_single_shot() -> None:
    bark = BarkBatchService(
        gateway=_gateway(OfflineBarksProvider(), "barks_batch"),
        bundle=_bundle(),
    ).generate(
        speaker_ids=["npc_lin"],
        topic="海上的传闻",
        variants_per_speaker=2,
        max_chars=40,
        allowed_entity_ids={"npc_lin"},
    )
    assert bark.accepted
    assert all(v.refine_rounds == 0 for v in bark.accepted)

    flavor = FlavorBatchService(
        gateway=_gateway(OfflineFlavorProvider(), "flavor_batch"),
        bundle=_bundle(),
    ).generate(category="item", names=["盐风护符"])
    assert flavor.refine_rounds == 0
    assert flavor.accepted
