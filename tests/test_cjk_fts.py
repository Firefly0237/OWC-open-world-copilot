"""CJK bigram FTS (scale-p0 #3): Chinese queries hit the FTS index instead of the O(N) fallback.

unicode61 treats a consecutive CJK run as ONE token, so Chinese queries used to miss content_fts
entirely and every search paid the linear fallback scan. Indexing and matching through the same
bigram rewrite makes 2+ char Chinese queries index-served; the residual fallback is bounded; and
the display text always comes from the authoritative tables (never the bigram-rewritten columns).
"""

from __future__ import annotations

from owcopilot.content.models import ContentBundle, Entity, EntityType
from owcopilot.retrieval.bm25 import BM25Retriever
from owcopilot.storage import SQLiteStore
from owcopilot.storage.sqlite import build_fts_match_query, cjk_fts_text


def _bundle() -> ContentBundle:
    return ContentBundle(
        entities={
            "fac_mist": Entity(
                id="fac_mist",
                name="雾隐盟",
                type=EntityType.FACTION,
                description="雾隐盟控制着北境的商路与渡口。",
            ),
            "npc_aldric": Entity(
                id="npc_aldric",
                name="Aldric",
                type=EntityType.NPC,
                description="Caravan master of the border road.",
            ),
        }
    )


# --------------------------------------------------------------------------- the rewrite itself
def test_cjk_fts_text_bigram_rewrite() -> None:
    assert cjk_fts_text("雾隐盟") == "雾隐 隐盟"
    assert cjk_fts_text("雾隐盟的商路") == "雾隐 隐盟 盟的 的商 商路"
    assert cjk_fts_text("雾") == "雾"  # single char: not bigram-indexable, left as-is
    assert cjk_fts_text("caravan road") == "caravan road"  # Latin untouched
    assert cjk_fts_text("北境 road 商路") == "北境 road 商路"  # 2-char runs bigram to themselves


def test_match_query_expands_cjk_to_bigram_phrase() -> None:
    # A CJK token becomes its bigram PHRASE — adjacent bigrams reassemble the original string,
    # so the phrase is an exact substring match against bigram-indexed rows.
    assert build_fts_match_query("雾隐盟") == '"雾隐 隐盟"'
    assert build_fts_match_query("雾隐盟 caravan") == '"雾隐 隐盟" OR "caravan"'


# --------------------------------------------------------------------------- index-served search
def test_chinese_query_is_served_by_the_fts_index(monkeypatch) -> None:
    store = SQLiteStore()
    try:
        store.replace_content_index(_bundle())
        retriever = BM25Retriever(store)

        def _no_fallback(query, *, limit):
            raise AssertionError("a 2+ char Chinese query must be served by FTS, not the fallback")

        monkeypatch.setattr(retriever, "_fallback_search", _no_fallback)
        hits = retriever.search("雾隐盟", limit=1)
        assert hits and hits[0].ref == "entity:fac_mist"
        # Display text is the authoritative original, never the bigram index rewrite.
        assert "雾隐盟" in hits[0].title or "雾隐盟" in hits[0].body
        assert "雾隐 隐盟" not in hits[0].body
    finally:
        store.close()


def test_single_char_cjk_is_a_documented_gap() -> None:
    """Honest limitation (pre-existing, not introduced here): the lexical matcher tokenises CJK at
    2+ chars, so a single-character query produces no term and matches nothing on either path. The
    2-char form of the same query is served normally — the gap is only the 1-char case."""
    store = SQLiteStore()
    try:
        store.replace_content_index(_bundle())
        retriever = BM25Retriever(store)
        assert retriever.search("雾", limit=5) == []  # single char: no matchable term
        assert any(hit.ref == "entity:fac_mist" for hit in retriever.search("雾隐", limit=5))
    finally:
        store.close()


def test_fallback_scan_is_bounded(monkeypatch) -> None:
    """The scan cap bounds the fallback: a match that sorts *after* the cap boundary is excluded,
    proving the LIMIT actually bites (the deliberate latency-ceiling-over-tail-recall trade)."""
    store = SQLiteStore()
    try:
        # A non-matching decoy that sorts BEFORE the only real match (entity:zzz_mist).
        store.replace_content_index(
            ContentBundle(
                entities={
                    "aaa_decoy": Entity(
                        id="aaa_decoy", name="Decoy", type=EntityType.NPC, description="无关内容。"
                    ),
                    "zzz_mist": Entity(
                        id="zzz_mist",
                        name="雾隐盟",
                        type=EntityType.FACTION,
                        description="雾隐盟控制北境。",
                    ),
                }
            )
        )
        retriever = BM25Retriever(store)
        # Unbounded: the fallback scans both rows and finds the match.
        assert [h.ref for h in retriever._fallback_search("雾隐", limit=5)] == ["entity:zzz_mist"]
        # Capped to 1 row: only the first ref (aaa_decoy, non-matching) is scanned → match excluded.
        monkeypatch.setattr(BM25Retriever, "_FALLBACK_SCAN_CAP", 1)
        assert retriever._fallback_search("雾隐", limit=5) == []
    finally:
        store.close()


# --------------------------------------------------------------------------- legacy migration
def test_legacy_raw_token_db_is_rebuilt_to_bigram_fts(tmp_path, monkeypatch) -> None:
    """A DB indexed before the bigram rewrite (whole-run CJK tokens, user_version=0) is rebuilt
    once on open — Chinese queries hit the FTS index right after the upgrade."""
    path = str(tmp_path / "runtime.sqlite")
    store = SQLiteStore(path)
    store.replace_content_index(_bundle())
    # Devolve to the pre-bigram generation: raw-token FTS rows + generation stamp 0.
    store.conn.execute("DELETE FROM content_fts")
    store.conn.execute(
        "INSERT INTO content_fts (ref, object_type, world_id, version, title, body) "
        "SELECT ref, object_type, world_id, version, title, body FROM content_index"
    )
    store.conn.execute("PRAGMA user_version = 0")
    store.conn.commit()
    store.close()

    reopened = SQLiteStore(path)
    try:
        assert int(reopened.conn.execute("PRAGMA user_version").fetchone()[0]) == 1
        retriever = BM25Retriever(reopened)

        def _no_fallback(query, *, limit):
            raise AssertionError("after the migration the FTS index must serve Chinese queries")

        monkeypatch.setattr(retriever, "_fallback_search", _no_fallback)
        hits = retriever.search("雾隐盟", limit=1)
        assert hits and hits[0].ref == "entity:fac_mist"
    finally:
        reopened.close()
