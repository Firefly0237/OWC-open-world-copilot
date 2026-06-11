"""OWCopilot Workbench — the game-world content workbench UI.

A thin Streamlit shell over `app.actions` / `app.view_models`; no business logic lives here.
Launch with:

    streamlit run src/owcopilot/app/dashboard.py

Design notes: dark "ink & bronze" palette (see .streamlit/config.toml) with serif display
headers — the look borrows from worldbuilding tools (articy/World Anvil) and modern dark
dashboards (Linear/shadcn) while staying inside Streamlit's theming surface. Every page shows
the cost of what it just did; the review queue is the only place AI content becomes real.
"""

from __future__ import annotations

import json
from typing import Any

import streamlit as st

from owcopilot.app.actions import (
    decide_review_action,
    list_patches_action,
    list_project_issues_action,
    list_review_items_action,
    run_apply_action,
    run_ask_action,
    run_barks_action,
    run_draft_action,
    run_impact_action,
    run_project_audit_action,
    run_project_export_action,
    run_rollback_action,
    run_suggest_action,
)
from owcopilot.app.view_models import build_project_overview
from owcopilot.impact import ChangeType

_SEVERITY_BADGE = {"error": "🟥", "warning": "🟨", "info": "🟦"}
_ITEM_TYPE_LABEL = {
    "quest_draft": "📜 任务草稿",
    "bark_variant": "💬 台词变体",
    "patch_candidate": "🩹 修复补丁",
}

st.set_page_config(
    page_title="OWCopilot · 世界观工作台",
    page_icon="🗺️",
    layout="wide",
)

st.markdown(
    """
    <style>
    h1, h2, h3 { font-family: Georgia, "Noto Serif SC", serif; letter-spacing: 0.02em; }
    .ow-banner {
        padding: 0.9rem 1.2rem; border-radius: 0.6rem;
        border: 1px solid rgba(201,162,39,.35);
        background: linear-gradient(135deg, rgba(201,162,39,.10), rgba(23,26,33,.6));
        margin-bottom: 0.8rem;
    }
    .ow-banner h1 { margin: 0; font-size: 1.55rem; color: #e8e2d0; }
    .ow-banner p { margin: 0.15rem 0 0 0; color: #b8b2a0; font-size: 0.9rem; }
    div[data-testid="stMetricValue"] { color: #c9a227; }
    </style>
    """,
    unsafe_allow_html=True,
)


def _track_cost(result: dict[str, Any]) -> None:
    budget = result.get("cost_budget") or {}
    st.session_state["session_cost_usd"] = round(
        st.session_state.get("session_cost_usd", 0.0) + float(budget.get("used_usd", 0.0)), 6
    )


def _show_cost(result: dict[str, Any]) -> None:
    budget = result.get("cost_budget") or {}
    used = float(budget.get("used_usd", 0.0))
    note = "（确定性，零模型成本）" if used == 0 else ""
    session_total = st.session_state.get("session_cost_usd", 0.0)
    st.caption(f"本次成本：${used:.6f}{note} ｜ 本会话累计：${session_total:.6f}")


def _fail(e: Exception) -> None:
    st.error(f"{e.__class__.__name__}: {e}")


# ----------------------------------------------------------------------------- sidebar
with st.sidebar:
    st.markdown("### 🗺️ OWCopilot")
    st.caption("世界观内容中枢 · 一致性审计 · 检索问答 · 受约束生成")
    content_root = st.text_input("内容仓目录", value=st.session_state.get("content_root", ""))
    st.session_state["content_root"] = content_root
    sqlite_override = st.text_input("运行库路径（可选）", value="")
    sqlite_path = sqlite_override or None

    st.divider()
    llm_mode = st.radio(
        "模型模式",
        options=["offline", "real"],
        horizontal=True,
        help="offline：确定性离线应答器，$0；real：真实 OpenAI 兼容模型（读取 .env 配置）。",
    )
    llm_model = st.text_input(
        "真实模型 ID", value="deepseek-v4-flash", disabled=llm_mode != "real"
    )
    operator = st.text_input(
        "操作者署名",
        value=st.session_state.get("operator", ""),
        help="apply / 审核决定会记录到审计日志。",
    )
    st.session_state["operator"] = operator

    st.divider()
    st.metric("本会话模型成本", f"${st.session_state.get('session_cost_usd', 0.0):.6f}")
    if llm_mode == "real":
        st.warning("真实模式会产生模型费用。", icon="💰")

