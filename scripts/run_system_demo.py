"""Run an offline end-to-end system smoke through the product CLI paths.

The script seeds a small synthetic game-world project and invokes the same ``agent``,
``agent-history``, and ``multi-agent`` commands used by normal workflows. The deterministic offline
model keeps the run self-contained while tools, storage, MCP, checkpoints, memory, and multi-agent
orchestration use the application implementations.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import tempfile
import uuid
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from owcopilot.cli.main import main  # noqa: E402
from owcopilot.content.models import ContentBundle, Entity, EntityType, Quest  # noqa: E402
from owcopilot.content.store import ContentStore  # noqa: E402
from owcopilot.inspiration import (  # noqa: E402
    Provenance,
    ReferenceContextBuilder,
    ReferenceStore,
)
from owcopilot.intake import (  # noqa: E402
    PDF_MIME,
    IntakePolicy,
    PdfTextLineLocator,
    parse_source_segments,
    resolve_source_locator,
    validate_upload_chunks,
)
from owcopilot.llm.cache import HashingEmbedder  # noqa: E402
from owcopilot.retrieval import ChunkingPolicy  # noqa: E402
from owcopilot.storage import SQLiteStore  # noqa: E402


def _invoke(arguments: list[str]) -> tuple[dict[str, Any], str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        exit_code = main(arguments)
    raw_stdout = stdout.getvalue().strip()
    raw_stderr = stderr.getvalue().strip()
    if exit_code != 0:
        raise RuntimeError(
            f"CLI failed with exit_code={exit_code}: {arguments[0]}\n"
            f"stdout={raw_stdout}\nstderr={raw_stderr}"
        )
    try:
        payload = json.loads(raw_stdout.splitlines()[-1])
    except (IndexError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"CLI did not emit a JSON result: {raw_stdout!r}") from exc
    return payload, raw_stderr


def _seed_project(content_root: Path) -> None:
    bundle = ContentBundle(
        entities={
            "npc_mara": Entity(
                id="npc_mara",
                name="Mara",
                type=EntityType.NPC,
                description="A border scout who reports to the northern watch.",
            )
        },
        quests={
            "quest_patrol": Quest(
                id="quest_patrol",
                title="Patrol the Border",
                giver_npc="npc_missing_captain",
                objective="Inspect the northern markers before dusk.",
            )
        },
    )
    ContentStore(content_root).save(bundle)


def _pdf_literal(text: str) -> bytes:
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    return escaped.encode("ascii")


def _pdf_bytes(pages: tuple[tuple[str, ...], ...]) -> bytes:
    """Build a tiny born-digital PDF without importing a test fixture."""
    width, height = 612, 792
    page_ids = tuple(3 + index * 2 for index in range(len(pages)))
    content_ids = tuple(value + 1 for value in page_ids)
    font_id = 3 + len(pages) * 2
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        (
            b"<< /Type /Pages /Kids ["
            + b" ".join(f"{value} 0 R".encode("ascii") for value in page_ids)
            + f"] /Count {len(pages)} >>".encode("ascii")
        ),
    ]
    for content_id, lines in zip(content_ids, pages, strict=True):
        commands = [b"BT", b"/F1 12 Tf", f"72 {height - 72} Td".encode("ascii")]
        for line_index, line in enumerate(lines):
            if line_index:
                commands.append(b"0 -24 Td")
            commands.append(b"(" + _pdf_literal(line) + b") Tj")
        commands.append(b"ET")
        stream = b"\n".join(commands) + b"\n"
        page = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {width} {height}] ".encode(
                "ascii"
            )
            + f"/Resources << /Font << /F1 {font_id} 0 R >> >> ".encode("ascii")
            + f"/Contents {content_id} 0 R >>".encode("ascii")
        )
        objects.extend(
            [
                page,
                b"<< /Length "
                + str(len(stream)).encode("ascii")
                + b" >>\nstream\n"
                + stream
                + b"endstream",
            ]
        )
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for object_id, payload in enumerate(objects, start=1):
        offsets.append(len(output))
        output.extend(f"{object_id} 0 obj\n".encode("ascii"))
        output.extend(payload)
        output.extend(b"\nendobj\n")
    xref_offset = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    output.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF\n"
        ).encode("ascii")
    )
    return bytes(output)


def _run_pdf_rag_demo(*, run_dir: Path, content_root: Path, runtime_path: Path) -> dict[str, Any]:
    pdf_path = run_dir / "quest_reference.pdf"
    pdf_bytes = _pdf_bytes(
        (
            ("Quest condition: recover the sigil.", "Return it to the archivist."),
            ("Completion unlocks the north gate.",),
        )
    )
    pdf_path.write_bytes(pdf_bytes)
    upload = validate_upload_chunks(
        (pdf_bytes[:17], pdf_bytes[17:]),
        original_filename=pdf_path.name,
        mime_type=PDF_MIME,
        policy=IntakePolicy(),
    )
    artifact_version_id = str(uuid.uuid4())
    segments = parse_source_segments(upload, artifact_version_id=artifact_version_id)
    reference_store = ReferenceStore(content_root)
    added = reference_store.add_source_segments(
        title="Quest PDF reference",
        segments=segments,
        policy=ChunkingPolicy(
            version="system-demo-structure-v1",
            strategy="structure",
            min_tokens=1,
            target_tokens=24,
            max_tokens=48,
            overlap_tokens=0,
        ),
        original_filename=pdf_path.name,
        provenance=Provenance.OWN_IP,
        token_counter=lambda text: len(text.split()),
        token_counter_id="demo-word-count-v1",
    )
    sidecar = reference_store.load_retrieval_document(added.source.id)
    if sidecar is None:
        raise AssertionError("PDF RetrievalDocument sidecar was not persisted")

    sqlite_store = SQLiteStore(runtime_path)
    try:
        reference_store.sync_index(sqlite_store)
        pack = ReferenceContextBuilder(sqlite_store, embedder=HashingEmbedder()).build(
            "north gate", limit=3
        )
    finally:
        sqlite_store.close()
    if not pack.hits:
        raise AssertionError("PDF reference retrieval returned no hits")
    hit = pack.hits[0]
    locators = [
        PdfTextLineLocator.model_validate(item)
        for item in json.loads(str(hit.metadata["locators_json"]))
    ]
    resolved = [resolve_source_locator(upload, locator) for locator in locators]
    expected = [segment.text for segment in segments]
    if resolved != expected:
        raise AssertionError(f"PDF locator round trip drifted: {resolved!r} != {expected!r}")

    return {
        "status": "passed",
        "original_pdf": str(pdf_path),
        "artifact_version_id": artifact_version_id,
        "reference_source_id": added.source.id,
        "segment_count": len(segments),
        "retrieval_unit_count": len(sidecar.units),
        "chunk_policy_version": hit.metadata["policy_version"],
        "chunk_strategy": hit.metadata["chunk_strategy"],
        "query": pack.query,
        "hit_ref": hit.ref,
        "locator_pages": sorted({locator.page_index + 1 for locator in locators}),
        "resolved_citation_text": resolved,
        "citation_round_trip": True,
        "retrieval_profile": (
            "BM25 plus HashingEmbedder powers the offline smoke; bge-m3 and the neural "
            "reranker are exercised by the retrieval benchmark."
        ),
    }


def _assert_demo(
    agent: dict[str, Any],
    memory: dict[str, Any],
    multi: dict[str, Any],
    pdf_rag: dict[str, Any],
) -> None:
    if agent.get("status") != "completed" or agent.get("stop_reason") != "finished":
        raise AssertionError("single-agent run did not complete normally")
    actions = [step.get("action") for step in agent.get("steps", [])]
    if "audit_project" not in actions or "build_context_pack" not in actions:
        raise AssertionError(f"single-agent tool chain is incomplete: {actions}")
    if memory.get("count", 0) < 1:
        raise AssertionError("the completed agent run was not visible through agent-history")

    participants = set(multi.get("participants", []))
    required_participants = {"orchestrator", "diag_01", "repair_01", "verifier_01"}
    if not required_participants.issubset(participants):
        raise AssertionError(f"multi-agent participants are incomplete: {sorted(participants)}")
    flow_types = {row.get("msg_type") for row in multi.get("blackboard_flow", [])}
    required_flow = {
        "task_assign",
        "task_result",
        "verify_request",
        "verify_result",
        "synthesize",
    }
    if not required_flow.issubset(flow_types):
        raise AssertionError(f"blackboard protocol is incomplete: {sorted(flow_types)}")
    if multi.get("blackboard_storage") != "persistent_sqlite":
        raise AssertionError("multi-agent protocol was not persisted")
    if not multi.get("verifier_verdicts"):
        raise AssertionError("independent verifier produced no verdict")
    if pdf_rag.get("status") != "passed" or not pdf_rag.get("citation_round_trip"):
        raise AssertionError("PDF-to-RAG locator/citation chain did not pass")


def run_demo(*, artifact_root: Path, transport: str) -> dict[str, Any]:
    run_stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_dir = artifact_root.resolve() / f"run-{run_stamp}-{uuid.uuid4().hex[:8]}"
    content_root = run_dir / "content"
    runtime_path = run_dir / "runtime.sqlite"
    checkpoint_path = run_dir / "checkpoints.sqlite"
    run_dir.mkdir(parents=True, exist_ok=False)
    _seed_project(content_root)

    # Enable the deterministic offline model for this self-contained system run.
    os.environ["OWCOPILOT_ALLOW_OFFLINE_LLM"] = "1"

    shared_project_args = [
        "--content-root",
        str(content_root),
        "--sqlite-path",
        str(runtime_path),
    ]
    agent, agent_stderr = _invoke(
        [
            "agent",
            *shared_project_args,
            "--goal",
            "Audit this world, ground the findings, and prepare it for review.",
            "--llm-mode",
            "offline",
            "--tool-transport",
            transport,
            "--checkpoint-path",
            str(checkpoint_path),
        ]
    )
    memory, memory_stderr = _invoke(["agent-history", *shared_project_args, "--limit", "5"])
    multi, multi_stderr = _invoke(
        [
            "multi-agent",
            *shared_project_args,
            "--goal",
            "Diagnose the broken quest, propose a safe repair, and verify the result.",
            "--llm-mode",
            "offline",
            "--checkpoint-path",
            str(checkpoint_path),
        ]
    )
    pdf_rag = _run_pdf_rag_demo(
        run_dir=run_dir,
        content_root=content_root,
        runtime_path=runtime_path,
    )
    _assert_demo(agent, memory, multi, pdf_rag)

    summary = {
        "demo_status": "passed",
        "run_dir": str(run_dir),
        "model_mode": "offline_deterministic",
        "tool_transport": transport,
        "single_agent": {
            "status": agent["status"],
            "stop_reason": agent["stop_reason"],
            "actions": [step["action"] for step in agent["steps"]],
            "thread_id": agent["thread_id"],
            "telemetry_calls": agent["telemetry"]["calls"],
            "mcp_traces": agent.get("mcp_traces", []),
        },
        "memory": {"persisted_run_count": memory["count"]},
        "multi_agent": {
            "session_id": multi["session_id"],
            "participants": multi["participants"],
            "blackboard_message_types": sorted(
                {row["msg_type"] for row in multi["blackboard_flow"]}
            ),
            "verifier_verdicts": multi["verifier_verdicts"],
            "decomposition_degraded": multi["decomposition_degraded"],
            "budget_scope": multi["budget_scope"],
            "telemetry": multi["telemetry"],
        },
        "pdf_rag": pdf_rag,
        "warnings": [
            message
            for message in (agent_stderr, memory_stderr, multi_stderr)
            if message
        ],
        "run_profile": (
            "Offline system smoke covering CLI, tools, storage, checkpoints, memory, MCP, "
            "multi-agent coordination, and the PDF-to-RAG citation path with a deterministic "
            "model."

        ),
    }
    (run_dir / "demo_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--artifact-root",
        type=Path,
        default=Path(tempfile.gettempdir()) / "owcopilot-system-demo",
        help="Parent directory for retained run artifacts (a unique child is created).",
    )
    parser.add_argument(
        "--transport",
        choices=("native", "mcp"),
        default="native",
        help="How audit_project crosses the tool boundary; mcp requires the optional MCP SDK.",
    )
    return parser.parse_args()


def main_entry() -> int:
    args = parse_args()
    summary = run_demo(artifact_root=args.artifact_root, transport=args.transport)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main_entry())
