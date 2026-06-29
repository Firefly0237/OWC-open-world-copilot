"""End-to-end via MultiAgentSession with the REAL offline ReAct provider + real audit registry."""
import tempfile
from pathlib import Path
from owcopilot.content.models import ContentBundle, Entity, EntityType, Quest
from owcopilot.content.store import ContentStore
from owcopilot.core.skills import default_skill_registry
from owcopilot.llm.gateway import LLMGateway
from owcopilot.llm.telemetry import TelemetryCollector
from owcopilot.agent.offline import OfflineReactProvider
from owcopilot.multi_agent import MultiAgentSession
from owcopilot.multi_agent.messages import TaskResultPayload, VerifyResultPayload

# Seed a world with 4 dangling-ref errors
tmp = Path(tempfile.mkdtemp()) / "content"
quests = {f"q{i}": Quest(id=f"q{i}", title=f"Q{i}", giver_npc=f"npc_missing_{i}", objective="do x") for i in range(4)}
bundle = ContentBundle(
    entities={"npc_a": Entity(id="npc_a", name="A", type=EntityType.NPC, description="x")},
    quests=quests,
)
ContentStore(tmp).save(bundle)

registry = default_skill_registry(content_root=str(tmp))
prov = OfflineReactProvider()
gw = LLMGateway({"cheap": prov, "frontier": prov}, telemetry=TelemetryCollector())

sess = MultiAgentSession(gateway=gw, registry=registry)
report = sess.run("clean up the world")

print("=== Worker summaries ===")
for ws in report.worker_summaries:
    print(f"  {ws['agent_id']:10s} role={ws['role']:16s} open_errors(field)={ws['open_errors']} stop={ws['stop_reason']}")
print("=== Verifier verdicts ===")
for vv in report.verifier_verdicts:
    print(f"  verdict={vv['verdict']:10s} audit_found={vv['open_errors_verified']}")
    print(f"    {vv['rationale']}")
