<script setup lang="ts">
import { onMounted, onUnmounted, ref } from "vue";
import { useRouter } from "vue-router";
import GuidedTour from "./components/GuidedTour.vue";
import { apiGet, currentProject, llmConfig, sessionCost, setCurrentProject, setLlmConfig } from "./api";

const router = useRouter();

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
    { to: "/characters", label: "人物工坊" },
    { to: "/creation", label: "创作工坊" },
  ] },
  { title: "内容带入", items: [
    { to: "/extraction", label: "文稿提炼" },
    { to: "/import", label: "表格导入" },
    { to: "/references", label: "灵感库" },
  ] },
  { title: "校勘 · 分析", items: [
    { to: "/audit", label: "校勘修复" },
    { to: "/impact", label: "影响分析" },
    { to: "/sweep", label: "专项清查" },
  ] },
  { title: "问答 · 交付", items: [
    { to: "/ask", label: "世界问答" },
    { to: "/review", label: "审阅台" },
    { to: "/export", label: "导出交付" },
  ] },
  { title: "管理", items: [
    { to: "/worlds", label: "工作区" },
    { to: "/settings", label: "设置 · 模型" },
  ] },
];

const booting = ref(true);
const workspaces = ref<string[]>([]);
const project = ref(currentProject());
const apiDown = ref(false);
const modelName = ref("");
const cost = ref(sessionCost());
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
    const status = await apiGet<{ configured: boolean }>("/settings/connection");
    const local = llmConfig();
    if (!status.configured && local.ready) setLlmConfig(false, "");
    modelName.value = status.configured && local.model ? local.model : "";
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

onMounted(async () => {
  window.addEventListener("ow-llm-changed", onLlmChanged);
  window.addEventListener("ow-cost-changed", onCostChanged);
  try {
    const body = await apiGet<{ workspaces: { name: string }[] }>("/workspaces");
    workspaces.value = body.workspaces.map((w) => w.name);
    if (!workspaces.value.length) {
      // clean install: don't surface the bare "demo" fallback in the switcher
      project.value = "";
    } else if (!localStorage.getItem("owcopilot_project")) {
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
        <div v-for="group in NAV" :key="group.title" class="nav-group" :data-tour="group.title">
          <span class="nav-title">{{ group.title }}</span>
          <RouterLink v-for="item in group.items" :key="item.to" :to="item.to" class="nav-link">
            {{ item.label }}
          </RouterLink>
        </div>
      </nav>

      <div class="side-footer">
        <RouterLink to="/settings" class="model" :class="{ off: !modelName }">
          <i class="dot"></i>{{ modelName || "未接入模型" }}
        </RouterLink>
        <span v-if="cost > 0" class="cost" title="本次会话累计模型调用成本">本次会话 ${{ cost.toFixed(4) }}</span>
        <select :value="project" @change="switchProject">
          <option v-if="!workspaces.length" value="" disabled>{{ apiDown ? "API 未连接" : "暂无世界" }}</option>
          <option v-if="project && !workspaces.includes(project)" :value="project">{{ project }}（外部注册）</option>
          <option v-for="name in workspaces" :key="name" :value="name">{{ name }}</option>
        </select>
        <button class="tour-btn" @click="openTour">新手引导</button>
      </div>
    </aside>

    <div class="content">
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

.shell {
  display: flex;
  align-items: flex-start;
  gap: 1rem;
  max-width: 1240px;
  margin: 0 auto;
  padding: 1.1rem 1rem 3rem;
}

.sidebar {
  position: sticky;
  top: 1.1rem;
  width: 200px;
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
  font-family: var(--ow-serif);
  display: block;
  line-height: 1.1;
}

.brand span {
  font-size: 0.7rem;
}

nav {
  display: flex;
  flex-direction: column;
  gap: 0.7rem;
  flex: 1;
}

.nav-group {
  display: flex;
  flex-direction: column;
  gap: 0.1rem;
}

.nav-title {
  font-size: 0.68rem;
  letter-spacing: 0.18em;
  color: var(--ow-muted);
  opacity: 0.7;
  padding: 0.1rem 0.5rem 0.25rem;
}

.nav-link {
  color: var(--ow-muted);
  text-decoration: none;
  font-size: 0.86rem;
  padding: 0.34rem 0.55rem;
  border-radius: 0.45rem;
  border-left: 2px solid transparent;
  transition:
    color 0.15s ease,
    background 0.15s ease,
    border-color 0.15s ease;
}

.nav-link:hover {
  color: var(--ow-ink);
  background: rgba(143, 214, 232, 0.06);
}

.nav-link.router-link-active {
  color: var(--ow-gold-bright);
  background: var(--ow-gold-faint);
  border-left-color: var(--ow-gold);
  text-shadow: 0 0 12px rgba(240, 210, 138, 0.35);
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

.tour-btn {
  background: transparent;
  border: 1px solid var(--ow-line);
  border-radius: 0.5rem;
  color: var(--ow-muted);
  font: inherit;
  font-size: 0.78rem;
  padding: 0.35rem 0.5rem;
  cursor: pointer;
}

.tour-btn:hover {
  border-color: var(--ow-gold-soft);
  color: var(--ow-gold-bright);
}

.content {
  flex: 1;
  min-width: 0;
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
