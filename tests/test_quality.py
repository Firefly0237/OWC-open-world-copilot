from owcopilot.core.state import ValidationIssue
from owcopilot.evaluation.quality import evaluate_quest_quality


def test_quality_report_passes_clear_complete_quest():
    report = evaluate_quest_quality(
        {
            "title": "The Northern Supply Run",
            "giver_npc": "Aldric",
            "location": "Northwatch",
            "objective": "Escort Aldric's caravan safely through the northern pass",
            "reward": "75 gold",
            "prerequisites": [],
        }
    )

    assert report.passed is True
    assert report.score >= 0.7
    assert report.warnings == []


def test_quality_report_flags_vague_or_inconsistent_quest():
    report = evaluate_quest_quality(
        {
            "title": "bad quest",
            "giver_npc": "",
            "location": "Northwatch",
            "objective": "stuff",
            "reward": "",
            "prerequisites": ["bad quest"],
        },
        [ValidationIssue(code="UNKNOWN_NPC", message="bad")],
    )

    assert report.passed is False
    assert "consistency errors remain" in report.warnings
    assert report.checks["required_fields"] < 1.0