st.markdown(
    """
    <div class="ow-banner">
      <h1>🗺️ OWCopilot 世界观工作台</h1>
      <p>把散落的设定整理成可审计的世界 —— 查设定有出处，跑审查有证据，AI 产物过人审。</p>
    </div>
    """,
    unsafe_allow_html=True,
)

if not content_root:
    st.info(
        "在左侧输入内容仓目录开始。示例数据可用 `owcopilot eval-acceptance` 生成（雾脊行省）。",
        icon="🧭",
    )
    st.stop()

(
    tab_overview,
    tab_audit,
    tab_review,
    tab_ask,
    tab_impact,
    tab_forge,
    tab_create,
    tab_export,
) = st.tabs(
    [
        "🏰 世界概览",
        "🛡️ 一致性审计",
        "📜 待审队列",
        "🔮 世界问答",
        "🕸️ 影响分析",
        "⚒️ 修复工坊",
        "🎭 生成工坊",
        "📦 导出",
    ]
)

# ----------------------------------------------------------------------------- overview
with tab_overview:
    try:
        overview = build_project_overview(content_root, sqlite_path=sqlite_path)
    except Exception as e:
        _fail(e)
    else:
        counts = overview["counts"]
        top = st.columns(4)
        top[0].metric("实体", counts["entities"])
        top[1].metric("任务", counts["quests"])
        top[2].metric("区域", counts["regions"])
        top[3].metric("关系", counts["relations"])
        mid = st.columns(4)
        mid[0].metric("兴趣点", counts["pois"])
        mid[1].metric("对白", counts["dialogues"])
        mid[2].metric("图谱节点", overview["graph"]["nodes"])
        mid[3].metric("图谱边", overview["graph"]["edges"])

        provenance = overview.get("provenance") or {}
        unreviewed = provenance.get("unreviewed_ai_refs") or []
        with st.container(border=True):
            st.markdown("**🔏 内容溯源（AI 参与度）**")
            st.json(
                {
                    "by_origin": provenance.get("by_origin"),
                    "by_review_status": provenance.get("by_review_status"),
                }
            )
            if unreviewed:
                preview = "、".join(unreviewed[:5])
                st.warning(f"有 {len(unreviewed)} 项 AI 内容未过人审：{preview}…")
            else:
                st.success("所有 AI 产物均已通过人工审核。")
        st.caption(f"content_hash：`{overview['content_hash']}`")

# ----------------------------------------------------------------------------- audit
with tab_audit:
    left, right = st.columns([1, 3])
    with left:
        run_clicked = st.button("🛡️ 运行全量审计", type="primary", use_container_width=True)
        severity_filter = st.selectbox("严重度筛选", ["全部", "error", "warning", "info"])
    if run_clicked:
        try:
            result = run_project_audit_action(content_root, sqlite_path=sqlite_path)
        except Exception as e:
            _fail(e)
        else:
            _track_cost(result)
            st.session_state["audit_markdown"] = result["markdown_report"]
            totals = result["audit_run"]["totals"]
            if result["open_errors"]:
                st.error(f"未解决 error：{result['open_errors']}")
            else:
                st.success("审计通过：无未解决 error。")
            st.caption(
                f"error {totals.get('error', 0)} ｜ warning {totals.get('warning', 0)} ｜ "
                f"info {totals.get('info', 0)}"
            )
            _show_cost(result)
    try:
        listing = list_project_issues_action(
            content_root,
            sqlite_path=sqlite_path,
            severity=None if severity_filter == "全部" else severity_filter,
        )
    except Exception as e:
        _fail(e)
    else:
        with right:
            st.markdown(f"**问题清单（{listing['count']}）**")
            for issue in listing["issues"][:200]:
                badge = _SEVERITY_BADGE.get(issue["severity"], "▫️")
                with st.expander(
                    f"{badge} `{issue['rule_code']}` — {issue['target_ref']}", expanded=False
                ):
                    st.write(issue["message"])
                    st.code(
                        json.dumps(issue["evidence"], ensure_ascii=False, indent=2),
                        language="json",
                    )
                    st.caption(f"issue id：`{issue['id']}`（修复工坊用）")
    if st.session_state.get("audit_markdown"):
        st.download_button(
            "⬇️ 下载 Markdown 审计报告",
            st.session_state["audit_markdown"],
            file_name="audit_report.md",
        )

