"""The PLAN-EXECUTE-VERIFY-REPAIR loop reaches consistency on a deliberately-inconsistent draft.

Builds the orchestrator graph directly (mock generator + reference validator + deterministic
repair) — the engine-landing step was removed, so the loop ends at a verified artifact.
"""

from owcopilot.consistency.repair import RepairStrategy
from owcopilot.consistency.validators import ReferenceValidator
from owcopilot.core.orchestrator import build_graph
from owcopilot.demo import seed_worldbible
from owcopilot.generation.quest import MockQuestGenerator
from owcopilot.llm.gateway import LLMGateway, MockProvider
from owcopilot.llm.router import StaticRouter
from owcopilot.llm.telemetry import TelemetryCollector


def test_verify_repair_loop_reaches_consistency():
    wb = seed_worldbible()
    tel = TelemetryCollector()
    gateway = LLMGateway(
        providers={"cheap": MockProvider(), "frontier": MockProvider()},
        router=StaticRouter(),
        telemetry=tel,
    )
    app = build_graph(
        gateway=gateway,
        generator=MockQuestGenerator(gateway),
        validators=[ReferenceValidator(wb)],
        repair_strategy=RepairStrategy(wb),
    )
    final = app.invoke({"intent": "Add a caravan quest.", "max_repair_attempts": 2, "log": []})

    # The mock generator emits 'Shadowfen' (unknown) -> repair should remap to a known location.
    assert final["artifact"]["location"] in wb.names()
    # After repair, verify must be clean (no error issues remain).
    assert [i for i in final.get("issues", []) if i.severity == "error"] == []
    # At least one repair attempt happened; plan + generate are the model calls (deterministic
    # repair is zero-token, no gateway call), so >= 2 telemetry records.
    assert final.get("repair_attempts", 0) >= 1
    assert len(tel.records) >= 2
