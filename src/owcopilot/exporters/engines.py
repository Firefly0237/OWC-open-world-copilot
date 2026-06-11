"""Engine-specific export formats.

`unreal` adds a DataTable-compatible quests CSV (column names mirror the FQuestTableRow struct
used by the legacy Remote Control adapter, so both landing paths agree on the schema) plus a
localization CSV. `unity` adds one ScriptableObject-friendly JSON per quest plus an index.
The generic `content_bundle.json` is always written alongside, so engine files are additive.
"""

from __future__ import annotations

import csv
import io
import json
import re
from pathlib import Path

from ..content.models import ContentBundle, Quest

# Keep aligned with the UE-side row struct (see adapters/unreal: FQuestTableRow).
_UE_QUEST_COLUMNS = [
    "Name",
    "Title",
    "GiverNPC",
    "Location",
    "Objective",
    "Prerequisites",
    "TimelineOrder",
    "LocalizationKeys",
    "Rewards",
]


def ue_row_name(quest: Quest) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", quest.id.strip().lower()).strip("_") or "untitled"
    return f"Quest_{slug}"


def write_ue_quests_csv(bundle: ContentBundle, path: Path) -> None:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(_UE_QUEST_COLUMNS)
    for quest in sorted(bundle.quests.values(), key=lambda item: item.id):
        writer.writerow(
            [
                ue_row_name(quest),
                quest.title,
                quest.giver_npc or "",
                quest.location or "",
                quest.objective,
                json.dumps(quest.prerequisites, ensure_ascii=False),
                "" if quest.timeline_order is None else str(quest.timeline_order),
                json.dumps(quest.localization_keys, ensure_ascii=False),
                json.dumps(
                    [reward.model_dump(mode="json", exclude_none=True) for reward in quest.rewards],
                    ensure_ascii=False,
                ),
            ]
        )
    path.write_text(buffer.getvalue(), encoding="utf-8")


def write_localization_csv(bundle: ContentBundle, path: Path) -> None:
    """Key/Locale/Text rows from localized texts plus localized dialogue lines."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["Key", "Locale", "Text", "UIMaxLen"])
    rows: list[tuple[str, str, str, str]] = []
    for text in bundle.localized_texts.values():
        rows.append(
            (
                text.text_key,
                text.locale,
                text.text,
                "" if text.ui_max_len is None else str(text.ui_max_len),
            )
        )
    for dialogue in bundle.dialogues.values():
        if dialogue.text is None or not dialogue.locale:
            continue
        rows.append(
            (
                dialogue.text_key,
                dialogue.locale,
                dialogue.text,
                "" if dialogue.ui_max_len is None else str(dialogue.ui_max_len),
            )
        )
    for row in sorted(rows):
        writer.writerow(row)
    path.write_text(buffer.getvalue(), encoding="utf-8")


def write_unity_quests(bundle: ContentBundle, quests_dir: Path) -> list[str]:
    """One ScriptableObject-friendly JSON per quest (camelCase keys for JsonUtility),
    plus an index.json. Returns relative paths of the written quest files."""
    quests_dir.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for quest in sorted(bundle.quests.values(), key=lambda item: item.id):
        payload = {
            "id": quest.id,
            "title": quest.title,
            "giverNpc": quest.giver_npc or "",
            "location": quest.location or "",
            "objective": quest.objective,
            "prerequisites": list(quest.prerequisites),
            "timelineOrder": quest.timeline_order,
            "localizationKeys": list(quest.localization_keys),
            "rewards": [
                {"kind": reward.kind, "value": reward.value, "amount": reward.amount}
                for reward in quest.rewards
            ],
            "origin": quest.origin.value,
            "reviewStatus": quest.review_status.value,
        }
        file_path = quests_dir / f"{quest.id}.json"
        file_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        written.append(f"quests/{quest.id}.json")
    index_path = quests_dir / "index.json"
    index_path.write_text(
        json.dumps(
            {"quests": [Path(item).name for item in written]},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return written