# ----------------------------------------------------------------------------- review queue
with tab_review:
    st.markdown("**人审是 AI 产物落盘的唯一通道。** 采纳任务草稿会写入内容仓（origin 留痕不变）。")
    try:
        queue = list_review_items_action(content_root, sqlite_path=sqlite_path)
    except Exception as e:
        _fail(e)
    else:
        if not queue["items"]:
            st.success("待审队列为空。")
        for item in queue["items"]:
            label = _ITEM_TYPE_LABEL.get(item["item_type"], item["item_type"])
            with st.container(border=True):
                head, accept_col, reject_col = st.columns([6, 1, 1])
                head.markdown(f"{label} ｜ `{item['object_ref']}`")
                if item["issue_refs"]:
                    head.caption(f"关联问题指纹 {len(item['issue_refs'])} 条")
                with st.expander("查看内容"):
                    st.code(
                        json.dumps(item["payload"], ensure_ascii=False, indent=2),
                        language="json",
                    )
                if accept_col.button("采纳", key=f"acc_{item['id']}", type="primary"):
                    try:
                        decided = decide_review_action(
                            content_root,
                            item_id=item["id"],
                            decision="accepted",
                            operator=operator,
                            sqlite_path=sqlite_path,
                        )
                    except Exception as e:
                        _fail(e)
                    else:
                        _track_cost(decided)
                        written = decided.get("written_ref")
                        st.success(f"已采纳{f'，写入 {written}' if written else ''}。")
                        st.rerun()
                if reject_col.button("驳回", key=f"rej_{item['id']}"):
                    try:
                        decide_review_action(
                            content_root,
                            item_id=item["id"],
                            decision="rejected",
                            operator=operator,
                            sqlite_path=sqlite_path,
                        )
                    except Exception as e:
                        _fail(e)
                    else:
                        st.rerun()

# ----------------------------------------------------------------------------- ask
with tab_ask:
    st.markdown("带引用的世界观问答：查不到的设定明确拒答，**绝不编造**。")
    if "ask_history" not in st.session_state:
        st.session_state["ask_history"] = []
    for entry in st.session_state["ask_history"]:
        with st.chat_message("user"):
            st.write(entry["question"])
        with st.chat_message("assistant", avatar="🔮"):
            st.write(entry["answer"])
            if entry["citations"]:
                st.caption("引用：" + "、".join(f"`{ref}`" for ref in entry["citations"]))
    question = st.chat_input("例如：沈清河是谁？玄武之约是什么事件？")
    if question:
        try:
            result = run_ask_action(
                content_root,
                query=question,
                sqlite_path=sqlite_path,
                llm_mode=llm_mode,
                llm_model=llm_model,
            )
        except Exception as e:
            _fail(e)
        else:
            _track_cost(result)
            answer = result["answer"]
            st.session_state["ask_history"].append(
                {
                    "question": question,
                    "answer": (
                        answer["answer"]
                        if not answer["refused"]
                        else "图谱中没有这条设定的依据（拒答）。"
                    ),
                    "citations": [c["ref"] for c in answer.get("citations", [])],
                }
            )
            st.rerun()

