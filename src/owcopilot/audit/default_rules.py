"""Default v2 deterministic rule catalog."""

from __future__ import annotations

from .registry import RuleRegistry
from .rules import (
    CharacterStateContradictionRule,
    DeprecatedEntityReferenceRule,
    DialogueTreeBrokenLinkRule,
    DialogueTreeUnknownSpeakerRule,
    DialogueTreeUnreachableNodeRule,
    DuplicateRelationRule,
    EventResultReferencedTooEarlyRule,
    FactionConflictRule,
    MissingDialogueReferenceRule,
    MissingEntityReferenceRule,
    MissingLocalizationKeyRule,
    MissingPrerequisiteRule,
    MissingRelationEndpointRule,
    PlaceholderMismatchRule,
    POILevelOutOfBoundsRule,
    POIWithoutNarrativePurposeRule,
    PrerequisiteCycleRule,
    PromptInjectionRule,
    QuestMissingObjectiveRule,
    RegionBannedContentRule,
    RegionLevelBoundsRule,
    RelationshipConflictRule,
    TermInconsistentRule,
    TextTooLongForUIRule,
    TimelineViolationRule,
    UnreviewedAIContentRule,
)


def build_default_rule_registry() -> RuleRegistry:
    return RuleRegistry(
        [
            MissingEntityReferenceRule(),
            DeprecatedEntityReferenceRule(),
            MissingPrerequisiteRule(),
            MissingDialogueReferenceRule(),
            MissingLocalizationKeyRule(),
            QuestMissingObjectiveRule(),
            TextTooLongForUIRule(),
            PlaceholderMismatchRule(),
            TermInconsistentRule(),
            MissingRelationEndpointRule(),
            DuplicateRelationRule(),
            RelationshipConflictRule(),
            PrerequisiteCycleRule(),
            FactionConflictRule(),
            TimelineViolationRule(),
            EventResultReferencedTooEarlyRule(),
            CharacterStateContradictionRule(),
            RegionLevelBoundsRule(),
            POILevelOutOfBoundsRule(),
            POIWithoutNarrativePurposeRule(),
            RegionBannedContentRule(),
            DialogueTreeBrokenLinkRule(),
            DialogueTreeUnknownSpeakerRule(),
            DialogueTreeUnreachableNodeRule(),
            PromptInjectionRule(),
            UnreviewedAIContentRule(),
        ]
    )
