"""Tests for IN-1 (FAIL-1 rework): vocabulary-constraint injection into worldgen stage suffixes.

The premise stage already surfaced term_count; the 4 downstream stage suffixes
(_factions_suffix / _regions_suffix / _cast_suffix / _quests_suffix) previously had only the
grounding block and no vocabulary constraints. This injects the project's existing-canon Term
constraints (forbidden / prefer-canonical) into each, so a generated faction/region/cast/quest
stays consistent with established terminology.

Covers:
- Each of the 4 suffixes, given terms, emits the [vocabulary-constraints] block (MUST NOT / PREFER).
- No terms -> no block (byte-identical to the pre-IN-1 prompt; the grounding block is untouched).
- The block is placed BEFORE the grounding block (constraints precede "already established").
- End-to-end: terms reach the real faction/region/cast/quest prompts via WorldSeedService.generate.
"""

from __future__ import annotations

from owcopilot.content.models import ContentBundle, Term
from owcopilot.inspiration import ReferenceContextBuilder
from owcopilot.llm.cache import HashingEmbedder, NoOpCache
from owcopilot.llm.gateway import LLMGateway
from owcopilot.llm.router import StaticRouter
from owcopilot.retrieval.bm25 import BM25Retriever
from owcopilot.retrieval.context_pack import ContextPackBuilder
from owcopilot.storage import SQLiteStore
from owcopilot.worldgen.models import WorldSeedBrief
from owcopilot.worldgen.offline import OfflineWorldSeedProvider
from owcopilot.worldgen.service import (
    WorldSeedService,
    _cast_suffix,
    _factions_suffix,
    _quests_suffix,
    _regions_suffix,
    _vocab_block,
)
from owcopilot.worldgen.stages import (
    CAST,
    FACTIONS,
    QUESTS,
    REGIONS,
    stage_from_system,
)

_SUFFIXES = [_factions_suffix, _regions_suffix, _cast_suffix, _quests_suffix]


def _terms() -> list[Term]:
    return [
        Term(
            id="term_aether",
            canonical="以太炉",
            aliases=["魔力引擎", "灵能核心"],
            forbidden=["蒸汽机", "核反应堆"],
            description="本世界的能源核心。",
        ),
        Term(
            id="term_warden",
            canonical="守夜人",
            aliases=["巡逻队"],
            forbidden=["警察"],
            description="边境秩序的执行者。",
        ),
    ]


def _brief() -> WorldSeedBrief:
    return WorldSeedBrief(
        idea="霜冷山脉边境的能源走私",
        use_references=False,
        use_project_facts=False,
        faction_count=2,
        region_count=2,
        npc_count=2,
        quest_count=2,
        term_count=0,
    )


# --------------------------------------------------------------------------- _vocab_block unit

def test_vocab_block_empty_for_no_terms() -> None:
    assert _vocab_block(None) == ""
    assert _vocab_block([]) == ""


def test_vocab_block_renders_constraints() -> None:
    block = _vocab_block(_terms())
    assert "[vocabulary-constraints]" in block
    assert "MUST NOT use:" in block
    assert "蒸汽机" in block
    assert "核反应堆" in block
    assert "警察" in block
    assert "PREFER:" in block
    assert "以太炉" in block
    # Trailing newline so it concatenates cleanly before the grounding block.
    assert block.endswith("\n")


# --------------------------------------------------------------------------- per-suffix injection

def test_each_suffix_injects_term_block_when_terms_present() -> None:
    """[硬] Each of the 4 suffixes emits the vocabulary block when terms are supplied."""
    brief = _brief()
    world_lines = ["faction:fac_iron 铁律会"]
    terms = _terms()
    for suffix_fn in _SUFFIXES:
        out = suffix_fn(brief, world_lines, terms)
        assert "[vocabulary-constraints]" in out, f"{suffix_fn.__name__} missing vocab block"
        assert "MUST NOT use:" in out
        assert "蒸汽机" in out
        assert "以太炉" in out