# ----------------------------------------------------------------------------- impact
with tab_impact:
    st.markdown("改表之前先看波及面：纯图遍历，零模型成本、零幻觉。")
    with st.form("impact_form"):
        cols = st.columns([2, 3, 1])
        change_type = cols[0].selectbox("变更类型", [item.value for item in ChangeType])
        target_ref = cols[1].text_input("目标引用", placeholder="entity:npc_aldric / quest:q_001")
        max_depth = cols[2].number_input("传播深度", min_value=1, max_value=4, value=2)
        submitted = st.form_submit_button("🕸️ 分析影响", type="primary")
    if submitted and target_ref:
        try:
            result = run_impact_action(
                content_root,
                changes=[{"change_type": change_type, "target_ref": target_ref}],
                sqlite_path=sqlite_path,
                max_depth=int(max_depth),
            )
        except Exception as e:
            _fail(e)
        else:
            _track_cost(result)
            must, suggest = st.columns(2)
            with must:
                st.markdown(f"**🟥 必须改（{len(result['must_change'])}）**")
                for item in result["must_change"]:
                    st.write(f"- `{item['target_ref']}`")
            with suggest:
                st.markdown(f"**🟨 建议查（{len(result['suggest_check'])}）**")
                for item in result["suggest_check"]:
                    st.write(f"- `{item['target_ref']}`")
            _show_cost(result)

# ----------------------------------------------------------------------- forge (suggest/apply)
with tab_forge:
    st.markdown(
        "**审计问题 → 候选补丁 → 影子校验 → 人工应用。** "
        "会引入新错误的候选在你看到之前就被丢弃；应用与回滚均记录操作者。"
    )
    issue_id = st.text_input("问题 ID（从“一致性审计”页复制）")
    forge_cols = st.columns([1, 1, 2])
    if forge_cols[0].button("⚒️ 生成修复候选", type="primary") and issue_id:
        try:
            result = run_suggest_action(
                content_root,
                issue_id=issue_id,
                sqlite_path=sqlite_path,
                llm_mode=llm_mode,
                llm_model=llm_model,
            )
        except Exception as e:
            _fail(e)
        else:
            _track_cost(result)
            st.session_state["last_suggest"] = result
    if st.session_state.get("last_suggest"):
        result = st.session_state["last_suggest"]
        st.caption(
            f"候选 {len(result['candidates'])} ｜ 影子校验淘汰 {result['rejected_count']} ｜ "
            f"{'使用了真实模型' if result['used_llm'] else '确定性修复器'}"
        )
        for candidate in result["candidates"]:
            with st.container(border=True):
                source = "🤖 模型" if candidate["source"] == "llm" else "⚙️ 确定性"
                resolved = "✅ 解决目标问题" if candidate["target_resolved"] else "➖ 间接缓解"
                st.markdown(f"{source} ｜ {resolved} ｜ `{candidate['patch_id']}`")
                if candidate["rationale"]:
                    st.write(candidate["rationale"])
                st.code(
                    json.dumps(candidate["ops"], ensure_ascii=False, indent=2), language="json"
                )
                if st.button("应用此补丁", key=f"apply_{candidate['patch_id']}", type="primary"):
                    try:
                        applied = run_apply_action(
                            content_root,
                            patch_id=candidate["patch_id"],
                            operator=operator,
                            sqlite_path=sqlite_path,
                        )
                    except Exception as e:
                        _fail(e)
                    else:
                        _track_cost(applied)
                        if applied["applied"]:
                            st.success(
                                "已应用；复跑审计后未解决 error = "
                                f"{applied['post_audit_open_errors']}。"
                            )
                        else:
                            st.error(f"拒绝应用：{applied['reason']}")
    st.divider()
    st.markdown("**已应用补丁（可回滚）**")
    try:
        applied_list = list_patches_action(
            content_root, sqlite_path=sqlite_path, status="applied"
        )
    except Exception as e:
        _fail(e)
    else:
        if not applied_list["patches"]:
            st.caption("暂无已应用补丁。")
        for patch in applied_list["patches"]:
            cols = st.columns([5, 1])
            cols[0].markdown(
                f"`{patch['id']}` ｜ 应用者 {patch['applied_by']} ｜ {patch['applied_at']}"
            )
            if cols[1].button("回滚", key=f"rb_{patch['id']}"):
                try:
                    rolled = run_rollback_action(
                        content_root,
                        patch_id=patch["id"],
                        operator=operator,
                        sqlite_path=sqlite_path,
                    )
                except Exception as e:
                    _fail(e)
                else:
                    st.success(f"已回滚 `{rolled['patch_id']}`。")
                    st.rerun()

