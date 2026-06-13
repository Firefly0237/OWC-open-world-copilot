<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref } from "vue";

interface Step {
  selector?: string; // sidebar group to highlight; absent = centered card
  title: string;
  body: string;
}

// One step per sidebar section, mirroring the legacy onboarding's "walk the whole tool".
const STEPS: Step[] = [
  {
    title: "欢迎来到 OWCopilot",
    body: "一个本地优先的开放世界策划工作台：查设定有出处、跑审查有证据、AI 产物过人审、引擎导出带校验。花一分钟带你认认门。",
  },
  {
    selector: '[data-tour="概览"]',
    title: "概览",
    body: "世界总览看实体/任务/关系的统计与内容溯源；设定档案翻阅、编辑、删除每一条已入档的设定。",
  },
  {
    selector: '[data-tour="创世 · 创作"]',
    title: "创世 · 创作",
    body: "创世工坊从一句话开辟整个世界；人物工坊生成可维护的角色卡；创作工坊产出任务草稿、对话树、台词、物案——都只引用图谱内实体。",
  },
  {
    selector: '[data-tour="内容带入"]',
    title: "内容带入",
    body: "已有设定不用重做：文稿提炼把文档整理成结构化草案、表格导入批量入库、灵感库收录参考素材。",
  },
  {
    selector: '[data-tour="校勘 · 分析"]',
    title: "校勘 · 分析",
    body: "校勘修复跑确定性审计并给出可应用、可回滚的修复；影响分析在改动前推演涟漪；专项清查地毯式排查某类主题。",
  },
  {
    selector: '[data-tour="问答 · 交付"]',
    title: "问答 · 交付",
    body: "世界问答有据必答、查无则拒；审阅台是 AI 产物落盘的唯一通道——你执朱笔；导出交付出设定集、世界包与引擎数据包。",
  },
  {
    selector: '[data-tour="管理"]',
    title: "管理",
    body: "工作区管理你的全部世界；设置里接入你自己的模型（Key 只进本机进程）。接入后，创世/人物/问答/创作才会走真实模型。",
  },
  {
    title: "开始吧",
    body: "建议先去「设置」接入模型，再到「工作区」建一个属于你的世界。随时可以从左下角重新打开本引导。",
  },
];

const props = defineProps<{ open: boolean }>();
const emit = defineEmits<{ close: [] }>();

const index = ref(0);
const rect = ref<{ top: number; left: number; width: number; height: number } | null>(null);
const reduced = typeof window !== "undefined" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

const step = computed(() => STEPS[index.value]);
const isLast = computed(() => index.value === STEPS.length - 1);

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

// card sits to the right of the highlighted group, or centered when there's no target
const cardStyle = computed(() => {
  if (!rect.value) {
    return { top: "50%", left: "50%", transform: "translate(-50%, -50%)" };
  }
  const left = Math.min(rect.value.left + rect.value.width + 16, window.innerWidth - 360);
  const top = Math.max(16, Math.min(rect.value.top, window.innerHeight - 240));
  return { top: `${top}px`, left: `${left}px` };
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

function onKey(e: KeyboardEvent): void {
  if (!props.open) return;
  if (e.key === "Escape") finish();
  else if (e.key === "ArrowRight") next();
  else if (e.key === "ArrowLeft") prev();
}
function onResize(): void {
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

// re-locate whenever opened
import { watch } from "vue";
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
      <div class="ring" :style="ringStyle"></div>
      <div class="card" :style="cardStyle">
        <div class="step-count">{{ index + 1 }} / {{ STEPS.length }}</div>
        <h3>{{ step.title }}</h3>
        <p>{{ step.body }}</p>
        <div class="actions">
          <button class="ghost" @click="finish">跳过</button>
          <span class="spacer"></span>
          <button v-if="index > 0" class="ghost" @click="prev">上一步</button>
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
  background: rgba(6, 9, 22, 0.66);
  backdrop-filter: blur(1.5px);
}

.ring {
  position: fixed;
  border: 1.5px solid var(--ow-gold-bright);
  border-radius: 0.6rem;
  box-shadow:
    0 0 0 9999px rgba(6, 9, 22, 0.66),
    0 0 18px rgba(240, 210, 138, 0.5);
  transition:
    top 0.3s ease,
    left 0.3s ease,
    width 0.3s ease,
    height 0.3s ease;
  pointer-events: none;
}

.card {
  position: fixed;
  width: 340px;
  max-width: calc(100vw - 32px);
  background: linear-gradient(180deg, rgba(24, 31, 64, 0.98), rgba(16, 21, 48, 0.98));
  border: 1px solid var(--ow-gold-soft);
  border-radius: 0.8rem;
  padding: 1rem 1.1rem;
  box-shadow: 0 10px 40px rgba(0, 0, 0, 0.5);
  transition:
    top 0.3s ease,
    left 0.3s ease;
}

.step-count {
  font-size: 0.72rem;
  color: var(--ow-cyan);
  letter-spacing: 0.1em;
  margin-bottom: 0.3rem;
}

.card h3 {
  margin: 0 0 0.4rem;
  font-family: var(--ow-serif);
  color: var(--ow-gold-bright);
  font-size: 1.05rem;
}

.card p {
  margin: 0 0 0.9rem;
  font-size: 0.86rem;
  line-height: 1.65;
  color: var(--ow-ink);
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
  border-radius: 0.5rem;
  cursor: pointer;
  font: inherit;
  font-size: 0.83rem;
  padding: 0.4rem 0.9rem;
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
