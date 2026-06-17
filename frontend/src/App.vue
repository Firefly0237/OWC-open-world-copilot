<script setup lang="ts">
import { onMounted, onUnmounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import CommandPalette from "./components/CommandPalette.vue";
import GuidedTour from "./components/GuidedTour.vue";
import ToastHost from "./components/ToastHost.vue";
import Starfield from "./components/Starfield.vue";
import { apiGet, currentProject, llmConfig, sessionCost, setCurrentProject, setLlmConfig } from "./api";

const router = useRouter();
const route = useRoute();

interface NavItem {
  to: string;
  label: string;
}
interface NavGroup {
  title: string;
  items: NavItem[];
}

// grouped the way the legacy workbench was — clear sections, not a flat strip
const NAV: NavGroup[] = [
  { title: "概览", items: [
    { to: "/overview", label: "世界总览" },
    { to: "/archive", label: "设定档案" },
  ] },
  { title: "创世 · 创作", items: [
    { to: "/genesis", label: "创世工坊" },
    { to: "/expand", label: "扩写工坊" },
    { to: "/characters", label: "人物工坊" },
    { to: "/creation", label: "创作工坊" },
    { to: "/templates", label: "模板库" },
    { to: "/dialogues", label: "对话流" },
  ] },
  { title: "汇编 · 入库", items: [
    { to: "/extraction", label: "文稿提炼" },
    { to: "/import", label: "表格导入" },
    { to: "/references", label: "灵感库" },
  ] },
  { title: "校勘 · 分析", items: [
    { to: "/audit", label: "校勘修复" },
    { to: "/impact", label: "影响分析" },
    { to: "/analytics", label: "世界分析" },
    { to: "/timeline", label: "时间线" },
    { to: "/graph", label: "关系图谱" },
    { to: "/sweep", label: "专项清查" },
    { to: "/compliance", label: "版号合规" },
  ] },
  { title: "问答 · 交付", items: [
    { to: "/ask", label: "世界问答" },
    { to: "/review", label: "审阅台" },
    { to: "/localization", label: "本地化" },
    { to: "/export", label: "导出交付" },
  ] },
  { title: "管理", items: [
    { to: "/worlds", label: "工作区" },
    { to: "/history", label: "变更史" },
  ] },
];

// accordion nav: only one group's items are open at a time, so the rail reads at a glance instead
// of dumping all 21 destinations. The group holding the current route is kept open automatically.
function groupOf(path: string): string {
  const hit = NAV.find((g) => g.items.some((i) => i.to === path));
  return hit ? hit.title : NAV[0].title;
}
const openGroup = ref(groupOf(route.path));
function toggleGroup(title: string): void {
  openGroup.value = openGroup.value === title ? "" : title;
}
watch(
  () => route.path,
  (path) => {
    openGroup.value = groupOf(path);
  },
);

const booting = ref(true);
const workspaces = ref<string[]>([]);
const project = ref(currentProject());
const apiDown = ref(false);
const modelName = ref("");
const cost = ref(sessionCost());
const pricesConfigured = ref(false);
const tourOpen = ref(false);

function openTour(): void {
  tourOpen.value = true;
}

function switchProject(event: Event): void {
  const name = (event.target as HTMLSelectElement).value;
  if (!name) return;
  setCurrentProject(name);
  window.location.reload();
}

async function refreshModelBadge(): Promise<void> {
  try {
    const status = await apiGet<{ configured: boolean; prices_configured?: boolean }>(
      "/settings/connection",
    );
    const local = llmConfig();
    if (!status.configured && local.ready) setLlmConfig(false, "");
    modelName.value = status.configured && local.model ? local.model : "";
    pricesConfigured.value = status.prices_configured ?? false;
  } catch {
    modelName.value = "";
  }
}

function onLlmChanged(): void {
  void refreshModelBadge();
}
function onCostChanged(): void {
  cost.value = sessionCost();
}

// parallax: the aurora + starfield lean a few pixels toward the cursor, so the sky feels like depth
// you move through rather than a static wallpaper. rAF-throttled; off entirely for reduced-motion.
let parallaxRaf = 0;
function onPointer(e: PointerEvent): void {
  if (parallaxRaf) return;
  parallaxRaf = window.requestAnimationFrame(() => {
    parallaxRaf = 0;
    const x = (e.clientX / window.innerWidth - 0.5) * 2;
    const y = (e.clientY / window.innerHeight - 0.5) * 2;
    const root = document.documentElement.style;
    root.setProperty("--ow-px", x.toFixed(3));
    root.setProperty("--ow-py", y.toFixed(3));
  });
}
const motionOK =
  typeof window !== "undefined" &&
  !window.matchMedia("(prefers-reduced-motion: reduce)").matches;

// B11 · pro mode: a user toggle (independent of the OS reduced-motion setting) that drops the heavy
// decorative motion (aurora drift, starfield, warp transition) for daily high-frequency users.
const proMode = ref(localStorage.getItem("owcopilot_pro_mode") === "1");
function applyProMode(): void {
  document.body.classList.toggle("pro-mode", proMode.value);
}
function onProChanged(): void {
  proMode.value = localStorage.getItem("owcopilot_pro_mode") === "1";
  applyProMode();
}

async function projectResolves(name: string): Promise<boolean> {
  // A managed world is in /workspaces; an external (OWCOPILOT_PROJECTS_JSON) one is not, but still
  // resolves. Probe its overview once so boot can tell a valid external project from a stale id.
  try {
    await apiGet(`/projects/${encodeURIComponent(name)}/overview`);
    return true;
  } catch {
    return false;
  }
}

onMounted(async () => {
  applyProMode();
  window.addEventListener("ow-pro-changed", onProChanged);
  window.addEventListener("ow-llm-changed", onLlmChanged);
  window.addEventListener("ow-cost-changed", onCostChanged);
  if (motionOK) window.addEventListener("pointermove", onPointer, { passive: true });
  try {
    const body = await apiGet<{ workspaces: { name: string }[] }>("/workspaces");
    workspaces.value = body.workspaces.map((w) => w.name);
    const stored = localStorage.getItem("owcopilot_project");
    if (!workspaces.value.length) {
      // clean install: don't surface the bare "demo" fallback in the switcher
      project.value = "";
    } else if (!stored) {
      setCurrentProject(workspaces.value[0]);
      project.value = workspaces.value[0];
    } else if (!workspaces.value.includes(stored) && !(await projectResolves(stored))) {
      // The stored project is neither a managed world nor a resolvable external one (stale
      // localStorage, or a removed OWCOPILOT_PROJECTS_JSON entry). Fall back to an available world
      // so the data pages don't repeatedly 404 on a phantom project.
      setCurrentProject(workspaces.value[0]);
      project.value = workspaces.value[0];
    }
    // clean install: no worlds yet -> land on the workspace page to create the first one,
    // instead of a data page that would 404 on a non-existent project
    if (!workspaces.value.length && !["/worlds", "/settings"].includes(router.currentRoute.value.path)) {
      await router.replace("/worlds");
    }
    await refreshModelBadge();
  } catch {
    apiDown.value = true;
  } finally {
    window.setTimeout(() => {
      booting.value = false;
      // first-run onboarding, once per browser; reopen anytime from the sidebar
      if (!apiDown.value && localStorage.getItem("owcopilot_tour_done") !== "1") {
        tourOpen.value = true;
      }
    }, 500);
  }
});

onUnmounted(() => {
  window.removeEventListener("ow-llm-changed", onLlmChanged);
  window.removeEventListener("ow-cost-changed", onCostChanged);
  window.removeEventListener("pointermove", onPointer);
});
</script>

<template>
  <Transition name="splash">
    <div v-if="booting" class="splash" aria-hidden="true">
      <svg width="84" height="84" viewBox="0 0 100 100" fill="none">
        <circle class="orb" cx="50" cy="50" r="44" stroke="#8fd6e8" stroke-opacity=".35" stroke-dasharray="3 6" />
        <circle cx="50" cy="50" r="34" stroke="#d9b56c" stroke-opacity=".4" />
        <path class="core" d="M50 14 L56.5 43.5 L86 50 L56.5 56.5 L50 86 L43.5 56.5 L14 50 L43.5 43.5 Z" fill="#d9b56c" fill-opacity=".25" stroke="#f0d28a" stroke-opacity=".8" />
        <path class="core" d="M50 34 L52.8 47.2 L66 50 L52.8 52.8 L50 66 L47.2 52.8 L34 50 L47.2 47.2 Z" fill="#f0d28a" fill-opacity=".85" />
      </svg>
      <div class="splash-text">正在展卷</div>
      <div class="splash-line"></div>
    </div>
  </Transition>

  <!-- dynamic background: two parallax aurora layers behind all content. They drift on their own and
       lean toward the cursor (--ow-px/--ow-py), so wide screens read as depth you move through. -->
  <div class="aurora l1" aria-hidden="true"></div>
  <div class="aurora l2" aria-hidden="true"></div>
  <div class="grid-haze" aria-hidden="true"></div>
  <!-- rotating astrolabe + orbiting star trails (dropped in pro mode for a calmer workspace) -->
  <Starfield v-if="!proMode" />

  <div class="shell">
    <aside class="sidebar pane">
      <div class="brand">
        <svg width="26" height="26" viewBox="0 0 100 100" fill="none" aria-hidden="true">
          <path d="M50 8 L58 42 L92 50 L58 58 L50 92 L42 58 L8 50 L42 42 Z" fill="#f0d28a" fill-opacity=".9" />
        </svg>
        <div>
          <b>OWCopilot</b>
          <span class="muted">世界观工作台</span>
        </div>
      </div>

      <nav>
        <div
          v-for="group in NAV"
          :key="group.title"
          class="nav-group"
          :class="{ open: openGroup === group.title }"
          :data-tour="group.title"
        >
          <button type="button" class="nav-head" @click="toggleGroup(group.title)">
            <span class="nav-title">{{ group.title }}</span>
            <span class="nav-chev" aria-hidden="true"></span>
          </button>
          <div class="nav-items">
            <div class="nav-items-inner">
              <RouterLink
                v-for="item in group.items"
                :key="item.to"
                :to="item.to"
                class="nav-link"
              >
                <span class="nav-mark" aria-hidden="true"></span>{{ item.label }}
              </RouterLink>
            </div>
          </div>
        </div>
      </nav>

      <div class="side-footer">
        <RouterLink to="/settings" class="model" :class="{ off: !modelName }">
          <i class="dot"></i>{{ modelName || "未接入模型" }}
        </RouterLink>
        <span
          v-if="cost > 0"
          class="cost"
          :title="pricesConfigured ? '本次会话累计模型调用成本' : '按示例价估算；在 OWCOPILOT_PRICE_* 设置实际费率后为准确成本'"
        >本次会话 ${{ cost.toFixed(4) }}{{ pricesConfigured ? "" : "（估算）" }}</span>
        <select :value="project" @change="switchProject">
          <option v-if="!workspaces.length" value="" disabled>{{ apiDown ? "API 未连接" : "暂无世界" }}</option>
          <option v-if="project && !workspaces.includes(project)" :value="project">{{ project }}（外部注册）</option>
          <option v-for="name in workspaces" :key="name" :value="name">{{ name }}</option>
        </select>
        <div class="foot-row">
          <RouterLink to="/settings" class="foot-btn settings-btn">
            <svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true">
              <path fill="currentColor" d="M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8Zm0 2a2 2 0 1 1 0 4 2 2 0 0 1 0-4Z" />
              <path fill="currentColor" d="m20.6 13.3-.1-.9.1-.9 1.4-1.1-.2-.7-.7-1.2-.4-.6-1.7.5-1.4-1-.3-1.8-.6-.2H12.4l-.6.2-.3 1.8-1.4 1-1.7-.5-.4.6-.7 1.2-.2.7L8.4 11l.1.9-.1.9-1.4 1.1.2.7.7 1.2.4.6 1.7-.5 1.4 1 .3 1.8.6.2h2.2l.6-.2.3-1.8 1.4-1 1.7.5.4-.6.7-1.2.2-.7-1.4-1.1Zm-2 .5.9.7-.3.5-1.1-.3-.4-.1-.4.3a4.7 4.7 0 0 1-1 .7l-.4.2-.1.5-.2 1.1h-.6l-.2-1.1-.1-.5-.4-.2a4.7 4.7 0 0 1-1-.7l-.4-.3-.4.1-1.1.3-.3-.5.9-.7.4-.3-.1-.5a4.6 4.6 0 0 1 0-1.2l.1-.5-.4-.3-.9-.7.3-.5 1.1.3.4.1.4-.3a4.7 4.7 0 0 1 1-.7l.4-.2.1-.5.2-1.1h.6l.2 1.1.1.5.4.2c.4.2.7.4 1 .7l.4.3.4-.1 1.1-.3.3.5-.9.7-.4.3.1.5a4.6 4.6 0 0 1 0 1.2l-.1.5.4.3Z" />
            </svg>
            设置
          </RouterLink>
          <button class="foot-btn tour-btn" @click="openTour">新手引导</button>
        </div>
      </div>
    </aside>

    <div class="content">
      <svg class="deco" viewBox="0 0 200 200" aria-hidden="true">
        <circle cx="100" cy="100" r="92" fill="none" stroke="currentColor" stroke-width="0.4" stroke-dasharray="2 7" />
        <circle cx="100" cy="100" r="66" fill="none" stroke="currentColor" stroke-width="0.4" />
        <circle cx="100" cy="100" r="40" fill="none" stroke="currentColor" stroke-width="0.4" stroke-dasharray="1 5" />
        <path d="M100 38 L106 94 L162 100 L106 106 L100 162 L94 106 L38 100 L94 94 Z" fill="currentColor" fill-opacity="0.5" />
      </svg>
      <p v-if="apiDown" class="pane apidown">
        连不上 API 服务。请在项目根目录运行：
        <code>.venv\Scripts\python.exe -m uvicorn owcopilot.service.api:create_app --factory --port 8000</code>
        ——构建版前端与 API 同端口（8000），无需任何环境变量。
      </p>
      <main v-if="!booting">
        <RouterView v-slot="{ Component }">
          <Transition name="page" mode="out-in">
            <component :is="Component" />
          </Transition>
        </RouterView>
      </main>
    </div>
  </div>

  <GuidedTour :open="tourOpen" @close="tourOpen = false" />
  <CommandPalette />
  <ToastHost />
</template>

<style scoped>
.splash {
  position: fixed;
  inset: 0;
  z-index: 99;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 18px;
  background:
    radial-gradient(900px 480px at 82% -10%, rgba(138, 123, 200, 0.16), transparent 60%),
    linear-gradient(168deg, #141b3e 0%, #0f1530 46%, #0a0e24 100%);
}

.splash .orb {
  animation: splash-spin 14s linear infinite;
  transform-origin: center;
  transform-box: fill-box;
}

.splash .core {
  animation: splash-pulse 2.2s ease-in-out infinite;
  transform-origin: center;
  transform-box: fill-box;
}

.splash-text {
  font-family: var(--ow-serif);
  color: var(--ow-gold-bright);
  font-size: 1.05rem;
  letter-spacing: 0.35em;
  text-indent: 0.35em;
}

.splash-line {
  position: relative;
  width: 180px;
  height: 1px;
  overflow: hidden;
  background: rgba(217, 181, 108, 0.18);
}

.splash-line::after {
  content: "";
  position: absolute;
  left: -40%;
  top: 0;
  width: 40%;
  height: 100%;
  background: linear-gradient(90deg, transparent, #f0d28a, transparent);
  animation: splash-sweep 1.4s ease-in-out infinite;
}

@keyframes splash-spin {
  to {
    transform: rotate(360deg);
  }
}

@keyframes splash-pulse {
  0%,
  100% {
    opacity: 0.72;
    transform: scale(0.96);
  }

  50% {
    opacity: 1;
    transform: scale(1.04);
  }
}

@keyframes splash-sweep {
  to {
    left: 100%;
  }
}

.splash-leave-active {
  transition: opacity 0.6s ease;
}

.splash-leave-to {
  opacity: 0;
}

/* aurora: two layered light fields that drift, breathe, and lean toward the cursor (parallax).
   Bolder violet + gold than before so the sky actually reads as the Herta/space-station palette.
   Calm and slow, not a screensaver; neutralised under reduced-motion. */
.aurora {
  position: fixed;
  inset: -24vmax;
  z-index: -1;
  pointer-events: none;
  --px: calc(var(--ow-px, 0) * 1);
  --py: calc(var(--ow-py, 0) * 1);
}
.aurora.l1 {
  background:
    radial-gradient(40vmax 32vmax at 16% 10%, rgba(150, 120, 235, 0.3), transparent 60%),
    radial-gradient(36vmax 30vmax at 86% 24%, rgba(143, 214, 232, 0.14), transparent 60%),
    radial-gradient(44vmax 36vmax at 72% 92%, rgba(217, 181, 108, 0.14), transparent 64%);
  filter: blur(10px) saturate(1.1);
  transform: translate3d(calc(var(--px) * 22px), calc(var(--py) * 16px), 0);
  animation: aurora-drift 46s ease-in-out infinite alternate;
}
.aurora.l2 {
  background:
    radial-gradient(30vmax 26vmax at 60% 8%, rgba(120, 96, 220, 0.2), transparent 62%),
    radial-gradient(26vmax 24vmax at 30% 80%, rgba(160, 138, 255, 0.16), transparent 64%);
  filter: blur(16px) saturate(1.1);
  transform: translate3d(calc(var(--px) * -36px), calc(var(--py) * -26px), 0);
  animation: aurora-drift2 60s ease-in-out infinite alternate;
}
/* a faint perspective grid haze near the floor — a quiet sci-fi "deck" cue */
.grid-haze {
  position: fixed;
  inset: auto 0 0 0;
  height: 36vh;
  z-index: -1;
  pointer-events: none;
  opacity: 0.5;
  background:
    linear-gradient(transparent, rgba(160, 138, 255, 0.05)),
    repeating-linear-gradient(90deg, transparent 0 78px, rgba(143, 214, 232, 0.05) 78px 79px);
  mask-image: linear-gradient(transparent, #000 80%);
  transform: translate3d(calc(var(--ow-px, 0) * -10px), 0, 0);
}

@keyframes aurora-drift {
  0% { transform: translate3d(calc(var(--px) * 22px), calc(var(--py) * 16px), 0) scale(1); opacity: 0.82; }
  50% { opacity: 1; }
  100% { transform: translate3d(calc(3vmax + var(--px) * 22px), calc(-2.4vmax + var(--py) * 16px), 0) scale(1.08); opacity: 0.9; }
}
@keyframes aurora-drift2 {
  0% { transform: translate3d(calc(var(--px) * -36px), calc(var(--py) * -26px), 0) scale(1.04); }
  100% { transform: translate3d(calc(-2.6vmax + var(--px) * -36px), calc(2vmax + var(--py) * -26px), 0) scale(1); }
}

@media (prefers-reduced-motion: reduce) {
  .aurora,
  .aurora.l1,
  .aurora.l2 {
    animation: none;
  }
}

/* Resolution-adaptive shell: fluid up to a generous cap so 2K/4K monitors fill instead of
   stranding the workbench in a narrow column, while line lengths stay readable. */
.shell {
  display: flex;
  align-items: flex-start;
  gap: clamp(1rem, 1.4vw, 1.8rem);
  width: min(100%, 1640px);
  margin: 0 auto;
  padding: clamp(1rem, 1.6vw, 1.9rem) clamp(1rem, 2.4vw, 2.6rem) 4rem;
}

@media (min-width: 2100px) {
  .shell {
    width: min(100%, 1880px);
  }
}

.sidebar {
  position: sticky;
  top: 1.1rem;
  width: clamp(200px, 14vw, 248px);
  flex: none;
  padding: 0.9rem 0.8rem;
  display: flex;
  flex-direction: column;
  gap: 1rem;
  max-height: calc(100vh - 2.2rem);
  overflow-y: auto;
}

.brand {
  display: flex;
  align-items: center;
  gap: 0.55rem;
  padding: 0 0.3rem;
}

.brand svg {
  filter: drop-shadow(0 0 8px rgba(217, 181, 108, 0.35));
  flex: none;
}

.brand b {
  font-family: var(--ow-display);
  font-weight: 600;
  letter-spacing: 0.04em;
  display: block;
  line-height: 1.1;
  background: linear-gradient(180deg, #f7e3ad 0%, #d9b56c 60%, #b9924a 100%);
  -webkit-background-clip: text;
  background-clip: text;
  -webkit-text-fill-color: transparent;
  color: var(--ow-gold-bright);
}

.brand span {
  font-size: 0.7rem;
}

/* star-map rail: the 6 groups are circular nodes threaded on a vertical constellation line, the
   way HSR's region selectors read. A node lights up + swells when its section is open; its items
   fan out below. The continuous gold line is painted as the nav's own background at the node x. */
nav {
  position: relative;
  display: flex;
  flex-direction: column;
  gap: 0.05rem;
  flex: 1;
  padding-left: 2px;
  background: linear-gradient(180deg, transparent, var(--ow-gold-soft) 6%, var(--ow-gold-soft) 94%, transparent)
    no-repeat 15px 0 / 1.5px 100%;
}

.nav-head {
  display: flex;
  align-items: center;
  gap: 0.6rem;
  width: 100%;
  background: transparent;
  border: 0;
  cursor: pointer;
  padding: 0.42rem 0.45rem 0.42rem 8px;
  border-radius: 0.4rem;
  transition: background 0.15s ease;
}
.nav-head:hover {
  background: rgba(160, 138, 255, 0.08);
}
/* the circular node, centred on the rail (8px pad + 6px half = 14px ≈ rail at 15px) */
.nav-head::before {
  content: "";
  width: 12px;
  height: 12px;
  flex: none;
  border-radius: 50%;
  border: 1.5px solid var(--ow-gold-soft);
  background: var(--ow-night-deep);
  box-shadow: 0 0 0 3px rgba(10, 14, 36, 0.9);
  transition: all 0.2s ease;
}
.nav-head:hover::before {
  border-color: var(--ow-gold);
}
.nav-group.open .nav-head::before {
  border-color: var(--ow-gold-bright);
  background:
    radial-gradient(circle, var(--ow-gold-bright) 0 34%, var(--ow-night-deep) 36%);
  box-shadow: 0 0 0 3px rgba(10, 14, 36, 0.9), 0 0 11px rgba(240, 210, 138, 0.65);
  transform: scale(1.15);
}
.nav-title {
  font-family: var(--ow-overline);
  font-weight: 600;
  font-size: 0.75rem;
  letter-spacing: 0.14em;
  color: #cdbf95;
}
.nav-group.open .nav-title {
  color: var(--ow-gold-bright);
  text-shadow: 0 0 10px rgba(240, 210, 138, 0.3);
}
.nav-chev {
  margin-left: auto;
  width: 6px;
  height: 6px;
  border-right: 1.5px solid currentColor;
  border-bottom: 1.5px solid currentColor;
  color: var(--ow-muted);
  transform: rotate(-45deg);
  transition: transform 0.25s ease, color 0.18s ease;
}
.nav-group.open .nav-chev {
  transform: rotate(45deg);
  color: var(--ow-gold);
}

/* accordion body: grid 0fr -> 1fr gives a smooth open/close with no magic max-height */
.nav-items {
  display: grid;
  grid-template-rows: 0fr;
  transition: grid-template-rows 0.28s cubic-bezier(0.4, 0, 0.2, 1);
}
.nav-group.open .nav-items {
  grid-template-rows: 1fr;
}
.nav-items-inner {
  overflow: hidden;
  min-height: 0;
  display: flex;
  flex-direction: column;
  gap: 0.08rem;
  padding: 0.1rem 0 0.25rem 26px;
}

.nav-link {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  color: #b7b2c6;
  text-decoration: none;
  font-size: 0.87rem;
  padding: 0.34rem 0.55rem;
  border-radius: 0.45rem;
  position: relative;
  transition: color 0.15s ease, background 0.15s ease, transform 0.15s ease;
}
.nav-mark {
  width: 5px;
  height: 5px;
  flex: none;
  border-radius: 50%;
  background: rgba(183, 178, 198, 0.4);
  transition: all 0.18s ease;
}
.nav-link:hover {
  color: var(--ow-ink);
  background: rgba(143, 214, 232, 0.07);
  transform: translateX(2px);
}
.nav-link:hover .nav-mark {
  background: var(--ow-cyan);
}
.nav-link.router-link-active {
  color: var(--ow-gold-bright);
  background: linear-gradient(90deg, var(--ow-gold-faint), transparent);
  text-shadow: 0 0 12px rgba(240, 210, 138, 0.35);
}
.nav-link.router-link-active::before {
  content: "";
  position: absolute;
  left: 0;
  top: 50%;
  width: 2px;
  height: 16px;
  margin-top: -8px;
  background: var(--ow-gold-bright);
  border-radius: 2px;
  box-shadow: 0 0 8px rgba(240, 210, 138, 0.7);
}
.nav-link.router-link-active .nav-mark {
  background: var(--ow-gold-bright);
  box-shadow: 0 0 7px rgba(240, 210, 138, 0.8);
  clip-path: polygon(50% 0, 100% 50%, 50% 100%, 0 50%);
  border-radius: 0;
  transform: scale(1.3);
}

.side-footer {
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
  border-top: 1px solid var(--ow-line);
  padding-top: 0.7rem;
}

.model {
  display: inline-flex;
  align-items: center;
  gap: 0.35rem;
  border: 1px solid var(--ow-gold-soft);
  border-radius: 999px;
  color: var(--ow-gold-bright);
  font-size: 0.76rem;
  padding: 0.2rem 0.6rem;
  text-decoration: none;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  transition: box-shadow 0.2s ease;
}

.model:hover {
  box-shadow: 0 0 10px rgba(240, 210, 138, 0.3);
}

.model .dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: #8ed4ac;
  box-shadow: 0 0 6px rgba(142, 212, 172, 0.8);
  flex: none;
}

.model.off {
  border-color: rgba(224, 180, 106, 0.45);
  color: #e6c07e;
}

.model.off .dot {
  background: #e6c07e;
  box-shadow: none;
}

.cost {
  font-family: ui-monospace, Consolas, monospace;
  font-size: 0.72rem;
  color: var(--ow-cyan);
}

.side-footer select {
  background: var(--ow-panel-2);
  border: 1px solid var(--ow-line);
  border-radius: 0.5rem;
  color: var(--ow-ink);
  padding: 0.4rem 0.5rem;
  font: inherit;
  font-size: 0.82rem;
  width: 100%;
}

.foot-row {
  display: flex;
  gap: 0.4rem;
}

.foot-btn {
  flex: 1;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 0.3rem;
  background: transparent;
  border: 1px solid var(--ow-line);
  border-radius: 0.5rem;
  color: var(--ow-muted);
  font: inherit;
  font-size: 0.78rem;
  padding: 0.4rem 0.5rem;
  cursor: pointer;
  text-decoration: none;
  transition: border-color 0.15s ease, color 0.15s ease, box-shadow 0.15s ease;
}

.foot-btn:hover {
  border-color: var(--ow-gold-soft);
  color: var(--ow-gold-bright);
}

/* settings reads as the primary footer action — a faint gold wash so it's findable, not buried */
.settings-btn {
  border-color: var(--ow-gold-soft);
  color: var(--ow-gold-bright);
  background: var(--ow-gold-faint);
}
.settings-btn:hover {
  box-shadow: 0 0 12px rgba(240, 210, 138, 0.22);
}
.settings-btn svg {
  animation: gear-turn 14s linear infinite;
}
@keyframes gear-turn {
  to {
    transform: rotate(360deg);
  }
}
@media (prefers-reduced-motion: reduce) {
  .settings-btn svg {
    animation: none;
  }
}

.content {
  flex: 1;
  min-width: 0;
  position: relative;
}

/* designed void: a faint celestial sigil anchored lower-right of the work area, so a sparse page
   reads as intentional deep-space composition rather than dead margin. Drifts/rotates slowly,
   sits behind content, fades out under reduced-motion-free anyway since it's decorative. */
.deco {
  position: fixed;
  right: max(2vw, calc(50% - 760px));
  bottom: 4vh;
  width: clamp(280px, 30vw, 520px);
  height: clamp(280px, 30vw, 520px);
  color: var(--ow-violet);
  opacity: 0.14;
  pointer-events: none;
  z-index: 0;
  animation: deco-spin 120s linear infinite;
  transform-origin: 50% 50%;
}
.deco path {
  color: var(--ow-gold-bright);
}
@keyframes deco-spin {
  to { transform: rotate(360deg); }
}
@media (prefers-reduced-motion: reduce) {
  .deco { animation: none; }
}
.content > *:not(.deco) {
  position: relative;
  z-index: 1;
}

.apidown {
  padding: 0.8rem 1rem;
  font-size: 0.85rem;
  border-color: rgba(224, 133, 133, 0.45);
  margin-bottom: 1rem;
}

.apidown code {
  color: var(--ow-cyan);
  font-size: 0.78rem;
}

@media (max-width: 760px) {
  .shell {
    flex-direction: column;
  }

  .sidebar {
    position: static;
    width: 100%;
    max-height: none;
    flex-direction: column;
  }

  nav {
    flex-flow: row wrap;
    gap: 0.4rem 1rem;
  }
}
</style>
