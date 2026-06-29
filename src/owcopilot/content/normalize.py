"""Normalize raw imported objects into the v2 content models."""

from __future__ import annotations

import re
import warnings
from typing import Any

from .importers.base import RawObject
from .models import (
    POI,
    ContentBundle,
    DialogueRef,
    Entity,
    EntityType,
    LocalizedText,
    Quest,
    QuestEventReference,
    QuestEventRefKind,
    RegionBrief,
    Relation,
    SourceRef,
    StyleGuide,
    Term,
)

_MAX_ID_LENGTH = 256
# Characters never allowed in an explicit (user-supplied) id. These are path
# separators and the drive/namespace colon — an id carrying them could escape the
# content directory when written as ``{id}.json`` (see store.py).
_FORBIDDEN_ID_CHARS = frozenset("/\\.:")
# A handful of kinds (quest_event_ref) build a *synthetic* default id of the form
# ``quest:event:kind``. The colon is structural there, so synthetic ids are allowed
# to contain it — but only when no explicit id was supplied. Everything else (control
# chars, traversal, blank, length) is still enforced.
_FORBIDDEN_ID_CHARS_SYNTHETIC = frozenset("/\\.")


def _validate_id_chars(value: str, *, context: str, forbidden: frozenset[str]) -> str:
    """Shared id-invariant core: blank / control char / forbidden char / traversal / length.

    Raises a domain ValueError (never a raw Python exception) so the CLI global error
    boundary can format a user-friendly message.  Both ``_require_valid_id`` and the
    synthetic-id path funnel through here so the invariants can never drift apart.
    """
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"id must not be blank (got {value!r}); context: {context}")
    # BUG-1: block ASCII control characters \x00-\x1f (NUL etc.)
    ctrl = [c for c in stripped if ord(c) < 32]
    if ctrl:
        chars_repr = ", ".join(sorted(repr(c) for c in ctrl))
        raise ValueError(
            f"id {stripped!r} contains control character(s) {chars_repr} "
            f"(ASCII 0x00-0x1f are not allowed in IDs); context: {context}"
        )
    bad = forbidden.intersection(stripped)
    if bad:
        chars_repr = ", ".join(sorted(repr(c) for c in bad))
        raise ValueError(
            f"id {stripped!r} contains forbidden character(s) {chars_repr} "
            f"(path separators or colons are not allowed); context: {context}"
        )
    if ".." in stripped:
        raise ValueError(
            f"id {stripped!r} contains '..' (path traversal is not allowed); context: {context}"
        )
    if len(stripped) > _MAX_ID_LENGTH:
        raise ValueError(
            f"id exceeds maximum length of {_MAX_ID_LENGTH} characters "
            f"(got {len(stripped)}); context: {context}"
        )
    return stripped


def _require_valid_id(value: str, *, context: str) -> str:
    """Enforce ID invariants for an explicit / per-file id (no path separators, no colon).

    All ``_xxx_from_raw`` entry points reach this via :func:`_resolve_id`.
    """
    return _validate_id_chars(value, context=context, forbidden=_FORBIDDEN_ID_CHARS)


def _assert_id_is_str_or_none(raw_id: object, *, context: str) -> None:
    """Raise ValueError if *raw_id* is a non-string non-None value (BUG-3/4/5).

    bool, list, and dict id values cause str() to silently produce 'True', a repr, or a slug
    from an arbitrary repr — all of which are valid-looking IDs that hide data errors.
    We reject them at the boundary so the caller sees the real type, not a mangled string.
    """
    if raw_id is None:
        return
    if isinstance(raw_id, bool):
        raise ValueError(
            f"id must be a string, got bool ({raw_id!r}); context: {context}"
        )
    if not isinstance(raw_id, str):
        type_name = type(raw_id).__name__
        raise ValueError(
            f"id must be a string, got {type_name} ({raw_id!r}); context: {context}"
        )


