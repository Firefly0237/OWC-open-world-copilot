"""OWCopilot Workbench — the game-world content workbench UI.

A thin Streamlit shell over `app.actions` / `app.view_models`; no business logic lives here.
Launch with:

    streamlit run src/owcopilot/app/dashboard.py

Design language: "星海之卷" — an As-I've-Written-inspired starbound tome: deep-space
blue canvas under faint nebula washes and star dust, crystal-glass panes edged with
gilded hairlines, ivory ink, starlight-cyan accents. Restraint keeps it premium and
readable: glow lives only on the primary CTA / active tab / hero emblem, decoration
stays far below the content layer, and every motion (unfurl, slow orbit, star
breathing, boot splash) honors prefers-reduced-motion.
Copy stays minimal: the UI shows what to do, never argues why the feature exists.

Voice: the UI speaks as a worldsmith's archive (创世/落墨/入档/朱批), Material Symbols
instead of emoji, and the offline providers never surface here — they are a test asset;
end users either connect their own key or the AI features stay locked with guidance.

Product rules the layout encodes:
  * every page shows the cost of what it just did (offline = $0, deterministic);
  * the review queue is the only place AI content becomes real;
  * pickers (issues, speakers, impact targets) offer real ids from the user's own world —
    no demo-world names are hardcoded anywhere in this file.
"""

from __future__ import annotations

import streamlit as st

st.set_page_config(
    page_title="OWCopilot · 世界观工作台",
    page_icon=":material/auto_stories:",
    layout="wide",
)

# Boot veil: Streamlit flushes elements as the script produces them, so this paints before
# the heavy imports below finish on a cold start — starlight instead of a long white page.
# The main theme stylesheet (rendered after those imports) retires it via #ow-splash
# override: signal-driven hand-off, no JS. The 30s animation is only a failsafe.
_BOOT_SPLASH = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Noto+Serif+SC:wght@600&display=swap');
#ow-splash { position: fixed; inset: 0; z-index: 999999; pointer-events: none;
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  gap: 18px;
  background:
    radial-gradient(900px 480px at 82% -10%, rgba(138, 123, 200, .16), transparent 60%),
    radial-gradient(800px 420px at -6% 8%, rgba(143, 214, 232, .09), transparent 55%),
    linear-gradient(168deg, #141b3e 0%, #0f1530 46%, #0a0e24 100%);
  animation: owSplashHold 30s linear forwards; }
#ow-splash::before { content: ""; position: absolute; inset: 0; opacity: .8;
  background-image:
    radial-gradient(1px 1px at 17px 23px, rgba(236, 229, 211, .5) 60%, transparent),
    radial-gradient(1px 1px at 89px 67px, rgba(143, 214, 232, .38) 60%, transparent),
    radial-gradient(1.4px 1.4px at 143px 118px, rgba(240, 210, 138, .42) 60%, transparent),
    radial-gradient(1px 1px at 201px 41px, rgba(236, 229, 211, .26) 60%, transparent),
    radial-gradient(1.6px 1.6px at 311px 83px, rgba(236, 229, 211, .5) 60%, transparent);
  background-size: 380px 240px; }
#ow-splash .orb { animation: owSplashSpin 14s linear infinite;
  transform-origin: center; transform-box: fill-box; }
#ow-splash .core { animation: owSplashPulse 2.2s ease-in-out infinite;
  transform-origin: center; transform-box: fill-box; }
#ow-splash .t { font-family: "Noto Serif SC", Georgia, serif; color: #f0d28a;
  font-size: 1.05rem; letter-spacing: .35em; text-indent: .35em; }
#ow-splash .ln { position: relative; width: 180px; height: 1px; overflow: hidden;
  background: rgba(217, 181, 108, .18); }
#ow-splash .ln::after { content: ""; position: absolute; left: -40%; top: 0;
  width: 40%; height: 100%;
  background: linear-gradient(90deg, transparent, #f0d28a, transparent);
  animation: owSplashSweep 1.4s ease-in-out infinite; }
@keyframes owSplashSweep { to { left: 100%; } }
@keyframes owSplashSpin { to { transform: rotate(360deg); } }
@keyframes owSplashPulse {
  0%, 100% { opacity: .72; transform: scale(.96); }
  50% { opacity: 1; transform: scale(1.04); }
}
@keyframes owSplashHold {
  0%, 96% { opacity: 1; visibility: visible; }
  100% { opacity: 0; visibility: hidden; }
}
@media (prefers-reduced-motion: reduce) {
  #ow-splash .orb, #ow-splash .core, #ow-splash .ln::after { animation: none; }
}
</style>
<div id="ow-splash" aria-hidden="true">
  <svg width="84" height="84" viewBox="0 0 100 100" fill="none">
    <circle class="orb" cx="50" cy="50" r="44" stroke="#8fd6e8" stroke-opacity=".35"
            stroke-dasharray="3 6"/>
    <circle cx="50" cy="50" r="34" stroke="#d9b56c" stroke-opacity=".4"/>
    <path class="core" d="M50 14 L56.5 43.5 L86 50 L56.5 56.5 L50 86 L43.5 56.5 L14 50
             L43.5 43.5 Z" fill="#d9b56c" fill-opacity=".25" stroke="#f0d28a"
          stroke-opacity=".8"/>
    <path class="core" d="M50 34 L52.8 47.2 L66 50 L52.8 52.8 L50 66 L47.2 52.8 L34 50
             L47.2 47.2 Z" fill="#f0d28a" fill-opacity=".85"/>
  </svg>
  <div class="t">正在展卷</div>
  <div class="ln"></div>