def test_each_suffix_no_block_when_no_terms() -> None:
    """[硬] No terms -> no vocabulary block (grounding block still present, unchanged)."""
    brief = _brief()
    world_lines = ["faction:fac_iron 铁律会"]
    for suffix_fn in _SUFFIXES:
        out = suffix_fn(brief, world_lines, None)
        assert "[vocabulary-constraints]" not in out, f"{suffix_fn.__name__} leaked vocab block"
        # The grounding block is untouched.
        assert "Already established" in out


def test_suffix_byte_identical_to_pre_in1_when_no_terms() -> None:
    """No terms -> suffix is byte-identical whether terms=None or terms=[] (no behavior change)."""
    brief = _brief()
    world_lines = ["faction:fac_iron 铁律会"]
    for suffix_fn in _SUFFIXES:
        assert suffix_fn(brief, world_lines, None) == suffix_fn(brief, world_lines, [])


def test_vocab_block_precedes_grounding_block() -> None:
    """Constraints come BEFORE 'Already established' so the model reads them first."""
    brief = _brief()
    world_lines = ["faction:fac_iron 铁律会"]
    terms = _terms()
    for suffix_fn in _SUFFIXES:
        out = suffix_fn(brief, world_lines, terms)
        assert out.index("[vocabulary-constraints]") < out.index("Already established")


# --------------------------------------------------------------------------- end-to-end (offline)

class _CapturingProvider:
    """Wraps the offline double; records the system prompt of each stage by stage name."""

    def __init__(self) -> None:
        self.inner = OfflineWorldSeedProvider()
        self.systems_by_stage: dict[str, str] = {}

    def complete(self, *, system: str, user: str, model: str):
        stage = stage_from_system(system)
        # keep the first system prompt seen per stage
        self.systems_by_stage.setdefault(stage, system)
        return self.inner.complete(system=system, user=user, model=model)


def _service_with_bundle(provider, bundle: ContentBundle) -> tuple[WorldSeedService, SQLiteStore]:
    store = SQLiteStore()
    gateway = LLMGateway(
        providers={"cheap": provider},
        router=StaticRouter(mapping={"world_seed": "cheap"}),
        cache=NoOpCache(),
    )
    service = WorldSeedService(
        gateway=gateway,
        bundle=bundle,
        project_context_builder=ContextPackBuilder(bm25=BM25Retriever(store)),
        reference_context_builder=ReferenceContextBuilder(store, embedder=HashingEmbedder()),
    )
    return service, store


def test_end_to_end_terms_reach_each_stage_prompt() -> None:
    """[硬] With canon terms in the bundle, the vocabulary block appears in the real
    faction/region/cast/quest prompts the gateway actually sends."""
    terms = _terms()
    bundle = ContentBundle(terms={t.id: t for t in terms})
    provider = _CapturingProvider()
    service, store = _service_with_bundle(provider, bundle)
    try:
        service.generate(_brief())
    finally:
        store.close()

    for stage_name in (FACTIONS, REGIONS, CAST, QUESTS):
        system = provider.systems_by_stage.get(stage_name)
        assert system is not None, f"stage {stage_name} was never generated"
        assert "[vocabulary-constraints]" in system, f"{stage_name} prompt missing vocab block"
        assert "蒸汽机" in system  # a forbidden word
        assert "以太炉" in system  # a canonical preferred form


def test_end_to_end_no_terms_no_block_in_prompts() -> None:
    """Empty bundle.terms -> no vocabulary block leaks into any stage prompt."""
    provider = _CapturingProvider()
    service, store = _service_with_bundle(provider, ContentBundle())
    try:
        service.generate(_brief())
    finally:
        store.close()

    for stage_name in (FACTIONS, REGIONS, CAST, QUESTS):
        system = provider.systems_by_stage.get(stage_name)
        assert system is not None
        assert "[vocabulary-constraints]" not in system