def _resolve_id(
    raw_id: object,
    fallback: str,
    *,
    context: str,
    allow_synthetic_separator: bool = False,
) -> str:
    """Single ingest entry point for resolving and validating any content id.

    This is the structural fix for the "per-function id hardening" root cause: rather
    than each ``_xxx_from_raw`` independently remembering to call the type pre-check and
    the char validator (which is how ``quest_event_ref`` / ``style_guide`` were missed),
    every kind routes its id through here. Adding a new kind therefore inherits the full
    invariant set for free.

    Steps:
      1. Type pre-check on the raw id (rejects bool/list/dict/int — BUG-3/4/5).
      2. An id *key that is present but blank* (``""`` / ``"   "``) is a user error and
         is rejected — it does NOT silently fall through to the slug fallback.
      3. Otherwise pick the explicit id when present, else the caller-supplied *fallback*
         (a slug or a synthetic id such as ``quest:event:kind``).
      4. Validate. An explicit id is always held to the strict rule set (no path
         separators, no colon). A synthetic fallback may keep its structural colon
         (``allow_synthetic_separator``) but is still checked for control chars,
         traversal, blankness and length.
    """
    _assert_id_is_str_or_none(raw_id, context=context)
    if isinstance(raw_id, str):
        # The id key was supplied. Validate it as an explicit id — even a whitespace-only
        # value raises "must not be blank" rather than silently slugging the fallback.
        return _require_valid_id(raw_id.strip(), context=context)
    forbidden = _FORBIDDEN_ID_CHARS_SYNTHETIC if allow_synthetic_separator else _FORBIDDEN_ID_CHARS
    return _validate_id_chars(fallback.strip(), context=context, forbidden=forbidden)


def slug_id(value: str, *, prefix: str | None = None) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")
    if not slug and value.strip():
        import hashlib

        slug = hashlib.sha1(value.strip().encode("utf-8")).hexdigest()[:10]
    if prefix and slug and not slug.startswith(f"{prefix}_"):
        return f"{prefix}_{slug}"
    return slug


def normalize_raw_objects(raw_objects: list[RawObject]) -> ContentBundle:
    bundle = ContentBundle()
    name_to_id: dict[str, str] = {}
    # BUG-2: track IDs seen within this batch to reject silent overwrites.
    # Maps "kind:id" → source location for the first occurrence.
    _seen_ids: dict[str, str] = {}

    def _check_duplicate(kind: str, obj_id: str, source: str) -> None:
        key = f"{kind}:{obj_id}"
        if key in _seen_ids:
            raise ValueError(
                f"duplicate id {obj_id!r} for kind '{kind}' in the same import batch "
                f"(first seen at {_seen_ids[key]}, duplicate at {source}); "
                "the second entry would silently overwrite the first — resolve the conflict "
                "before importing."
            )
        _seen_ids[key] = source

    for raw in raw_objects:
        if raw.kind == "entity" or _looks_like_entity(raw.data):
            entity = _entity_from_raw(raw)
            _check_duplicate("entity", entity.id, raw.source_path)
            bundle.add_entity(entity)
            name_to_id[entity.name] = entity.id
        elif raw.kind == "quest":
            quest = _quest_from_raw(raw)
            _check_duplicate("quest", quest.id, raw.source_path)
            bundle.quests[quest.id] = quest
        elif raw.kind in {"quest_event_ref", "questeventref", "quest_event"}:
            ref = _quest_event_ref_from_raw(raw)
            _check_duplicate("quest_event_ref", ref.id, raw.source_path)
            bundle.quest_event_refs[ref.id] = ref
        elif raw.kind in {"region", "regionbrief", "region_brief"}:
            region = _region_from_raw(raw)
            _check_duplicate("region", region.id, raw.source_path)
            bundle.regions[region.id] = region
        elif raw.kind == "poi":
            poi = _poi_from_raw(raw)
            _check_duplicate("poi", poi.id, raw.source_path)
            bundle.pois[poi.id] = poi
        elif raw.kind in {"dialogue", "dialogueref", "dialogue_ref"}:
            dialogue = _dialogue_from_raw(raw)
            _check_duplicate("dialogue", dialogue.id, raw.source_path)
            bundle.dialogues[dialogue.id] = dialogue
        elif raw.kind in {"localized_text", "localizedtext", "localization", "text"}:
            for text in _localized_texts_from_raw(raw):
                _check_duplicate("localized_text", text.id, raw.source_path)
                bundle.localized_texts[text.id] = text
        elif raw.kind == "term":
            term = _term_from_raw(raw)
            _check_duplicate("term", term.id, raw.source_path)
            bundle.terms[term.id] = term
        elif raw.kind in {"style_guide", "styleguide"}:
            style = _style_guide_from_raw(raw)
            _check_duplicate("style_guide", style.id, raw.source_path)
            bundle.style_guides[style.id] = style

    for raw in raw_objects:
        if raw.kind == "relation":
            bundle.add_relation(_relation_from_raw(raw, name_to_id))
    _derive_relations(bundle)

    return bundle


