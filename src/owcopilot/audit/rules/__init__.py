# 审计规则实现集：本项目 ground-truth oracle 的规则本体（按主题分文件装配）。
"""Audit rule implementations."""

from .dialogue_rules import (
    DialogueChoiceConditionRule,
    DialogueSelfReportInconsistencyRule,
    DialogueTreeBrokenLinkRule,
    DialogueTreeMissingTextKeyRule,
    DialogueTreeUnknownSpeakerRule,
    DialogueTreeUnreachableNodeRule,
    DialogueUngatedSiblingChoiceRule,
)
from .graph_rules import (
    DuplicateRelationRule,
    FactionConflictRule,
    MissingRelationEndpointRule,
    PrerequisiteCycleRule,
    RelationshipConflictRule,
)
from .import_rules import detect_import_conflicts
from .logic_rules import QuestLogicRule
from .lore_rules import (
    CharacterStateContradictionRule,
    EventResultReferencedTooEarlyRule,
    TimelineViolationRule,
)
from .pipeline_rules import (
    MissingLocalizationKeyRule,
    PlaceholderMismatchRule,
    QuestMissingObjectiveRule,
    TermInconsistentRule,
    TextTooLongForUIRule,
)
from .quest_reachability import QuestGlobalReachabilityRule
from .reference_rules import (
    DeprecatedEntityReferenceRule,
    MissingDialogueReferenceRule,
    MissingEntityReferenceRule,
    MissingPrerequisiteRule,
)
from .region_rules import (
    MissingRegionReferenceRule,
    POILevelOutOfBoundsRule,
    POIWithoutNarrativePurposeRule,
    RegionBannedContentRule,
    RegionLevelBoundsRule,
)
from .security_rules import PromptInjectionRule
from .temporal_rules import FlagTemporalInvariantRule
from .trust_rules import UnreviewedAIContentRule

__all__ = [
    "CharacterStateContradictionRule",
    "DialogueChoiceConditionRule",
    "DialogueSelfReportInconsistencyRule",
    "DialogueUngatedSiblingChoiceRule",
    "DialogueTreeBrokenLinkRule",
    "DialogueTreeMissingTextKeyRule",
    "DialogueTreeUnknownSpeakerRule",
    "DialogueTreeUnreachableNodeRule",
    "DeprecatedEntityReferenceRule",
    "DuplicateRelationRule",
    "EventResultReferencedTooEarlyRule",
    "FactionConflictRule",
    "FlagTemporalInvariantRule",
    "MissingDialogueReferenceRule",
    "MissingEntityReferenceRule",
    "MissingLocalizationKeyRule",
    "MissingPrerequisiteRule",
    "MissingRegionReferenceRule",
    "MissingRelationEndpointRule",
    "POILevelOutOfBoundsRule",
    "POIWithoutNarrativePurposeRule",
    "PlaceholderMismatchRule",
    "PrerequisiteCycleRule",
    "PromptInjectionRule",
    "QuestGlobalReachabilityRule",
    "QuestLogicRule",
    "QuestMissingObjectiveRule",
    "RegionBannedContentRule",
    "RegionLevelBoundsRule",
    "RelationshipConflictRule",
    "TermInconsistentRule",
    "TextTooLongForUIRule",
    "TimelineViolationRule",
    "UnreviewedAIContentRule",
    "detect_import_conflicts",
]
