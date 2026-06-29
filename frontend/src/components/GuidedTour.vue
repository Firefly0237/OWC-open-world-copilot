<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from "vue";
import { useRouter } from "vue-router";

const router = useRouter();

interface Feature {
  name: string;
  desc: string;
}
interface Step {
  selector?: string; // sidebar group to spotlight; absent = centered card
  title: string;
  body: string;
  features?: Feature[];
}

// One step per sidebar section. Each lists the features inside it with a one-line "what it does /
// when to reach for it", so the tour actually teaches the tool instead of naming the menus.
const STEPS: Step[] = [
  {
    title: "欢迎来到 OWCopilot",
    body: "本地优先的开放世界策划工作台。下面逐个区块带你认门，方向键 ← → 翻页，Esc 关闭。",
  },
  {
    selector: '[data-tour="概览"]',
    title: "概览",
    body: "掌握世界的全貌与每条设定的来路。",
    features: [
      { name: "世界总览", desc: "规模、来源与就绪度，点指标可展开明细。" },
      { name: "设定档案", desc: "逐条查阅与编辑已入档设定。" },
    ],
  },
  {
    selector: '[data-tour="创世 · 创作"]',
    title: "创世 · 创作",
    body: "从一句话长成整个世界，再把人物与故事逐层做厚。",
    features: [
      { name: "创世工坊", desc: "一句话开辟整个世界。" },
      { name: "扩写工坊", desc: "锚定焦点，长出接地于既有设定的新内容。" },
      { name: "人物工坊", desc: "生成可维护的角色卡。" },
      { name: "创作工坊", desc: "产出任务、对话树、台词与物品文案。" },
      { name: "对话流", desc: "把对话树铺成可编辑的分支流程图。" },
    ],
  },
  {
    selector: '[data-tour="汇编 · 入库"]',
    title: "汇编 · 入库",
    body: "把现成素材接进来变成结构化设定。",
    features: [
      { name: "文稿提炼", desc: "文档与小说整理成结构化设定。" },
      { name: "表格导入", desc: "表格批量入库，先预演再写入。" },
      { name: "灵感库", desc: "收录参考素材，仅用于创世检索。" },
    ],
  },
  {
    selector: '[data-tour="校勘 · 分析"]',
    title: "校勘 · 分析",
    body: "改动前看清涟漪，改动后用规则引擎检查一致性。",
    features: [
      { name: "校勘修复", desc: "审查一致性，给出可回滚的修复。" },
      { name: "影响分析", desc: "预演一处改动会牵连哪些设定。" },
      { name: "时间线", desc: "任务与事件按编年排布，违例标红。" },
      { name: "关系星图", desc: "点节点改名、连线、拖动重排。" },
      { name: "专项清查", desc: "按主题地毯式排查同类问题。" },
    ],
  },
  {
    selector: '[data-tour="问答 · 交付"]',
    title: "问答 · 交付",
    body: "对世界发问、守门落盘、打包交付。",
    features: [
      { name: "世界问答", desc: "基于已入档设定回答，查不到会直说。" },
      { name: "审阅台", desc: "AI 产物逐条采纳或退回。" },
      { name: "导出交付", desc: "出设定集、世界包与引擎数据。" },
    ],
  },
  {
    selector: '[data-tour="管理"]',
    title: "管理",
    body: "管理世界、拍快照对比。模型接入在左下角「设置」。",
    features: [
      { name: "工作区", desc: "新建、切换与管理世界。" },
      { name: "变更史", desc: "快照与当前状态对比。" },
    ],
  },
  {
    title: "开始吧",
    body:
      "建议先到「设置」填入 AI 服务的账号凭证（API Key），这样才能用生成功能。如果暂时没有，也可以先到「工作区」建一个世界，导入已有素材、使用一致性检查等功能不需要接入模型。" +
      "每个页面标题旁的「?」可看本页详细说明，左下角「新手引导」随时重开本向导。",
  },
];

const props = defineProps<{ open: boolean }>();
const emit = defineEmits<{ close: [] }>();

const index = ref(0);
const rect = ref<{ top: number; left: number; width: number; height: number } | null>(null);
const reduced =
  typeof window !== "undefined" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

const step = computed(() => STEPS[index.value]);
const isLast = computed(() => index.value === STEPS.length - 1);
const CARD_W = 384;

async function locate(): Promise<void> {
  await nextTick();
  const sel = step.value.selector;
  if (!sel) {
    rect.value = null;
    return;
  }
  const el = document.querySelector(sel);
  if (!el) {
    rect.value = null;
    return;
  }
  el.scrollIntoView({ block: "nearest", behavior: reduced ? "auto" : "smooth" });
  const r = el.getBoundingClientRect();
  rect.value = { top: r.top, left: r.left, width: r.width, height: r.height };
}