def _looks_like_entity(data: dict[str, Any]) -> bool:
    return "name" in data and "type" in data


def _entity_from_raw(raw: RawObject) -> Entity:
    name = str(raw.data.get("name") or "").strip()
    prefix = str(raw.data.get("type") or "concept").lower()
    entity_id = _resolve_id(
        raw.data.get("id"),
        slug_id(name, prefix=prefix),
        context=f"entity row from {raw.source_path}",
    )
    if not name:
        name = entity_id
    entity_type = _entity_type(raw, entity_id)
    tags = _list(raw.data.get("tags"))
    aliases = _list(raw.data.get("aliases"))
    metadata = _metadata(
        raw.data,
        exclude={
            "kind",
            "object_type",
            "id",
            "name",
            "type",
            "description",
            "aliases",
            "tags",
            "status",
            "version",
        },
    )
    return Entity(
        id=entity_id,
        name=name,
        type=entity_type,
        description=str(raw.data.get("description") or raw.data.get("desc") or ""),
        aliases=aliases,
        tags=tags,
        status=str(raw.data.get("status") or "active"),
        version=_optional_str(raw.data.get("version")),
        metadata=metadata,
        source_ref=SourceRef(path=raw.source_path, line=raw.line, row=raw.row, sheet=raw.sheet),
    )


def _quest_from_raw(raw: RawObject) -> Quest:
    title = str(raw.data.get("title") or raw.data.get("name") or "").strip()
    quest_id = _resolve_id(
        raw.data.get("id"),
        slug_id(title, prefix="quest"),
        context=f"quest row from {raw.source_path}",
    )
    if not title:
        title = quest_id
    return Quest(
        id=quest_id,
        title=title,
        giver_npc=_optional_str(raw.data.get("giver_npc")),
        location=_optional_str(raw.data.get("location")),
        objective=str(raw.data.get("objective") or raw.data.get("objectives") or ""),
        prerequisites=_list(raw.data.get("prerequisites")),
        timeline_order=_optional_int(raw.data.get("timeline_order")),
        dialogue_refs=_list(raw.data.get("dialogue_refs")),
        localization_keys=_list(raw.data.get("localization_keys")),
        tags=_list(raw.data.get("tags")),
        metadata=_metadata(
            raw.data,
            exclude={
                "kind",
                "object_type",
                "id",
                "title",
                "name",
                "giver_npc",
                "location",
                "objective",
                "objectives",
                "prerequisites",
                "timeline_order",
                "dialogue_refs",
                "localization_keys",
                "tags",
            },
        ),
        source_ref=_source_ref(raw),
    )


def _quest_event_ref_from_raw(raw: RawObject) -> QuestEventReference:
    quest_id = str(raw.data.get("quest_id") or "").strip()
    event_id = str(raw.data.get("event_id") or raw.data.get("event") or "").strip()
    ref_kind = _event_ref_kind(raw.data)
    # The synthetic default id is structurally "quest:event:kind" (colon-bearing), so the
    # synthetic path tolerates the colon; an *explicit* id is still held to the strict rules.
    ref_id = _resolve_id(
        raw.data.get("id"),
        f"{quest_id}:{event_id}:{ref_kind.value}",
        context=f"quest_event_ref row from {raw.source_path}",
        allow_synthetic_separator=True,
    )
    return QuestEventReference(
        id=ref_id,
        quest_id=quest_id,
        event_id=event_id,
        ref_kind=ref_kind,
        note=str(raw.data.get("note") or raw.data.get("remark") or raw.data.get("备注") or ""),
        metadata=_metadata(
            raw.data,
            exclude={
                "kind",
                "object_type",
                "id",
                "quest_id",
                "event_id",
                "event",
                "ref_kind",
                "note",
                "remark",
                "备注",
            },
        ),
        source_ref=_source_ref(raw),
    )


