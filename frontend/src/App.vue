<script setup lang="ts">
import { onMounted, onUnmounted, ref } from "vue";
import { apiGet, currentProject, llmConfig, sessionCost, setCurrentProject, setLlmConfig } from "./api";

const booting = ref(true);
const workspaces = ref<string[]>([]);
const project = ref(currentProject());
const apiDown = ref(false);
const modelName = ref("");
const cost = ref(sessionCost());

function switchProject(event: Event): void {
  const name = (event.target as HTMLSelectElement).value;
  if (!name) return;
  setCurrentProject(name);
  window.location.reload();
}

async function refreshModelBadge(): Promise<void> {
  // server env is the source of truth: a restart drops the key, the badge must follow
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
    // zero-config default: no stored choice -> the most recent managed world
    if (!localStorage.getItem("owcopilot_project") && workspaces.value.length) {
      setCurrentProject(workspaces.value[0]);
      project.value = workspaces.value[0];
    }
    await refreshModelBadge();
  } catch {
    apiDown.value = true;
  } finally {
    window.setTimeout(() => {
      booting.value = false;
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
        <path
          class="core"
          d="M50 14 L56.5 43.5 L86 50 L56.5 56.5 L50 86 L43.5 56.5 L14 50 L43.5 43.5 Z"
          fill="#d9b56c"
          fill-opacity=".25"
          stroke="#f0d28a"
          stroke-opacity=".8"
        />
        <path
          class="core"
          d="M50 34 L52.8 47.2 L66 50 L52.8 52.8 L50 66 L47.2 52.8 L34 50 L47.2 47.2 Z"
          fill="#f0d28a"
          fill-opacity=".85"
        />
      </svg>
      <div class="splash-text">正在展卷</div>
      <div class="splash-line"></div>
    </div>
  </Transition>

  <div class="shell">
    <header class="topbar pane">
      <div class="brand">
        <svg width="26" height="26" viewBox="0 0 100 100" fill="none" aria-hidden="true">
          <path
            d="M50 8 L58 42 L92 50 L58 58 L50 92 L42 58 L8 50 L42 42 Z"
            fill="#f0d28a"
            fill-opacity=".9"
          />
        </svg>
        <div>
          <b>OWCopilot</b>
          <span class="muted">世界观工作台</span>
        </div>
      </div>
      <nav>
        <RouterLink to="/overview">世界总览</RouterLink>
        <RouterLink to="/archive">设定档案</RouterLink>
        <RouterLink to="/genesis">创世工坊</RouterLink>
        <RouterLink to="/characters">人物工坊</RouterLink>
        <RouterLink to="/ask">世界问答</RouterLink>
        <RouterLink to="/review">审阅台</RouterLink>
        <RouterLink to="/sweep">专项清查</RouterLink>
        <RouterLink to="/worlds">工作区</RouterLink>
        <RouterLink to="/export">导出</RouterLink>
      </nav>
      <div class="status">
        <span v-if="cost > 0" class="cost" title="本次会话累计模型调用成本">
          ${{ cost.toFixed(4) }}
        </span>
        <RouterLink to="/settings" class="model" :class="{ off: !modelName }">
          <i class="dot"></i>{{ modelName || "未接入模型" }}
        </RouterLink>
        <select :value="project" @change="switchProject">
          <option v-if="!workspaces.length" value="" disabled>
            {{ apiDown ? "API 未连接" : "暂无世界" }}
          </option>
          <option v-if="project && !workspaces.includes(project)" :value="project">
            {{ project }}（外部注册）
          </option>
          <option v-for="name in workspaces" :key="name" :value="name">{{ name }}</option>
        </select>
      </div>
    </header>
    <p v-if="apiDown" class="pane apidown">
      连不上 API 服务。请在项目根目录运行：
      <code>.venv\Scripts\python.exe -m uvicorn owcopilot.service.api:create_app --factory --port 8000</code>
      ——构建版前端与 API 同端口（8000），无需任何环境变量。
    </p>
    <main>
      <RouterView v-slot="{ Component }">
        <Transition name="page" mode="out-in">
          <component :is="Component" />
        </Transition>
      </RouterView>
    </main>
  </div>
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
  max-width: 1100px;
  margin: 0 auto;
  padding: 1.2rem 1rem 3rem;
}

.topbar {
  display: flex;
  align-items: center;
  gap: 1.2rem;
  padding: 0.7rem 1.1rem;
  margin-bottom: 1.1rem;
}

.brand {
  display: flex;
  align-items: center;
  gap: 0.6rem;
}

.brand svg {
  filter: drop-shadow(0 0 8px rgba(217, 181, 108, 0.35));
}

.brand b {
  font-family: var(--ow-serif);
  display: block;
  line-height: 1.1;
}

.brand span {
  font-size: 0.72rem;
}

nav {
  display: flex;
  gap: 0.7rem;
  flex: 1;
  flex-wrap: wrap;
  font-size: 0.88rem;
}

nav a {
  color: var(--ow-muted);
  text-decoration: none;
  padding: 0.25rem 0.1rem;
  border-bottom: 2px solid transparent;
  transition:
    color 0.15s ease,
    border-color 0.15s ease;
}

nav a:hover {
  color: var(--ow-ink);
}

nav a.router-link-active {
  color: var(--ow-gold-bright);
  border-bottom-color: var(--ow-gold);
  text-shadow: 0 0 12px rgba(240, 210, 138, 0.4);
}

.status {
  display: flex;
  align-items: center;
  gap: 0.55rem;
}

.status select {
  background: var(--ow-panel-2);
  border: 1px solid var(--ow-line);
  border-radius: 0.5rem;
  color: var(--ow-ink);
  padding: 0.4rem 0.6rem;
  max-width: 11rem;
}

.cost {
  font-family: ui-monospace, Consolas, monospace;
  font-size: 0.76rem;
  color: var(--ow-cyan);
  border: 1px solid rgba(143, 214, 232, 0.35);
  border-radius: 999px;
  padding: 0.16rem 0.55rem;
  white-space: nowrap;
}

.model {
  display: inline-flex;
  align-items: center;
  gap: 0.35rem;
  border: 1px solid var(--ow-gold-soft);
  border-radius: 999px;
  color: var(--ow-gold-bright);
  font-size: 0.78rem;
  padding: 0.18rem 0.65rem;
  text-decoration: none;
  white-space: nowrap;
  max-width: 13rem;
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
</style>