</div>
"""
if not st.session_state.get("_booted"):
    st.session_state["_booted"] = True
    st.markdown(_BOOT_SPLASH, unsafe_allow_html=True)

import html
import json
import os
from collections import Counter
from pathlib import Path
from typing import Any

import altair as alt
import pandas as pd
import streamlit.components.v1 as components

from owcopilot.app.actions import (
    add_reference_action,
    decide_review_action,
    delete_object_action,
    fill_extraction_gaps_action,
    list_patches_action,
    list_project_issues_action,
    list_references_action,
    list_review_items_action,
    probe_llm_connection_action,
    run_apply_action,
    run_ask_action,
    run_barks_action,
    run_dialogue_tree_action,
    run_draft_action,
    run_extraction_action,
    run_flavor_action,
    run_impact_action,
    run_ingest_action,
    run_lorebook_export_action,
    run_project_audit_action,
    run_project_export_action,
    run_prose_check_action,
    run_rollback_action,
    run_suggest_action,
    run_theme_sweep_action,
    run_world_seed_action,
    search_references_action,
    submit_extraction_action,
    update_entity_action,
)
from owcopilot.app.genesis_templates import GENESIS_TEMPLATES
from owcopilot.app.view_models import build_content_inventory, build_project_overview
from owcopilot.app.workspaces import (
    create_managed_world,
    export_world_zip,
    import_world_zip,
    list_managed_worlds,
    load_recent_workspaces,
    remember_workspace,
)
from owcopilot.content.models import ContentBundle
from owcopilot.content.store import ContentStore
from owcopilot.extraction import decode_document_bytes as decode_manuscript_bytes
from owcopilot.impact import ChangeType
from owcopilot.inspiration import decode_reference_bytes
from owcopilot.llm.gateway import LLMGatewayError
from owcopilot.util import load_dotenv

# ------------------------------------------------------------------------------ labels
_VERSION = "v0.2.0"

_SEVERITY_META = {
    "error": ("致命", "red", ":material/error:"),
    "warning": ("警告", "amber", ":material/warning:"),
    "info": ("提示", "blue", ":material/info:"),
}
# plain text for pickers (st.pills renders options literally), icons for markdown contexts
_ITEM_TYPE_LABEL = {
    "quest_draft": "任务草稿",
    "bark_variant": "台词变体",
    "patch_candidate": "修复补丁",
    "world_seed": "世界草案",
    "import_draft": "提炼草案",
    "dialogue_tree": "对话树",
    "flavor_batch": "物案批次",
}
_ITEM_TYPE_ICON = {
    "quest_draft": ":material/draft:",
    "bark_variant": ":material/record_voice_over:",
    "patch_candidate": ":material/healing:",
    "world_seed": ":material/public:",
    "import_draft": ":material/history_edu:",
    "dialogue_tree": ":material/account_tree:",
    "flavor_batch": ":material/category:",
}
_ENTITY_TYPE_LABEL = {
    "npc": "角色",
    "location": "地点",
    "faction": "势力",
    "item": "物品",
    "event": "事件",
    "region": "区域",
    "organization": "组织",
    "concept": "概念",
    "term": "术语",
    "skill": "技能",
    "achievement": "成就",
}
_ORIGIN_LABEL = {"human": "人工执笔", "ai_draft": "AI 缮写", "ai_patch": "AI 修订"}
_REFUSAL_TEXT = "档案中查无此条——我不杜撰。先在创世工坊写下它，或到设定档案里补全，再来问我。"
_REVIEW_LABEL = {"approved": "已入档", "pending_review": "待朱批", "rejected": "已焚稿"}
_CHANGE_TYPE_LABEL = {
    "entity_rename": "重命名实体",
    "entity_delete": "删除实体",
    "entity_field_change": "修改实体字段",
    "relation_change": "调整关系",
    "content_change": "修改内容",
}
_INGEST_CHANGE_LABEL = {
    "add": "新增",
    "update": "更新",
    "unchanged": "不变",
    "conflict": "冲突",
}
_ENGINE_META = {
    "generic": ("通用 JSON Bundle", "结构化 JSON 全集，适合自建管线。"),
    "unreal": ("Unreal Engine", "DataTable 兼容 CSV + 本地化 CSV。"),
    "unity": ("Unity", "每任务 camelCase JSON + index 清单。"),
}
_FLAVOR_CATEGORY_LABEL = {"item": "物品", "skill": "技能", "achievement": "成就"}
# Vendor presets verified 2026-06 (official docs / launch notes); the model dropdown always
# offers a custom escape hatch and the UI says "以厂商文档为准".
_PROVIDER_PRESETS: dict[str, dict[str, Any]] = {
    "DeepSeek": {
        "base_url": "https://api.deepseek.com",
        "models": ["deepseek-v4-flash", "deepseek-v4-pro"],
    },
    "OpenAI": {
        "base_url": "https://api.openai.com/v1",
        "models": [
            "gpt-5.5",
            "gpt-5.4-mini",
            "gpt-5.4-nano",
            "gpt-5.2",
            "gpt-5.2-chat-latest",
        ],
    },
    "Anthropic Claude": {
        "base_url": "https://api.anthropic.com/v1/",
        "models": [
            "claude-fable-5",
            "claude-opus-4-8",
            "claude-sonnet-4-6",
            "claude-haiku-4-5-20251001",
        ],
    },
    "Moonshot Kimi": {
        "base_url": "https://api.moonshot.cn/v1",
        "models": ["kimi-k2.6", "kimi-k2.5"],
    },
    "智谱 GLM": {
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "models": ["glm-5.1", "glm-5", "glm-4.7"],
    },
    "通义千问": {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "models": ["qwen3.7-max", "qwen3.5-plus", "qwen3.5-flash"],
    },
    "豆包（火山方舟）": {
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "models": ["doubao-seed-1.8", "doubao-seed-1.6", "doubao-seed-1.6-flash"],
    },
    "自定义": {"base_url": "", "models": []},
}
_CUSTOM_MODEL_OPTION = "自定义输入…"
_PROBE_ERROR_TEXT = {
    "auth": "鉴权失败（401）：请检查 API Key 是否正确、是否有该模型的权限。",
    "rate_limit": "限流（429）：请求太频繁或额度受限，稍后再试。",
    "timeout": (
        "连接超时：网络不稳，或本次生成耗时超过上限。创世/提炼等长任务已自动放宽至 240 秒；"
        "仍超时可在「设置 → 高级」调大生成超时，或减小生成规模后重试。"
    ),
    "connection": "无法连接：Base URL 可能不对，或网络不通。",
    "missing_dependency": "未安装真实模型依赖：pip install owcopilot[live]",
    "provider_error": "服务商返回错误：检查模型 ID 与账户状态。",
}
# Node border colors tuned for the night canvas (node fill #161e40, ivory labels).
_GRAPH_NODE_COLOR = {
    "npc": "#7fa7e0",
    "faction": "#d9b56c",
    "location": "#8fc89a",
    "region": "#8e96ad",
    "poi": "#7fd0c0",
    "quest": "#b89ad8",
    "item": "#d0a878",
    "event": "#e08585",
    "skill": "#7fc8c8",
    "achievement": "#d8c870",
}

# ------------------------------------------------------------------------------ theme css
st.markdown(
    """
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Noto+Serif+SC:wght@600;700&display=swap');

    :root {
      --ow-gold: #d9b56c;            /* gilded hairlines, marks, active states */
      --ow-gold-bright: #f0d28a;     /* highlights, headline numerals, glints */
      --ow-gold-deep: #a8853f;
      --ow-gold-soft: rgba(217, 181, 108, .34);
      --ow-gold-faint: rgba(217, 181, 108, .08);
      --ow-night: #0f1530;           /* bg/base: deep-space blue */
      --ow-night-deep: #0a0e24;
      --ow-panel: rgba(20, 27, 56, .72);    /* crystal panes (no blur: cheap to paint) */
      --ow-panel-2: rgba(13, 18, 42, .55);
      --ow-edge: rgba(217, 181, 108, .26);  /* gilded pane edge */
      --ow-line: #2e3658;                   /* neutral hairline on night */
      --ow-ink: #ece5d3;             /* ivory body text */
      --ow-muted: #9d97ad;           /* star-grey secondary text */
      --ow-cyan: #8fd6e8;            /* starlight accent: links, code, info */
      --ow-violet: #8a7bc8;
      --ow-shadow: 0 1px 3px rgba(0, 0, 0, .35), 0 6px 20px rgba(4, 7, 22, .45);
      --ow-shadow-lift: 0 2px 8px rgba(0, 0, 0, .4), 0 12px 34px rgba(4, 7, 22, .55);
      --ow-serif: "Noto Serif SC", Georgia, "Songti SC", "SimSun", serif;
    }

    /* boot veil hand-off: this stylesheet only arrives once the heavy imports and first
       render are done, so its presence is the "app is ready" signal that retires the
       splash — no JS, no guessed timers */
    #ow-splash { animation: owSplashOut .7s ease .15s forwards; }
    @keyframes owSplashOut { to { opacity: 0; visibility: hidden; } }

    /* deep-space canvas: nebula washes + a fixed star-dust veil, far below the content.
       isolation creates a stacking context so both star layers can sit at z=-1 —
       guaranteed above the canvas, guaranteed below every piece of content. */
    [data-testid="stAppViewContainer"] {
      isolation: isolate;
      background:
        radial-gradient(1100px 560px at 86% -12%, rgba(138, 123, 200, .14), transparent 60%),
        radial-gradient(900px 480px at -6% 4%, rgba(143, 214, 232, .08), transparent 55%),
        radial-gradient(1100px 720px at 52% 118%, rgba(217, 181, 108, .09), transparent 62%),
        linear-gradient(168deg, #141b3e 0%, var(--ow-night) 46%, var(--ow-night-deep) 100%);
      background-attachment: fixed;
    }
    [data-testid="stAppViewContainer"]::before {
      content: ""; position: fixed; inset: 0; pointer-events: none; z-index: -1;
      background-image:
        radial-gradient(1px 1px at 17px 23px, rgba(236, 229, 211, .5) 60%, transparent),
        radial-gradient(1px 1px at 89px 67px, rgba(143, 214, 232, .38) 60%, transparent),
        radial-gradient(1.4px 1.4px at 143px 118px, rgba(240, 210, 138, .42) 60%, transparent),
        radial-gradient(1px 1px at 201px 41px, rgba(236, 229, 211, .26) 60%, transparent),
        radial-gradient(1px 1px at 53px 141px, rgba(236, 229, 211, .32) 60%, transparent),
        radial-gradient(1.6px 1.6px at 311px 83px, rgba(236, 229, 211, .5) 60%, transparent);
      background-size: 380px 240px;
      animation: owTwinkle 16s ease-in-out infinite alternate;
    }
    @keyframes owTwinkle { from { opacity: .5; } to { opacity: .9; } }
    [data-testid="stHeader"] { background: transparent; }
    .block-container { padding-top: 1.2rem; max-width: 1280px; }

    h1, h2, h3 { font-family: var(--ow-serif); letter-spacing: .02em; color: var(--ow-ink); }

    /* entrance: one quiet unfurl for structural blocks (functional, not decorative) */
    @keyframes owFadeUp {
      from { opacity: 0; transform: translateY(6px); }
      to   { opacity: 1; transform: translateY(0); }
    }
    .ow-hero, .ow-empty,
    div[data-testid="stMetric"],
    div[data-testid="stVerticalBlockBorderWrapper"],
    details[data-testid="stExpander"],
    div[data-testid="stForm"] {
      animation: owFadeUp .24s ease-out both;
    }

    /* hero: the tome's title page — crystal pane, gilded corner brackets, slow orbit */
    .ow-hero {
      position: relative; overflow: hidden;
      padding: 1.45rem 1.7rem 1.25rem; margin-bottom: 1rem;
      border-radius: 1rem; border: 1px solid var(--ow-edge);
      background:
        linear-gradient(125deg, rgba(217, 181, 108, .10) 0%,
                        rgba(24, 32, 68, .72) 38%, rgba(12, 17, 40, .8) 100%);
      backdrop-filter: blur(10px);
      box-shadow: var(--ow-shadow), inset 0 1px 0 rgba(240, 210, 138, .12);
    }
    .ow-hero::before, .ow-hero::after {
      content: ""; position: absolute; width: 22px; height: 22px;
      border: 2px solid rgba(240, 210, 138, .55);
    }
    .ow-hero::before { top: 9px; left: 9px; border-right: none; border-bottom: none;
                       border-top-left-radius: 6px; }
    .ow-hero::after { bottom: 9px; right: 9px; border-left: none; border-top: none;
                      border-bottom-right-radius: 6px; }
    .ow-hero h1 {
      margin: 0; font-size: 1.65rem;
      background: linear-gradient(180deg, #f6e2a8 0%, #d9b56c 58%, #b08d4f 100%);
      -webkit-background-clip: text; background-clip: text;
      -webkit-text-fill-color: transparent; color: #e8c87a;
    }
    .ow-hero .ow-tagline { margin: .3rem 0 .7rem; color: var(--ow-muted); font-size: .92rem; }
    .ow-hero .ow-mark {
      position: absolute; right: 1.15rem; top: 50%;
      transform: translateY(-50%); opacity: .85; pointer-events: none;
      filter: drop-shadow(0 0 10px rgba(217, 181, 108, .35));
    }
    .ow-orbit { animation: owSpin 90s linear infinite;
                transform-origin: center; transform-box: fill-box; }
    @keyframes owSpin { to { transform: rotate(360deg); } }

    /* chips: star-seal badges */
    .ow-chip {
      display: inline-flex; align-items: center; gap: .3rem;
      padding: .14rem .62rem; margin: 0 .4rem .3rem 0;
      border-radius: 999px; font-size: .78rem;
      border: 1px solid var(--ow-line);
      background: rgba(16, 22, 48, .6); color: var(--ow-muted);
      transition: border-color .15s ease, background .15s ease;
    }
    .ow-chip b { color: var(--ow-ink); font-weight: 600; }
    .ow-chip.gold  { border-color: var(--ow-gold-soft); color: var(--ow-gold-bright);
                     background: var(--ow-gold-faint); }
    .ow-chip.red   { border-color: rgba(224, 133, 133, .45); color: #e89a9a;
                     background: rgba(224, 133, 133, .08); }
    .ow-chip.amber { border-color: rgba(224, 180, 106, .45); color: #e6c07e;
                     background: rgba(224, 180, 106, .08); }
    .ow-chip.blue  { border-color: rgba(143, 214, 232, .4); color: var(--ow-cyan);
                     background: rgba(143, 214, 232, .07); }
    .ow-chip.green { border-color: rgba(126, 200, 160, .42); color: #8ed4ac;
                     background: rgba(126, 200, 160, .08); }

    /* section heading: four-point star + a constellation rule that unfurls once */
    .ow-section { display: flex; align-items: baseline; gap: .55rem; margin: .35rem 0 .55rem; }
    .ow-section::before { content: "✦"; color: var(--ow-gold); font-size: .62rem;
                          align-self: center; }
    .ow-section .t { font-family: var(--ow-serif); font-weight: 700;
                     font-size: 1.02rem; color: var(--ow-ink); }
    .ow-section .s { color: var(--ow-muted); font-size: .8rem; }
    .ow-section::after {
      content: ""; flex: 1; height: 1px;
      background: linear-gradient(90deg, var(--ow-gold-soft), transparent);
      transform-origin: left center;
      animation: owUnfurl .5s ease-out both;
    }
    @keyframes owUnfurl { from { transform: scaleX(0); } to { transform: scaleX(1); } }

    /* empty / onboarding state */
    .ow-empty {
      border: 1px dashed rgba(217, 181, 108, .4); border-radius: 1rem;
      padding: 2rem 1.5rem 1.6rem; margin: .4rem 0 1rem;
      text-align: center; background: var(--ow-panel-2);
      box-shadow: var(--ow-shadow);
    }
    .ow-empty .icon svg { filter: drop-shadow(0 0 8px rgba(240, 210, 138, .4)); }
    .ow-empty h3 { margin: .4rem 0 .25rem; }
    .ow-empty p { color: var(--ow-muted); margin: 0 0 1.05rem; }
    .ow-steps { display: flex; gap: .8rem; justify-content: center; flex-wrap: wrap; }
    .ow-step {
      width: 215px; text-align: left; padding: .7rem .9rem;
      border: 1px solid var(--ow-line); border-radius: .7rem;
      background: rgba(12, 17, 40, .6);
      transition: border-color .15s ease, transform .15s ease, box-shadow .15s ease;
    }
    .ow-step:hover { border-color: var(--ow-gold-soft); transform: translateY(-2px);
                     box-shadow: var(--ow-shadow); }
    .ow-step .n {
      display: inline-flex; width: 1.35rem; height: 1.35rem; border-radius: 50%;
      align-items: center; justify-content: center; margin-bottom: .35rem;
      background: var(--ow-gold-faint); border: 1px solid var(--ow-gold-soft);
      color: var(--ow-gold-bright); font-size: .78rem;
    }
    .ow-step b { display: block; color: var(--ow-ink); font-size: .88rem;
                 margin-bottom: .15rem; }
    .ow-step span { color: var(--ow-muted); font-size: .78rem; line-height: 1.5; }

    /* metric tiles: night panes with a gilded hairline (dozens can share a page) */
    div[data-testid="stMetric"] {
      position: relative; overflow: hidden;
      border: 1px solid var(--ow-line); border-radius: .8rem;
      padding: .72rem .9rem .55rem;
      background: var(--ow-panel-2);
      box-shadow: var(--ow-shadow);
      transition: border-color .15s ease, box-shadow .2s ease, transform .15s ease;
    }
    div[data-testid="stMetric"]::before {
      content: ""; position: absolute; top: 0; left: 10%; right: 10%; height: 1px;
      background: linear-gradient(90deg, transparent, rgba(240, 210, 138, .5), transparent);
    }
    div[data-testid="stMetric"]:hover {
      border-color: var(--ow-gold-soft);
      box-shadow: var(--ow-shadow-lift);
      transform: translateY(-1px);
    }
    div[data-testid="stMetricLabel"] p {
      color: var(--ow-muted) !important; font-size: .8rem !important;
      letter-spacing: .08em;
    }
    div[data-testid="stMetricValue"] { color: var(--ow-gold-bright);
                                       font-family: var(--ow-serif); }

    /* tabs: chapter bookmarks — the active one glints */
    div[data-baseweb="tab-list"] { gap: .1rem; border-bottom: 1px solid var(--ow-line); }
    button[data-baseweb="tab"] {
      background: transparent !important;
      border-radius: .55rem .55rem 0 0; padding: .45rem .8rem;
      transition: background .15s ease;
    }
    button[data-baseweb="tab"]:hover { background: var(--ow-gold-faint) !important; }
    button[data-baseweb="tab"] p { color: #b6b0c2 !important; }
    button[data-baseweb="tab"][aria-selected="true"] p {
      color: var(--ow-gold-bright) !important; font-weight: 600;
      text-shadow: 0 0 12px rgba(240, 210, 138, .4);
    }
    div[data-baseweb="tab-highlight"] {
      background-color: var(--ow-gold);
      box-shadow: 0 0 10px rgba(240, 210, 138, .7);
    }
    div[data-baseweb="tab-border"] { background: transparent; }

    /* primary buttons: gilded seal (hover = bloom, press = settle) */
    button[data-testid="stBaseButton-primary"],
    div[data-testid="stFormSubmitButton"] button[kind="primary"],
    .stButton button[kind="primary"] {
      background: linear-gradient(180deg, #f0d28a 0%, #b9924a 100%) !important;
      color: #241a05 !important; font-weight: 600;
      border: 1px solid rgba(240, 210, 138, .65) !important;
      box-shadow: 0 1px 3px rgba(0, 0, 0, .4), 0 0 12px rgba(217, 181, 108, .2);
      transition: transform .12s ease, box-shadow .15s ease, filter .15s ease;
    }
    button[data-testid="stBaseButton-primary"]:hover,
    .stButton button[kind="primary"]:hover {
      transform: translateY(-1px); filter: brightness(1.05);
      box-shadow: 0 3px 8px rgba(0, 0, 0, .45), 0 0 18px rgba(240, 210, 138, .35);
    }
    button[data-testid="stBaseButton-primary"]:active,
    .stButton button[kind="primary"]:active { transform: scale(.985); }
    .stButton button { transition: transform .12s ease, box-shadow .15s ease,
                       border-color .15s ease; }
    .stButton button:hover { border-color: var(--ow-gold-soft); }

    /* bordered containers read as crystal panes */
    div[data-testid="stVerticalBlockBorderWrapper"] {
      border-color: var(--ow-edge) !important;
      background: var(--ow-panel);
      box-shadow: var(--ow-shadow), inset 0 1px 0 rgba(240, 210, 138, .07);
      transition: border-color .15s ease, box-shadow .2s ease;
    }
    div[data-testid="stVerticalBlockBorderWrapper"]:hover {
      border-color: rgba(240, 210, 138, .42) !important;
      box-shadow: var(--ow-shadow-lift);
    }

    details[data-testid="stExpander"] {
      border: 1px solid var(--ow-line); border-radius: .6rem;
      background: var(--ow-panel);
      transition: border-color .15s ease, box-shadow .2s ease;
    }
    details[data-testid="stExpander"]:hover { border-color: var(--ow-gold-soft); }
    details[data-testid="stExpander"] summary:hover { color: var(--ow-gold-bright); }

    div[data-testid="stForm"] {
      border: 1px solid var(--ow-edge); border-radius: .9rem;
      background: var(--ow-panel); padding: 1.05rem 1.15rem .85rem;
      box-shadow: var(--ow-shadow), inset 0 1px 0 rgba(240, 210, 138, .07);
    }

    /* the few frosted surfaces (sidebar / popover / hero only, so blur stays cheap) */
    section[data-testid="stSidebar"] {
      background: rgba(11, 15, 36, .92);
      border-right: 1px solid #262e52;
      backdrop-filter: blur(12px);
    }
    /* st.popover surfaces become centered crystal dialogs. The anchored placement walks
       off the viewport when the trigger sits near an edge (measured: the settings panel
       poked 92-116px past the top at 800x600) — fixed centering + vh caps make overflow
       geometrically impossible. Selector note: stPopoverBody IS the positioned baseweb
       element itself (no portal wrapper), so the override lands directly on it; selectbox
       menus are separate popovers without this testid and keep anchored behavior. */
    div[data-testid="stPopoverBody"] {
      position: fixed !important;
      top: 50% !important;
      left: 50% !important;
      transform: translate(-50%, -50%) !important;
      z-index: 999995;
      background: rgba(13, 18, 42, .96) !important;
      border: 1px solid var(--ow-edge) !important;
      backdrop-filter: blur(14px);
      box-shadow: var(--ow-shadow-lift);
      /* a floating surface must never leave the viewport: cap it and scroll inside */
      max-height: min(82vh, 720px) !important;
      max-width: min(460px, 94vw) !important;
      overflow-y: auto !important;
      overscroll-behavior: contain;
    }
    .ow-brand { display: flex; gap: .6rem; align-items: center; padding: .15rem 0 .5rem; }
    .ow-brand .mark {
      width: 2.2rem; height: 2.2rem; border-radius: .65rem; font-size: 1.1rem;
      display: flex; align-items: center; justify-content: center;
      background: linear-gradient(150deg, #1d2650, #11173a);
      border: 1px solid var(--ow-gold-soft);
      box-shadow: 0 0 10px rgba(217, 181, 108, .18);
    }
    .ow-brand b { font-family: var(--ow-serif); font-size: 1.05rem;
                  color: var(--ow-ink); display: block; line-height: 1.2; }
    .ow-brand span { font-size: .72rem; color: var(--ow-muted); letter-spacing: .06em; }

    div[data-testid="stChatMessage"] {
      background: var(--ow-panel);
      border: 1px solid var(--ow-line); border-radius: .85rem;
      box-shadow: var(--ow-shadow);
    }

    [data-testid="stSpinner"] p { color: var(--ow-gold-bright) !important; }
    [data-testid="stCaptionContainer"], [data-testid="stCaptionContainer"] p {
      color: var(--ow-muted) !important;
    }
    [data-testid="stMarkdownContainer"] :not(pre) > code {
      color: var(--ow-cyan); background: rgba(143, 214, 232, .08);
    }

    /* alerts: dark crystal tints (Streamlit's stock alert fills glare on the night bg) */
    div[data-testid="stAlertContainer"] { border-radius: .7rem; }
    div[data-testid="stAlertContainer"]:has([data-testid="stAlertContentInfo"]) {
      background: rgba(143, 214, 232, .09) !important;
      border: 1px solid rgba(143, 214, 232, .3);
    }
    div[data-testid="stAlertContainer"]:has([data-testid="stAlertContentInfo"]) * {
      color: #a9d8e8 !important;
    }
    div[data-testid="stAlertContainer"]:has([data-testid="stAlertContentSuccess"]) {
      background: rgba(126, 200, 160, .09) !important;
      border: 1px solid rgba(126, 200, 160, .3);
    }
    div[data-testid="stAlertContainer"]:has([data-testid="stAlertContentSuccess"]) * {
      color: #9ed8b8 !important;
    }
    div[data-testid="stAlertContainer"]:has([data-testid="stAlertContentWarning"]) {
      background: rgba(224, 180, 106, .1) !important;
      border: 1px solid rgba(224, 180, 106, .32);
    }
    div[data-testid="stAlertContainer"]:has([data-testid="stAlertContentWarning"]) * {
      color: #e6c890 !important;
    }
    div[data-testid="stAlertContainer"]:has([data-testid="stAlertContentError"]) {
      background: rgba(224, 133, 133, .1) !important;
      border: 1px solid rgba(224, 133, 133, .32);
    }
    div[data-testid="stAlertContainer"]:has([data-testid="stAlertContentError"]) * {
      color: #eaa9a9 !important;
    }

    ::-webkit-scrollbar { width: 10px; height: 10px; }
    ::-webkit-scrollbar-thumb { background: #2c3458; border-radius: 6px; }
    ::-webkit-scrollbar-thumb:hover { background: #3a4470; }

    /* accessibility: honor the user's motion preference */
    @media (prefers-reduced-motion: reduce) {
      *, *::before, *::after {
        animation-duration: .01ms !important;
        animation-iteration-count: 1 !important;
        transition-duration: .01ms !important;
      }
    }
    </style>
    """,
    unsafe_allow_html=True,
)


def _spark_uri(tile_w: int, tile_h: int, px: float, x: int, y: int, fill: str, alpha: str) -> str:
    """One small four-point star glint on a large transparent tile, as a CSS data URI.

    The tile IS the spacing: background-size is set to the tile's intrinsic size (1:1),
    so the star stays `px` pixels tall. Scaling a bare star SVG with background-size
    stretches the star itself to the tile — screen-wide ghost shapes (round-10 bug).
    """
    scale = round(px / 14, 3)
    return (
        "url(\"data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' "
        f"width='{tile_w}' height='{tile_h}'%3E"
        f"%3Cpath transform='translate({x} {y}) scale({scale})' "
        "d='M7 0 L8.3 5.7 L14 7 L8.3 8.3 L7 14 L5.7 8.3 L0 7 L5.7 5.7 Z' "
        f"fill='%23{fill}' fill-opacity='{alpha}'/%3E%3C/svg%3E\")"
    )


# The floating-stars layer: one four-point glint per large tile across four parallax
# layers, drifting and glimmering on GPU-cheap transform/opacity only. z=-1 inside the
# isolated app container keeps it above the canvas yet below every piece of content;
# the global prefers-reduced-motion rule stills it.
st.markdown(
    "<style>"
    '[data-testid="stAppViewContainer"]::after{'
    "content:'';position:fixed;inset:-90px;pointer-events:none;z-index:-1;"
    "background-image:"
    + ",".join(
        [
            _spark_uri(660, 540, 14, 60, 80, "f0d28a", ".5"),
            _spark_uri(900, 720, 8, 420, 300, "f0d28a", ".38"),
            _spark_uri(780, 600, 10, 220, 160, "8fd6e8", ".4"),
            _spark_uri(540, 460, 7, 100, 360, "ece5d3", ".32"),
        ]
    )
    + ";"
    "background-size:660px 540px,900px 720px,780px 600px,540px 460px;"
    "background-position:0 0,0 0,0 0,0 0;"
    "animation:owDrift 80s ease-in-out infinite alternate,"
    "owGlimmer 9s ease-in-out infinite;}"
    "@keyframes owDrift{from{transform:translate3d(0,0,0)}"
    "to{transform:translate3d(-48px,-70px,0)}}"
    "@keyframes owGlimmer{0%,100%{opacity:.5}50%{opacity:.95}}"
    "</style>",
    unsafe_allow_html=True,
)


# ------------------------------------------------------------------------------ ui helpers
def _chip(text: str, *, kind: str = "", strong: str | None = None) -> str:
    cls = f"ow-chip {kind}".strip()
    label = html.escape(text)
    if strong is not None:
        label = f"{label}<b>{html.escape(strong)}</b>"
    return f'<span class="{cls}">{label}</span>'


def _chips(*chips: str) -> None:
    st.markdown("<div>" + "".join(chips) + "</div>", unsafe_allow_html=True)


def _section(title: str, subtitle: str = "") -> None:
    sub = f'<span class="s">{html.escape(subtitle)}</span>' if subtitle else ""
    st.markdown(
        f'<div class="ow-section"><span class="t">{html.escape(title)}</span>{sub}</div>',
        unsafe_allow_html=True,
    )


def _empty_state(title: str, body: str, steps: list[tuple[str, str]]) -> None:
    cards = "".join(
        f'<div class="ow-step"><span class="n">{i + 1}</span>'
        f"<b>{html.escape(name)}</b><span>{html.escape(desc)}</span></div>"
        for i, (name, desc) in enumerate(steps)
    )
    st.markdown(
        f"""
        <div class="ow-empty">
          <div class="icon"><svg width="38" height="38" viewBox="0 0 100 100"
               fill="none"><path d="M50 8 L58 42 L92 50 L58 58 L50 92 L42 58
               L8 50 L42 42 Z" fill="#f0d28a" fill-opacity=".85"/></svg></div>
          <h3>{html.escape(title)}</h3>
          <p>{html.escape(body)}</p>
          <div class="ow-steps">{cards}</div>
        </div>
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
    note = "（零模型成本）" if used == 0 else ""
    session_total = st.session_state.get("session_cost_usd", 0.0)
    st.caption(f"本次 ${used:.6f}{note} ｜ 会话累计 ${session_total:.6f}")


def _flash(kind: str, text: str) -> None:
    """Queue a notice that must survive the st.rerun() issued right after an action.

    st.success() followed by st.rerun() never reaches the screen — the rerun interrupts
    the flush. Anything the user must SEE after a state-changing action goes through here.
    """
    st.session_state.setdefault("_flash", []).append((kind, text))


def _drain_flash() -> None:
    icons = {
        "success": ":material/done_all:",
        "error": ":material/error:",
        "warning": ":material/warning:",
        "info": ":material/info:",
    }
    for kind, text in st.session_state.pop("_flash", []):
        getattr(st, kind, st.info)(text, icon=icons.get(kind))


def _set_extraction_draft(draft: dict[str, Any] | None) -> None:
    """Swap the working draft atomically: gap_* answer widgets are keyed by gap ref, so
    stale answers from a previous draft would silently pre-fill the next one."""
    for key in [k for k in st.session_state if str(k).startswith("gap_")]:
        del st.session_state[key]
    if draft is None:
        st.session_state.pop("extraction_draft", None)
    else:
        st.session_state["extraction_draft"] = draft


def _prune_picker_state(key: str, valid: set[str]) -> None:
    """Drop selections whose options no longer exist (entity deleted, world rolled back).
    A keyed multiselect re-instantiated with stale values raises and kills the page."""
    picked = st.session_state.get(key)
    if isinstance(picked, list):
        kept = [v for v in picked if v in valid]
        if len(kept) != len(picked):
            st.session_state[key] = kept


def _call(label: str, fn, /, *args, **kwargs):
    """Run an action under a themed spinner so long model calls feel alive."""
    with st.spinner(label):
        return fn(*args, **kwargs)


def _fail(e: Exception) -> None:
    """Translate raw exceptions into actionable Chinese guidance (A7)."""
    if isinstance(e, LLMGatewayError):
        friendly = _PROBE_ERROR_TEXT.get(e.category, "模型调用失败，请稍后重试。")
        st.error(f"{friendly}（任务：{e.task}，已重试 {e.attempts} 次）")
    elif isinstance(e, json.JSONDecodeError):
        st.error("模型返回的内容不是有效 JSON（可能被截断）。可以重试一次，或换更强的模型。")
    elif isinstance(e, FileNotFoundError):
        st.error("找不到目标文件或目录：请确认左侧内容仓路径是否正确。")
    elif isinstance(e, ModuleNotFoundError):
        st.error("缺少依赖：真实模式需要 pip install owcopilot[live]。")
    elif isinstance(e, ValueError):
        st.error(str(e))
    else:
        st.error(f"操作失败：{e}")
    with st.expander("技术细节"):
        st.code(f"{e.__class__.__name__}: {e}")


def _dark_axes(chart: Any) -> Any:
    return (
        chart.configure_axis(
            labelColor="#a8a2b8",
            titleColor="#a8a2b8",
            gridColor="#262e52",
            domainColor="#3c4570",
        )
        .configure_view(strokeWidth=0)
        .configure(background="transparent")
    )


def _bar_chart(rows: list[dict[str, Any]], *, x: str, y: str, height: int = 210) -> None:
    df = pd.DataFrame(rows)
    chart = (
        alt.Chart(df)
        .mark_bar(cornerRadiusTopLeft=3, cornerRadiusTopRight=3, color="#d9b56c")
        .encode(
            x=alt.X(f"{x}:N", sort="-y", axis=alt.Axis(labelAngle=0, title=None)),
            y=alt.Y(f"{y}:Q", axis=alt.Axis(title=None, tickMinStep=1)),
            tooltip=[x, y],
        )
        .properties(height=height)
    )
    st.altair_chart(_dark_axes(chart), use_container_width=True)


def _hbar_chart(rows: list[dict[str, Any]], *, y: str, x: str, height: int = 240) -> None:
    df = pd.DataFrame(rows)
    chart = (
        alt.Chart(df)
        .mark_bar(cornerRadiusTopRight=3, cornerRadiusBottomRight=3, color="#d9b56c")
        .encode(
            y=alt.Y(f"{y}:N", sort="-x", axis=alt.Axis(title=None)),
            x=alt.X(f"{x}:Q", axis=alt.Axis(title=None, tickMinStep=1)),
            tooltip=[y, x],
        )
        .properties(height=height)
    )
    st.altair_chart(_dark_axes(chart), use_container_width=True)


def _timeline_chart(rows: list[dict[str, Any]]) -> None:
    df = pd.DataFrame(rows).sort_values("order")
    base = alt.Chart(df).encode(
        x=alt.X("order:Q", axis=alt.Axis(title=None, tickMinStep=1)),
        y=alt.Y(
            "title:N",
            sort=alt.EncodingSortField("order"),
            axis=alt.Axis(title=None),
        ),
        tooltip=["order", "title"],
    )
    chart = base.mark_line(color="#4a5078", strokeWidth=2) + base.mark_point(
        filled=True, size=110, color="#f0d28a"
    )
    st.altair_chart(
        _dark_axes(chart.properties(height=max(140, 28 * len(rows)))),
        use_container_width=True,
    )


def _dot_graph(
    display: dict[str, tuple[str, str]],
    edges: list[dict[str, str]],
    *,
    max_edges: int = 80,
) -> tuple[str, int]:
    """Build a Graphviz DOT network from (id -> (name, group)) plus relation rows."""
    shown = edges[:max_edges]
    truncated = max(0, len(edges) - len(shown))
    node_ids = {e["source"] for e in shown} | {e["target"] for e in shown}
    lines = [
        "digraph world {",
        '  bgcolor="transparent"; rankdir=LR; pad=0.2;',
        '  node [shape=box style="rounded,filled" fillcolor="#161e40" color="#3c4570"',
        '        fontcolor="#ece5d3" fontsize=11 margin="0.16,0.08"];',
        '  edge [color="#5a6388" fontcolor="#9d97ad" fontsize=9 arrowsize=0.7];',
    ]
    for node_id in sorted(node_ids):
        name, group = display.get(node_id, (node_id, ""))
        color = _GRAPH_NODE_COLOR.get(group, "#6a7095")
        label = name.replace('"', "'")
        lines.append(f'  "{node_id}" [label="{label}" color="{color}"];')
    for edge in shown:
        kind = str(edge["kind"]).replace('"', "'")
        source, target = edge["source"], edge["target"]
        lines.append(f'  "{source}" -> "{target}" [label="{kind}"];')
    lines.append("}")
    return "\n".join(lines), truncated


def _inventory_display(inventory: dict[str, Any]) -> dict[str, tuple[str, str]]:
    display: dict[str, tuple[str, str]] = {}
    for row in inventory["entities"]:
        display[row["id"]] = (row["name"], row["type"])
    for row in inventory["pois"]:
        display.setdefault(row["id"], (row["name"], "poi"))
    for row in inventory["regions"]:
        display.setdefault(row["id"], (row["name"], "region"))
    for row in inventory["quests"]:
        display.setdefault(row["id"], (row["title"], "quest"))
    return display


def _bundle_display(bundle: dict[str, Any]) -> dict[str, tuple[str, str]]:
    display: dict[str, tuple[str, str]] = {}
    for entity_id, entity in (bundle.get("entities") or {}).items():
        display[entity_id] = (entity.get("name", entity_id), entity.get("type", ""))
    for poi_id, poi in (bundle.get("pois") or {}).items():
        display.setdefault(poi_id, (poi.get("name", poi_id), "poi"))
    for quest_id, quest in (bundle.get("quests") or {}).items():
        display.setdefault(quest_id, (quest.get("title", quest_id), "quest"))
    return display


def _tree_dot(tree: dict[str, Any], name_of: dict[str, str]) -> str:
    """Render a dialogue tree as a top-down DOT graph (choices labelled on edges)."""
    lines = [
        "digraph dialogue {",
        '  bgcolor="transparent"; rankdir=TB; pad=0.2;',
        '  node [shape=box style="rounded,filled" fillcolor="#161e40" color="#3c4570"',
        '        fontcolor="#ece5d3" fontsize=10 margin="0.14,0.1"];',
        '  edge [color="#5a6388" fontcolor="#d9b56c" fontsize=9 arrowsize=0.7];',
    ]
    nodes = tree.get("nodes") or {}
    root = tree.get("root_node") or ""
    for node_id, node in nodes.items():
        speaker = node.get("speaker_id") or ""
        speaker_name = name_of.get(speaker, speaker)
        text = str(node.get("text") or "").replace('"', "'")
        if len(text) > 26:
            text = text[:26] + "…"
        label = f"{speaker_name}\\n{text}" if speaker_name else text
        extra = ' color="#f0d28a" penwidth=2' if node_id == root else ""
        lines.append(f'  "{node_id}" [label="{label}"{extra}];')
    for node_id, node in nodes.items():
        next_node = node.get("next_node")
        if next_node:
            lines.append(f'  "{node_id}" -> "{next_node}";')
        for choice in node.get("choices") or []:
            target = choice.get("next_node")
            if not target:
                continue
            text = str(choice.get("text") or "").replace('"', "'")
            if len(text) > 14:
                text = text[:14] + "…"
            lines.append(f'  "{node_id}" -> "{target}" [label="{text}"];')
    lines.append("}")
    return "\n".join(lines)


def _default_content_root() -> str:
    from_query = st.query_params.get("root", "").strip()
    if from_query:
        return from_query
    managed = list_managed_worlds()
    if managed:  # most recently touched managed world wins
        return str(managed[0]["path"])
    return str((Path.cwd() / "content").resolve())


def _project_ready(content_root: str) -> bool:
    if not content_root.strip():
        return False
    return Path(content_root).exists()


def _show_project_required(content_root: str) -> None:
    if content_root.strip():
        st.warning(
            "这个目录还不存在。先在左侧「世界」一栏建立或选择它。", icon=":material/warning:"
        )
        st.code(content_root, language="text")
    else:
        st.info("先在左侧「世界」一栏为你的世界选一个安身之处。", icon=":material/explore:")


def _initialize_content_root(content_root: str) -> None:
    root = Path(content_root)
    if root.exists() and any(root.iterdir()):
        raise ValueError(
            f"目录已存在且非空：{root}。为避免覆盖真实内容，请选择空目录或直接打开已有内容仓。"
        )
    ContentStore(root).save(ContentBundle())


_TOUR_JS = """
(function () {
  const doc = window.parent.document;
  const win = window.parent;
  // relaunch semantics: if a previous tour is still (or half) alive, clear it and start
  // fresh instead of silently refusing
  ["ow-tour-card", "ow-tour-ring", "ow-tour-shield", "ow-tour-style"].forEach(
    function (id) {
      const prev = doc.getElementById(id);
      if (prev) { prev.remove(); }
    }
  );
  const style = doc.createElement("style");
  style.id = "ow-tour-style";
  style.textContent =
    "#ow-tour-card{position:fixed;z-index:9999998;width:340px;max-width:86vw;" +
    "background:rgba(16,22,47,.97);border:1px solid rgba(240,210,138,.5);" +
    "border-radius:12px;padding:14px 16px;" +
    "box-shadow:0 8px 30px rgba(0,0,0,.55),0 0 18px rgba(217,181,108,.18);" +
    "font-family:system-ui,sans-serif;color:#ece5d3;" +
    "transition:top .28s ease,left .28s ease,opacity .2s ease;}" +
    "#ow-tour-card.ow-moving{opacity:.25;}" +
    "#ow-tour-card .t{font-weight:700;font-size:15px;margin-bottom:6px;color:#f0d28a;}" +
    "#ow-tour-card .b{font-size:13px;line-height:1.7;margin-bottom:10px;}" +
    "#ow-tour-card .f{display:flex;justify-content:space-between;align-items:center;" +
    "font-size:12px;color:#9d97ad;}" +
    "#ow-tour-card button{margin-left:6px;border:1px solid #3c4570;border-radius:8px;" +
    "background:#1a2348;color:#ece5d3;padding:4px 10px;font-size:12px;cursor:pointer;}" +
    "#ow-tour-card button.primary{background:linear-gradient(180deg,#f0d28a,#b9924a);" +
    "border-color:#d9b56c;color:#241a05;font-weight:600;}" +
    "#ow-tour-shield{position:fixed;inset:0;z-index:9999996;background:transparent;}" +
    "#ow-tour-ring{position:fixed;z-index:9999997;pointer-events:none;" +
    "border:2px solid #f0d28a;border-radius:10px;" +
    "transition:all .28s ease;opacity:1;" +
    "box-shadow:0 0 0 9999px rgba(4,7,20,.6),0 0 18px rgba(240,210,138,.7);}";
  doc.head.appendChild(style);
  const shield = doc.createElement("div");
  shield.id = "ow-tour-shield";
  const ring = doc.createElement("div");
  ring.id = "ow-tour-ring";
  const card = doc.createElement("div");
  card.id = "ow-tour-card";
  doc.body.appendChild(shield);
  doc.body.appendChild(ring);
  doc.body.appendChild(card);
  function findEl(spec) {
    if (!spec) { return null; }
    if (spec.kind === "tab" || spec.kind === "button") {
      const sel = spec.kind === "tab" ? 'button[data-baseweb="tab"]' : "button";
      const nodes = doc.querySelectorAll(sel);
      for (const b of nodes) {
        if (b.innerText.includes(spec.text)) { return b; }
      }
      return null;
    }
    if (spec.kind === "testid") {
      return doc.querySelector('[data-testid="' + spec.value + '"]');
    }
    return doc.querySelector(spec.value || "");
  }
  function cleanup() {
    [card, ring, shield, style].forEach(function (n) { if (n) { n.remove(); } });
  }
  // poll until the anchor exists and its rect is stable -> the bubble moves in the same
  // beat as the page instead of waiting out a fixed timer
  function waitFor(spec, cb, deadline) {
    const limit = deadline || (performance.now() + 900);
    let last = null;
    function tick() {
      const el = findEl(spec);
      if (el) {
        const r = el.getBoundingClientRect();
        const key = Math.round(r.top) + ":" + Math.round(r.left) + ":" + Math.round(r.width);
        if (key === last) { cb(el); return; }
        last = key;
      }
      if (performance.now() > limit) { cb(el); return; }
      win.requestAnimationFrame(tick);
    }
    win.requestAnimationFrame(tick);
  }
  function place(idx, el, retried) {
    if (!el) {
      // the click that launches the tour also triggers a Streamlit rerun, which can hide
      // an anchor for a beat — give it one longer grace pass before skipping the station
      if (!retried) {
        waitFor(STEPS[idx].find, function (el2) { place(idx, el2, true); },
                performance.now() + 1800);
        return;
      }
      next(idx + 1);
      return;
    }
    const r0 = el.getBoundingClientRect();
    if (r0.top < 0 || r0.bottom > win.innerHeight) {
      el.scrollIntoView({ block: "center", behavior: "instant" });
    }
    const r = el.getBoundingClientRect();
    ring.style.left = (r.left - 6) + "px";
    ring.style.top = (r.top - 6) + "px";
    ring.style.width = (r.width + 12) + "px";
    ring.style.height = (r.height + 12) + "px";
    const winH = win.innerHeight;
    const winW = win.innerWidth;
    // pick a side that never covers the target: right > below > above
    let top;
    let left;
    if (r.right + 364 < winW && r.height > 160) {
      left = r.right + 14;
      top = Math.min(Math.max(12, r.top), winH - 240);
    } else if (r.bottom + 240 < winH) {
      left = Math.min(Math.max(12, r.left), winW - 354);
      top = r.bottom + 14;
    } else {
      left = Math.min(Math.max(12, r.left), winW - 354);
      top = Math.max(12, r.top - 234);
    }
    card.style.top = top + "px";
    card.style.left = left + "px";
    const s = STEPS[idx];
    const last = idx === STEPS.length - 1;
    card.innerHTML =
      '<div class="t">' + s.title + "</div>" +
      '<div class="b">' + s.body + "</div>" +
      '<div class="f"><span>' + (idx + 1) + " / " + STEPS.length + "</span><span>" +
      '<button id="owt-prev">上一步</button>' +
      '<button id="owt-skip">跳过</button>' +
      '<button id="owt-next" class="primary">' + (last ? "完成" : "下一步") +
      "</button></span></div>";
    card.classList.remove("ow-moving");
    doc.getElementById("owt-prev").onclick = function () { next(idx - 1); };
    doc.getElementById("owt-skip").onclick = cleanup;
    doc.getElementById("owt-next").onclick = function () {
      if (last) { cleanup(); } else { next(idx + 1); }
    };
    // some anchors drift briefly after settling (e.g. the tab strip auto-scrolls the
    // active tab into view) -> one delayed re-check snaps the card to its final spot
    win.setTimeout(function () {
      const el2 = findEl(STEPS[idx].find);
      if (!el2) { return; }
      const r2 = el2.getBoundingClientRect();
      if (Math.abs(r2.left - r.left) > 4 || Math.abs(r2.top - r.top) > 4) {
        place(idx, el2);
      }
    }, 260);
  }
  function next(idx) {
    if (idx < 0) { idx = 0; }
    if (idx >= STEPS.length) { cleanup(); return; }
    const s = STEPS[idx];
    card.classList.add("ow-moving");
    if (s.click) {
      const c = findEl(s.click);
      if (c) { c.click(); }
    }
    waitFor(s.find, function (el) { place(idx, el); });
  }
  doc.addEventListener(
    "keydown",
    function (ev) { if (ev.key === "Escape") { cleanup(); } },
    { once: true }
  );
  next(0);
})();
"""


def _render_tour(run_id: int) -> None:
    """Game-style guided tour for first-time users: a same-origin component script drives
    the parent DOM (dim cutout + highlight ring + step card). Placement prefers the side
    of the target so the card never covers what it explains; anchors are polled each
    animation frame so the bubble moves in the same beat as the page. Stations follow the
    tabs left-to-right and explain what each page is for and what to click first."""
    steps = [
        {
            "find": {"kind": "testid", "value": "stSidebar"},
            "title": "欢迎来到 OWCopilot",
            "body": (
                "这是一座属于你的世界档案馆。左侧是总控台：在「世界」一栏给新世界起个名字，"
                "点「创建空白世界」；同事发来的世界包（.zip）也在这里导入——"
                "不需要填写任何路径。"
            ),
        },
        {
            "find": {"kind": "button", "text": "设置"},
            "title": "接入你的 AI 缮写员",
            "body": (
                "点开「设置」：选择服务商（DeepSeek、OpenAI、Kimi 等）、粘贴你自己的"
                " API Key、在下拉里挑一个模型，再点「测试连接」。"
                "Key 只存在你的电脑上，直连厂商。不接入也能翻档案、做校勘、导出——"
                "但创世和写作需要它。"
            ),
        },
        {
            "find": {"kind": "tab", "text": "世界总览"},
            "click": {"kind": "tab", "text": "世界总览"},
            "title": "世界总览 · 全景一眼",
            "body": (
                "这一页是世界的鸟瞰图：有多少角色、任务、地区，谁和谁有牵连"
                "（人物关系网），还有任务年表。世界还空着？别急，下一站就是创世。"
            ),
        },
        {
            "find": {"kind": "tab", "text": "设定档案"},
            "click": {"kind": "tab", "text": "设定档案"},
            "title": "设定档案 · 世界的藏书阁",
            "body": (
                "所有设定都收录在册：角色、地点、势力、术语、对话树……点上方的类别"
                "切换书架，搜索框里输入名字就能找到任何一条设定。写作时记不清了，来这里翻。"
            ),
        },
        {
            "find": {"kind": "tab", "text": "创世工坊"},
            "click": {"kind": "tab", "text": "创世工坊"},
            "title": "创世工坊 · 从无到有，或带着旧世界来",
            "body": (
                "三种开局任选：写一句想法点「开辟世界」；小说、剧本、散乱笔记丢进"
                "「文稿提炼」（txt / md / docx 都行，AI 替你整理成档案）；规整的"
                " Excel / JSON 设定表走「表格导入」——页内附「我该上传什么」清单。"
                "灵感素材放「灵感书阁」，只供借鉴、不入正史。"
            ),
        },
        {
            "find": {"kind": "tab", "text": "世界问答"},
            "click": {"kind": "tab", "text": "世界问答"},
            "title": "世界问答 · 有问必有据",
            "body": (
                "在下方输入框问任何关于你世界的问题，回答会逐条标注出处；"
                "档案里没有的，它会坦白说查无此条——绝不编造。"
            ),
        },
        {
            "find": {"kind": "tab", "text": "校勘修复"},
            "click": {"kind": "tab", "text": "校勘修复"},
            "title": "校勘修复 · 让规则替你盯着",
            "body": (
                "点「巡阅全卷」，26 条规则自动检查时间线、阵营、引用是否自相矛盾，"
                "每个问题都附证据；发现的问题可以一键生成修复方案。新写的章节"
                "贴进「文稿体检」，和档案对一遍。"
            ),
        },
        {
            "find": {"kind": "tab", "text": "影响分析"},
            "click": {"kind": "tab", "text": "影响分析"},
            "title": "影响分析 · 动一发，知全身",
            "body": (
                "想删一个角色、改一个地点？先来这里选中它，看看会牵连哪些任务和设定，"
                "再决定动不动手。"
            ),
        },
        {
            "find": {"kind": "tab", "text": "创作工坊"},
            "click": {"kind": "tab", "text": "创作工坊"},
            "title": "创作工坊 · AI 学徒执笔",
            "body": (
                "让 AI 替你打草稿：任务、分支对话树、角色台词、物品文案。"
                "它只会引用你档案里已有的设定——写出来的每一笔都有出处。"
            ),
        },
        {
            "find": {"kind": "tab", "text": "审阅台"},
            "click": {"kind": "tab", "text": "审阅台"},
            "title": "审阅台 · 你执朱笔",
            "body": (
                "AI 写的一切都先在这里排队等你过目：点「采纳」才会写入世界档案，"
                "点「驳回」就地焚稿。这是 AI 内容进入你世界的唯一通道。"
            ),
        },
        {
            "find": {"kind": "tab", "text": "导出交付"},
            "click": {"kind": "tab", "text": "导出交付"},
            "title": "导出交付 · 装订成册",
            "body": (
                "完成的世界从这里带走：导出 Unreal / Unity 引擎数据表，装订成设定集"
                "（Markdown / Word），或下载世界包（.zip）——换机、备份、交接，"
                "导入即还原。左下角随时能看到每一步花了多少钱。祝创作愉快！"
            ),
        },
    ]
    payload = json.dumps(steps, ensure_ascii=False)
    components.html(
        f"<script>const TOUR_RUN = {run_id};\n"
        "const STEPS = " + payload + ";\n" + _TOUR_JS + "</script>",
        height=0,
    )


# ------------------------------------------------------------------------------ sidebar
with st.sidebar:
    st.markdown(
        """
        <div class="ow-brand">
          <div class="mark">
            <svg width="20" height="20" viewBox="0 0 100 100" fill="none">
              <path d="M50 8 L58 42 L92 50 L58 58 L50 92 L42 58 L8 50 L42 42 Z"
                    fill="#f0d28a" fill-opacity=".9"/>
            </svg>
          </div>
          <div><b>OWCopilot</b><span>世界观工作台</span></div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    _section("世界")
    # Managed worlds (app-owned storage, picked by NAME) are the default; pointing at an
    # arbitrary local path survives as the advanced option. This mirrors how shipped
    # tools handle user content and is the only mode portable to a hosted deployment.
    _CUSTOM_WORLD = "自定义路径…"
    _managed_worlds = list_managed_worlds()
    _world_paths = {w["name"]: str(w["path"]) for w in _managed_worlds}
    st.session_state.setdefault("content_root", _default_content_root())

    def _apply_world_pick() -> None:
        pick = st.session_state.get("world_pick")
        target = _world_paths.get(str(pick))
        if target:
            st.session_state["content_root"] = target

    if _managed_worlds:
        _root_now = (
            str(Path(st.session_state["content_root"]))
            if (st.session_state["content_root"].strip())
            else ""
        )
        _match_name = next((n for n, p in _world_paths.items() if str(Path(p)) == _root_now), None)
        # keep the picker truthful: a managed pick that no longer matches the actual root
        # is stale and would swallow the next change event for the same entry
        _pick_now = st.session_state.get("world_pick")
        if _pick_now is None:
            st.session_state["world_pick"] = _match_name or _CUSTOM_WORLD
        elif _pick_now != _CUSTOM_WORLD and _world_paths.get(str(_pick_now)) != _root_now:
            st.session_state["world_pick"] = _match_name or _CUSTOM_WORLD
        st.selectbox(
            "我的世界",
            list(_world_paths) + [_CUSTOM_WORLD],
            key="world_pick",
            on_change=_apply_world_pick,
            help="世界保存在本机应用目录（~/.owcopilot/worlds）。选「自定义路径…」可指向任意文件夹。",
        )

    with st.expander("新建 / 导入世界", icon=":material/add_circle:"):
        _new_name = st.text_input("新世界名称", key="new_world_name", placeholder="例如：盐汐群岛")
        if st.button("创建空白世界", icon=":material/flare:", use_container_width=True):
            try:
                _created = create_managed_world(_new_name)
            except Exception as e:
                _fail(e)
            else:
                st.session_state["content_root"] = str(_created)
                _flash("success", f"世界「{_created.name}」已落成。")
                st.rerun()
        _pack_file = st.file_uploader(
            "导入世界包（.zip）",
            type=["zip"],
            key="world_pack_upload",
            help="OWCopilot 导出的世界包，或任何含 world/quests 等目录的内容仓压缩包。",
        )
        if _pack_file is not None:
            _pack_name = st.text_input(
                "导入为", value=Path(_pack_file.name).stem, key="world_pack_name"
            )
            if st.button("导入世界包", icon=":material/unarchive:", use_container_width=True):
                try:
                    _imported = import_world_zip(_pack_file.getvalue(), _pack_name)
                except Exception as e:
                    _fail(e)
                else:
                    st.session_state["content_root"] = str(_imported)
                    _flash("success", f"世界「{_imported.name}」已导入。")
                    st.rerun()

    _show_custom_path = (not _managed_worlds) or (
        st.session_state.get("world_pick") == _CUSTOM_WORLD
    )
    if _show_custom_path:
        _RECENT_MANUAL = "（手动输入路径）"

        def _apply_recent_pick() -> None:
            pick = st.session_state.get("recent_pick")
            if pick and pick != _RECENT_MANUAL:
                st.session_state["content_root"] = pick

        _recents = load_recent_workspaces()
        if _recents:
            # same truthfulness rule as the world picker (the "can't re-pick" bug class)
            _stale_pick = st.session_state.get("recent_pick")
            if _stale_pick and _stale_pick != _RECENT_MANUAL:
                if (
                    _stale_pick not in _recents
                    or st.session_state.get("content_root") != _stale_pick
                ):
                    st.session_state["recent_pick"] = _RECENT_MANUAL
            st.selectbox(
                "最近打开",
                [_RECENT_MANUAL] + _recents,
                key="recent_pick",
                on_change=_apply_recent_pick,
                help="最近翻阅过的路径，一键回到现场。",
            )
        content_root = st.text_input(
            "档案目录",
            value=st.session_state.get("content_root", _default_content_root()),
            help="你的世界存放在这个文件夹里。新世界请先选一个空目录。",
        )
        st.session_state["content_root"] = content_root
        if st.button(
            "在此路径建立新世界", icon=":material/create_new_folder:", use_container_width=True
        ):
            try:
                _initialize_content_root(content_root)
            except Exception as e:
                _fail(e)
            else:
                _flash("success", "档案馆已落成。")
                st.rerun()
    content_root = st.session_state["content_root"]

    # Workspace-scoped session keys die with the workspace: a draft extracted from world A
    # must never be submittable into world B, answers must not cite another world, and an
    # ingest preview must not commit foreign files. One root change, one sweep.
    _WORKSPACE_KEYS = (
        "extraction_draft",
        "ingest_preview",
        "last_suggest",
        "audit_markdown",
        "audit_flash",
        "ask_history",
        "tree_participants",
        "barks_speakers",
        "sweep_report",
    )
    if st.session_state.get("_ws_root") != content_root:
        for _ws_key in _WORKSPACE_KEYS:
            st.session_state.pop(_ws_key, None)
        for _ws_key in [k for k in st.session_state if str(k).startswith("gap_")]:
            st.session_state.pop(_ws_key, None)
        st.session_state["_ws_root"] = content_root

    # The offline providers are a test asset and never surface here: users either connect
    # their own key (BYO, in-process only) or the AI features stay locked with guidance.
    load_dotenv()
    llm_mode = "real"
    btn_cols = st.columns(2)
    if btn_cols[0].button("新手引导", icon=":material/explore:", use_container_width=True):
        st.session_state["start_tour"] = True
    with btn_cols[1].popover("设置", icon=":material/settings:", use_container_width=True):
        _section("模型接入", "用你自己的 Key，直连厂商")
        provider_preset = st.selectbox("服务商", list(_PROVIDER_PRESETS))
        _preset = _PROVIDER_PRESETS[provider_preset]
        base_url = st.text_input(
            "Base URL",
            value=str(_preset["base_url"]),
            key=f"base_url_{provider_preset}",
        )
        api_key = st.text_input(
            "API Key",
            type="password",
            key="byo_api_key",
            help="只保存在本机本次会话的内存中；调用直连服务商，不经过任何中间服务器。",
        )
        _models: list[str] = list(_preset["models"])
        if _models:
            picked_model = st.selectbox(
                "模型",
                options=_models + [_CUSTOM_MODEL_OPTION],
                key=f"model_pick_{provider_preset}",
                help="常用模型已列出，更多请以厂商文档为准。",
            )
        else:
            picked_model = _CUSTOM_MODEL_OPTION
        if picked_model == _CUSTOM_MODEL_OPTION:
            llm_model = st.text_input(
                "自定义模型 ID",
                key=f"model_custom_{provider_preset}",
                placeholder="填入该服务商的模型名称",
            )
        else:
            llm_model = picked_model
        # Connection env is a pure function of (this session's inputs, the .env snapshot
        # taken at session start). Without the snapshot, switching to a provider with an
        # empty Base URL would silently keep the PREVIOUS vendor's URL — requests would
        # go to vendor A with vendor B's model id.
        _env_defaults = st.session_state.setdefault(
            "_env_defaults",
            {
                "base_url": os.environ.get("OPENAI_BASE_URL", ""),
                "api_key": os.environ.get("OPENAI_API_KEY", ""),
                "timeout": os.environ.get("OWCOPILOT_PROVIDER_TIMEOUT_SEC", ""),
            },
        )
        _eff_base = base_url.strip() or _env_defaults["base_url"]
        _eff_key = api_key.strip() or _env_defaults["api_key"]
        if _eff_base:
            os.environ["OPENAI_BASE_URL"] = _eff_base
        else:
            os.environ.pop("OPENAI_BASE_URL", None)
        if _eff_key:
            os.environ["OPENAI_API_KEY"] = _eff_key
        else:
            os.environ.pop("OPENAI_API_KEY", None)
        if st.button("测试连接", icon=":material/bolt:", use_container_width=True):
            with st.spinner("正在叩响厂商之门…"):
                probe = probe_llm_connection_action(
                    base_url=base_url, api_key=api_key, model=llm_model
                )
            if probe["ok"]:
                st.success(f"连接成功 · {probe['latency_ms']:.0f}ms")
            else:
                st.error(
                    _PROBE_ERROR_TEXT.get(
                        str(probe.get("category", "")),
                        str(probe.get("message", "连接失败")),
                    )
                )
        st.divider()
        _section("创作护栏")
        st.session_state.setdefault("session_cap", 0.0)
        st.number_input(
            "会话成本上限（USD，0 = 不限）",
            min_value=0.0,
            step=0.05,
            format="%.2f",
            key="session_cap",
            help="花费达到上限后，创作类按钮会自动锁定，防止超支。",
        )
        operator = st.text_input(
            "署名",
            value=st.session_state.get("operator", ""),
            placeholder="审阅与写入会落下这个名字",
        )
        st.session_state["operator"] = operator
        st.divider()
        _section("高级")
        st.session_state.setdefault("llm_timeout_sec", 0)
        st.number_input(
            "生成超时（秒，0 = 按任务自动）",
            min_value=0,
            step=30,
            key="llm_timeout_sec",
            help=(
                "创世、文稿提炼等长任务默认放宽到 240 秒。"
                "网络偏慢或生成规模很大时可再调高；调小不会低于任务安全下限。"
            ),
        )
        _timeout_choice = int(st.session_state.get("llm_timeout_sec", 0) or 0)
        if _timeout_choice > 0:
            os.environ["OWCOPILOT_PROVIDER_TIMEOUT_SEC"] = str(_timeout_choice)
        elif _env_defaults.get("timeout"):
            os.environ["OWCOPILOT_PROVIDER_TIMEOUT_SEC"] = str(_env_defaults["timeout"])
        else:
            os.environ.pop("OWCOPILOT_PROVIDER_TIMEOUT_SEC", None)
        sqlite_override = st.text_input(
            "运行库路径（可选）",
            value="",
            help="默认在档案目录内 .owcopilot/runtime.sqlite。",
        )

    operator = st.session_state.get("operator", "")
    sqlite_path = sqlite_override or None

    _key_configured = bool(os.environ.get("OPENAI_API_KEY", "").strip())
    _session_cost = st.session_state.get("session_cost_usd", 0.0)
    _session_cap = float(st.session_state.get("session_cap", 0.0))
    _over_budget = _key_configured and _session_cap > 0 and _session_cost >= _session_cap
    _ai_locked = (not _key_configured) or _over_budget

    if _key_configured:
        _chips(_chip("模型 ", strong=llm_model or "未选择", kind="gold"))
    else:
        _chips(_chip("模型未接入", kind="amber"))
        st.caption("浏览、校勘、导出随时可用；创世与写作需在「设置」中接入模型。")
    if _over_budget:
        st.error("已达会话成本上限，创作功能暂时落锁。可在设置中调高上限。", icon=":material/lock:")

    st.metric("本会话成本", f"${_session_cost:.6f}")
    st.caption(f"OWCopilot {_VERSION}")

# ------------------------------------------------------------------------------ hero
_ready = _project_ready(content_root)
if _ready and st.session_state.get("_remembered_root") != content_root:
    try:
        remember_workspace(content_root)
    except OSError:
        pass
    st.session_state["_remembered_root"] = content_root
_root_name = Path(content_root).name if content_root.strip() else "未选择"
if _key_configured:
    _model_chip = _chip("模型 ", kind="green", strong=llm_model or "未选择")
else:
    _model_chip = _chip("模型未接入", kind="amber")
if _ready:
    _project_chip = _chip("世界 ", kind="gold", strong=_root_name)
else:
    _project_chip = _chip("尚未选择世界", kind="red")
_cost_chip = _chip("本次行程 ", strong=f"${st.session_state.get('session_cost_usd', 0.0):.4f}")
st.markdown(
    f"""
    <div class="ow-hero">
      <svg class="ow-mark" width="120" height="120" viewBox="0 0 100 100" fill="none">
        <circle cx="50" cy="50" r="41" stroke="#d9b56c" stroke-opacity=".45"/>
        <circle class="ow-orbit" cx="50" cy="50" r="30" stroke="#8fd6e8"
                stroke-opacity=".32" stroke-dasharray="3 6"/>
        <path d="M50 12 L57 43 L88 50 L57 57 L50 88 L43 57 L12 50 L43 43 Z"
              fill="#d9b56c" fill-opacity=".16" stroke="#f0d28a" stroke-opacity=".6"/>
        <path d="M50 30 L53.5 46.5 L70 50 L53.5 53.5 L50 70 L46.5 53.5 L30 50 L46.5 46.5 Z"
              fill="#f0d28a" fill-opacity=".4"/>
      </svg>
      <h1>OWCopilot · 世界观工作台</h1>
      <p class="ow-tagline">执笔创世，落墨成史——每一条设定皆有出处，每一份草稿必经你手。</p>
      <div>{_project_chip}{_model_chip}{_cost_chip}{_chip(_VERSION)}</div>
    </div>
    """,
    unsafe_allow_html=True,
)

if st.session_state.pop("start_tour", False):
    # Streamlit dedupes components by content: an identical iframe is NOT remounted, so
    # its script would never re-execute. A per-run nonce makes every launch unique.
    st.session_state["tour_run_id"] = st.session_state.get("tour_run_id", 0) + 1
    _render_tour(st.session_state["tour_run_id"])

_drain_flash()

inventory: dict[str, Any] | None = None
if _ready:
    try:
        inventory = build_content_inventory(content_root, sqlite_path=sqlite_path)
    except Exception as e:
        _fail(e)

(
    tab_overview,
    tab_archive,
    tab_genesis,
    tab_ask,
    tab_audit,
    tab_impact,
    tab_create,
    tab_review,
    tab_export,
) = st.tabs(
    [
        ":material/public: 世界总览",
        ":material/auto_stories: 设定档案",
        ":material/flare: 创世工坊",
        ":material/forum: 世界问答",
        ":material/fact_check: 校勘修复",
        ":material/hub: 影响分析",
        ":material/edit_note: 创作工坊",
        ":material/approval: 审阅台",
        ":material/archive: 导出交付",
    ]
)

_ONBOARDING_STEPS = [
    ("建立档案馆", "在左侧「世界」选一个文件夹，点「建立新世界」。"),
    ("接入模型", "打开「设置」粘贴你的 API Key——只存本机，直连厂商。"),
    ("落下第一笔", "去创世工坊：一句话开辟世界，或让文稿提炼整理旧稿。"),
    ("朱批入档", "AI 写的草稿都会在审阅台候着，采纳才会写进世界。"),
]

# ------------------------------------------------------------------------------ overview
with tab_overview:
    if not _ready:
        _show_project_required(content_root)
        _empty_state("建立你的世界", "四步，把散落的设定化作一座可考据的世界：", _ONBOARDING_STEPS)
    else:
        try:
            overview = build_project_overview(content_root, sqlite_path=sqlite_path)
        except Exception as e:
            _fail(e)
        else:
            counts = overview["counts"]
            is_blank = counts["entities"] == 0 and counts["quests"] == 0
            if is_blank:
                _empty_state(
                    "这个世界尚是一页素笺",
                    "落下第一笔吧：",
                    _ONBOARDING_STEPS[1:],
                )
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

            if not is_blank and inventory is not None:
                left, right = st.columns([3, 2])
                with left:
                    _section("关系图谱")
                    if inventory["relations"]:
                        dot, truncated = _dot_graph(
                            _inventory_display(inventory), inventory["relations"]
                        )
                        st.graphviz_chart(dot, use_container_width=True)
                        if truncated:
                            total = len(inventory["relations"])
                            st.caption(f"仅展示前 80 条关系（共 {total} 条）。")
                    else:
                        st.info("尚无关系数据。")
                with right:
                    _section("实体类型分布")
                    type_counter = Counter(row["type"] for row in inventory["entities"])
                    if type_counter:
                        _bar_chart(
                            [
                                {"类型": _ENTITY_TYPE_LABEL.get(key, key), "数量": value}
                                for key, value in type_counter.items()
                            ],
                            x="类型",
                            y="数量",
                            height=190,
                        )
                    _section("内容溯源")
                    provenance = overview.get("provenance") or {}
                    by_origin = provenance.get("by_origin") or {}
                    by_status = provenance.get("by_review_status") or {}
                    _chips(
                        *(
                            _chip(f"{_ORIGIN_LABEL.get(k, k)} ", strong=str(v), kind="gold")
                            for k, v in by_origin.items()
                        )
                    )
                    _chips(
                        *(
                            _chip(
                                f"{_REVIEW_LABEL.get(k, k)} ",
                                strong=str(v),
                                kind="green" if k == "approved" else "amber",
                            )
                            for k, v in by_status.items()
                        )
                    )
                    unreviewed = provenance.get("unreviewed_ai_refs") or []
                    if unreviewed:
                        preview = "、".join(unreviewed[:5])
                        st.warning(f"{len(unreviewed)} 项 AI 内容未过人审：{preview}…")
                    else:
                        st.success("所有 AI 产物均已通过人工审核。")
            if inventory is not None:
                timeline_rows = [
                    {"order": row["timeline_order"], "title": row["title"]}
                    for row in inventory["quests"]
                    if row.get("timeline_order") is not None
                ]
                if timeline_rows:
                    _section("任务年表", "按时间序排列的故事脉络")
                    _timeline_chart(timeline_rows)
            st.caption(f"内容指纹：`{overview['content_hash']}`")

# ------------------------------------------------------------------------------ archive
with tab_archive:
    if not _ready:
        _show_project_required(content_root)
    elif inventory is None:
        st.info("内容仓读取失败，请检查上方错误信息。")
    else:
        kind = (
            st.pills(
                "档案类别",
                options=[
                    "实体",
                    "任务",
                    "区域",
                    "兴趣点",
                    "术语",
                    "对白",
                    "对话树",
                    "关系",
                    "风格圣经",
                ],
                default="实体",
                label_visibility="collapsed",
            )
            or "实体"
        )
        query = st.text_input(
            "搜索",
            placeholder="按名称 / ID / 描述过滤……",
            label_visibility="collapsed",
        )

        def _match(row: dict[str, Any]) -> bool:
            if not query.strip():
                return True
            needle = query.strip().lower()
            return any(needle in str(value).lower() for value in row.values())

        if kind == "实体":
            rows = [r for r in inventory["entities"] if _match(r)]
            st.caption(f"{len(rows)} / {len(inventory['entities'])} 条")
            if rows:
                df = pd.DataFrame(rows)
                df["type"] = df["type"].map(lambda t: _ENTITY_TYPE_LABEL.get(t, t))
                df["origin"] = df["origin"].map(lambda o: _ORIGIN_LABEL.get(o, o))
                df["review_status"] = df["review_status"].map(lambda s: _REVIEW_LABEL.get(s, s))
                df = df.rename(
                    columns={
                        "id": "ID",
                        "name": "名称",
                        "type": "类型",
                        "description": "描述",
                        "tags": "标签",
                        "origin": "来源",
                        "review_status": "审核",
                    }
                )
                st.dataframe(df, use_container_width=True, hide_index=True, height=380)
                picked = st.selectbox(
                    "查看详情",
                    options=[""] + [r["id"] for r in rows],
                    format_func=lambda i: "选择一个实体…" if not i else i,
                )
                if picked:
                    row = next(r for r in rows if r["id"] == picked)
                    with st.container(border=True):
                        st.markdown(f"**{row['name']}** ｜ `{row['id']}`")
                        _chips(
                            _chip(_ENTITY_TYPE_LABEL.get(row["type"], row["type"]), kind="blue"),
                            _chip(_ORIGIN_LABEL.get(row["origin"], row["origin"]), kind="gold"),
                            _chip(
                                _REVIEW_LABEL.get(row["review_status"], row["review_status"]),
                                kind="green" if row["review_status"] == "approved" else "amber",
                            ),
                        )
                        if row["description"]:
                            st.write(row["description"])
                        related = [
                            rel
                            for rel in inventory["relations"]
                            if rel["source"] == picked or rel["target"] == picked
                        ]
                        if related:
                            st.markdown("**关系**")
                            for rel in related[:20]:
                                st.write(f"- `{rel['source']}` —{rel['kind']}→ `{rel['target']}`")
                    with st.expander("管理此实体（编辑 / 删除）", icon=":material/edit_note:"):
                        with st.form(f"edit_entity_{picked}"):
                            edit_name = st.text_input("名称", value=row["name"])
                            edit_desc = st.text_area(
                                "描述", value=row.get("description") or "", height=100
                            )
                            edit_tags = st.text_input(
                                "标签（逗号分隔）",
                                value=", ".join(row.get("tags") or []),
                            )
                            if st.form_submit_button(
                                "保存修改", icon=":material/save:", type="primary"
                            ):
                                try:
                                    update_entity_action(
                                        content_root,
                                        entity_id=picked,
                                        name=edit_name,
                                        description=edit_desc,
                                        tags=[
                                            tag.strip()
                                            for tag in edit_tags.replace("，", ",").split(",")
                                            if tag.strip()
                                        ],
                                        sqlite_path=sqlite_path,
                                    )
                                except Exception as e:
                                    _fail(e)
                                else:
                                    _flash("success", f"已更新 `{picked}`（ID 不变，引用安然）。")
                                    st.rerun()
                        st.caption("名称与描述随时可改——ID 保持不变，所有引用安然无恙。")
                        st.divider()
                        if st.button(
                            "预览删除影响",
                            key=f"impact_{picked}",
                            icon=":material/hub:",
                            use_container_width=True,
                        ):
                            try:
                                impact_preview = run_impact_action(
                                    content_root,
                                    changes=[
                                        {
                                            "change_type": "entity_delete",
                                            "target_ref": f"entity:{picked}",
                                        }
                                    ],
                                    sqlite_path=sqlite_path,
                                )
                            except Exception as e:
                                _fail(e)
                            else:
                                st.session_state["_del_impact"] = {
                                    "ref": picked,
                                    "result": impact_preview,
                                }
                        _del_impact = st.session_state.get("_del_impact")
                        if _del_impact and _del_impact.get("ref") == picked:
                            _ir = _del_impact["result"]
                            if _ir["total"]:
                                st.warning(
                                    f"删除将波及 {_ir['total']} 处："
                                    f"必须改 {len(_ir['must_change'])}，"
                                    f"建议查 {len(_ir['suggest_check'])}。"
                                )
                                for item in (_ir["must_change"] + _ir["suggest_check"])[:12]:
                                    st.write(f"- `{item['target_ref']}`")
                            else:
                                st.success("没有发现牵连引用，可以放心删除。")
                        confirm_del = st.checkbox(
                            "我已确认影响，删除这个实体（其关系一并移除）",
                            key=f"confirm_del_{picked}",
                        )
                        if st.button(
                            "删除此实体",
                            key=f"del_{picked}",
                            icon=":material/delete_forever:",
                            disabled=not confirm_del,
                            use_container_width=True,
                        ):
                            try:
                                deleted = delete_object_action(
                                    content_root,
                                    ref_type="entity",
                                    object_id=picked,
                                    cascade_relations=True,
                                    sqlite_path=sqlite_path,
                                )
                            except Exception as e:
                                _fail(e)
                            else:
                                st.session_state.pop("_del_impact", None)
                                _flash(
                                    "success",
                                    f"已删除 `{deleted['deleted_ref']}`"
                                    f"（连带移除 {deleted['removed_relations']} 条关系）。"
                                    f"复跑校勘后待修 error = "
                                    f"{deleted['post_audit_open_errors']}。",
                                )
                                st.rerun()
        elif kind == "任务":
            rows = [r for r in inventory["quests"] if _match(r)]
            st.caption(f"{len(rows)} / {len(inventory['quests'])} 条")
            if rows:
                df = pd.DataFrame(rows).rename(
                    columns={
                        "id": "ID",
                        "title": "标题",
                        "giver_npc": "发布人",
                        "location": "地点",
                        "objective": "目标",
                        "stages": "阶段数",
                        "timeline_order": "时间序",
                        "origin": "来源",
                        "review_status": "审核",
                    }
                )
                df["来源"] = df["来源"].map(lambda o: _ORIGIN_LABEL.get(o, o))
                df["审核"] = df["审核"].map(lambda s: _REVIEW_LABEL.get(s, s))
                st.dataframe(df, use_container_width=True, hide_index=True, height=420)
                with st.expander("管理任务（删除）", icon=":material/edit_note:"):
                    quest_to_delete = st.selectbox(
                        "选择任务",
                        options=[""] + [r["id"] for r in rows],
                        format_func=lambda i: "选择一个任务…" if not i else i,
                        key="quest_delete_pick",
                    )
                    if quest_to_delete:
                        confirm_q = st.checkbox(
                            "确认删除此任务（引用它的内容会在校勘中亮出来）",
                            key=f"confirm_qdel_{quest_to_delete}",
                        )
                        if st.button(
                            "删除此任务",
                            key=f"qdel_{quest_to_delete}",
                            icon=":material/delete_forever:",
                            disabled=not confirm_q,
                        ):
                            try:
                                deleted_q = delete_object_action(
                                    content_root,
                                    ref_type="quest",
                                    object_id=quest_to_delete,
                                    sqlite_path=sqlite_path,
                                )
                            except Exception as e:
                                _fail(e)
                            else:
                                _flash(
                                    "success",
                                    f"已删除 `{deleted_q['deleted_ref']}`。复跑校勘后"
                                    f"待修 error = {deleted_q['post_audit_open_errors']}。",
                                )
                                st.rerun()
        elif kind == "区域":
            rows = [r for r in inventory["regions"] if _match(r)]
            st.caption(f"{len(rows)} / {len(inventory['regions'])} 条")
            if rows:
                df = pd.DataFrame(rows).rename(
                    columns={
                        "id": "ID",
                        "name": "名称",
                        "level_min": "等级下限",
                        "level_max": "等级上限",
                        "themes": "主题",
                        "banned_content": "禁入内容",
                    }
                )
                st.dataframe(df, use_container_width=True, hide_index=True)
        elif kind == "兴趣点":
            rows = [r for r in inventory["pois"] if _match(r)]
            st.caption(f"{len(rows)} / {len(inventory['pois'])} 条")
            if rows:
                df = pd.DataFrame(rows).rename(
                    columns={
                        "id": "ID",
                        "name": "名称",
                        "region_id": "所属区域",
                        "purpose": "用途",
                        "controlling_faction": "控制势力",
                    }
                )
                st.dataframe(df, use_container_width=True, hide_index=True)
        elif kind == "术语":
            rows = [r for r in inventory["terms"] if _match(r)]
            st.caption(f"{len(rows)} / {len(inventory['terms'])} 条")
            if rows:
                df = pd.DataFrame(rows).rename(
                    columns={
                        "id": "ID",
                        "canonical": "标准名",
                        "aliases": "别名",
                        "forbidden": "禁用写法",
                        "description": "说明",
                    }
                )
                st.dataframe(df, use_container_width=True, hide_index=True)
        elif kind == "对白":
            rows = [r for r in inventory["dialogues"] if _match(r)]
            st.caption(f"{len(rows)} / {len(inventory['dialogues'])} 条")
            if rows:
                df = pd.DataFrame(rows).rename(
                    columns={
                        "id": "ID",
                        "text_key": "文本键",
                        "speaker_id": "说话人",
                        "quest_id": "所属任务",
                        "text": "文本",
                    }
                )
                st.dataframe(df, use_container_width=True, hide_index=True)
        elif kind == "对话树":
            trees = inventory.get("dialogue_trees") or []
            rows = [r for r in trees if _match(r)]
            st.caption(f"{len(rows)} / {len(trees)} 条")
            if rows:
                df = pd.DataFrame(rows).rename(
                    columns={
                        "id": "ID",
                        "title": "标题",
                        "quest_id": "所属任务",
                        "participants": "参与者",
                        "nodes": "节点数",
                    }
                )
                st.dataframe(df, use_container_width=True, hide_index=True)
                picked_tree = st.selectbox(
                    "查看结构",
                    options=[""] + [r["id"] for r in rows],
                    format_func=lambda i: "选择一棵对话树…" if not i else i,
                )
                if picked_tree:
                    full = (inventory.get("dialogue_tree_payloads") or {}).get(picked_tree)
                    if full:
                        name_of = {row["id"]: row["name"] for row in inventory["entities"]}
                        st.graphviz_chart(_tree_dot(full, name_of), use_container_width=True)
                    with st.expander("管理对话树（删除）", icon=":material/edit_note:"):
                        confirm_t = st.checkbox(
                            "确认删除这棵对话树", key=f"confirm_tdel_{picked_tree}"
                        )
                        if st.button(
                            "删除此对话树",
                            key=f"tdel_{picked_tree}",
                            icon=":material/delete_forever:",
                            disabled=not confirm_t,
                        ):
                            try:
                                deleted_t = delete_object_action(
                                    content_root,
                                    ref_type="dialogue_tree",
                                    object_id=picked_tree,
                                    sqlite_path=sqlite_path,
                                )
                            except Exception as e:
                                _fail(e)
                            else:
                                _flash("success", f"已删除 `{deleted_t['deleted_ref']}`。")
                                st.rerun()
            else:
                st.info("还没有对话树。可在创作工坊生成。")
        elif kind == "关系":
            rows = [r for r in inventory["relations"] if _match(r)]
            st.caption(f"{len(rows)} / {len(inventory['relations'])} 条")
            if rows:
                df = pd.DataFrame(rows).rename(
                    columns={"source": "源", "kind": "关系", "target": "目标"}
                )
                st.dataframe(df, use_container_width=True, hide_index=True)
        else:  # 风格圣经
            guides = inventory["style_guides"]
            if not guides:
                st.info("尚无风格圣经。采纳一份世界草案后会自动生成。")
            for guide in guides:
                with st.container(border=True):
                    st.markdown(f"**风格圣经** ｜ `{guide['id']}`")
                    if guide["body"]:
                        st.write(guide["body"])
                    for rule in guide["rules"]:
                        st.write(f"- {rule}")

# ------------------------------------------------------------------------------ genesis
with tab_genesis:
    if not _ready:
        _show_project_required(content_root)
    else:
        seed_tab, distill_tab, ingest_tab, refs_tab = st.tabs(
            [
                ":material/flare: 一键创世",
                ":material/history_edu: 文稿提炼",
                ":material/upload_file: 表格导入",
                ":material/collections_bookmark: 灵感书阁",
            ]
        )
        # ---------------------------------------------------------------- world seed
        with seed_tab:
            # Progressive brief: only the idea is required. Optional dimensions appear
            # on demand and EMPTY ones never reach the model — an empty "玩家身份" field
            # in the prompt reads as "invent a protagonist", which wrecks worldview-only
            # requests (round-12 user report).
            _OPT_MEDIUM = "载体/媒介"
            _OPT_GENRE = "玩法/类型"
            _OPT_FANTASY = "主角/玩家身份"
            _OPT_STYLES = "世界风格"
            _OPT_TONE = "基调"
            _OPT_ERA = "时代/技术水平"
            _OPT_CONFLICT = "核心冲突"
            _OPT_REFS = "参考用法"
            _OPT_FACTS = "在现有世界上扩写"
            _OPT_NOTES = "补充要求"
            _SEED_OPTIONS = [
                _OPT_MEDIUM,
                _OPT_GENRE,
                _OPT_FANTASY,
                _OPT_STYLES,
                _OPT_TONE,
                _OPT_ERA,
                _OPT_CONFLICT,
                _OPT_REFS,
                _OPT_FACTS,
                _OPT_NOTES,
            ]
            _TPL_FIELD_OPTIONS = [
                _OPT_GENRE,
                _OPT_FANTASY,
                _OPT_STYLES,
                _OPT_TONE,
                _OPT_ERA,
                _OPT_CONFLICT,
                _OPT_NOTES,
            ]

            def _apply_template_pick() -> None:
                pick = st.session_state.get("tpl_pick")
                if not pick or pick == "自定义":
                    return
                _tpl = GENESIS_TEMPLATES[pick]
                st.session_state["seed_idea"] = str(_tpl["idea"])
                st.session_state["seed_genre"] = str(_tpl["game_genre"])
                st.session_state["seed_fantasy"] = str(_tpl["player_fantasy"])
                st.session_state["seed_styles"] = [str(s) for s in _tpl["world_styles"]]
                st.session_state["seed_tone"] = str(_tpl["tone"])
                st.session_state["seed_era"] = str(_tpl["era"])
                st.session_state["seed_conflict"] = str(_tpl["core_conflict"])
                st.session_state["seed_notes"] = str(_tpl["notes"])
                # the template's dimensions must become visible, or the user can't see
                # (or clear) what it just filled in
                chosen = set(st.session_state.get("seed_optional_fields") or [])
                st.session_state["seed_optional_fields"] = [
                    opt for opt in _SEED_OPTIONS if opt in (chosen | set(_TPL_FIELD_OPTIONS))
                ]

            # on_change fires exactly when the widget value actually changes — no applied
            # flag to desync, and 自定义→同一模板 always refills
            st.selectbox(
                "从模板开始",
                ["自定义"] + list(GENESIS_TEMPLATES),
                key="tpl_pick",
                on_change=_apply_template_pick,
                help="选择题材模板一键填表，再随意修改。",
            )
            idea = st.text_area(
                "核心想法（唯一必填）",
                placeholder=(
                    "例如：一个靠蒸汽巨树维持生命的群岛世界，各方势力争夺树心的控制权。"
                    "——只写这一句也能开辟世界。"
                ),
                height=110,
                key="seed_idea",
            )
            chosen_fields = st.multiselect(
                "补充设定（可选——想约束哪个维度就添加哪个，未添加的交给模型自行裁量）",
                _SEED_OPTIONS,
                key="seed_optional_fields",
                placeholder="例如只要世界观：什么都不加，直接开辟",
            )
            with st.form("world_seed_form"):
                medium = ""
                game_genre = ""
                player_fantasy = ""
                style_choices: list[str] = []
                other_style = ""
                tone = ""
                era = ""
                core_conflict = ""
                reference_mode = "灵感参考"
                reference_query = ""
                use_project_facts = False
                notes = ""
                if _OPT_MEDIUM in chosen_fields:
                    medium = st.selectbox(
                        _OPT_MEDIUM,
                        ["开放世界游戏", "RPG", "视觉小说", "剧本", "小说设定"],
                        key="seed_medium",
                    )
                if _OPT_GENRE in chosen_fields:
                    game_genre = st.text_input(
                        _OPT_GENRE, placeholder="开放世界 RPG / 叙事冒险", key="seed_genre"
                    )
                if _OPT_FANTASY in chosen_fields:
                    player_fantasy = st.text_input(
                        _OPT_FANTASY, placeholder="流亡调查员 / 新任领主", key="seed_fantasy"
                    )
                if _OPT_STYLES in chosen_fields:
                    style_choices = st.multiselect(
                        _OPT_STYLES,
                        [
                            "蒸汽朋克",
                            "魔幻",
                            "黑暗奇幻",
                            "科幻",
                            "废土",
                            "武侠",
                            "赛博朋克",
                            "历史架空",
                        ],
                        key="seed_styles",
                    )
                    other_style = st.text_input("其他风格（自由填写）", key="seed_style_other")
                if _OPT_TONE in chosen_fields:
                    tone = st.text_input(_OPT_TONE, placeholder="克制、悬疑、史诗", key="seed_tone")
                if _OPT_ERA in chosen_fields:
                    era = st.text_input(
                        _OPT_ERA, placeholder="工业革命早期 / 近未来", key="seed_era"
                    )
                if _OPT_CONFLICT in chosen_fields:
                    core_conflict = st.text_input(
                        _OPT_CONFLICT,
                        placeholder="能源枯竭、王权更替、旧神复苏",
                        key="seed_conflict",
                    )
                if _OPT_REFS in chosen_fields:
                    ref_cols = st.columns(2)
                    reference_mode = ref_cols[0].selectbox(
                        "参考模式",
                        ["灵感参考", "参考剧情结构", "参考人物关系", "参考任务节奏", "做一个变体"],
                        key="seed_ref_mode",
                    )
                    reference_query = ref_cols[1].text_input(
                        "参考检索关键词", placeholder="留空则用核心想法检索", key="seed_ref_query"
                    )
                if _OPT_FACTS in chosen_fields:
                    use_project_facts = st.checkbox(
                        "在现有世界上扩写（读取项目事实）", value=True, key="seed_use_facts"
                    )
                if _OPT_NOTES in chosen_fields:
                    notes = st.text_area(_OPT_NOTES, height=70, key="seed_notes")
                _section("生成规模", "0 = 完全不要这一类")
                counts_cols = st.columns(5)
                faction_count = counts_cols[0].slider("阵营", 0, 8, 3)
                region_count = counts_cols[1].slider("区域", 0, 8, 2)
                npc_count = counts_cols[2].slider("角色", 0, 24, 8)
                quest_count = counts_cols[3].slider("任务", 0, 16, 5)
                term_count = counts_cols[4].slider("术语", 0, 24, 5)
                submitted = st.form_submit_button(
                    "开辟世界",
                    icon=":material/flare:",
                    type="primary",
                    use_container_width=True,
                    disabled=_ai_locked,
                )
            if submitted and idea.strip():
                styles = list(style_choices)
                if other_style.strip():
                    styles.append(other_style.strip())
                try:
                    result = _call(
                        "正在开辟新世界…",
                        run_world_seed_action,
                        content_root,
                        brief={
                            "idea": idea.strip(),
                            "medium": medium if _OPT_MEDIUM in chosen_fields else "",
                            "game_genre": game_genre.strip(),
                            "world_styles": styles,
                            "tone": tone.strip(),
                            "era": era.strip(),
                            "player_fantasy": player_fantasy.strip(),
                            "core_conflict": core_conflict.strip(),
                            "reference_mode": reference_mode,
                            "reference_query": reference_query.strip(),
                            "use_project_facts": use_project_facts,
                            "faction_count": int(faction_count),
                            "region_count": int(region_count),
                            "npc_count": int(npc_count),
                            "quest_count": int(quest_count),
                            "term_count": int(term_count),
                            "notes": notes.strip(),
                        },
                        sqlite_path=sqlite_path,
                        llm_mode=llm_mode,
                        llm_model=llm_model,
                    )
                except Exception as e:
                    _fail(e)
                else:
                    _track_cost(result)
                    st.success("新世界的初稿已写就，正于审阅台候你朱批。")
                    st.write(result["summary"])
                    counts = result["counts"]
                    _chips(
                        _chip("阵营+角色 ", strong=str(counts["entities"]), kind="gold"),
                        _chip("区域 ", strong=str(counts["regions"]), kind="blue"),
                        _chip("地点 ", strong=str(counts["pois"]), kind="green"),
                        _chip("任务 ", strong=str(counts["quests"]), kind="amber"),
                        _chip("术语 ", strong=str(counts["terms"])),
                        _chip("关系 ", strong=str(counts["relations"])),
                    )
                    if result["reference_report"]:
                        _section("参考来源与创作取舍")
                        for row in result["reference_report"]:
                            with st.expander(f"{row['source_title']} ｜ `{row['source_ref']}`"):
                                st.write(f"用于：{row['used_for']}")
                                st.write(f"转化：{row['transformation']}")
                                if row["excluded"]:
                                    st.write("未使用：" + "、".join(row["excluded"]))
                    if result["issues"]:
                        issue_count = len(result["issues"])
                        st.warning(f"草案自动审计发现 {issue_count} 条提示。")
                    with st.expander("结构化草案 JSON"):
                        st.code(
                            json.dumps(result["bundle"], ensure_ascii=False, indent=2),
                            language="json",
                        )
                    _show_cost(result)
        # ---------------------------------------------------------------- distill
        with distill_tab:
            st.caption("把一卷旧稿交给缮写室：人物、关系、剧情脉络自动归档成册。")
            src_col, opt_col = st.columns([3, 2])
            with src_col:
                manuscript_file = st.file_uploader(
                    "上传文稿",
                    type=["txt", "md", "markdown", "docx", "json", "csv"],
                    key="manuscript_upload",
                )
                manuscript_text = st.text_area("或直接粘贴文本", height=160, key="manuscript_paste")
            with opt_col:
                manuscript_title = st.text_input("来源标题", placeholder="默认用文件名")
                source_kind = st.selectbox("文稿类型", ["小说", "剧本", "设定笔记", "其他文稿"])
                max_chunks = st.slider("最多处理分块", 1, 24, 12, help="每块约 3500 字")
            if st.button(
                "展卷提炼",
                icon=":material/history_edu:",
                type="primary",
                disabled=_ai_locked,
            ):
                raw_text = ""
                title = manuscript_title.strip()
                try:
                    if manuscript_file is not None:
                        raw_text = decode_manuscript_bytes(
                            manuscript_file.getvalue(), manuscript_file.name
                        )
                        title = title or Path(manuscript_file.name).stem
                    if manuscript_text.strip():
                        raw_text = (raw_text + "\n\n" + manuscript_text).strip()
                        title = title or "粘贴文稿"
                    if not raw_text.strip():
                        st.warning("请先上传文件或粘贴文本。")
                    else:
                        result = _call(
                            "正在研读文稿、提炼设定…",
                            run_extraction_action,
                            content_root,
                            title=title,
                            text=raw_text,
                            source_kind=source_kind,
                            sqlite_path=sqlite_path,
                            max_chunks=int(max_chunks),
                            llm_mode=llm_mode,
                            llm_model=llm_model,
                        )
                        _track_cost(result)
                        _set_extraction_draft(result["draft"])
                        st.rerun()
                except Exception as e:
                    _fail(e)
            draft = st.session_state.get("extraction_draft")
            if draft:
                st.divider()
                stats = draft.get("stats") or {}
                _chips(
                    _chip("实体 ", strong=str(stats.get("entities", 0)), kind="gold"),
                    _chip("关系 ", strong=str(stats.get("relations", 0)), kind="blue"),
                    _chip("剧情节拍 ", strong=str(stats.get("beats", 0)), kind="green"),
                    _chip("术语 ", strong=str(stats.get("terms", 0))),
                    _chip("待补缺口 ", strong=str(len(draft.get("gaps") or [])), kind="amber"),
                )
                st.write(draft.get("summary", ""))
                graph_col, beats_col = st.columns([3, 2])
                with graph_col:
                    _section("人物关系图")
                    bundle = draft.get("bundle") or {}
                    relations = bundle.get("relations") or []
                    if relations:
                        edges = [
                            {"source": r["source"], "target": r["target"], "kind": r["kind"]}
                            for r in relations
                        ]
                        dot, _ = _dot_graph(_bundle_display(bundle), edges)
                        st.graphviz_chart(dot, use_container_width=True)
                    else:
                        st.info("文稿中未发现明确的人物关系。")
                with beats_col:
                    _section("剧情结构")
                    beats = draft.get("plot_beats") or []
                    if beats:
                        for beat in beats:
                            with st.container(border=True):
                                st.markdown(f"**{beat['order']}. {beat['title']}**")
                                if beat.get("summary"):
                                    st.caption(beat["summary"])
                    else:
                        st.info("未拆解出剧情节拍。")
                with st.expander("提炼出的实体清单"):
                    ents = (draft.get("bundle") or {}).get("entities") or {}
                    if ents:
                        df = pd.DataFrame(
                            [
                                {
                                    "ID": eid,
                                    "名称": e.get("name", ""),
                                    "类型": _ENTITY_TYPE_LABEL.get(
                                        e.get("type", ""), e.get("type", "")
                                    ),
                                    "描述": e.get("description", ""),
                                }
                                for eid, e in ents.items()
                            ]
                        )
                        st.dataframe(df, use_container_width=True, hide_index=True)
                gaps = draft.get("gaps") or []
                if gaps:
                    _section("待补缺口", "逐项填写，或交给 AI 先补一版再确认")
                    if st.button(
                        "请 AI 补全缺口",
                        icon=":material/auto_fix_high:",
                        disabled=_ai_locked,
                    ):
                        try:
                            filled = _call(
                                "正在为缺口拟写建议…",
                                fill_extraction_gaps_action,
                                content_root,
                                draft=draft,
                                sqlite_path=sqlite_path,
                                llm_mode=llm_mode,
                                llm_model=llm_model,
                            )
                        except Exception as e:
                            _fail(e)
                        else:
                            _track_cost(filled)
                            _set_extraction_draft(filled["draft"])
                            st.rerun()
                    for gap in gaps:
                        st.text_input(
                            gap["question"],
                            value=gap.get("suggestion", ""),
                            key=f"gap_{gap['ref']}",
                        )
                unresolved = draft.get("unresolved_relations") or []
                if unresolved:
                    with st.expander(f"未能解析的关系（{len(unresolved)}）"):
                        for rel in unresolved:
                            st.write(f"- {rel['source']} —{rel['kind']}→ {rel['target']}")
                submit_cols = st.columns([2, 2, 1])
                beats_as_quests = submit_cols[0].checkbox("把剧情节拍生成任务骨架", value=True)
                if submit_cols[1].button("呈送审阅台", icon=":material/approval:", type="primary"):
                    answers = {
                        gap["ref"]: st.session_state.get(f"gap_{gap['ref']}", "") for gap in gaps
                    }
                    try:
                        submit_result = _call(
                            "正在呈送审阅台…",
                            submit_extraction_action,
                            content_root,
                            draft=draft,
                            answers=answers,
                            include_beats_as_quests=beats_as_quests,
                            sqlite_path=sqlite_path,
                        )
                    except Exception as e:
                        _fail(e)
                    else:
                        _track_cost(submit_result)
                        _set_extraction_draft(None)
                        _flash(
                            "success",
                            f"草案已呈送审阅台（尚有 {submit_result['open_gaps']} 处留白）。",
                        )
                        st.rerun()
                if submit_cols[2].button("焚稿重来", icon=":material/delete:"):
                    _set_extraction_draft(None)
                    st.rerun()
        # ---------------------------------------------------------------- ingest
        with ingest_tab:
            st.caption(
                "现成的设定表格（Excel 多 Sheet / JSON / Luban / Markdown）：先预览，无误再入档。"
            )
            with st.expander("我该上传什么？格式要求一览", icon=":material/help:"):
                st.markdown(
                    "带着现有世界观进来，有三条路，按你手头的材料选：\n\n"
                    "| 你手头有什么 | 走哪条路 | 支持格式 |\n"
                    "| --- | --- | --- |\n"
                    "| 规整的设定表 | 本页「表格导入」 | `.xlsx` 多 Sheet"
                    "（实体/任务/区域各一张表，支持中文表头）、"
                    "`.json` / `.jsonl`、Luban 表、`.md`、`.csv` |\n"
                    "| 小说 / 剧本 / 散乱笔记 | 左边「文稿提炼」 | `.txt`、`.md`、"
                    "`.docx`、`.json`（AI 整理成实体、关系、剧情节拍，缺口列出来问你） |\n"
                    "| 另一台机器导出的世界 | 侧栏「新建 / 导入世界」 | "
                    "OWCopilot 世界包 `.zip` |\n\n"
                    "拿不准的话：直接把材料粘进「文稿提炼」最稳——它不要求任何格式。\n"
                    "所有导入都先预览、后入档，不会偷偷覆盖任何东西。"
                )
            ingest_files = st.file_uploader(
                "上传表格/文档",
                type=["xlsx", "json", "jsonl", "md", "markdown", "csv"],
                accept_multiple_files=True,
                key="ingest_upload",
            )
            if st.button(
                "预览导入",
                icon=":material/search:",
                type="primary",
                disabled=not ingest_files,
            ):
                upload_dir = Path(content_root) / ".owcopilot" / "uploads"
                upload_dir.mkdir(parents=True, exist_ok=True)
                saved: list[str] = []
                for file in ingest_files or []:
                    target = upload_dir / file.name
                    target.write_bytes(file.getvalue())
                    saved.append(str(target))
                try:
                    ingest_dry = _call(
                        "正在解析表格…",
                        run_ingest_action,
                        content_root,
                        paths=saved,
                        sqlite_path=sqlite_path,
                        dry_run=True,
                    )
                except Exception as e:
                    _fail(e)
                else:
                    st.session_state["ingest_preview"] = {"paths": saved, "result": ingest_dry}
                    st.rerun()
            preview_state = st.session_state.get("ingest_preview")
            if preview_state:
                preview = preview_state["result"]
                change_counter = Counter(c["change_type"] for c in preview["changes"])
                _chips(
                    _chip("对象 ", strong=str(preview["incoming_count"]), kind="gold"),
                    *(
                        _chip(
                            f"{_INGEST_CHANGE_LABEL.get(k, k)} ",
                            strong=str(v),
                            kind="red" if k == "conflict" else "blue",
                        )
                        for k, v in sorted(change_counter.items())
                    ),
                )
                if preview["changes"]:
                    df = pd.DataFrame(
                        [
                            {
                                "对象": f"{c['object_type']}:{c['object_id']}",
                                "变化": _INGEST_CHANGE_LABEL.get(
                                    c["change_type"], c["change_type"]
                                ),
                            }
                            for c in preview["changes"]
                        ]
                    )
                    st.dataframe(df, use_container_width=True, hide_index=True, height=260)
                if preview["issues"]:
                    with st.expander(f"导入问题（{len(preview['issues'])}）"):
                        for issue in preview["issues"][:50]:
                            st.write(f"- `{issue['rule_code']}` {issue['message']}")
                commit_cols = st.columns([2, 2, 1])
                allow_partial = commit_cols[0].checkbox("跳过冲突对象，写入其余", value=False)
                if commit_cols[1].button("确认入档", icon=":material/done_all:", type="primary"):
                    try:
                        committed = _call(
                            "正在写入档案…",
                            run_ingest_action,
                            content_root,
                            paths=preview_state["paths"],
                            sqlite_path=sqlite_path,
                            dry_run=False,
                            write_non_conflicting=allow_partial,
                        )
                    except Exception as e:
                        _fail(e)
                    else:
                        del st.session_state["ingest_preview"]
                        if committed["has_errors"] and not allow_partial:
                            st.error("存在冲突，未写入。可勾选「跳过冲突对象」部分导入。")
                        else:
                            _flash("success", "导入完成。")
                            st.rerun()
                if commit_cols[2].button("取消"):
                    del st.session_state["ingest_preview"]
                    st.rerun()
        # ---------------------------------------------------------------- references
        with refs_tab:
            st.caption("灵感书阁只供借鉴，不入正史——这里的材料永远不会被当作世界观事实。")
            upload_tab, paste_tab, search_tab = st.tabs(["上传", "粘贴", "检索"])
            use_options = ["inspiration", "style", "structure", "adaptation", "analysis"]
            use_labels = {
                "inspiration": "灵感",
                "style": "文风",
                "structure": "结构",
                "adaptation": "改编",
                "analysis": "拆解",
            }
            with upload_tab:
                uploaded = st.file_uploader(
                    "参考材料",
                    type=["txt", "md", "markdown", "json", "csv"],
                    accept_multiple_files=True,
                )
                upload_uses = st.multiselect(
                    "允许用途",
                    use_options,
                    default=["inspiration", "style", "structure"],
                    format_func=lambda u: use_labels.get(u, u),
                    key="upload_reference_uses",
                )
                if st.button("加入参考库", type="primary", disabled=not uploaded):
                    for file in uploaded or []:
                        try:
                            text = decode_reference_bytes(file.getvalue(), file.name)
                            result = add_reference_action(
                                content_root,
                                title=Path(file.name).stem,
                                text=text,
                                sqlite_path=sqlite_path,
                                original_filename=file.name,
                                allowed_uses=upload_uses,
                            )
                        except Exception as e:
                            _fail(e)
                        else:
                            st.success(
                                f"已加入 `{result['source']['title']}`，"
                                f"切片 {result['indexed_count']} 段。"
                            )
            with paste_tab:
                ref_title = st.text_input("标题", placeholder="某段主线节奏参考")
                ref_text = st.text_area("文本", height=180)
                paste_uses = st.multiselect(
                    "允许用途",
                    use_options,
                    default=["inspiration"],
                    format_func=lambda u: use_labels.get(u, u),
                    key="paste_reference_uses",
                )
                if (
                    st.button("保存文本参考", type="primary")
                    and ref_title.strip()
                    and ref_text.strip()
                ):
                    try:
                        result = add_reference_action(
                            content_root,
                            title=ref_title.strip(),
                            text=ref_text.strip(),
                            sqlite_path=sqlite_path,
                            source_type="pasted_text",
                            allowed_uses=paste_uses,
                        )
                    except Exception as e:
                        _fail(e)
                    else:
                        st.success(
                            f"已加入 `{result['source']['title']}`，"
                            f"切片 {result['indexed_count']} 段。"
                        )
            with search_tab:
                ref_query = st.text_input("检索问题", placeholder="三方势力冲突、护送任务节奏")
                if st.button("检索参考") and ref_query.strip():
                    try:
                        result = search_references_action(
                            content_root,
                            query=ref_query.strip(),
                            sqlite_path=sqlite_path,
                        )
                    except Exception as e:
                        _fail(e)
                    else:
                        if not result["hits"]:
                            st.info("没有命中的参考片段。")
                        for hit in result["hits"]:
                            with st.expander(f"`{hit['ref']}` ｜ {hit['title']}"):
                                st.write(hit["body"])
                                st.caption(
                                    "来源："
                                    + hit["metadata"].get("source_title", "")
                                    + f" ｜ 相关度 {hit['score']:.3f}"
                                )
            st.divider()
            try:
                listing = list_references_action(content_root, sqlite_path=sqlite_path)
            except Exception as e:
                _fail(e)
            else:
                _section("已入库参考", f"共 {listing['count']} 份")
                if not listing["sources"]:
                    st.caption("参考库还是空的。")
                for source in listing["sources"]:
                    with st.container(border=True):
                        st.markdown(f"**{source['title']}** ｜ `{source['id']}`")
                        _chips(
                            *(
                                _chip(use_labels.get(use, use), kind="blue")
                                for use in source["allowed_uses"]
                            )
                        )

# ------------------------------------------------------------------------------ ask
with tab_ask:
    if not _ready:
        _show_project_required(content_root)
    else:
        _section("世界问答", "有问必有据；查无此条，绝不杜撰")
        if "ask_history" not in st.session_state:
            st.session_state["ask_history"] = []

        def _submit_question(question: str) -> None:
            try:
                result = _call(
                    "正在翻阅世界档案…",
                    run_ask_action,
                    content_root,
                    query=question,
                    sqlite_path=sqlite_path,
                    llm_mode=llm_mode,
                    llm_model=llm_model,
                )
            except Exception as e:
                _fail(e)
                return
            _track_cost(result)
            answer = result["answer"]
            st.session_state["ask_history"].append(
                {
                    "question": question,
                    "answer": (answer["answer"] if not answer["refused"] else _REFUSAL_TEXT),
                    "citations": [c["ref"] for c in answer.get("citations", [])],
                    "cost": float((result.get("cost_budget") or {}).get("used_usd", 0.0)),
                }
            )
            st.rerun()

        if not st.session_state["ask_history"] and inventory is not None:
            suggestions: list[str] = []
            if inventory["entities"]:
                suggestions.append(f"{inventory['entities'][0]['name']}是谁？有哪些关系？")
            if inventory["quests"]:
                suggestions.append(f"任务「{inventory['quests'][0]['title']}」的目标是什么？")
            if inventory["terms"]:
                suggestions.append(f"「{inventory['terms'][0]['canonical']}」指什么？")
            if suggestions:
                cols = st.columns(len(suggestions))
                for idx, suggestion in enumerate(suggestions):
                    if cols[idx].button(suggestion, key=f"sug_q_{idx}", disabled=_ai_locked):
                        _submit_question(suggestion)
        for entry in st.session_state["ask_history"]:
            with st.chat_message("user"):
                st.write(entry["question"])
            with st.chat_message("assistant"):
                st.write(entry["answer"])
                if entry["citations"]:
                    _chips(*(_chip(ref, kind="gold") for ref in entry["citations"]))
                if entry.get("cost"):
                    st.caption(f"本问成本 ${entry['cost']:.6f}")
        placeholder = "向你的世界提问……"
        if inventory is not None and inventory["entities"]:
            placeholder = f"例如：{inventory['entities'][0]['name']}的背景是什么？"
        question = st.chat_input(placeholder, disabled=_ai_locked)
        if question:
            _submit_question(question)

# ------------------------------------------------------------------------------ audit & forge
with tab_audit:
    if not _ready:
        _show_project_required(content_root)
    else:
        issues_listing: dict[str, Any] | None = None
        try:
            issues_listing = list_project_issues_action(content_root, sqlite_path=sqlite_path)
        except Exception as e:
            _fail(e)
        check_tab, forge_tab, prose_tab, sweep_tab = st.tabs(
            [
                ":material/fact_check: 一致性校勘",
                ":material/healing: 修复工坊",
                ":material/spellcheck: 文稿体检",
                ":material/search_insights: 专项清查",
            ]
        )
        with check_tab:
            st.caption("让规则替你巡视世界的每个角落——逐条带证据，零模型成本。")
            run_clicked = st.button("巡阅全卷", icon=":material/fact_check:", type="primary")
            if run_clicked:
                try:
                    result = _call(
                        "正在巡阅全卷…",
                        run_project_audit_action,
                        content_root,
                        sqlite_path=sqlite_path,
                    )
                except Exception as e:
                    _fail(e)
                else:
                    _track_cost(result)
                    st.session_state["audit_markdown"] = result["markdown_report"]
                    st.session_state["audit_flash"] = result["open_errors"]
                    st.rerun()
            flash = st.session_state.pop("audit_flash", None)
            if flash is not None:
                if flash:
                    st.error(f"巡阅完毕：尚有 {flash} 处致命错漏待修。")
                else:
                    st.success("巡阅完毕：全卷无致命错漏。")
            if issues_listing is not None:
                issues = issues_listing["issues"]
                severity_counter = Counter(issue["severity"] for issue in issues)
                _chips(
                    *(
                        _chip(
                            f"{_SEVERITY_META[sev][0]} ",
                            strong=str(severity_counter.get(sev, 0)),
                            kind=_SEVERITY_META[sev][1],
                        )
                        for sev in ("error", "warning", "info")
                    ),
                    _chip("总计 ", strong=str(len(issues))),
                )
                if issues:
                    rule_counter = Counter(issue["rule_code"] for issue in issues)
                    chart_col, list_col = st.columns([2, 3])
                    with chart_col:
                        _section("问题按规则分布")
                        _hbar_chart(
                            [
                                {"规则": rule, "数量": count}
                                for rule, count in rule_counter.most_common(12)
                            ],
                            y="规则",
                            x="数量",
                            height=max(160, 26 * min(len(rule_counter), 12)),
                        )
                        if st.session_state.get("audit_markdown"):
                            st.download_button(
                                "下载校勘报告",
                                st.session_state["audit_markdown"],
                                file_name="audit_report.md",
                                icon=":material/download:",
                            )
                    with list_col:
                        _section("问题清单")
                        severity_pick = (
                            st.pills(
                                "严重度",
                                options=["全部", "致命", "警告", "提示"],
                                default="全部",
                                label_visibility="collapsed",
                            )
                            or "全部"
                        )
                        sev_map = {"致命": "error", "警告": "warning", "提示": "info"}
                        shown = [
                            issue
                            for issue in issues
                            if severity_pick == "全部"
                            or issue["severity"] == sev_map.get(severity_pick)
                        ]
                        for issue in shown[:200]:
                            sev_label, _kind, sev_icon = _SEVERITY_META.get(
                                issue["severity"], ("", "", ":material/circle:")
                            )
                            with st.expander(
                                f"{sev_icon} {sev_label} ｜ "
                                f"{issue['rule_code']} — {issue['target_ref']}"
                            ):
                                st.write(issue["message"])
                                st.code(
                                    json.dumps(issue["evidence"], ensure_ascii=False, indent=2),
                                    language="json",
                                )
                                st.caption(f"问题 ID：`{issue['id']}`")
                else:
                    st.info("卷宗尚未巡阅。点「巡阅全卷」，生成第一份校勘报告。")
        with forge_tab:
            st.caption("候选补丁先过影子校验——会引入新错误的方案你根本不会看到。")
            issue_options: list[str] = []
            issue_label: dict[str, str] = {}
            if issues_listing is not None:
                ordered = sorted(
                    issues_listing["issues"],
                    key=lambda i: (i["severity"] != "error", i["rule_code"]),
                )
                for issue in ordered:
                    issue_options.append(issue["id"])
                    sev_label = _SEVERITY_META.get(issue["severity"], ("?", "", ""))[0]
                    issue_label[issue["id"]] = (
                        f"[{sev_label}] {issue['rule_code']} — {issue['target_ref']}"
                    )
            if not issue_options:
                st.info("没有待修复的问题。先在「一致性体检」运行审计。")
            else:
                issue_id = st.selectbox(
                    "选择要修复的问题",
                    options=issue_options,
                    format_func=lambda i: issue_label.get(i, i),
                )
                if (
                    st.button(
                        "锻造修复候选",
                        icon=":material/healing:",
                        type="primary",
                        disabled=_ai_locked,
                    )
                    and issue_id
                ):
                    try:
                        result = _call(
                            "正在锻造修复候选…",
                            run_suggest_action,
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
                    _chips(
                        _chip("候选 ", strong=str(len(result["candidates"])), kind="gold"),
                        _chip("影子校验淘汰 ", strong=str(result["rejected_count"]), kind="red"),
                        _chip("真实模型" if result["used_llm"] else "确定性修复器", kind="blue"),
                    )
                    if not result["candidates"]:
                        st.warning("没有通过影子校验的候选。该类问题需要人工处理。")
                    for candidate in result["candidates"]:
                        with st.container(border=True):
                            source = (
                                ":material/smart_toy: 模型"
                                if candidate["source"] == "llm"
                                else ":material/build: 确定性"
                            )
                            resolved = (
                                ":material/check_circle: 解决目标问题"
                                if candidate["target_resolved"]
                                else ":material/remove: 间接缓解"
                            )
                            st.markdown(f"{source} ｜ {resolved} ｜ `{candidate['patch_id']}`")
                            if candidate["rationale"]:
                                st.write(candidate["rationale"])
                            st.code(
                                json.dumps(candidate["ops"], ensure_ascii=False, indent=2),
                                language="json",
                            )
                            apply_disabled = not operator.strip()
                            if apply_disabled:
                                st.caption("⚠️ 先在左侧填写操作者署名。")
                            if st.button(
                                "应用此补丁",
                                key=f"apply_{candidate['patch_id']}",
                                type="primary",
                                disabled=apply_disabled,
                            ):
                                try:
                                    applied = _call(
                                        "正在应用补丁并复跑审计…",
                                        run_apply_action,
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
            _section("已应用补丁", "可一键回滚")
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
                    if cols[1].button(
                        "回滚", key=f"rb_{patch['id']}", disabled=not operator.strip()
                    ):
                        try:
                            rolled = _call(
                                "正在回滚…",
                                run_rollback_action,
                                content_root,
                                patch_id=patch["id"],
                                operator=operator,
                                sqlite_path=sqlite_path,
                            )
                        except Exception as e:
                            _fail(e)
                        else:
                            _flash("success", f"已回滚 `{rolled['patch_id']}`。")
                            st.rerun()

        with prose_tab:
            st.caption("新写的章节贴进来，与档案对读一遍——错漏与生面孔，皆无所遁形。")
            prose_text = st.text_area(
                "文稿内容",
                height=220,
                key="prose_text",
                placeholder="把新写的章节、对白或任务文本粘贴到这里…",
            )
            if (
                st.button("开始体检", icon=":material/spellcheck:", type="primary")
                and prose_text.strip()
            ):
                try:
                    prose_result = _call(
                        "正在比对文稿与档案…",
                        run_prose_check_action,
                        content_root,
                        text=prose_text,
                        sqlite_path=sqlite_path,
                    )
                except Exception as e:
                    _fail(e)
                else:
                    _track_cost(prose_result)
                    stats = prose_result["stats"]
                    _chips(
                        _chip("字数 ", strong=str(stats["chars"])),
                        _chip(
                            "识别到的设定 ",
                            strong=str(stats["resolved_mentions"]),
                            kind="green",
                        ),
                        _chip("禁用写法 ", strong=str(stats["forbidden_terms"]), kind="red"),
                        _chip("未知名词 ", strong=str(stats["unknown_mentions"]), kind="amber"),
                    )
                    if prose_result["resolved_mentions"]:
                        _chips(
                            *(
                                _chip(f"{m['name']} ", strong=str(m["count"]), kind="gold")
                                for m in prose_result["resolved_mentions"][:12]
                            )
                        )
                    if not prose_result["issues"]:
                        st.success("通篇与档案相符，无一处冲突。")
                    for issue in prose_result["issues"]:
                        with st.container(border=True):
                            kind_label = (
                                ":material/error: 禁用写法"
                                if issue["kind"] == "forbidden_term"
                                else ":material/warning: 未知名词"
                            )
                            st.markdown(f"{kind_label} ｜ {issue['message']}")
                            st.caption(issue["excerpt"])
                            if issue["suggestion"]:
                                st.write(f"建议：{issue['suggestion']}")
                    _show_cost(prose_result)

        with sweep_tab:
            st.caption(
                "突发任务：限期消除某类主题或元素？这里对全库做一次无预设的地毯式排查——"
                "词面命中 + 模型逐项判定（可选）+ 关系扩散，产出可勾选的工作单。"
            )
            sweep_theme = st.text_input(
                "要清查的主题或元素",
                key="sweep_theme",
                placeholder="例如：赌博相关元素 / 某个被弃用的角色 / 涉及宗教符号的描写",
            )
            sweep_terms_raw = st.text_input(
                "关联词（可选，逗号分隔）",
                key="sweep_terms",
                placeholder="同义词、俗称、易漏写法——例如：骰子, 赌坊, 押注",
            )
            use_judge = st.checkbox(
                "用模型逐项判定（覆盖换了说法、没用关键词的内容）",
                value=not _ai_locked,
                disabled=_ai_locked,
                help=(
                    "未接入模型时仅做词面命中 + 关系扩散；"
                    "接入后模型会逐个对象判定是否相关并给出原文证据。"
                ),
            )
            if st.button(
                "开始清查",
                icon=":material/search_insights:",
                type="primary",
                disabled=not sweep_theme.strip(),
            ):
                try:
                    sweep_result = _call(
                        "正在地毯式排查全库…",
                        run_theme_sweep_action,
                        content_root,
                        theme=sweep_theme.strip(),
                        extra_terms=[
                            t.strip()
                            for t in sweep_terms_raw.replace("，", ",").split(",")
                            if t.strip()
                        ],
                        use_llm=bool(use_judge and not _ai_locked),
                        llm_mode=llm_mode,
                        llm_model=llm_model,
                        sqlite_path=sqlite_path,
                    )
                except Exception as e:
                    _fail(e)
                else:
                    _track_cost(sweep_result)
                    st.session_state["sweep_report"] = sweep_result
            sweep_report = st.session_state.get("sweep_report")
            if sweep_report:
                _chips(
                    _chip("扫描对象 ", strong=str(sweep_report["scanned_total"]), kind="blue"),
                    _chip("直接命中 ", strong=str(len(sweep_report["hits"])), kind="red"),
                    _chip(
                        "关联待查 ",
                        strong=str(len(sweep_report["review_suggested"])),
                        kind="amber",
                    ),
                    _chip(
                        "语义判定 ",
                        strong=(
                            f"{sweep_report['judged_count']} 项"
                            if sweep_report["llm_used"]
                            else "未启用"
                        ),
                        kind="gold" if sweep_report["llm_used"] else "",
                    ),
                )
                if sweep_report["judge_skipped"]:
                    st.warning(
                        f"有 {sweep_report['judge_skipped']} 个对象超出单次判定上限未过模型，"
                        "工作单中已注明——必要时分批再跑。"
                    )
                all_findings = sweep_report["hits"] + sweep_report["review_suggested"]
                if all_findings:
                    sweep_df = pd.DataFrame(
                        [
                            {
                                "处置": "待处理" if f["verdict"] == "hit" else "建议复查",
                                "类型": f["object_kind"],
                                "名称": f["name"],
                                "引用": f["ref"],
                                "证据": f["evidence"],
                            }
                            for f in all_findings
                        ]
                    )
                    st.dataframe(sweep_df, use_container_width=True, hide_index=True, height=320)
                else:
                    st.success("全库扫描完毕，未发现与该主题相关的内容。")
                st.download_button(
                    "导出工作单 (.md)",
                    data=sweep_report["markdown"].encode("utf-8"),
                    file_name=f"sweep-{sweep_report['theme'][:20]}.md",
                    mime="text/markdown",
                    icon=":material/checklist:",
                )
                _show_cost(sweep_report)

# ------------------------------------------------------------------------------ impact
with tab_impact:
    if not _ready:
        _show_project_required(content_root)
    else:
        _section("影响分析", "动一发之前，先看全身")
        ref_options: list[str] = []
        ref_display: dict[str, str] = {}
        if inventory is not None:
            for row in inventory["entities"]:
                ref = f"entity:{row['id']}"
                ref_display[ref] = (
                    f"{row['name']}（{_ENTITY_TYPE_LABEL.get(row['type'], row['type'])}）"
                )
            for row in inventory["quests"]:
                ref_display[f"quest:{row['id']}"] = f"{row['title']}（任务）"
            for row in inventory["regions"]:
                ref_display[f"region:{row['id']}"] = f"{row['name']}（区域）"
            for row in inventory["pois"]:
                ref_display[f"poi:{row['id']}"] = f"{row['name']}（兴趣点）"
            ref_options = inventory["graph_refs"]
        with st.form("impact_form"):
            cols = st.columns([2, 3, 1])
            change_type = cols[0].selectbox(
                "变更类型",
                [item.value for item in ChangeType],
                format_func=lambda v: _CHANGE_TYPE_LABEL.get(v, v),
            )
            if ref_options:
                target_ref = cols[1].selectbox(
                    "目标对象（可输入过滤）",
                    options=ref_options,
                    format_func=lambda r: f"{ref_display.get(r, r)} · {r}",
                )
            else:
                target_ref = cols[1].text_input(
                    "目标引用", placeholder="entity:<实体ID> / quest:<任务ID>"
                )
            max_depth = cols[2].number_input("传播深度", min_value=1, max_value=4, value=2)
            submitted = st.form_submit_button("推演波及", icon=":material/hub:", type="primary")
        if submitted and target_ref:
            try:
                result = _call(
                    "正在遍历影响图谱…",
                    run_impact_action,
                    content_root,
                    changes=[{"change_type": change_type, "target_ref": target_ref}],
                    sqlite_path=sqlite_path,
                    max_depth=int(max_depth),
                )
            except Exception as e:
                _fail(e)
            else:
                _track_cost(result)
                _chips(
                    _chip("波及对象 ", strong=str(result["total"]), kind="gold"),
                    _chip("必须改 ", strong=str(len(result["must_change"])), kind="red"),
                    _chip("建议查 ", strong=str(len(result["suggest_check"])), kind="amber"),
                )
                must, suggest = st.columns(2)
                with must, st.container(border=True):
                    st.markdown(f":material/error: **必须改（{len(result['must_change'])}）**")
                    if not result["must_change"]:
                        st.caption("没有强制联动项。")
                    for item in result["must_change"]:
                        name = ref_display.get(item["target_ref"], "")
                        st.write(f"- `{item['target_ref']}` {name}")
                with suggest, st.container(border=True):
                    st.markdown(f":material/warning: **建议查（{len(result['suggest_check'])}）**")
                    if not result["suggest_check"]:
                        st.caption("没有建议复查项。")
                    for item in result["suggest_check"]:
                        name = ref_display.get(item["target_ref"], "")
                        st.write(f"- `{item['target_ref']}` {name}")
                _show_cost(result)

# ------------------------------------------------------------------------------ create
with tab_create:
    if not _ready:
        _show_project_required(content_root)
    else:
        draft_tab, tree_tab, barks_tab, flavor_tab = st.tabs(
            [
                ":material/draft: 任务草稿",
                ":material/account_tree: 对话树",
                ":material/record_voice_over: 台词工坊",
                ":material/category: 物案文集",
            ]
        )
        npc_rows = (
            [row for row in inventory["entities"] if row["type"] == "npc"]
            if inventory is not None
            else []
        )
        npc_label = {row["id"]: f"{row['name']} · {row['id']}" for row in npc_rows}
        with draft_tab:
            draft_placeholder = "描述任务背景、目标与冲突……"
            if inventory is not None and inventory["pois"]:
                poi = inventory["pois"][0]
                draft_placeholder = f"例如：为{poi['name']}写一个调查异常事件的支线任务。"
            brief = st.text_area("任务简报", placeholder=draft_placeholder, height=100)
            if (
                st.button("起草任务", icon=":material/draft:", type="primary", disabled=_ai_locked)
                and brief.strip()
            ):
                try:
                    result = _call(
                        "正在起草任务…",
                        run_draft_action,
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
                    quest = result["quest"]
                    st.success(f"任务草稿 `{quest['id']}` 已入审阅台候批。")
                    with st.container(border=True):
                        st.markdown(f"**{quest.get('title', quest['id'])}**")
                        if quest.get("objective"):
                            st.write(quest["objective"])
                        for stage in quest.get("stages", []):
                            st.write(f"- {stage.get('summary', '')}")
                    if result["issues"]:
                        st.warning(f"自动审计发现 {len(result['issues'])} 条提示。")
                    with st.expander("完整 JSON"):
                        st.code(json.dumps(quest, ensure_ascii=False, indent=2), language="json")
                    _show_cost(result)
        with tree_tab:
            if not npc_rows:
                st.info("档案中还没有角色。先创世或导入角色设定。")
            else:
                _prune_picker_state("tree_participants", set(npc_label))
                participants = st.multiselect(
                    "对话参与者",
                    options=list(npc_label),
                    format_func=lambda i: npc_label[i],
                    key="tree_participants",
                )
                tree_brief = st.text_input(
                    "对话简报", placeholder="一次对质 / 委托交接 / 雨夜重逢……"
                )
                quest_options = [""] + [row["id"] for row in (inventory or {}).get("quests", [])]
                tree_quest = st.selectbox(
                    "挂接任务（可选）",
                    options=quest_options,
                    format_func=lambda q: "不挂接" if not q else q,
                )
                cols = st.columns(2)
                tree_max_nodes = cols[0].slider("最大节点数", 4, 24, 12)
                tree_max_chars = cols[1].slider("单句最大字数", 20, 200, 120)
                if (
                    st.button(
                        "编织对话树",
                        icon=":material/account_tree:",
                        type="primary",
                        disabled=_ai_locked,
                    )
                    and participants
                    and tree_brief.strip()
                ):
                    try:
                        result = _call(
                            "正在编织对话树…",
                            run_dialogue_tree_action,
                            content_root,
                            participant_ids=participants,
                            brief=tree_brief.strip(),
                            quest_id=tree_quest or None,
                            sqlite_path=sqlite_path,
                            max_nodes=int(tree_max_nodes),
                            max_chars=int(tree_max_chars),
                            llm_mode=llm_mode,
                            llm_model=llm_model,
                        )
                    except Exception as e:
                        _fail(e)
                    else:
                        _track_cost(result)
                        tree = result["tree"]
                        st.success(f"对话树 `{tree['id']}` 已入审阅台候批。")
                        name_of = {row["id"]: row["name"] for row in npc_rows}
                        st.graphviz_chart(_tree_dot(tree, name_of), use_container_width=True)
                        if result["structure_problems"]:
                            st.warning("结构提示：" + "；".join(result["structure_problems"]))
                        if result["lint_issues"]:
                            st.warning(f"文本 lint 提示 {len(result['lint_issues'])} 条。")
                        with st.expander("完整 JSON"):
                            st.code(json.dumps(tree, ensure_ascii=False, indent=2), language="json")
                        _show_cost(result)
        with barks_tab:
            if not npc_rows:
                st.info("档案中还没有角色。先创世或导入角色设定。")
                speaker_ids: list[str] = []
            else:
                _prune_picker_state("barks_speakers", set(npc_label))
                speaker_ids = st.multiselect(
                    "说话人",
                    options=list(npc_label),
                    format_func=lambda i: npc_label[i],
                    key="barks_speakers",
                )
            topic = st.text_input("台词主题", placeholder="发现可疑人物 / 雨夜闲谈 / 战前动员")
            cols = st.columns(2)
            variants = cols[0].slider("每人变体数", 1, 10, 4)
            max_chars = cols[1].slider("单条最大字数", 8, 200, 40)
            if (
                st.button(
                    "批量撰写台词",
                    icon=":material/record_voice_over:",
                    type="primary",
                    disabled=_ai_locked,
                )
                and speaker_ids
                and topic.strip()
            ):
                try:
                    result = _call(
                        "正在批量撰写台词…",
                        run_barks_action,
                        content_root,
                        speaker_ids=speaker_ids,
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
                    _chips(
                        _chip("入队 ", strong=str(len(result["accepted"])), kind="green"),
                        _chip("被过滤 ", strong=str(len(result["rejected"])), kind="red"),
                    )
                    name_of = {row["id"]: row["name"] for row in npc_rows}
                    for variant in result["accepted"]:
                        speaker = name_of.get(variant["speaker_id"], variant["speaker_id"])
                        st.write(f"- **{speaker}**：{variant['text']}")
                    if result["rejected"]:
                        with st.expander(f"被过滤的 {len(result['rejected'])} 条"):
                            for rejected in result["rejected"]:
                                codes = "、".join(rejected["issues"])
                                st.write(f"- {rejected['text']}（{codes}）")
                    _show_cost(result)
        with flavor_tab:
            category_label = (
                st.pills(
                    "类别",
                    options=["物品", "技能", "成就"],
                    default="物品",
                    label_visibility="collapsed",
                )
                or "物品"
            )
            label_to_cat = {v: k for k, v in _FLAVOR_CATEGORY_LABEL.items()}
            flavor_names = st.text_area(
                "名称清单（每行一个）",
                placeholder="雾隐灯\n枯叶军徽\n远航者的罗盘",
                height=120,
            )
            cols = st.columns(2)
            flavor_theme = cols[0].text_input("主题/风味", placeholder="雾隐城异象 / 旧王朝遗物")
            flavor_max_chars = cols[1].slider("风味文本最大字数", 20, 300, 120)
            names = [line.strip() for line in flavor_names.splitlines() if line.strip()]
            if (
                st.button(
                    "批量撰写物案",
                    icon=":material/category:",
                    type="primary",
                    disabled=_ai_locked,
                )
                and names
            ):
                try:
                    result = _call(
                        "正在为物案润色…",
                        run_flavor_action,
                        content_root,
                        category=label_to_cat[category_label],
                        names=names,
                        theme=flavor_theme.strip(),
                        sqlite_path=sqlite_path,
                        max_chars=int(flavor_max_chars),
                        llm_mode=llm_mode,
                        llm_model=llm_model,
                    )
                except Exception as e:
                    _fail(e)
                else:
                    _track_cost(result)
                    _chips(
                        _chip("入队 ", strong=str(len(result["accepted"])), kind="green"),
                        _chip("被过滤 ", strong=str(len(result["rejected"])), kind="red"),
                    )
                    for entry in result["accepted"]:
                        with st.container(border=True):
                            st.markdown(f"**{entry['name']}**")
                            if entry["description"]:
                                st.write(entry["description"])
                            if entry["flavor"]:
                                st.caption(f"“{entry['flavor']}”")
                    if result["rejected"]:
                        with st.expander(f"被过滤的 {len(result['rejected'])} 条"):
                            for rejected in result["rejected"]:
                                codes = "、".join(rejected["issues"])
                                st.write(f"- {rejected['name']}（{codes}）")
                    _show_cost(result)

# ------------------------------------------------------------------------------ review
with tab_review:
    if not _ready:
        _show_project_required(content_root)
    else:
        _section("审阅台", "朱批之处，方成正史——采纳入档，驳回焚稿，皆留你的署名")
        if not operator.strip():
            st.info(
                "先到「设置 → 创作护栏」落下署名——每道朱批都会记下是谁的手笔。",
                icon=":material/draw:",
            )
        try:
            queue = list_review_items_action(content_root, sqlite_path=sqlite_path)
        except Exception as e:
            _fail(e)
        else:
            items = queue["items"]
            if not items:
                st.success("案头清净——暂无候批的草稿。")
            else:
                type_counter = Counter(item["item_type"] for item in items)
                filter_options = ["全部"] + [_ITEM_TYPE_LABEL.get(t, t) for t in type_counter]
                label_to_type = {_ITEM_TYPE_LABEL.get(t, t): t for t in type_counter}
                picked_label = (
                    st.pills(
                        "类型筛选",
                        options=filter_options,
                        default="全部",
                        label_visibility="collapsed",
                    )
                    or "全部"
                )
                shown = [
                    item
                    for item in items
                    if picked_label == "全部"
                    or item["item_type"] == label_to_type.get(picked_label)
                ]
                st.caption(f"待审 {len(shown)} / {len(items)} 项")
                name_of_all = (
                    {row["id"]: row["name"] for row in inventory["entities"]}
                    if inventory is not None
                    else {}
                )
                for item in shown:
                    type_icon = _ITEM_TYPE_ICON.get(item["item_type"], ":material/description:")
                    label = _ITEM_TYPE_LABEL.get(item["item_type"], item["item_type"])
                    payload = item["payload"]
                    with st.container(border=True):
                        head, accept_col, reject_col = st.columns([6, 1, 1])
                        head.markdown(f"{type_icon} **{label}** ｜ `{item['object_ref']}`")
                        if item["issue_refs"]:
                            head.caption(f"关联问题指纹 {len(item['issue_refs'])} 条")
                        if item["item_type"] == "quest_draft":
                            title = payload.get("title") or payload.get("id", "")
                            head.write(f"**{title}** — {payload.get('objective', '')}")
                        elif item["item_type"] == "bark_variant":
                            head.write(
                                f"**{payload.get('speaker_id', '')}**：{payload.get('text', '')}"
                            )
                        elif item["item_type"] in {"world_seed", "import_draft"}:
                            head.write(payload.get("summary", ""))
                            bundle = payload.get("bundle") or {}
                            head.caption(
                                f"实体 {len(bundle.get('entities', {}))} ｜ "
                                f"任务 {len(bundle.get('quests', {}))} ｜ "
                                f"区域 {len(bundle.get('regions', {}))} ｜ "
                                f"关系 {len(bundle.get('relations', []))}"
                            )
                            open_gaps = payload.get("open_gaps") or []
                            if open_gaps:
                                head.warning(f"仍有 {len(open_gaps)} 个未补全字段。")
                        elif item["item_type"] == "dialogue_tree":
                            head.write(f"**{payload.get('title', '')}**")
                            node_count = len(payload.get("nodes") or {})
                            head.caption(f"节点 {node_count}")
                            with st.expander("对话树结构"):
                                st.graphviz_chart(
                                    _tree_dot(payload, name_of_all),
                                    use_container_width=True,
                                )
                        elif item["item_type"] == "flavor_batch":
                            entries = payload.get("entities") or []
                            category = payload.get("category", "")
                            cat_label = _FLAVOR_CATEGORY_LABEL.get(category, category)
                            head.caption(f"{cat_label} × {len(entries)}")
                            for entry in entries[:3]:
                                head.write(f"- **{entry.get('name', '')}**")
                        elif item["item_type"] == "patch_candidate":
                            head.write(payload.get("rationale", ""))
                        with st.expander("完整内容 JSON"):
                            st.code(
                                json.dumps(payload, ensure_ascii=False, indent=2),
                                language="json",
                            )
                        decide_disabled = not operator.strip()
                        if accept_col.button(
                            "采纳",
                            key=f"acc_{item['id']}",
                            icon=":material/task_alt:",
                            type="primary",
                            disabled=decide_disabled,
                        ):
                            try:
                                decided = _call(
                                    "正在钤印入档…",
                                    decide_review_action,
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
                                _flash(
                                    "success",
                                    f"已钤印入档{f'：{written}' if written else ''}。",
                                )
                                st.rerun()
                        if reject_col.button(
                            "驳回",
                            key=f"rej_{item['id']}",
                            icon=":material/close:",
                            disabled=decide_disabled,
                        ):
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
                                _flash("success", "已驳回，草稿就地焚毁。")
                                st.rerun()

# ------------------------------------------------------------------------------ export
with tab_export:
    if not _ready:
        _show_project_required(content_root)
    else:
        _section("导出交付", "装订成册，或交付引擎——每件产物附校验指纹")
        cols = st.columns([2, 3])
        with cols[0]:
            engine = st.radio(
                "目标引擎",
                options=list(_ENGINE_META),
                format_func=lambda e: _ENGINE_META[e][0],
                captions=[meta[1] for meta in _ENGINE_META.values()],
            )
            output_dir = st.text_input("输出目录", value=".tmp/exports")
            export_clicked = st.button("交付引擎", icon=":material/archive:", type="primary")
        with cols[1]:
            if export_clicked:
                try:
                    result = _call(
                        "正在打包导出…",
                        run_project_export_action,
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
                    files = result["manifest"]["files"]
                    df = pd.DataFrame(
                        [
                            {
                                "文件": file["path"],
                                "类型": file["kind"],
                                "sha256": file["sha256"][:16] + "…",
                            }
                            for file in files
                        ]
                    )
                    st.dataframe(df, use_container_width=True, hide_index=True)
                    _show_cost(result)
            else:
                st.caption("选好引擎与输出目录，点「交付引擎」。")

        st.divider()
        _section("设定集导出", "可阅读的世界书（Markdown / Word）")
        lb_cols = st.columns([2, 2, 1])
        lb_title = lb_cols[0].text_input("设定集标题", value="世界设定集")
        lb_formats = lb_cols[1].multiselect(
            "格式",
            ["md", "docx"],
            default=["md", "docx"],
            format_func=lambda f: "Markdown" if f == "md" else "Word (.docx)",
        )
        if (
            lb_cols[2].button("装订设定集", icon=":material/menu_book:", type="primary")
            and lb_formats
        ):
            try:
                lb_result = _call(
                    "正在装订设定集…",
                    run_lorebook_export_action,
                    content_root,
                    output_dir=Path(output_dir) / "lorebook",
                    formats=tuple(lb_formats),
                    title=lb_title.strip() or "世界设定集",
                    sqlite_path=sqlite_path,
                )
            except Exception as e:
                _fail(e)
            else:
                _track_cost(lb_result)
                st.success(f"已导出到 `{lb_result['output_dir']}`")
                for row in lb_result["files"]:
                    st.write(f"- `{row['path']}` `sha256:{row['sha256'][:12]}…`")

        st.divider()
        _section("世界包", "整个世界一包带走——备份、换机、交接都用它")
        pack_cols = st.columns([3, 2])
        pack_cols[0].caption(
            "打包当前世界的全部档案（不含可重建的运行库）。"
            "在侧栏「新建 / 导入世界」上传同一个 zip，即可在任何机器上还原这个世界。"
        )
        try:
            _pack_bytes = export_world_zip(content_root)
        except Exception as e:
            _fail(e)
        else:
            pack_cols[1].download_button(
                "下载世界包 (.zip)",
                data=_pack_bytes,
                file_name=f"{Path(content_root).name or 'world'}-pack.zip",
                mime="application/zip",
                icon=":material/archive:",
                type="primary",
                use_container_width=True,
            )