def _region_from_raw(raw: RawObject) -> RegionBrief:
    name = str(raw.data.get("name") or "").strip()
    region_id = _resolve_id(
        raw.data.get("id"),
        slug_id(name, prefix="region"),
        context=f"region row from {raw.source_path}",
    )
    if not name:
        name = region_id
    return RegionBrief(
        id=region_id,
        name=name,
        level_min=_optional_int(raw.data.get("level_min")),
        level_max=_optional_int(raw.data.get("level_max")),
        themes=_list(raw.data.get("themes")),
        allowed_content=_list(raw.data.get("allowed_content")),
        banned_content=_list(raw.data.get("banned_content") or raw.data.get("forbidden_elements")),
        metadata=_metadata(
            raw.data,
            exclude={
                "kind",
                "object_type",
                "id",
                "name",
                "level_min",
                "level_max",
                "themes",
                "allowed_content",
                "banned_content",
                "forbidden_elements",
            },
        ),
        source_ref=_source_ref(raw),
    )


def _poi_from_raw(raw: RawObject) -> POI:
    name = str(raw.data.get("name") or "").strip()
    poi_id = _resolve_id(
        raw.data.get("id"),
        slug_id(name, prefix="poi"),
        context=f"poi row from {raw.source_path}",
    )
    if not name:
        name = poi_id
    return POI(
        id=poi_id,
        name=name,
        region_id=_optional_str(raw.data.get("region_id") or raw.data.get("region")),
        purpose=str(raw.data.get("purpose") or raw.data.get("narrative_purpose") or ""),
        controlling_faction=_optional_str(raw.data.get("controlling_faction")),
        level_min=_optional_int(raw.data.get("level_min") or raw.data.get("level")),
        level_max=_optional_int(raw.data.get("level_max") or raw.data.get("level")),
        tags=_list(raw.data.get("tags")),
        metadata=_metadata(
            raw.data,
            exclude={
                "kind",
                "object_type",
                "id",
                "name",
                "region_id",
                "region",
                "purpose",
                "narrative_purpose",
                "controlling_faction",
                "level",
                "level_min",
                "level_max",
                "tags",
            },
        ),
        source_ref=_source_ref(raw),
    )


def _dialogue_from_raw(raw: RawObject) -> DialogueRef:
    text_key = str(raw.data.get("text_key") or raw.data.get("key") or "")
    dialogue_id = _resolve_id(
        raw.data.get("id"),
        slug_id(text_key, prefix="dialogue"),
        context=f"dialogue row from {raw.source_path}",
    )
    if not text_key:
        text_key = dialogue_id
    dialogue_locale = _optional_str(raw.data.get("locale"))
    if dialogue_locale is not None:
        # Same ISO 639-1 whitelist as localized_text's explicit locale: warn, don't drop.
        _validate_explicit_locale(
            dialogue_locale, context=f"dialogue row from {raw.source_path}"
        )
    return DialogueRef(
        id=dialogue_id,
        text_key=text_key,
        speaker_id=_optional_str(raw.data.get("speaker_id") or raw.data.get("speaker")),
        quest_id=_optional_str(raw.data.get("quest_id") or raw.data.get("quest")),
        text=_optional_str(raw.data.get("text")),
        locale=dialogue_locale,
        ui_max_len=_optional_int(raw.data.get("ui_max_len")),
        metadata=_metadata(
            raw.data,
            exclude={
                "kind",
                "object_type",
                "id",
                "text_key",
                "key",
                "speaker_id",
                "speaker",
                "quest_id",
                "quest",
                "text",
                "locale",
                "ui_max_len",
            },
        ),
        source_ref=_source_ref(raw),
    )


