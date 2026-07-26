"""G2 acceptance fix: content_vectors / reference_vectors blob caches now include the scope in
their primary key, so the same ref in two scopes keeps two independent cached fp32 blobs instead
of colliding on ON CONFLICT (which would re-stamp/overwrite and thrash the incremental embedding
cache on a shared DB / copy-on-write sub-version). The C1 isolation tests only covered
content_index; this closes that blind spot for the two vector tables.
"""

from __future__ import annotations

import pytest

from owcopilot.storage import SQLiteStore


@pytest.mark.parametrize("table", ["content_vectors", "reference_vectors"])
def test_vector_blob_cache_isolates_same_ref_across_scopes(tmp_path, table: str) -> None:
    db = str(tmp_path / "runtime.sqlite")  # shared, persistent DB across scopes

    a = SQLiteStore(db, world_id="default", version="v1")
    try:
        a.upsert_vectors("m", [("ref1", "hash-default", 4, b"AAAAAAAAAAAAAAAA")], table=table)
    finally:
        a.close()

    b = SQLiteStore(db, world_id="w2", version="v3")
    try:
        # same ref, different scope -> must NOT overwrite default's row (pre-fix it would)
        b.upsert_vectors("m", [("ref1", "hash-w2", 4, b"BBBBBBBBBBBBBBBB")], table=table)
        assert b.get_vectors("m", table=table)["ref1"][0] == "hash-w2"  # w2 sees its own blob
    finally:
        b.close()

    c = SQLiteStore(db, world_id="default", version="v1")
    try:
        # default scope's blob survived the w2 same-ref upsert (independent cache entry)
        assert c.get_vectors("m", table=table)["ref1"][0] == "hash-default"
    finally:
        c.close()
