"""R4 chaos probe: bypass + over-rejection attacks on normalize.py unified id entry.

Goal: (a) can a malformed id/locale/content slip past _resolve_id silently?
      (b) is a LEGAL but uncommon input wrongly rejected by the new validators?
"""
import sys
sys.path.insert(0, r"F:\openworld\src")

from owcopilot.content.importers.base import RawObject
from owcopilot.content.normalize import normalize_raw_objects, _looks_like_locale


def ro(kind, data):
    return RawObject(kind=kind, data=data, source_path="<chaos>", line=1)


def probe(label, raw_objs, expect):
    try:
        bundle = normalize_raw_objects(raw_objs)
        ids = {}
        for attr in ("entities", "quests", "quest_event_refs", "regions", "pois",
                     "dialogues", "localized_texts", "terms", "style_guides"):
            v = list(getattr(bundle, attr))
            if v:
                ids[attr] = v
        verdict = "ACCEPTED"
        flag = "  <<<< UNEXPECTED ACCEPT" if expect == "reject" else ""
        print(f"[{verdict}] {label}{flag}")
        for k, v in ids.items():
            for i in v:
                print(f"        {k}: {i!r}")
    except Exception as e:
        flag = "  <<<< UNEXPECTED REJECT (over-rejection?)" if expect == "accept" else ""
        print(f"[REJECTED] {label}{flag}")
        print(f"        {type(e).__name__}: {str(e)[:160]}")


print("=" * 80)
print("PART A: BYPASS ATTACKS (these SHOULD be rejected)")
print("=" * 80)

# Synthetic-separator abuse: explicit id with colon on quest_event_ref?
probe("A1 qer explicit id WITH colon (should still be strict-rejected)",
      [ro("quest_event_ref", {"id": "evil:colon", "quest_id": "q1", "event_id": "e1"})], "reject")

# Can quest_id/event_id inject extra colons into synthetic id, then that id used as dict key?
probe("A2 qer quest_id with colon -> synthetic id 'a:b:c:...:result'",
      [ro("quest_event_ref", {"quest_id": "a:b", "event_id": "c:d"})], "accept")  # synthetic ok-ish, inspect

# localized_text: locale field that is a path-ish string
probe("A3 localized_text locale='../x' (becomes locale value, slug id)",
      [ro("localized_text", {"text_key": "k", "locale": "../x", "text": "hi"})], "accept")

# localized_text: a 2-letter column that is NOT iso-639-1 should NOT fabricate a row
probe("A4 localized_text stray col 'zz' should NOT become a locale row",
      [ro("localized_text", {"text_key": "k", "zz": "ghost"})], "accept")

# entity id with fullwidth solidus U+FF0F (looks like / but not in forbidden set)
probe("A5 entity id fullwidth-slash U+FF0F (could be a path sep visually)",
      [ro("entity", {"id": "a／b", "name": "X", "type": "npc"})], "accept")

# entity id with unicode '..' lookalike or trailing dot via fullwidth period U+FF0E
probe("A6 entity id fullwidth-dot-dot U+FF0E x2",
      [ro("entity", {"id": "a．．b", "name": "X", "type": "npc"})], "accept")

# entity id with a literal newline (control char \n = ord 10)
probe("A7 entity id with newline",
      [ro("entity", {"id": "a\nb", "name": "X", "type": "npc"})], "reject")

# entity id with DEL (0x7f) - NOT < 32, so passes control check
probe("A8 entity id with DEL 0x7f",
      [ro("entity", {"id": "a\x7fb", "name": "X", "type": "npc"})], "accept")

# entity id that is only a dot-segment after strip -> '.' is forbidden, but '...'?
probe("A9 entity id = '...' (forbidden '.' + '..')",
      [ro("entity", {"id": "...", "name": "X", "type": "npc"})], "reject")

print()
print("=" * 80)
print("PART B: OVER-REJECTION ATTACKS (these SHOULD be accepted)")
print("=" * 80)

# Legal but rare ISO-639-1 locale codes
for loc in ["zh", "en", "ja", "ko", "vi", "th", "cy", "ga", "eu", "fo", "kl", "ie", "ia", "io", "vo"]:
    ok = _looks_like_locale(loc)
    print(f"  _looks_like_locale({loc!r}) = {ok}" + ("" if ok else "  <<<< rejects a VALID iso-639-1 code"))

# Region/territory subtag forms
for loc in ["zh-cn", "zh-tw", "en-us", "pt-br", "en-gb"]:
    ok = _looks_like_locale(loc)
    print(f"  _looks_like_locale({loc!r}) = {ok}" + ("" if ok else "  <<<< rejects a VALID locale-region"))

# 3-letter ISO 639-3 only codes (no 2-letter form) e.g. 'yue' (Cantonese), 'nan'
for loc in ["yue", "nan", "fil", "haw"]:
    ok = _looks_like_locale(loc)
    print(f"  _looks_like_locale({loc!r}) = {ok}  (iso-639-3 only; no 2-letter form)")

# Uppercase / mixed-case region BCP-47 standard form
for loc in ["zh-CN", "en-US", "ZH"]:
    ok = _looks_like_locale(loc)
    print(f"  _looks_like_locale({loc!r}) = {ok}")

print()

# id exactly at boundary lengths
probe("B1 id length 256 (exactly max, should pass)",
      [ro("entity", {"id": "a" * 256, "name": "X", "type": "npc"})], "accept")
probe("B2 id length 257 (over max, should reject)",
      [ro("entity", {"id": "a" * 257, "name": "X", "type": "npc"})], "reject")

# Legal fullwidth content in name/description (not id) - must NOT be rejected
probe("B3 entity name fullwidth chars (legal content)",
      [ro("entity", {"id": "npc_a", "name": "ＡＢＣ", "type": "npc"})], "accept")

# Legal CJK id (non-ascii) - is it allowed? slug fallback would strip it, but explicit?
probe("B4 entity explicit CJK id (e.g. chinese chars in id)",
      [ro("entity", {"id": "赵云", "name": "赵云", "type": "npc"})], "accept")

# Legal locale 'id' (Indonesian!) supplied as actual locale field, not a column
probe("B5 localized_text locale='id' (Indonesian - legal ISO 639-1)",
      [ro("localized_text", {"text_key": "k", "locale": "id", "text": "halo"})], "accept")

# A localized_text row that legitimately uses column-style 'id' as Indonesian translation
probe("B6 localized_text column 'id'=Indonesian translation (RESERVED -> dropped!)",
      [ro("localized_text", {"text_key": "k", "en": "hello", "id": "halo"})], "accept")

print("\nDONE")