# ----------------------------------------------------------------------------- create (draft/barks)
with tab_create:
    st.markdown("生成物只引用图谱内实体、生成即审计、**全部进入待审队列**——不会直接落盘。")
    draft_tab, barks_tab = st.tabs(["📜 任务草稿", "💬 台词变体"])
    with draft_tab:
        brief = st.text_area("任务简报", placeholder="为雾脊山道写一个护送盐车去烽燧的支线任务……")
        if st.button("起草任务", type="primary") and brief.strip():
            try:
                result = run_draft_action(
                    content_root,
                    brief=brief.strip(),
                    sqlite_path=sqlite_path,
                    llm_mode=llm_mode,
                    llm_model=llm_model,
                )
            except Exception as e:
                _fail(e)
            else:
                _track_cost(result)
                st.success(
                    f"草稿 `{result['quest']['id']}` 已入待审队列"
                    f"（审计新增问题 {len(result['issues'])} 条）。"
                )
                st.code(
                    json.dumps(result["quest"], ensure_ascii=False, indent=2), language="json"
                )
                _show_cost(result)
    with barks_tab:
        speakers = st.text_input("说话人实体 ID（逗号分隔）", placeholder="npc_r1_a, npc_r2_b")
        topic = st.text_input("主题", placeholder="发现可疑商队靠近烽燧")
        cols = st.columns(2)
        variants = cols[0].number_input("每人变体数", min_value=1, max_value=10, value=4)
        max_chars = cols[1].number_input("最大字数", min_value=8, max_value=200, value=40)
        if st.button("批量生成台词", type="primary") and speakers.strip() and topic.strip():
            try:
                result = run_barks_action(
                    content_root,
                    speaker_ids=[s.strip() for s in speakers.split(",") if s.strip()],
                    topic=topic.strip(),
                    sqlite_path=sqlite_path,
                    variants_per_speaker=int(variants),
                    max_chars=int(max_chars),
                    llm_mode=llm_mode,
                    llm_model=llm_model,
                )
            except Exception as e:
                _fail(e)
            else:
                _track_cost(result)
                st.success(
                    f"通过 lint：{len(result['accepted'])} 条入队 ｜ "
                    f"被过滤：{len(result['rejected'])} 条"
                )
                for variant in result["accepted"]:
                    st.write(f"- **{variant['speaker_id']}**：{variant['text']}")
                _show_cost(result)

# ----------------------------------------------------------------------------- export
with tab_export:
    st.markdown("导出确定性文件交给引擎或下游管线；manifest 对每个产物记 sha256。")
    cols = st.columns([2, 2, 1])
    output_dir = cols[0].text_input("输出目录", value=".tmp/exports")
    engine = cols[1].selectbox("目标引擎", ["generic", "unreal", "unity"])
    if cols[2].button("📦 导出", type="primary"):
        try:
            result = run_project_export_action(
                content_root,
                output_dir=output_dir,
                target_engine=engine,
                sqlite_path=sqlite_path,
            )
        except Exception as e:
            _fail(e)
        else:
            _track_cost(result)
            st.success(f"已导出到 `{result['output_dir']}`")
            st.markdown("**产物清单**")
            for file in result["manifest"]["files"]:
                st.write(f"- `{file['path']}` （{file['kind']}） `sha256:{file['sha256'][:12]}…`")
            _show_cost(result)