def _localized_texts_from_raw(raw: RawObject) -> list[LocalizedText]:
    # Type pre-check on the id (BUG-3/4/5): a non-str id (list/dict/bool/int) must not be
    # silently str()-coerced into a fabricated slug. localized_text ids are always slugged
    # below, so they go through the type guard here rather than the full _resolve_id.
    _assert_id_is_str_or_none(
        raw.data.get("id"), context=f"localized_text row from {raw.source_path}"
    )
    text_key = str(raw.data.get("text_key") or raw.data.get("key") or raw.data.get("id") or "")
    rows: list[LocalizedText] = []
    locale_values: dict[str, Any] = {}
    if raw.data.get("locale") and raw.data.get("text"):
        explicit_locale = str(raw.data["locale"])
        # An explicit locale field is now held to the same ISO 639-1 whitelist as a locale
        # *column* — but as a warning, not a drop (region/case forms like 'zh-CN' must pass).
        _validate_explicit_locale(
            explicit_locale, context=f"localized_text row from {raw.source_path}"
        )
        locale_values[explicit_locale] = raw.data["text"]
    for key, value in raw.data.items():
        if _looks_like_locale(key) and value not in (None, ""):
            locale_values[key] = value
    # A reserved key that is *also* a language code (notably 'id' = Indonesian) is kept as a
    # structural field, never a locale. That is the right call for an ambiguous column — BUT it
    # must not be a *silent* one. When no real locale data exists, an 'id'-only row yields zero
    # translations and the value vanishes without a trace (the red-line case). We surface a
    # warning there, guiding the user to an explicit `locale=` field if it was a real
    # translation. (When other locale data is present, the row still produces rows and the 'id'
    # field plays its normal structural-identifier role, so no warning is needed.)
    if not locale_values:
        for key, value in raw.data.items():
            if key.lower() in _RESERVED_KEYS_THAT_ARE_LOCALES and value not in (None, ""):
                warnings.warn(
                    f"column {key!r} on localized_text row from {raw.source_path} is a valid "
                    f"ISO 639-1 language code but is a reserved structural field, so its value "
                    f"{value!r} was NOT imported as a translation, and this row produced no "
                    f"localized text. If {key!r} was meant as a translation, supply it via an "
                    f"explicit 'locale' field (e.g. locale='{key}').",
                    stacklevel=2,
                )
    for locale, text in sorted(locale_values.items()):
        text_id = str(
            raw.data.get("id")
            if len(locale_values) == 1 and raw.data.get("id")
            else f"{text_key}:{locale}"
        )
        rows.append(
            LocalizedText(
                id=slug_id(text_id, prefix="loc"),
                text_key=text_key,
                locale=locale,
                text=str(text),
                ui_max_len=_optional_int(raw.data.get("ui_max_len")),
                metadata=_metadata(
                    raw.data,
                    exclude={
                        "kind",
                        "object_type",
                        "id",
                        "text_key",
                        "key",
                        "locale",
                        "text",
                        "ui_max_len",
                        *locale_values.keys(),
                    },
                ),
                source_ref=_source_ref(raw),
            )
        )
    return rows


def _term_from_raw(raw: RawObject) -> Term:
    canonical = str(
        raw.data.get("canonical")
        or raw.data.get("preferred")
        or raw.data.get("name")
        or ""
    )
    term_id = _resolve_id(
        raw.data.get("id"),
        slug_id(canonical, prefix="term"),
        context=f"term row from {raw.source_path}",
    )
    if not canonical:
        canonical = term_id
    return Term(
        id=term_id,
        canonical=canonical,
        aliases=_list(raw.data.get("aliases")),
        forbidden=_list(raw.data.get("forbidden")),
        description=str(raw.data.get("description") or raw.data.get("note") or ""),
        source_ref=_source_ref(raw),
    )


def _style_guide_from_raw(raw: RawObject) -> StyleGuide:
    style_id = _resolve_id(
        raw.data.get("id"),
        "style_guide",
        context=f"style_guide row from {raw.source_path}",
    )
    return StyleGuide(
        id=style_id,
        body=str(raw.data.get("body") or raw.data.get("text") or ""),
        rules=_list(raw.data.get("rules")),
        source_ref=_source_ref(raw),
    )


def _relation_from_raw(raw: RawObject, name_to_id: dict[str, str]) -> Relation:
    source = str(raw.data.get("source") or "").strip()
    target = str(raw.data.get("target") or "").strip()
    return Relation(
        source=name_to_id.get(source, source),
        target=name_to_id.get(target, target),
        kind=str(raw.data.get("kind") or "").strip(),
        valid_from=_optional_int(raw.data.get("valid_from")),
        valid_until=_optional_int(raw.data.get("valid_until")),
        metadata=_metadata(
            raw.data,
            exclude={
                "kind",
                "object_type",
                "source",
                "target",
                "valid_from",
                "valid_until",
            },
        ),
        source_ref=_source_ref(raw),
    )


