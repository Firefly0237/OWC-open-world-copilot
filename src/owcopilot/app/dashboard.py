"""Streamlit dashboard. Run: streamlit run src/owcopilot/app/dashboard.py"""

from __future__ import annotations

import streamlit as st

from owcopilot.app.actions import run_project_audit_action, run_project_export_action
from owcopilot.app.view_models import (
    build_context_pack_preview,
    build_export_summary,
    build_issue_summary,
    build_project_overview,
)
from owcopilot.exporters import EngineTarget


def main() -> None:
    st.set_page_config(page_title="Open-World Copilot", layout="wide")
    st.title("Open-World Copilot")
    st.caption("v2 workflow: INGEST -> INDEX -> AUDIT -> SUGGEST -> REVIEW -> EXPORT")

    workbench_tab, demo_tab, bench_tab = st.tabs(
        ["Project Workbench", "Legacy P0 demo", "Legacy cost benchmark"]
    )
    with workbench_tab:
        _render_project_workbench()
    with demo_tab:
        _render_legacy_demo()
    with bench_tab:
        _render_legacy_benchmark()


def _render_project_workbench() -> None:
    st.subheader("Project Workbench")
    st.caption("Use a v2 content root. Streamlit is only the shell; actions reuse pipeline code.")

    content_root = st.text_input("Content root", value="content")
    sqlite_path = st.text_input("SQLite path override", value="")
    sqlite_override = sqlite_path.strip() or None

    overview_col, issue_col = st.columns(2)
    if st.button("Load project overview"):
        try:
            overview = build_project_overview(content_root, sqlite_path=sqlite_override)
            issue_summary = build_issue_summary(content_root, sqlite_path=sqlite_override)
            with overview_col:
                st.markdown("### Content")
                _render_count_metrics(overview["counts"])
                st.metric("Graph nodes", overview["graph"]["nodes"])
                st.metric("Graph edges", overview["graph"]["edges"])
                st.caption(f"content_hash={overview['content_hash']}")
            with issue_col:
                st.markdown("### Issues")
                st.metric("Persisted issues", issue_summary["count"])
                st.json(issue_summary)
        except Exception as e:
            st.error(str(e))

    st.divider()
    audit_col, context_col, export_col = st.columns(3)
    with audit_col:
        st.markdown("### Audit")
        persist = st.checkbox("Persist audit run", value=True)
        if st.button("Run audit"):
            try:
                audit = run_project_audit_action(
                    content_root,
                    sqlite_path=sqlite_override,
                    persist=persist,
                )
                if audit["open_errors"]:
                    st.error(f"{audit['open_errors']} open error(s)")
                else:
                    st.success("No open errors")
                st.caption(f"cost used=${audit['cost_budget']['used_usd']:.6f}")
                st.json(audit)
            except Exception as e:
                st.error(str(e))

    with context_col:
        st.markdown("### Context Pack")
        query = st.text_input("Lore query", value="Aldric caravan")
        budget = st.number_input("Budget tokens", min_value=1, max_value=8000, value=800, step=100)
        if st.button("Build context pack"):
            try:
                preview = build_context_pack_preview(
                    content_root,
                    query=query,
                    sqlite_path=sqlite_override,
                    budget_tokens=int(budget),
                )
                st.write(preview["refs"])
                st.caption(f"cost used=${preview['cost_budget']['used_usd']:.6f}")
                st.json(preview["hits"])
            except Exception as e:
                st.error(str(e))

    with export_col:
        st.markdown("### Export")
        output_dir = st.text_input("Export output root", value="exports")
        target_engine = st.selectbox(
            "Target engine",
            [target.value for target in EngineTarget],
            index=0,
        )
        if st.button("Export bundle"):
            try:
                export = run_project_export_action(
                    content_root,
                    output_dir=output_dir,
                    target_engine=target_engine,
                    sqlite_path=sqlite_override,
                )
                st.success(f"Exported to {export['output_dir']}")
                st.caption(f"cost used=${export['cost_budget']['used_usd']:.6f}")
                st.json(export["manifest"])
            except Exception as e:
                st.error(str(e))
        try:
            st.caption("Latest export manifest")
            st.json(build_export_summary(output_dir=output_dir, target_engine=target_engine))
        except Exception as e:
            st.warning(str(e))


def _render_count_metrics(counts: dict[str, int]) -> None:
    cols = st.columns(4)
    for index, (name, value) in enumerate(counts.items()):
        cols[index % len(cols)].metric(name.replace("_", " ").title(), value)


def _render_legacy_demo() -> None:
    from owcopilot.demo import build_demo_app, seed_worldbible

    st.subheader("Legacy single-task run")
    st.caption("Kept for compatibility. New project work should use the Project Workbench tab.")
    intent = st.text_input(
        "Intent",
        "Add a quest about a missing supply caravan near the northern road.",
        key="legacy_intent",
    )
    if st.button("Run legacy task"):
        wb = seed_worldbible()
        app, telemetry = build_demo_app(wb)
        final = app.invoke({"intent": intent, "max_repair_attempts": 2, "log": []})
        col1, col2 = st.columns(2)
        with col1:
            st.subheader("Trace")
            for line in final.get("log", []):
                st.write("-", line)
            st.subheader("Artifact")
            st.json(final.get("artifact") or {})
        with col2:
            st.subheader("Cost telemetry")
            st.json(telemetry.summary())


def _render_legacy_benchmark() -> None:
    from owcopilot.benchmark import OFF, ON, BenchmarkConfig, compare, run_benchmark

    st.subheader("Legacy before/after benchmark")
    st.caption("Offline by default. Real model mode uses provider credentials and may cost money.")
    use_real = st.toggle("Use real model", value=False)
    if st.button("Run legacy benchmark"):
        if use_real:
            off_cfg = BenchmarkConfig(
                "OFF (real)",
                cache="off",
                router="static",
                prefix_mode="retrieval",
                use_real_model=True,
            )
            on_cfg = BenchmarkConfig(
                "ON (real)",
                cache="exact+semantic",
                router="cascade",
                prefix_mode="retrieval",
                use_real_model=True,
            )
        else:
            off_cfg, on_cfg = OFF, ON

        before = run_benchmark(config=off_cfg)
        after = run_benchmark(config=on_cfg)
        diff = compare(before, after)
        st.metric("Cost reduction", f"{diff['total_cost_usd']['reduction_pct']:.0f}%")
        st.json({"before": before.as_dict(), "after": after.as_dict(), "diff": diff})


if __name__ == "__main__":
    main()
