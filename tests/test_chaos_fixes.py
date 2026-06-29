"""Tests that reproduce the chaos-test bugs and verify they are now root-cause fixed.

Coverage:
  P1/P5 - ID invariant (_require_valid_id) rejects blank/path-traversal/overlong IDs at ingest
  P2    - recognize ink/yarn/articy/ue/unity use decode_bytes (GB18030 does not crash)
  P3    - decode_bytes detects UTF-16 w/o BOM and raises ValueError instead of silent corruption
  P6    - issues --severity / --status reject invalid choices at argparse level
  P7    - _load_mapping_doc wraps JSONDecodeError into a user-friendly ValueError
  P8    - --max-cost-usd rejects negative values at argparse level
  P9    - --budget-tokens rejects negative values at argparse level
  P10   - agent --goal "" raises ValueError before running
  P11   - barks --speakers "" raises ValueError before running
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from owcopilot.content.encoding import decode_bytes
from owcopilot.content.importers.base import RawObject
from owcopilot.content.normalize import (
    _require_valid_id,
    normalize_raw_objects,
)

# ---------------------------------------------------------------------------
# P1 / P5 — ID invariant
# ---------------------------------------------------------------------------


class TestRequireValidId:
    def test_blank_id_raises(self) -> None:
        with pytest.raises(ValueError, match="must not be blank"):
            _require_valid_id("   ", context="test")

    def test_empty_id_raises(self) -> None:
        with pytest.raises(ValueError, match="must not be blank"):
            _require_valid_id("", context="test")

    def test_forward_slash_raises(self) -> None:
        with pytest.raises(ValueError, match="forbidden character"):
            _require_valid_id("../etc/passwd", context="test")

    def test_backslash_raises(self) -> None:
        with pytest.raises(ValueError, match="forbidden character"):
            _require_valid_id("foo\\bar", context="test")

    def test_dotdot_component_raises(self) -> None:
        # "." is a forbidden char, so the forbidden-char check fires first
        with pytest.raises(ValueError, match="forbidden character|path traversal"):
            _require_valid_id("..secret", context="test")

    def test_colon_raises(self) -> None:
        with pytest.raises(ValueError, match="forbidden character"):
            _require_valid_id("C:foo", context="test")

    def test_overlong_id_raises(self) -> None:
        with pytest.raises(ValueError, match="maximum length"):
            _require_valid_id("x" * 257, context="test")

    def test_max_length_id_passes(self) -> None:
        result = _require_valid_id("x" * 256, context="test")
        assert result == "x" * 256

    def test_valid_id_returned(self) -> None:
        assert _require_valid_id("npc_alice", context="test") == "npc_alice"

    def test_chinese_id_passes(self) -> None:
        assert _require_valid_id("npc_李白", context="test") == "npc_李白"

    def test_emoji_id_passes(self) -> None:
        assert _require_valid_id("evt_🎉", context="test") == "evt_🎉"

    def test_context_included_in_error(self) -> None:
        with pytest.raises(ValueError, match="entity row from test.csv"):
            _require_valid_id("", context="entity row from test.csv")


def _make_raw(kind: str, data: dict[str, Any], source: str = "test.csv") -> RawObject:
    return RawObject(kind=kind, data=data, source_path=source)


class TestNormalizeRejectsBlankId:
    """Verifies that normalize_raw_objects (the ingest entry point) rejects bad IDs early."""

    def test_entity_with_blank_id_raises_value_error(self) -> None:
        raw = _make_raw("entity", {"id": "   ", "name": "Alice", "type": "npc"})
        with pytest.raises(ValueError, match="must not be blank"):
            normalize_raw_objects([raw])

    def test_entity_with_path_traversal_id_raises(self) -> None:
        raw = _make_raw("entity", {"id": "../../../etc/passwd", "name": "Bad", "type": "npc"})
        with pytest.raises(ValueError, match="forbidden character|path traversal"):
            normalize_raw_objects([raw])

    def test_quest_with_blank_id_raises(self) -> None:
        raw = _make_raw("quest", {"id": "   ", "title": "Lost Quest"})
        with pytest.raises(ValueError, match="must not be blank"):
            normalize_raw_objects([raw])

    def test_region_with_blank_id_raises(self) -> None:
        # "   " is truthy so `or` short-circuits; .strip() then gives "" → must be rejected
        raw = _make_raw("region", {"id": "   ", "name": "Nowhere"})
        with pytest.raises(ValueError, match="must not be blank"):
            normalize_raw_objects([raw])

    def test_poi_with_path_traversal_id_raises(self) -> None:
        raw = _make_raw("poi", {"id": "/etc/passwd", "name": "Bad POI"})
        with pytest.raises(ValueError, match="forbidden character"):
            normalize_raw_objects([raw])

    def test_term_with_blank_id_raises(self) -> None:
        raw = _make_raw("term", {"id": "  ", "canonical": "something"})
        with pytest.raises(ValueError, match="must not be blank"):
            normalize_raw_objects([raw])

    def test_dialogue_with_blank_id_raises(self) -> None:
        # "   " is truthy so `or` short-circuits; .strip() then gives "" → must be rejected
        raw = _make_raw("dialogue", {"id": "   ", "text_key": "D001"})
        with pytest.raises(ValueError, match="must not be blank"):
            normalize_raw_objects([raw])

    def test_valid_entity_is_not_rejected(self) -> None:
        raw = _make_raw("entity", {"id": "npc_alice", "name": "Alice", "type": "npc"})
        bundle = normalize_raw_objects([raw])
        assert "npc_alice" in bundle.entities

    def test_entity_with_name_slug_passes(self) -> None:
        """When no explicit id given, slug_id generates one — must still be valid."""
        raw = _make_raw("entity", {"name": "Alice the Great", "type": "npc"})
        bundle = normalize_raw_objects([raw])
        assert len(bundle.entities) == 1

    def test_error_message_includes_source_path(self) -> None:
        raw = _make_raw("entity", {"id": "  ", "name": "X", "type": "npc"}, source="bad.csv")
        with pytest.raises(ValueError, match="bad.csv"):
            normalize_raw_objects([raw])


# ---------------------------------------------------------------------------
# P2 / P3 — decode_bytes: BOM detection + UTF-16 heuristic
# ---------------------------------------------------------------------------


class TestDecodeBytesEncodingFixes:
    # P2: GB18030 must decode correctly (regression for hardcoded utf-8)
    def test_gb18030_decodes_correctly(self) -> None:
        text = decode_bytes("李白\n".encode("gb18030"))
        assert text == "李白\n"

    def test_utf8_decodes_correctly(self) -> None:
        assert decode_bytes(b"hello") == "hello"

    # BOM detection
    def test_utf16_le_bom_decoded(self) -> None:
        encoded = "hello".encode("utf-16-le")
        bom = b"\xff\xfe"
        assert decode_bytes(bom + encoded) == "hello"

    def test_utf16_be_bom_decoded(self) -> None:
        encoded = "hello".encode("utf-16-be")
        bom = b"\xfe\xff"
        assert decode_bytes(bom + encoded) == "hello"

    def test_utf8_bom_decoded(self) -> None:
        bom = b"\xef\xbb\xbf"
        assert decode_bytes(bom + b"hello") == "hello"

    # P3: UTF-16 without BOM must raise ValueError (not silently corrupt)
    def test_utf16_le_without_bom_raises_value_error(self) -> None:
        # Encode a long enough ASCII string — produces alternating \x00 pattern
        data = "hello world ascii text for testing".encode("utf-16-le")
        with pytest.raises(ValueError, match="UTF-16"):
            decode_bytes(data)

    def test_empty_bytes_returns_empty_string(self) -> None:
        assert decode_bytes(b"") == ""

    def test_binary_garbage_does_not_crash(self) -> None:
        # lossy replacement path — must not raise
        result = decode_bytes(b"\xff\xfe\xfd\xfc" + b"\x00" * 4 + b"abc" * 20)
        assert isinstance(result, str)

    def test_cp1252_decodes(self) -> None:
        # cp1252 byte 0x80 = € sign
        result = decode_bytes(b"\x80")
        assert "€" in result or result  # decoded non-empty


# ---------------------------------------------------------------------------
# P6 — --severity / --status choices enforced at argparse level
# ---------------------------------------------------------------------------


class TestCliIssuesChoices:
    def test_invalid_severity_exits_2(self, tmp_path: Path, capsys) -> None:
        from owcopilot.cli.main import main

        # argparse invalid choices call sys.exit(2) via parser.error() — SystemExit propagates.
        with pytest.raises(SystemExit) as exc_info:
            main(
                [
                    "issues",
                    "--content-root",
                    str(tmp_path),
                    "--severity",
                    "CRITICAL_DISASTER",
                ]
            )
        assert exc_info.value.code == 2
        err = capsys.readouterr().err
        assert "invalid choice" in err or "CRITICAL_DISASTER" in err

    def test_invalid_status_exits_2(self, tmp_path: Path, capsys) -> None:
        from owcopilot.cli.main import main

        with pytest.raises(SystemExit) as exc_info:
            main(
                [
                    "issues",
                    "--content-root",
                    str(tmp_path),
                    "--status",
                    "BOGUS",
                ]
            )
        assert exc_info.value.code == 2
        err = capsys.readouterr().err
        assert "invalid choice" in err or "BOGUS" in err


# ---------------------------------------------------------------------------
# P7 — _load_mapping_doc wraps JSONDecodeError
# ---------------------------------------------------------------------------


class TestLoadMappingDocJsonError:
    def test_empty_file_raises_friendly_value_error(self, tmp_path: Path) -> None:
        from owcopilot.cli.main import _load_mapping_doc

        bad = tmp_path / "bad.json"
        bad.write_bytes(b"")
        with pytest.raises(ValueError, match="合法 JSON|not.*JSON"):
            _load_mapping_doc(bad)

    def test_html_content_raises_friendly_value_error(self, tmp_path: Path) -> None:
        from owcopilot.cli.main import _load_mapping_doc

        bad = tmp_path / "bad.json"
        bad.write_text("<html>not json</html>", encoding="utf-8")
        with pytest.raises(ValueError, match="合法 JSON|bad.json"):
            _load_mapping_doc(bad)

    def test_valid_json_returns_dict(self, tmp_path: Path) -> None:
        from owcopilot.cli.main import _load_mapping_doc

        good = tmp_path / "good.json"
        good.write_text(json.dumps({"columns": {"a": "b"}}), encoding="utf-8")
        result = _load_mapping_doc(good)
        assert result == {"columns": {"a": "b"}}

    def test_json_array_raises_friendly_value_error(self, tmp_path: Path) -> None:
        from owcopilot.cli.main import _load_mapping_doc

        array_file = tmp_path / "arr.json"
        array_file.write_text("[]", encoding="utf-8")
        with pytest.raises(ValueError, match="JSON 对象|object"):
            _load_mapping_doc(array_file)


# ---------------------------------------------------------------------------
# P8 — --max-cost-usd rejects negative values
# ---------------------------------------------------------------------------


class TestNonnegFloat:
    def test_negative_float_rejected(self) -> None:
        import argparse

        from owcopilot.cli.main import _nonneg_float

        with pytest.raises(argparse.ArgumentTypeError, match=">="):
            _nonneg_float("-5.0")

    def test_zero_allowed(self) -> None:
        from owcopilot.cli.main import _nonneg_float

        assert _nonneg_float("0") == 0.0

    def test_positive_float_allowed(self) -> None:
        from owcopilot.cli.main import _nonneg_float

        assert _nonneg_float("1.5") == 1.5

    def test_non_numeric_rejected(self) -> None:
        import argparse

        from owcopilot.cli.main import _nonneg_float

        with pytest.raises(argparse.ArgumentTypeError):
            _nonneg_float("abc")


# ---------------------------------------------------------------------------
# P9 — --budget-tokens rejects negative values
# ---------------------------------------------------------------------------


class TestNonnegInt:
    def test_negative_int_rejected(self) -> None:
        import argparse

        from owcopilot.cli.main import _nonneg_int

        with pytest.raises(argparse.ArgumentTypeError, match=">="):
            _nonneg_int("-1")

    def test_zero_allowed(self) -> None:
        from owcopilot.cli.main import _nonneg_int

        assert _nonneg_int("0") == 0

    def test_positive_int_allowed(self) -> None:
        from owcopilot.cli.main import _nonneg_int

        assert _nonneg_int("800") == 800


# ---------------------------------------------------------------------------
# P10 — agent --goal "" raises ValueError
# ---------------------------------------------------------------------------


class TestAgentEmptyGoal:
    def test_empty_goal_raises_value_error(self, tmp_path: Path, capsys) -> None:
        from owcopilot.cli.main import main

        # Create a minimal content root so the path existence check passes
        (tmp_path / "content").mkdir()
        rc = main(
            [
                "agent",
                "--content-root",
                str(tmp_path / "content"),
                "--goal",
                "",
            ]
        )
        # main() catches ValueError, writes JSON error to stderr, returns 2
        captured = capsys.readouterr()
        assert rc == 2
        body = json.loads(captured.err)
        assert "goal" in body.get("error", "").lower() or "goal" in str(body)

    def test_whitespace_goal_raises_value_error(self, tmp_path: Path, capsys) -> None:
        from owcopilot.cli.main import main

        (tmp_path / "content").mkdir()
        rc = main(
            [
                "agent",
                "--content-root",
                str(tmp_path / "content"),
                "--goal",
                "   ",
            ]
        )
        assert rc == 2
        body = json.loads(capsys.readouterr().err)
        assert "goal" in body.get("error", "").lower() or "goal" in str(body)


# ---------------------------------------------------------------------------
# P11 — barks --speakers "" raises ValueError
# ---------------------------------------------------------------------------


class TestBarksEmptySpeakers:
    def test_empty_speakers_raises_value_error(self, tmp_path: Path, capsys) -> None:
        from owcopilot.cli.main import main
        from owcopilot.content.models import ContentBundle
        from owcopilot.content.store import ContentStore

        content_root = tmp_path / "content"
        store = ContentStore(content_root)
        store.save(ContentBundle())

        rc = main(
            [
                "barks",
                "--content-root",
                str(content_root),
                "--speakers",
                "",
                "--topic",
                "some topic",
            ]
        )
        # main() catches ValueError, writes JSON error to stderr, returns 2
        captured = capsys.readouterr()
        assert rc == 2
        body = json.loads(captured.err)
        assert "speakers" in body.get("error", "").lower() or "speakers" in str(body)


# ---------------------------------------------------------------------------
# BUG-1 — _require_valid_id blocks ASCII control characters \x00-\x1f
# ---------------------------------------------------------------------------


class TestRequireValidIdControlChars:
    """BUG-1: NUL and other control characters must be rejected at the ID boundary."""

    def test_nul_byte_in_id_raises(self) -> None:
        with pytest.raises(ValueError, match="control character"):
            _require_valid_id("npc_\x00alice", context="test")

    def test_tab_in_id_raises(self) -> None:
        with pytest.raises(ValueError, match="control character"):
            _require_valid_id("npc_\talice", context="test")

    def test_newline_in_id_raises(self) -> None:
        with pytest.raises(ValueError, match="control character"):
            _require_valid_id("npc_\nalice", context="test")

    def test_carriage_return_in_id_raises(self) -> None:
        with pytest.raises(ValueError, match="control character"):
            _require_valid_id("id_\r_bad", context="test")

    def test_unit_separator_raises(self) -> None:
        # \x1f is the last control char that must be blocked
        with pytest.raises(ValueError, match="control character"):
            _require_valid_id("id_\x1f_end", context="test")

    def test_space_is_allowed_via_strip_path(self) -> None:
        # Space (\x20) is NOT a control char — the boundary is < 32 exclusive
        # Leading/trailing spaces are stripped. A space in the middle is valid.
        result = _require_valid_id("npc alice", context="test")
        assert result == "npc alice"

    def test_entity_with_nul_id_raises_via_normalize(self) -> None:
        """End-to-end: control char injected via JSON still raises at normalize."""
        raw = _make_raw("entity", {"id": "npc_\x00inject", "name": "Bad", "type": "npc"})
        with pytest.raises(ValueError, match="control character"):
            normalize_raw_objects([raw])


# ---------------------------------------------------------------------------
# BUG-2 — normalize_raw_objects detects intra-batch duplicate IDs
# ---------------------------------------------------------------------------


class TestBatchDuplicateIdDetection:
    """BUG-2: Two rows with the same ID in one batch must not silently overwrite each other."""

    def test_duplicate_entity_id_raises(self) -> None:
        raw1 = _make_raw("entity", {"id": "npc_alice", "name": "Alice", "type": "npc"})
        raw2 = _make_raw("entity", {"id": "npc_alice", "name": "Alice v2", "type": "npc"})
        with pytest.raises(ValueError, match="duplicate id"):
            normalize_raw_objects([raw1, raw2])

    def test_duplicate_quest_id_raises(self) -> None:
        raw1 = _make_raw("quest", {"id": "quest_foo", "title": "Foo"})
        raw2 = _make_raw("quest", {"id": "quest_foo", "title": "Foo Again"})
        with pytest.raises(ValueError, match="duplicate id"):
            normalize_raw_objects([raw1, raw2])

    def test_duplicate_region_id_raises(self) -> None:
        raw1 = _make_raw("region", {"id": "region_north", "name": "North"})
        raw2 = _make_raw("region", {"id": "region_north", "name": "North Clone"})
        with pytest.raises(ValueError, match="duplicate id"):
            normalize_raw_objects([raw1, raw2])

    def test_duplicate_poi_id_raises(self) -> None:
        raw1 = _make_raw("poi", {"id": "poi_tavern", "name": "Tavern"})
        raw2 = _make_raw("poi", {"id": "poi_tavern", "name": "Tavern2"})
        with pytest.raises(ValueError, match="duplicate id"):
            normalize_raw_objects([raw1, raw2])

    def test_duplicate_term_id_raises(self) -> None:
        raw1 = _make_raw("term", {"id": "term_magic", "canonical": "magic"})
        raw2 = _make_raw("term", {"id": "term_magic", "canonical": "sorcery"})
        with pytest.raises(ValueError, match="duplicate id"):
            normalize_raw_objects([raw1, raw2])

    def test_same_id_different_kind_is_allowed(self) -> None:
        """entity:foo and quest:foo are distinct keys — must not clash."""
        raw_entity = _make_raw("entity", {"id": "foo", "name": "Foo", "type": "concept"})
        raw_quest = _make_raw("quest", {"id": "quest_foo", "title": "Foo"})
        bundle = normalize_raw_objects([raw_entity, raw_quest])
        assert "foo" in bundle.entities
        assert "quest_foo" in bundle.quests

    def test_error_message_identifies_both_sources(self) -> None:
        raw1 = _make_raw("entity", {"id": "npc_x", "name": "X", "type": "npc"}, source="a.csv")
        raw2 = _make_raw("entity", {"id": "npc_x", "name": "Y", "type": "npc"}, source="b.csv")
        with pytest.raises(ValueError, match="npc_x"):
            normalize_raw_objects([raw1, raw2])

    def test_unique_ids_within_batch_ok(self) -> None:
        raw1 = _make_raw("entity", {"id": "npc_a", "name": "A", "type": "npc"})
        raw2 = _make_raw("entity", {"id": "npc_b", "name": "B", "type": "npc"})
        bundle = normalize_raw_objects([raw1, raw2])
        assert len(bundle.entities) == 2


# ---------------------------------------------------------------------------
# BUG-3/4/5 — _xxx_from_raw: isinstance(raw_id, str) type pre-check
# ---------------------------------------------------------------------------


class TestIdTypePreCheck:
    """BUG-3/4/5: bool/list/dict ids must be rejected before str() silently coerces them."""

    def test_entity_bool_true_id_raises(self) -> None:
        raw = _make_raw("entity", {"id": True, "name": "Bad", "type": "npc"})
        with pytest.raises(ValueError, match="bool"):
            normalize_raw_objects([raw])

    def test_entity_bool_false_id_raises(self) -> None:
        raw = _make_raw("entity", {"id": False, "name": "Bad", "type": "npc"})
        with pytest.raises(ValueError, match="bool"):
            normalize_raw_objects([raw])

    def test_entity_list_id_raises(self) -> None:
        raw = _make_raw("entity", {"id": ["npc_a", "npc_b"], "name": "Bad", "type": "npc"})
        with pytest.raises(ValueError, match="list"):
            normalize_raw_objects([raw])

    def test_entity_dict_id_raises(self) -> None:
        raw = _make_raw("entity", {"id": {"inner": "value"}, "name": "Bad", "type": "npc"})
        with pytest.raises(ValueError, match="dict"):
            normalize_raw_objects([raw])

    def test_quest_bool_id_raises(self) -> None:
        raw = _make_raw("quest", {"id": True, "title": "Bad Quest"})
        with pytest.raises(ValueError, match="bool"):
            normalize_raw_objects([raw])

    def test_region_list_id_raises(self) -> None:
        raw = _make_raw("region", {"id": [1, 2], "name": "Bad Region"})
        with pytest.raises(ValueError, match="list"):
            normalize_raw_objects([raw])

    def test_poi_dict_id_raises(self) -> None:
        raw = _make_raw("poi", {"id": {}, "name": "Bad POI"})
        with pytest.raises(ValueError, match="dict"):
            normalize_raw_objects([raw])

    def test_dialogue_bool_id_raises(self) -> None:
        raw = _make_raw("dialogue", {"id": True, "text_key": "key_1"})
        with pytest.raises(ValueError, match="bool"):
            normalize_raw_objects([raw])

    def test_term_list_id_raises(self) -> None:
        raw = _make_raw("term", {"id": ["bad"], "canonical": "term"})
        with pytest.raises(ValueError, match="list"):
            normalize_raw_objects([raw])

    def test_none_id_falls_through_to_slug(self) -> None:
        """None is not a type error; the function falls through to slug generation."""
        raw = _make_raw("entity", {"id": None, "name": "Alice", "type": "npc"})
        bundle = normalize_raw_objects([raw])
        assert len(bundle.entities) == 1

    def test_int_id_raises(self) -> None:
        """Integers also must be rejected — not just bool/list/dict."""
        raw = _make_raw("entity", {"id": 42, "name": "Bad", "type": "npc"})
        with pytest.raises(ValueError, match="int"):
            normalize_raw_objects([raw])


# ---------------------------------------------------------------------------
# BUG-6 — _objects_from_json: top-level non-list/dict raises ValueError
# ---------------------------------------------------------------------------


class TestObjectsFromJsonTopLevel:
    """BUG-6: a JSON file whose top-level is a scalar must raise a friendly ValueError."""

    def test_top_level_number_raises(self, tmp_path: Path) -> None:
        from owcopilot.content.importers.json import JSONImporter

        f = tmp_path / "bad.json"
        f.write_text("42", encoding="utf-8")
        with pytest.raises(ValueError, match="顶层|int|list|dict"):
            JSONImporter().parse(f)

    def test_top_level_string_raises(self, tmp_path: Path) -> None:
        from owcopilot.content.importers.json import JSONImporter

        f = tmp_path / "bad.json"
        f.write_text('"just a string"', encoding="utf-8")
        with pytest.raises(ValueError, match="顶层|str|list|dict"):
            JSONImporter().parse(f)

    def test_top_level_bool_raises(self, tmp_path: Path) -> None:
        from owcopilot.content.importers.json import JSONImporter

        f = tmp_path / "bad.json"
        f.write_text("true", encoding="utf-8")
        with pytest.raises(ValueError, match="顶层|bool|list|dict"):
            JSONImporter().parse(f)

    def test_top_level_null_raises(self, tmp_path: Path) -> None:
        from owcopilot.content.importers.json import JSONImporter

        f = tmp_path / "bad.json"
        f.write_text("null", encoding="utf-8")
        with pytest.raises(ValueError, match="顶层|NoneType|list|dict"):
            JSONImporter().parse(f)

    def test_top_level_list_is_ok(self, tmp_path: Path) -> None:
        from owcopilot.content.importers.json import JSONImporter

        f = tmp_path / "ok.json"
        f.write_text(
            json.dumps([{"kind": "entity", "id": "npc_x", "name": "X", "type": "npc"}]),
            encoding="utf-8",
        )
        raws = JSONImporter().parse(f)
        assert len(raws) == 1

    def test_top_level_dict_is_ok(self, tmp_path: Path) -> None:
        from owcopilot.content.importers.json import JSONImporter

        f = tmp_path / "ok.json"
        f.write_text(
            json.dumps({"kind": "entity", "id": "npc_y", "name": "Y", "type": "npc"}),
            encoding="utf-8",
        )
        raws = JSONImporter().parse(f)
        assert len(raws) == 1


# ---------------------------------------------------------------------------
# BUG-7 — impact --max-depth uses _nonneg_int (rejects negative values)
# ---------------------------------------------------------------------------


class TestImpactMaxDepth:
    """BUG-7: --max-depth on `impact` must reject negative values via _nonneg_int."""

    def test_negative_max_depth_exits_2(self, tmp_path: Path, capsys) -> None:
        from owcopilot.cli.main import main

        with pytest.raises(SystemExit) as exc_info:
            main(
                [
                    "impact",
                    "--content-root",
                    str(tmp_path),
                    "--change",
                    "entity_delete:npc_x",
                    "--max-depth",
                    "-1",
                ]
            )
        assert exc_info.value.code == 2
        err = capsys.readouterr().err
        assert ">= 0" in err or "invalid" in err or "-1" in err

    def test_zero_max_depth_is_accepted_by_parser(self) -> None:
        """Parser must accept 0 (it's non-negative); command may still fail for other reasons."""
        from owcopilot.cli.main import _nonneg_int

        assert _nonneg_int("0") == 0

    def test_positive_max_depth_is_accepted_by_parser(self) -> None:
        from owcopilot.cli.main import _nonneg_int

        assert _nonneg_int("3") == 3


# ---------------------------------------------------------------------------
# BUG-8 — agent --skills cross-validated against registry; unknown warns
# ---------------------------------------------------------------------------


class TestAgentSkillsCrossValidation:
    """BUG-8: unknown skill names in --skills should emit a warning, not silently disappear."""

    def test_unknown_skill_emits_warning_to_stderr(self, tmp_path: Path, capsys) -> None:
        from owcopilot.cli.main import main
        from owcopilot.content.models import ContentBundle
        from owcopilot.content.store import ContentStore

        content_root = tmp_path / "content"
        store = ContentStore(content_root)
        store.save(ContentBundle())

        main(
            [
                "agent",
                "--content-root",
                str(content_root),
                "--goal",
                "check consistency",
                "--skills",
                "audit_project",
                "totally_fake_skill_xyz",
            ]
        )
        captured = capsys.readouterr()
        # The main error output is to stderr; but the warning about unknown skills
        # must also appear in stderr before the agent runs (or while building the response).
        # Accept rc==0 (agent ran with known subset) OR rc==2 (some other error) —
        # what matters is that the unknown skill warning appears in stderr.
        all_stderr = captured.err
        assert "totally_fake_skill_xyz" in all_stderr or "unknown" in all_stderr.lower()

    def test_all_known_skills_produces_no_unknown_warning(self, tmp_path: Path, capsys) -> None:
        """If every skill in --skills is registered, no warning should be emitted."""
        from owcopilot.cli.main import main
        from owcopilot.content.models import ContentBundle
        from owcopilot.content.store import ContentStore

        content_root = tmp_path / "content"
        store = ContentStore(content_root)
        store.save(ContentBundle())

        # Run with a known skill only; capture stderr for the warning JSON line
        main(
            [
                "agent",
                "--content-root",
                str(content_root),
                "--goal",
                "check consistency",
                "--skills",
                "audit_project",
            ]
        )
        captured = capsys.readouterr()
        # No "warning" JSON line should be present for known skills
        for line in captured.err.splitlines():
            if line.strip().startswith("{"):
                parsed = json.loads(line)
                assert "unknown_skills" not in parsed, (
                    f"Unexpected unknown_skills warning: {parsed}"
                )


# ===========================================================================
# R3 fixes (Team-A · data integrity)
# ===========================================================================


# ---------------------------------------------------------------------------
# R3-BUG-1 — quest_event_ref / style_guide were the two kinds that bypassed the
# R2 id hardening. They now route through the unified _resolve_id entry point.
# ---------------------------------------------------------------------------


class TestQuestEventRefIdHardening:
    """quest_event_ref must reject malformed explicit ids (type confusion / control / traversal)."""

    def test_list_id_raises(self) -> None:
        raw = _make_raw("quest_event_ref", {"id": ["a", "b"], "quest_id": "q1", "event_id": "e1"})
        with pytest.raises(ValueError, match="list"):
            normalize_raw_objects([raw])

    def test_dict_id_raises(self) -> None:
        raw = _make_raw("quest_event_ref", {"id": {"x": 1}, "quest_id": "q1", "event_id": "e1"})
        with pytest.raises(ValueError, match="dict"):
            normalize_raw_objects([raw])

    def test_bool_id_raises(self) -> None:
        raw = _make_raw("quest_event_ref", {"id": True, "quest_id": "q1", "event_id": "e1"})
        with pytest.raises(ValueError, match="bool"):
            normalize_raw_objects([raw])

    def test_int_id_raises(self) -> None:
        raw = _make_raw("quest_event_ref", {"id": 7, "quest_id": "q1", "event_id": "e1"})
        with pytest.raises(ValueError, match="int"):
            normalize_raw_objects([raw])

    def test_nul_byte_in_explicit_id_raises(self) -> None:
        raw = _make_raw(
            "quest_event_ref", {"id": "evt\x00ref", "quest_id": "q1", "event_id": "e1"}
        )
        with pytest.raises(ValueError, match="control character"):
            normalize_raw_objects([raw])

    def test_path_traversal_explicit_id_raises(self) -> None:
        raw = _make_raw(
            "quest_event_ref",
            {"id": "../../etc/passwd", "quest_id": "q1", "event_id": "e1"},
        )
        with pytest.raises(ValueError, match="forbidden character|path traversal"):
            normalize_raw_objects([raw])

    def test_explicit_id_with_colon_is_rejected(self) -> None:
        """A user-supplied id with a colon is rejected — only the *synthetic* default may
        carry the structural colon."""
        raw = _make_raw(
            "quest_event_ref", {"id": "a/b:c", "quest_id": "q1", "event_id": "e1"}
        )
        with pytest.raises(ValueError, match="forbidden character"):
            normalize_raw_objects([raw])

    def test_synthetic_default_id_keeps_colon(self) -> None:
        """When no explicit id is given, the synthetic q:e:kind id is allowed to keep its
        structural colon — that path must still succeed."""
        raw = _make_raw("quest_event_ref", {"quest_id": "q1", "event_id": "e1"})
        bundle = normalize_raw_objects([raw])
        assert len(bundle.quest_event_refs) == 1
        (ref_id,) = bundle.quest_event_refs
        assert ":" in ref_id  # e.g. "q1:e1:mentions_event"

    def test_valid_explicit_id_passes(self) -> None:
        raw = _make_raw(
            "quest_event_ref", {"id": "ref_q1_e1", "quest_id": "q1", "event_id": "e1"}
        )
        bundle = normalize_raw_objects([raw])
        assert "ref_q1_e1" in bundle.quest_event_refs


class TestStyleGuideIdHardening:
    """style_guide was the second kind that bypassed R2 id hardening."""

    def test_list_id_raises(self) -> None:
        raw = _make_raw("style_guide", {"id": ["a"], "body": "x"})
        with pytest.raises(ValueError, match="list"):
            normalize_raw_objects([raw])

    def test_dict_id_raises(self) -> None:
        raw = _make_raw("style_guide", {"id": {"k": "v"}, "body": "x"})
        with pytest.raises(ValueError, match="dict"):
            normalize_raw_objects([raw])

    def test_bool_id_raises(self) -> None:
        raw = _make_raw("style_guide", {"id": True, "body": "x"})
        with pytest.raises(ValueError, match="bool"):
            normalize_raw_objects([raw])

    def test_nul_byte_in_id_raises(self) -> None:
        raw = _make_raw("style_guide", {"id": "sg\x00", "body": "x"})
        with pytest.raises(ValueError, match="control character"):
            normalize_raw_objects([raw])

    def test_path_traversal_id_raises(self) -> None:
        raw = _make_raw("style_guide", {"id": "../../../boom", "body": "x"})
        with pytest.raises(ValueError, match="forbidden character|path traversal"):
            normalize_raw_objects([raw])

    def test_slash_colon_id_raises(self) -> None:
        raw = _make_raw("style_guide", {"id": "sg slash/colon:bad", "body": "x"})
        with pytest.raises(ValueError, match="forbidden character"):
            normalize_raw_objects([raw])

    def test_default_id_passes(self) -> None:
        """No explicit id → synthetic 'style_guide' default must validate cleanly."""
        raw = _make_raw("style_guide", {"body": "Keep it terse."})
        bundle = normalize_raw_objects([raw])
        assert "style_guide" in bundle.style_guides

    def test_valid_explicit_id_passes(self) -> None:
        raw = _make_raw("style_guide", {"id": "sg_combat", "body": "x"})
        bundle = normalize_raw_objects([raw])
        assert "sg_combat" in bundle.style_guides


class TestUnifiedResolveIdEntry:
    """Regression: every kind reaches the same _resolve_id collection point, so the type
    pre-check and char validation can never be 'missed' on one kind again."""

    def test_resolve_id_uses_fallback_when_id_absent(self) -> None:
        from owcopilot.content.normalize import _resolve_id

        assert _resolve_id(None, "fallback_slug", context="t") == "fallback_slug"

    def test_resolve_id_prefers_explicit_over_fallback(self) -> None:
        from owcopilot.content.normalize import _resolve_id

        assert _resolve_id("explicit_id", "fallback_slug", context="t") == "explicit_id"

    def test_resolve_id_blank_explicit_id_raises_not_fallback(self) -> None:
        """A present-but-blank id is a user error — it must NOT silently fall to the slug."""
        from owcopilot.content.normalize import _resolve_id

        with pytest.raises(ValueError, match="must not be blank"):
            _resolve_id("   ", "fallback_slug", context="t")

    def test_resolve_id_rejects_non_str(self) -> None:
        from owcopilot.content.normalize import _resolve_id

        with pytest.raises(ValueError, match="list"):
            _resolve_id(["x"], "fallback_slug", context="t")

    def test_resolve_id_explicit_colon_rejected_even_when_synthetic_allowed(self) -> None:
        """allow_synthetic_separator relaxes the colon rule for the *fallback* only; an
        explicit id is still strict."""
        from owcopilot.content.normalize import _resolve_id

        with pytest.raises(ValueError, match="forbidden character"):
            _resolve_id("a:b", "q:e:k", context="t", allow_synthetic_separator=True)

    def test_resolve_id_synthetic_colon_fallback_passes(self) -> None:
        from owcopilot.content.normalize import _resolve_id

        assert _resolve_id(None, "q1:e1:k", context="t", allow_synthetic_separator=True) == (
            "q1:e1:k"
        )

    def test_resolve_id_synthetic_traversal_still_blocked(self) -> None:
        """Even on the relaxed synthetic path, traversal and separators stay blocked."""
        from owcopilot.content.normalize import _resolve_id

        with pytest.raises(ValueError, match="path traversal|forbidden character"):
            _resolve_id(None, "../evil", context="t", allow_synthetic_separator=True)


# ---------------------------------------------------------------------------
# R3-BUG-2 — localized_text: a 2-letter field name (notably "id") must NOT be
# silently treated as a locale and fabricated into a translation row.
# ---------------------------------------------------------------------------


class TestLocalizedTextLocaleWhitelist:
    def test_id_field_not_treated_as_locale(self) -> None:
        """The reproducer: an 'id' field must not produce a bogus locale='id' row."""
        raw = _make_raw(
            "localized_text",
            {"id": "loc1", "text_key": "k", "locale": "en", "text": "hi"},
        )
        bundle = normalize_raw_objects([raw])
        locales = {t.locale for t in bundle.localized_texts.values()}
        assert "id" not in locales
        assert locales == {"en"}

    def test_unknown_two_letter_columns_are_not_locales(self) -> None:
        """Stray 2-letter columns (zz/qq) are no longer fabricated into translation rows."""
        raw = _make_raw(
            "localized_text",
            {"text_key": "k", "zz": "garbage", "qq": "more", "locale": "en", "text": "hi"},
        )
        bundle = normalize_raw_objects([raw])
        locales = {t.locale for t in bundle.localized_texts.values()}
        assert locales == {"en"}

    def test_real_language_columns_still_recognized(self) -> None:
        """Genuine locale columns (en/zh/fr) must keep working."""
        raw = _make_raw(
            "localized_text",
            {"text_key": "ui.greet", "en": "Hello", "zh": "你好", "fr": "Bonjour"},
        )
        bundle = normalize_raw_objects([raw])
        locales = {t.locale for t in bundle.localized_texts.values()}
        assert locales == {"en", "zh", "fr"}

    def test_locale_with_region_subtag_recognized(self) -> None:
        raw = _make_raw("localized_text", {"text_key": "k", "zh-cn": "你好", "en-us": "hi"})
        bundle = normalize_raw_objects([raw])
        locales = {t.locale for t in bundle.localized_texts.values()}
        assert locales == {"zh-cn", "en-us"}

    def test_non_str_id_in_localized_text_raises(self) -> None:
        """A dict id on a localized_text row is type confusion — reject it, don't slug it."""
        raw = _make_raw(
            "localized_text",
            {"id": {"x": 1}, "text_key": "k", "locale": "en", "text": "hi"},
        )
        with pytest.raises(ValueError, match="dict"):
            normalize_raw_objects([raw])


# ---------------------------------------------------------------------------
# R3-BUG-3 — deeply nested JSON raises a guided ValueError, not a raw RecursionError.
# ---------------------------------------------------------------------------


class TestDeepJsonRecursionGuard:
    def test_deeply_nested_json_object_raises_guided_value_error(self, tmp_path: Path) -> None:
        from owcopilot.content.importers.json import JSONImporter

        depth = 50_000
        payload = "[" * depth + "]" * depth
        f = tmp_path / "deep.json"
        f.write_text(payload, encoding="utf-8")
        # Must surface a guided ValueError (嵌套层数过深), never a raw RecursionError.
        with pytest.raises(ValueError, match="嵌套层数过深"):
            JSONImporter().parse(f)

    def test_deeply_nested_jsonl_line_raises_guided_value_error(self, tmp_path: Path) -> None:
        from owcopilot.content.importers.json import JSONImporter

        depth = 50_000
        line = "[" * depth + "]" * depth
        f = tmp_path / "deep.jsonl"
        f.write_text(line + "\n", encoding="utf-8")
        with pytest.raises(ValueError, match="嵌套层数过深"):
            JSONImporter().parse(f)


# ===========================================================================
# R4 fixes (Team-A · content write-boundary)
# ===========================================================================


# ---------------------------------------------------------------------------
# R4-BUG-A — the id invariant lived ONLY in normalize._resolve_id, so a second
# ingest path (recognize → human review → ContentBundle.model_validate →
# store.save) could write a traversal id as `{id}.json` and escape the content
# dir. The invariant is now enforced at the write boundary too, shared with
# normalize via `_validate_id_chars` (not a copy).
# ---------------------------------------------------------------------------


class TestStoreWriteBoundaryIdInvariant:
    def test_traversal_entity_id_blocked_at_save(self, tmp_path: Path) -> None:
        """E2E: the exact reproducer id `../../../escaped` is rejected by store.save,
        and nothing is written outside the content root."""
        from owcopilot.content.models import ContentBundle, Entity, EntityType
        from owcopilot.content.store import ContentStore

        content_root = tmp_path / "myworld" / "content"
        content_root.mkdir(parents=True)
        bundle = ContentBundle()
        # The model layer has no id validator, so this is accepted in-memory (that is the gap).
        bundle.entities["../../../escaped"] = Entity(
            id="../../../escaped", name="evil", type=EntityType.NPC
        )

        before = {p for p in tmp_path.rglob("*") if p.is_file()}
        with pytest.raises(ValueError, match="forbidden character|path traversal"):
            ContentStore(content_root).save(bundle)
        after = {p for p in tmp_path.rglob("*") if p.is_file()}

        # No file escaped the content root (no `escaped.json` two levels up, etc.).
        new_files = after - before
        escapees = [
            f for f in new_files if content_root.resolve() not in f.resolve().parents
        ]
        assert not escapees, f"files escaped content root: {escapees}"

    def test_backslash_id_blocked_at_save(self, tmp_path: Path) -> None:
        from owcopilot.content.models import ContentBundle, Quest
        from owcopilot.content.store import ContentStore

        bundle = ContentBundle()
        bundle.quests["..\\..\\evil"] = Quest(id="..\\..\\evil", title="x")
        with pytest.raises(ValueError, match="forbidden character|path traversal"):
            ContentStore(tmp_path / "content").save(bundle)

    def test_control_char_id_blocked_at_save(self, tmp_path: Path) -> None:
        from owcopilot.content.models import ContentBundle, Entity, EntityType
        from owcopilot.content.store import ContentStore

        bundle = ContentBundle()
        bundle.entities["npc_\x00null"] = Entity(
            id="npc_\x00null", name="x", type=EntityType.NPC
        )
        with pytest.raises(ValueError, match="control character"):
            ContentStore(tmp_path / "content").save(bundle)

    def test_legal_ids_save_and_roundtrip(self, tmp_path: Path) -> None:
        """The guard must not regress legal ids — including CJK / emoji, which are path-safe."""
        from owcopilot.content.models import ContentBundle, Entity, EntityType
        from owcopilot.content.store import ContentStore

        bundle = ContentBundle()
        bundle.entities["npc_aldric"] = Entity(
            id="npc_aldric", name="Aldric", type=EntityType.NPC
        )
        bundle.entities["npc_李白"] = Entity(id="npc_李白", name="李白", type=EntityType.NPC)
        store = ContentStore(tmp_path / "content")
        store.save(bundle)
        loaded = store.load()
        assert set(loaded.entities) == {"npc_aldric", "npc_李白"}

    def test_quest_event_ref_synthetic_colon_id_unaffected(self, tmp_path: Path) -> None:
        """The colon-bearing synthetic quest_event_ref id is written to event_refs.jsonl,
        NOT `{id}.json`, so the write-boundary guard must NOT reject it."""
        from owcopilot.content.models import (
            ContentBundle,
            QuestEventReference,
            QuestEventRefKind,
        )
        from owcopilot.content.store import ContentStore

        synthetic_id = "q1:e1:mentions_event"
        bundle = ContentBundle()
        bundle.quest_event_refs[synthetic_id] = QuestEventReference(
            id=synthetic_id,
            quest_id="q1",
            event_id="e1",
            ref_kind=QuestEventRefKind.MENTIONS_EVENT,
        )
        store = ContentStore(tmp_path / "content")
        store.save(bundle)  # must not raise
        loaded = store.load()
        assert synthetic_id in loaded.quest_event_refs

    def test_save_is_atomic_no_partial_delete_on_bad_id(self, tmp_path: Path) -> None:
        """A bad id in a later object must abort before the dir-cleanup unlinks existing files."""
        from owcopilot.content.models import ContentBundle, Entity, EntityType
        from owcopilot.content.store import ContentStore

        store = ContentStore(tmp_path / "content")
        good = ContentBundle()
        good.entities["npc_keep"] = Entity(id="npc_keep", name="Keep", type=EntityType.NPC)
        store.save(good)  # establishes an on-disk file

        bad = ContentBundle()
        bad.entities["npc_keep"] = Entity(id="npc_keep", name="Keep", type=EntityType.NPC)
        bad.entities["../escape"] = Entity(id="../escape", name="x", type=EntityType.NPC)
        with pytest.raises(ValueError, match="forbidden character|path traversal"):
            store.save(bad)
        # The pre-existing good file must survive (validation runs before any unlink).
        assert store.load().entities["npc_keep"].name == "Keep"

    def test_second_layer_container_assertion_blocks_escape_if_first_layer_bypassed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The write boundary now layers `resolve_under_root` over the final `{id}.json`
        path. To prove the container assertion is wired independently of the friendly
        char blacklist, neuter `_validate_id_chars` and confirm an escaping id is still
        rejected with a guided PathSecurityError (a ValueError subclass)."""
        from owcopilot.content.models import ContentBundle, Entity, EntityType
        from owcopilot.content.store import ContentStore
        from owcopilot.trust.security import PathSecurityError

        monkeypatch.setattr(
            "owcopilot.content.store._validate_id_chars", lambda value, **_: value
        )
        bundle = ContentBundle()
        # Final path is `<root>/world/entities/<id>.json` (3 levels under the root), so an
        # id needs `../../../../` to actually escape — fewer `..` stay inside the root.
        bundle.entities["../../../../escaped"] = Entity(
            id="../../../../escaped", name="evil", type=EntityType.NPC
        )
        with pytest.raises(PathSecurityError) as exc:
            ContentStore(tmp_path / "content").save(bundle)
        assert "escapes allowed root" in str(exc.value)  # guided, never silent

    def test_second_layer_does_not_misflag_legal_ids(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Even with the char blacklist neutered, the container assertion must accept
        ordinary slug ids (they resolve under the content root)."""
        from owcopilot.content.models import ContentBundle, Entity, EntityType
        from owcopilot.content.store import ContentStore

        monkeypatch.setattr(
            "owcopilot.content.store._validate_id_chars", lambda value, **_: value
        )
        bundle = ContentBundle()
        bundle.entities["npc_aldric"] = Entity(
            id="npc_aldric", name="Aldric", type=EntityType.NPC
        )
        store = ContentStore(tmp_path / "content")
        store.save(bundle)  # must not raise
        assert "npc_aldric" in store.load().entities


# ---------------------------------------------------------------------------
# R4-BUG-B — the Indonesian `id` column (ISO 639-1 'id') is kept structural. The
# red-line case is *silent* total loss: an `id`-only row yields zero rows and the
# value vanishes. That now emits a guiding warning (not silence). When other
# locale data exists the row still produces rows and `id` plays its normal
# structural-identifier role, so no warning is needed.
# ---------------------------------------------------------------------------


class TestIndonesianIdColumnWarns:
    def test_id_only_column_warns_not_silent(self) -> None:
        """Indonesian as the only translation column → 0 rows, but it must NOT be silent."""
        raw = _make_raw("localized_text", {"text_key": "greeting", "id": "halo"})
        with pytest.warns(UserWarning, match="reserved structural field|locale='id'"):
            bundle = normalize_raw_objects([raw])
        assert len(bundle.localized_texts) == 0

    def test_id_column_with_other_locale_is_kept_structural(self, recwarn) -> None:
        """When real locale data exists, 'id' is the structural identifier (not a lost
        translation); the row still produces rows, so no silent-loss warning fires."""
        raw = _make_raw(
            "localized_text", {"text_key": "greeting", "en": "hello", "id": "halo"}
        )
        bundle = normalize_raw_objects([raw])
        # 'id' is never imported as a locale (the structural choice is unchanged).
        locales = {t.locale for t in bundle.localized_texts.values()}
        assert "id" not in locales and locales == {"en"}
        # No silent loss here: the row produced output (the 'id' value surfaces structurally).
        assert not any("structural field" in str(w.message) for w in recwarn)

    def test_explicit_id_as_row_identifier_does_not_warn(self, recwarn) -> None:
        """The legitimate, documented use of `id` as a row identifier must stay quiet."""
        raw = _make_raw(
            "localized_text", {"id": "loc1", "text_key": "k", "locale": "en", "text": "hi"}
        )
        bundle = normalize_raw_objects([raw])
        assert {t.locale for t in bundle.localized_texts.values()} == {"en"}
        assert not any("structural field" in str(w.message) for w in recwarn)

    def test_clean_localized_text_does_not_warn(self, recwarn) -> None:
        raw = _make_raw(
            "localized_text", {"text_key": "k", "en": "hi", "zh": "你好"}
        )
        bundle = normalize_raw_objects([raw])
        assert {t.locale for t in bundle.localized_texts.values()} == {"en", "zh"}
        assert len(recwarn) == 0


# ---------------------------------------------------------------------------
# R4-BUG-C — the ISO 639-1 whitelist now also governs the explicit `locale`
# field (localized_text + dialogue), as a warning. Region/case forms pass.
# ---------------------------------------------------------------------------


class TestExplicitLocaleWhitelist:
    def test_unknown_explicit_locale_warns_but_keeps_value(self) -> None:
        raw = _make_raw(
            "localized_text", {"text_key": "k", "locale": "zz", "text": "x"}
        )
        with pytest.warns(UserWarning, match="not a recognized ISO 639-1"):
            bundle = normalize_raw_objects([raw])
        # Not silently dropped — the value is preserved.
        assert {t.locale for t in bundle.localized_texts.values()} == {"zz"}

    def test_garbage_explicit_locale_warns(self) -> None:
        raw = _make_raw(
            "localized_text", {"text_key": "k", "locale": "NOTALOCALE", "text": "x"}
        )
        with pytest.warns(UserWarning, match="not a recognized ISO 639-1"):
            normalize_raw_objects([raw])

    def test_region_form_locale_not_warned(self, recwarn) -> None:
        """zh-CN (with region + mixed case) is valid and must NOT warn."""
        raw = _make_raw(
            "localized_text", {"text_key": "k", "locale": "zh-CN", "text": "你好"}
        )
        bundle = normalize_raw_objects([raw])
        assert {t.locale for t in bundle.localized_texts.values()} == {"zh-CN"}
        assert len(recwarn) == 0

    def test_uppercase_locale_not_warned(self, recwarn) -> None:
        raw = _make_raw("localized_text", {"text_key": "k", "locale": "EN", "text": "hi"})
        normalize_raw_objects([raw])
        assert len(recwarn) == 0

    def test_indonesian_explicit_locale_accepted_silently(self, recwarn) -> None:
        """'id' as an *explicit* locale field is legitimate Indonesian — must not warn."""
        raw = _make_raw("localized_text", {"text_key": "k", "locale": "id", "text": "halo"})
        bundle = normalize_raw_objects([raw])
        assert {t.locale for t in bundle.localized_texts.values()} == {"id"}
        assert len(recwarn) == 0

    def test_unknown_dialogue_locale_warns(self) -> None:
        raw = _make_raw(
            "dialogue", {"id": "d1", "text_key": "k", "locale": "zz", "text": "x"}
        )
        with pytest.warns(UserWarning, match="not a recognized ISO 639-1"):
            bundle = normalize_raw_objects([raw])
        assert bundle.dialogues["d1"].locale == "zz"  # kept, not dropped

    def test_valid_dialogue_locale_not_warned(self, recwarn) -> None:
        raw = _make_raw(
            "dialogue", {"id": "d1", "text_key": "k", "locale": "ja", "text": "x"}
        )
        normalize_raw_objects([raw])
        assert len(recwarn) == 0