def _source_ref(raw: RawObject) -> SourceRef:
    return SourceRef(path=raw.source_path, line=raw.line, row=raw.row, sheet=raw.sheet)


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    return int(text) if text.lstrip("-").isdigit() else None


def _list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [item.strip() for item in re.split(r"[;,；、|]", value) if item.strip()]
    return [str(value).strip()] if str(value).strip() else []


def _metadata(data: dict[str, Any], *, exclude: set[str]) -> dict[str, Any]:
    return {
        key: value for key, value in data.items() if key not in exclude and value not in (None, "")
    }


# Structural / reserved field names on a localized_text row that must NEVER be mistaken
# for a locale column, even when they happen to be two letters. The worst offender is
# "id" (also the ISO 639-1 code for Indonesian), which previously got silently fabricated
# into a bogus "id" locale row carrying whatever the id field held.
_LOCALE_RESERVED_KEYS = frozenset(
    {
        "id",
        "kind",
        "object_type",
        "type",
        "key",
        "text",
        "note",
        "remark",
        "tag",
        "url",
    }
)

# ISO 639-1 two-letter language subtags. A locale column must be a *known* language code;
# arbitrary two-letter field names (zz, qq, xy, …) are no longer accepted as locales, so a
# typo or stray column can never silently fabricate a translation row.
_ISO_639_1 = frozenset(
    {
        "aa", "ab", "ae", "af", "ak", "am", "an", "ar", "as", "av", "ay", "az",
        "ba", "be", "bg", "bh", "bi", "bm", "bn", "bo", "br", "bs",
        "ca", "ce", "ch", "co", "cr", "cs", "cu", "cv", "cy",
        "da", "de", "dv", "dz",
        "ee", "el", "en", "eo", "es", "et", "eu",
        "fa", "ff", "fi", "fj", "fo", "fr", "fy",
        "ga", "gd", "gl", "gn", "gu", "gv",
        "ha", "he", "hi", "ho", "hr", "ht", "hu", "hy", "hz",
        "ia", "id", "ie", "ig", "ii", "ik", "io", "is", "it", "iu",
        "ja", "jv",
        "ka", "kg", "ki", "kj", "kk", "kl", "km", "kn", "ko", "kr", "ks",
        "ku", "kv", "kw", "ky",
        "la", "lb", "lg", "li", "ln", "lo", "lt", "lu", "lv",
        "mg", "mh", "mi", "mk", "ml", "mn", "mr", "ms", "mt", "my",
        "na", "nb", "nd", "ne", "ng", "nl", "nn", "no", "nr", "nv", "ny",
        "oc", "oj", "om", "or", "os",
        "pa", "pi", "pl", "ps", "pt",
        "qu",
        "rm", "rn", "ro", "ru", "rw",
        "sa", "sc", "sd", "se", "sg", "si", "sk", "sl", "sm", "sn", "so",
        "sq", "sr", "ss", "st", "su", "sv", "sw",
        "ta", "te", "tg", "th", "ti", "tk", "tl", "tn", "to", "tr", "ts",
        "tt", "tw", "ty",
        "ug", "uk", "ur", "uz",
        "ve", "vi", "vo",
        "wa", "wo",
        "xh",
        "yi", "yo",
        "za", "zh", "zu",
    }
)


# Reserved keys that are *also* a valid ISO 639-1 language code — so dropping them as a
# locale column genuinely discards a possible translation. ``id`` is the offender
# (structural row identifier vs. Indonesian). We keep treating these as structural (a column
# literally named ``id`` is overwhelmingly a row id, not Indonesian), but we no longer do it
# *silently*: see :func:`_localized_texts_from_raw`.
_RESERVED_KEYS_THAT_ARE_LOCALES = frozenset(_LOCALE_RESERVED_KEYS & _ISO_639_1)