// Card sits to the right of the spotlighted group (with an arrow pointing back at it), or centered
// when there's no target or the viewport is too narrow to fit it beside the sidebar.
const narrow = ref(typeof window !== "undefined" && window.innerWidth < 720);
const placeRight = computed(() => !!rect.value && !narrow.value);

const cardStyle = computed(() => {
  if (!placeRight.value || !rect.value) {
    return { top: "50%", left: "50%", transform: "translate(-50%, -50%)" };
  }
  const left = Math.min(rect.value.left + rect.value.width + 22, window.innerWidth - CARD_W - 16);
  const top = Math.max(16, Math.min(rect.value.top - 8, window.innerHeight - 320));
  return { top: `${top}px`, left: `${left}px` };
});

// Vertical offset of the arrow so it points at the middle of the spotlighted group.
const arrowTop = computed(() => {
  if (!placeRight.value || !rect.value) return "32px";
  const cardTop = Math.max(16, Math.min(rect.value.top - 8, window.innerHeight - 320));
  const target = rect.value.top + rect.value.height / 2 - cardTop;
  return `${Math.max(18, Math.min(target, 300))}px`;
});

const ringStyle = computed(() => {
  if (!rect.value) return { display: "none" };
  const pad = 6;
  return {
    top: `${rect.value.top - pad}px`,
    left: `${rect.value.left - pad}px`,
    width: `${rect.value.width + pad * 2}px`,
    height: `${rect.value.height + pad * 2}px`,
  };
});

function next(): void {
  if (isLast.value) finish();
  else {
    index.value += 1;
    void locate();
  }
}
function prev(): void {
  if (index.value > 0) {
    index.value -= 1;
    void locate();
  }
}
function finish(): void {
  localStorage.setItem("owcopilot_tour_done", "1");
  emit("close");
}

function goSettings(): void {
  finish();
  void router.push("/settings");
}

function onKey(e: KeyboardEvent): void {
  if (!props.open) return;
  if (e.key === "Escape") finish();
  else if (e.key === "ArrowRight") next();
  else if (e.key === "ArrowLeft") prev();
}
function onResize(): void {
  narrow.value = window.innerWidth < 720;
  void locate();
}

onMounted(() => {
  window.addEventListener("keydown", onKey);
  window.addEventListener("resize", onResize);
  if (props.open) {
    index.value = 0;
    void locate();
  }
});
onUnmounted(() => {
  window.removeEventListener("keydown", onKey);
  window.removeEventListener("resize", onResize);
});

watch(
  () => props.open,
  (v) => {
    if (v) {
      index.value = 0;
      void locate();
    }
  },
);
</script>

<template>
  <Transition name="tour-fade">
    <div v-if="open" class="tour" @click.self="finish">
      <!-- Full-screen dim only when there's no spotlight; otherwise the ring's box-shadow dims the
           surround and leaves the highlighted element perfectly crisp (no blur over the target). -->
      <div v-if="!rect" class="scrim"></div>
      <div class="ring" :class="{ pulse: !reduced }" :style="ringStyle"></div>
      <div class="card" :class="{ 'point-left': placeRight }" :style="cardStyle">
        <span v-if="placeRight" class="arrow" :style="{ top: arrowTop }"></span>
        <div class="step-count">{{ index + 1 }} / {{ STEPS.length }} · 新手引导</div>
        <h3>{{ step.title }}</h3>
        <p class="body">{{ step.body }}</p>
        <ul v-if="step.features" class="features">
          <li v-for="f in step.features" :key="f.name">
            <b>{{ f.name }}</b><span>{{ f.desc }}</span>
          </li>
        </ul>
        <div class="actions">
          <button class="ghost" @click="finish">跳过</button>
          <span class="spacer"></span>
          <button v-if="index > 0" class="ghost" @click="prev">上一步</button>
          <button v-if="isLast" class="settings-link" @click="goSettings">去设置</button>
          <button class="primary" @click="next">{{ isLast ? "完成" : "下一步" }}</button>
        </div>
      </div>
    </div>
  </Transition>
</template>

<style scoped>
.tour {
  position: fixed;
  inset: 0;
  z-index: 9000;
}

/* the only full-screen dim — used when there is no element to spotlight */
.scrim {
  position: fixed;
  inset: 0;
  background: rgba(6, 9, 22, 0.72);
}

