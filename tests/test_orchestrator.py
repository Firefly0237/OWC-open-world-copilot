from owcopilot.demo import build_demo_app, seed_worldbible


def test_verify_repair_loop_reaches_consistency():
    wb = seed_worldbible()
    app, tel = build_demo_app(wb)
    final = app.invoke(
        {
            "intent": "Add a caravan quest.",
            "max_repair_attempts": 2,
            "log": [],
        }
    )

    # The generator emits 'Shadowfen' (unknown) -> repair should remap to a known location.
    assert final["artifact"]["location"] in wb.names()
    # After repair, verify must be clean (no error issues remain).
    assert [i for i in final.get("issues", []) if i.severity == "error"] == []
    # At least one repair attempt happened. The model calls are plan + generate; the P0
    # deterministic repair is zero-token (no gateway call), so >= 2 records.
    assert final.get("repair_attempts", 0) >= 1
    assert len(tel.records) >= 2
