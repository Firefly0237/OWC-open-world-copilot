"""Shared stage contract for the staged world-seed chain.

The single big ``world_seed`` call became a *grounded chain*: premise → factions → regions →
cast → quests, each call grounded on the prior stages' output. The stage marker below is the
contract between the production service (``service.py``, which stamps it onto every stage's
system prompt) and the offline double (``offline.py``, which dispatches on it). Centralising the
marker here means the double exercises the exact multi-call shape production uses — an interface
drift between them cannot hide, which is the whole point of keeping the double on the same contract.

Real models simply ignore the marker line (it reads as an inert tag), exactly like the critic
sentinel in ``assist/critic.py``; only the offline double keys on it.
"""

from __future__ import annotations

import re

PREMISE = "premise"
FACTIONS = "factions"
REGIONS = "regions"
CAST = "cast"
QUESTS = "quests"
# The optional world-level reviewer that drives the quests-stage refine loop. It is modelled as
# one more "stage" so the offline double has a single dispatch surface.
QUEST_CRITIQUE = "quest_critique"

# The grounded generation chain, in order. QUEST_CRITIQUE is deliberately excluded: it is a
# review pass over QUESTS, not a content stage of its own.
ORDER = (PREMISE, FACTIONS, REGIONS, CAST, QUESTS)

# --- world EXPANSION chain ---------------------------------------------------------------------
# Expansion grows MORE content on an EXISTING world (a focus region/faction/quest) instead of
# cold-starting a new one. It is the same staged, grounded, stage-marked discipline as creation —
# the only difference is the grounding is the *existing canon* (ids the new content must reference),
# not the world the chain just built. The markers below keep the offline double on the exact same
# multi-call contract as production (see worldgen/offline_expand.py).
EXPAND_FOCUS = "expand_focus"
EXPAND_POIS = "expand_pois"
EXPAND_CAST = "expand_cast"
EXPAND_QUESTS = "expand_quests"

# The expansion chain, in order. The quests-stage critique reuses QUEST_CRITIQUE (the critic is
# generic over a quest batch + grounding context, creation or expansion alike).
EXPAND_ORDER = (EXPAND_FOCUS, EXPAND_POIS, EXPAND_CAST, EXPAND_QUESTS)

_MARKER = "[WORLD_SEED_STAGE:{stage}]"
_MARKER_RE = re.compile(r"\[WORLD_SEED_STAGE:([a-z_]+)\]")


def stage_marker(stage: str) -> str:
    """The marker line stamped onto a stage's system prompt."""
    return _MARKER.format(stage=stage)


def stage_from_system(system: str) -> str | None:
    """Extract the stage name from a system prompt, or None if unmarked."""
    match = _MARKER_RE.search(system)
    return match.group(1) if match else None