.ring {
  position: fixed;
  border: 1.5px solid var(--ow-gold-bright);
  border-radius: 0.6rem;
  /* the huge spread box-shadow IS the dimmer: everything outside the ring darkens, the element
     inside stays sharp and unblurred — fixing the old "the thing being explained is blurry" bug. */
  box-shadow:
    0 0 0 9999px rgba(6, 9, 22, 0.72),
    0 0 0 1px rgba(240, 210, 138, 0.6),
    0 0 22px rgba(240, 210, 138, 0.55);
  transition:
    top 0.3s ease,
    left 0.3s ease,
    width 0.3s ease,
    height 0.3s ease;
  pointer-events: none;
}

.ring.pulse {
  animation: ring-pulse 2s ease-in-out infinite;
}

@keyframes ring-pulse {
  0%,
  100% {
    box-shadow:
      0 0 0 9999px rgba(6, 9, 22, 0.72),
      0 0 0 1px rgba(240, 210, 138, 0.55),
      0 0 18px rgba(240, 210, 138, 0.4);
  }
  50% {
    box-shadow:
      0 0 0 9999px rgba(6, 9, 22, 0.72),
      0 0 0 1px rgba(240, 210, 138, 0.85),
      0 0 28px rgba(240, 210, 138, 0.7);
  }
}

.card {
  position: fixed;
  width: 384px;
  max-width: calc(100vw - 32px);
  max-height: calc(100vh - 32px);
  overflow-y: auto;
  background: linear-gradient(180deg, rgba(26, 33, 68, 0.99), rgba(15, 20, 46, 0.99));
  border: 1px solid var(--ow-gold-soft);
  border-radius: 0.85rem;
  padding: 1.05rem 1.2rem;
  box-shadow:
    0 14px 48px rgba(0, 0, 0, 0.55),
    inset 0 1px 0 rgba(240, 210, 138, 0.1);
  transition:
    top 0.3s ease,
    left 0.3s ease;
}

/* the connector arrow that ties the card to the highlighted group */
.card.point-left .arrow {
  position: absolute;
  left: -9px;
  width: 16px;
  height: 16px;
  background: linear-gradient(135deg, rgba(26, 33, 68, 0.99), rgba(20, 26, 58, 0.99));
  border-left: 1px solid var(--ow-gold-soft);
  border-bottom: 1px solid var(--ow-gold-soft);
  transform: rotate(45deg);
}

.step-count {
  font-size: 0.7rem;
  color: var(--ow-cyan);
  letter-spacing: 0.12em;
  margin-bottom: 0.3rem;
}

.card h3 {
  margin: 0 0 0.45rem;
  font-family: var(--ow-serif);
  color: var(--ow-gold-bright);
  font-size: 1.12rem;
}

.card .body {
  margin: 0 0 0.75rem;
  font-size: 0.85rem;
  line-height: 1.66;
  color: var(--ow-ink);
}

.features {
  list-style: none;
  margin: 0 0 0.85rem;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}

.features li {
  display: flex;
  flex-direction: column;
  gap: 0.12rem;
  padding: 0.42rem 0.6rem;
  border-left: 2px solid var(--ow-gold-soft);
  background: rgba(143, 214, 232, 0.045);
  border-radius: 0 0.4rem 0.4rem 0;
}

.features b {
  font-family: var(--ow-serif);
  color: var(--ow-gold-bright);
  font-size: 0.86rem;
}

.features span {
  font-size: 0.79rem;
  line-height: 1.5;
  color: var(--ow-muted);
}

.actions {
  display: flex;
  align-items: center;
  gap: 0.5rem;
}

.spacer {
  flex: 1;
}

button {
  border-radius: var(--ow-control-radius);
  cursor: pointer;
  font: inherit;
  font-size: 0.83rem;
  padding: 0.42rem 0.95rem;
  border: 1px solid var(--ow-line);
  background: var(--ow-panel-2);
  color: var(--ow-ink);
}

button.ghost {
  color: var(--ow-muted);
}

button.ghost:hover {
  color: var(--ow-ink);
  border-color: var(--ow-gold-soft);
}

button.primary {
  background: linear-gradient(180deg, #f0d28a 0%, #b9924a 100%);
  border-color: rgba(240, 210, 138, 0.65);
  color: #241a05;
  font-weight: 600;
}

button.settings-link {
  border-color: var(--ow-gold-soft);
  color: var(--ow-gold-bright);
  background: var(--ow-gold-faint);
}

button.settings-link:hover {
  box-shadow: 0 0 10px rgba(240, 210, 138, 0.25);
}

.tour-fade-enter-active,
.tour-fade-leave-active {
  transition: opacity 0.25s ease;
}

.tour-fade-enter-from,
.tour-fade-leave-to {
  opacity: 0;
}

@media (prefers-reduced-motion: reduce) {
  .ring,
  .card,
  .tour-fade-enter-active,
  .tour-fade-leave-active {
    transition: none;
  }
}
</style>