def _is_known_locale(value: str) -> bool:
    """True if *value* is a recognized locale tag (``en``, ``zh-cn``, ``zh-CN`` …).

    Case-insensitive; accepts an optional region subtag. Shared by both the column
    detector (:func:`_looks_like_locale`) and the explicit-``locale``-field validator so the
    "what counts as a language code" rule lives in exactly one place.
    """
    match = re.match(r"^([a-z]{2})(?:-[a-z]{2})?$", value.strip().lower())
    if not match:
        return False
    return match.group(1) in _ISO_639_1


def _looks_like_locale(key: str) -> bool:
    """True only for a recognized locale tag (``en``, ``zh-cn``, …).

    Two guards close the "any two-letter field name is a locale" hole that silently
    fabricated translation rows from columns like ``id`` or stray typos:
      1. Reserved structural field names (``id``, ``text``, ``key`` …) are never locales.
      2. The language subtag must be a known ISO 639-1 code; arbitrary two letters
         (``zz``, ``qq`` …) are rejected.
    """
    if key.lower() in _LOCALE_RESERVED_KEYS:
        return False
    return _is_known_locale(key)


def _validate_explicit_locale(locale: str, *, context: str) -> None:
    """Warn (do not drop) when an explicit ``locale=`` field is not a known language code.

    The ISO 639-1 whitelist previously only governed *column* detection, so an explicit
    ``locale`` field would store ``zz`` / ``NOTALOCALE`` unchecked — the simplified-brief
    promise that "locale values are known language codes" was only half-true. We surface a
    warning rather than rejecting or silently dropping: region/case forms such as ``zh-CN``
    must still pass, and an unrecognized-but-deliberate value is the user's to keep.
    """
    if not _is_known_locale(locale):
        warnings.warn(
            f"locale {locale!r} is not a recognized ISO 639-1 language code "
            f"(e.g. 'en', 'zh-CN'); it is stored as-is but downstream coverage/locale "
            f"reporting may not recognize it; context: {context}",
            stacklevel=2,
        )


def _event_ref_kind(data: dict[str, Any]) -> QuestEventRefKind:
    raw = str(data.get("ref_kind") or data.get("kind") or "").lower()
    note = str(data.get("note") or data.get("remark") or data.get("备注") or "")
    if raw in {QuestEventRefKind.REFERENCES_RESULT.value, "result", "references_event_result"}:
        return QuestEventRefKind.REFERENCES_RESULT
    if "结果" in note or "剧透" in note or "新盟主" in note:
        return QuestEventRefKind.REFERENCES_RESULT
    return QuestEventRefKind.MENTIONS_EVENT


def _entity_type(raw: RawObject, entity_id: str) -> EntityType:
    raw_type = str(raw.data.get("type") or "").lower().strip()
    if raw_type:
        return EntityType(raw_type)
    lowered = " ".join([entity_id, raw.source_path, raw.sheet or ""]).lower()
    if entity_id.startswith("npc_") or "npc" in lowered:
        return EntityType.NPC
    if entity_id.startswith("fac_") or "faction" in lowered or "阵营" in lowered:
        return EntityType.FACTION
    if entity_id.startswith("evt_") or "event" in lowered or "事件" in lowered:
        return EntityType.EVENT
    if entity_id.startswith("poi_") or "poi" in lowered:
        return EntityType.LOCATION
    return EntityType.CONCEPT


def _derive_relations(bundle: ContentBundle) -> None:
    existing = {(r.source, r.kind, r.target) for r in bundle.relations}
    for entity in bundle.entities.values():
        faction = _optional_str(entity.metadata.get("faction"))
        if faction and (entity.id, "member_of", faction) not in existing:
            bundle.relations.append(
                Relation(
                    source=entity.id,
                    kind="member_of",
                    target=faction,
                    source_ref=entity.source_ref,
                )
            )
            existing.add((entity.id, "member_of", faction))
    for poi in bundle.pois.values():
        if (
            poi.controlling_faction
            and (
                poi.id,
                "controlled_by",
                poi.controlling_faction,
            )
            not in existing
        ):
            bundle.relations.append(
                Relation(
                    source=poi.id,
                    kind="controlled_by",
                    target=poi.controlling_faction,
                    source_ref=poi.source_ref,
                )
            )
            existing.add((poi.id, "controlled_by", poi.controlling_faction))
