<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import CommandPalette from "./components/CommandPalette.vue";
import GuidedTour from "./components/GuidedTour.vue";
import ToastHost from "./components/ToastHost.vue";
import ConfirmHost from "./components/ConfirmHost.vue";
import Starfield from "./components/Starfield.vue";
import {
  ApiError,
  apiGet,
  currentProject,
  currentServiceApiKey,
  llmConfig,
  sessionCost,
  setCurrentProject,
  setLlmConfig,
  setServiceApiKey,
} from "./api";

const router = useRouter();
const route = useRoute();

interface NavItem { to: string; label: string }
interface NavGroup { title: string; items: NavItem[] }
type DeploymentProfile = "local" | "team";

const LOCAL_NAV: NavGroup[] = [
  { title: "总览", items: [{ to: "/overview", label: "世界总览" }] },
  { title: "项目", items: [
    { to: "/worlds", label: "工作区" }, { to: "/archive", label: "设定档案" },
    { to: "/history", label: "变更史" }, { to: "/platform", label: "平台管理" },
    { to: "/settings", label: "设置" },
  ] },
  { title: "素材 · 生成", items: [
    { to: "/extraction", label: "文稿提炼" }, { to: "/import", label: "表格导入" },
    { to: "/genesis", label: "创世工坊" }, { to: "/expand", label: "扩写工坊" },
    { to: "/creation", label: "创作工坊" }, { to: "/templates", label: "模板库" },
  ] },
  { title: "知识库", items: [
    { to: "/characters", label: "人物工坊" }, { to: "/dialogues", label: "对话流" },
    { to: "/references", label: "灵感库" }, { to: "/graph", label: "关系图谱" },
    { to: "/timeline", label: "时间线" }, { to: "/localization", label: "本地化" },
  ] },
  { title: "验证", items: [
    { to: "/audit", label: "校勘修复" }, { to: "/agent", label: "智能体诊断" },
    { to: "/multi-agent", label: "多智能体协作" },
    { to: "/engineering", label: "工程实验室" },
    { to: "/impact", label: "影响分析" }, { to: "/analytics", label: "世界分析" },
    { to: "/sweep", label: "专项清查" }, { to: "/compliance", label: "版号合规" },
  ] },
  { title: "交付", items: [
    { to: "/ask", label: "世界问答" }, { to: "/review", label: "审阅台" },
    { to: "/export", label: "导出交付" },
  ] },
];
const TEAM_NAV: NavGroup[] = [
  { title: "团队资料", items: [
    { to: "/intake", label: "来源摄取" },
    { to: "/canonical", label: "团队正典" },
  ] },
];

const profile = ref<DeploymentProfile | null>(null);
const navGroups = computed(() => profile.value === "team" ? TEAM_NAV : LOCAL_NAV);
function groupOf(path: string): string {
  const groups = navGroups.value;
  return groups.find((g) => g.items.some((item) => item.to === path))?.title ?? groups[0].title;
}
const openGroup = ref(groupOf(route.path));
const mobileNavOpen = ref(false);
const mobileMenuEl = ref<HTMLButtonElement | null>(null);
const sidebarEl = ref<HTMLElement | null>(null);
const mainEl = ref<HTMLElement | null>(null);
let mobileNavReturnFocus: HTMLElement | null = null;
const MOBILE_NAV_FOCUSABLE = [
  "a[href]",
  "button:not([disabled])",
  "input:not([disabled])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  "[tabindex]:not([tabindex='-1'])",
].join(",");
const currentPageLabel = computed(() =>
  navGroups.value.flatMap((group) => group.items).find((item) => item.to === route.path)?.label
    ?? "世界观工作台",
);
function toggleGroup(title: string): void {
  openGroup.value = openGroup.value === title ? "" : title;
}
function openMobileNav(): void {
  if (mobileNavOpen.value) return;
  mobileNavReturnFocus = document.activeElement instanceof HTMLElement
    ? document.activeElement
    : mobileMenuEl.value;
  mobileNavOpen.value = true;
  void nextTick(() => sidebarEl.value?.querySelector<HTMLElement>(".sidebar-close")?.focus());
}
function closeMobileNav(restoreFocus = true): void {
  if (!mobileNavOpen.value) return;
  const target = mobileNavReturnFocus?.isConnected ? mobileNavReturnFocus : mobileMenuEl.value;
  mobileNavOpen.value = false;
  mobileNavReturnFocus = null;
  if (restoreFocus) void nextTick(() => target?.focus({ preventScroll: true }));
}
watch(mobileNavOpen, (open) => document.body.classList.toggle("nav-open", open));
watch(
  () => route.path,
  (path) => {
    openGroup.value = groupOf(path);
    closeMobileNav(false);
    document.title = `${currentPageLabel.value} · OWCopilot`;
    if (!booting.value) void nextTick(() => mainEl.value?.focus({ preventScroll: true }));
  },
);

const booting = ref(true);
const retrying = ref(false);
const workspaces = ref<string[]>([]);
const project = ref(currentProject());
const apiDown = ref(false);
const authRequired = ref(false);
const accessKey = ref(currentServiceApiKey());
const rememberAccessKey = ref(Boolean(localStorage.getItem("owcopilot_api_key")));
const modelName = ref("");
const cost = ref(sessionCost());
const pricesConfigured = ref(false);
const tourOpen = ref(false);
const commandPalette = ref<InstanceType<typeof CommandPalette> | null>(null);

function openTour(): void { tourOpen.value = true; }
function openCommandPalette(): void {
  closeMobileNav(false);
  commandPalette.value?.show();
}
function switchProject(event: Event): void {
  const name = (event.target as HTMLSelectElement).value;
  if (!name) return;
  setCurrentProject(name);
  window.location.reload();
}
async function refreshModelBadge(): Promise<void> {
  try {
    const status = await apiGet<{ configured: boolean; prices_configured?: boolean }>("/settings/connection");
    const local = llmConfig();
    if (!status.configured && local.ready) setLlmConfig(false, "");
    modelName.value = status.configured && local.model ? local.model : "";
    pricesConfigured.value = status.prices_configured ?? false;
  } catch { modelName.value = ""; }
}
function onLlmChanged(): void { void refreshModelBadge(); }
function onCostChanged(): void { cost.value = sessionCost(); }

let parallaxRaf = 0;
function onPointer(event: PointerEvent): void {
  if (parallaxRaf) return;
  parallaxRaf = window.requestAnimationFrame(() => {
    parallaxRaf = 0;
    const root = document.documentElement.style;
    root.setProperty("--ow-px", ((event.clientX / window.innerWidth - 0.5) * 2).toFixed(3));
    root.setProperty("--ow-py", ((event.clientY / window.innerHeight - 0.5) * 2).toFixed(3));
  });
}
const motionOK = !window.matchMedia("(prefers-reduced-motion: reduce)").matches;
const proMode = ref(localStorage.getItem("owcopilot_pro_mode") === "1");
function applyProMode(): void { document.body.classList.toggle("pro-mode", proMode.value); }
function onProChanged(): void {
  proMode.value = localStorage.getItem("owcopilot_pro_mode") === "1";
  applyProMode();
}
function onGlobalKey(event: KeyboardEvent): void {
  if (!mobileNavOpen.value || document.querySelector(".modal-root")) return;
  if (event.key === "Escape") {
    event.preventDefault();
    closeMobileNav();
    return;
  }
  if (event.key !== "Tab" || !sidebarEl.value) return;
  const focusable = [...sidebarEl.value.querySelectorAll<HTMLElement>(MOBILE_NAV_FOCUSABLE)]
    .filter((item) => item.offsetParent !== null);
  if (!focusable.length) {
    event.preventDefault();
    sidebarEl.value.focus();
    return;
  }
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  const active = document.activeElement;
  if (!sidebarEl.value.contains(active)) {
    event.preventDefault();
    (event.shiftKey ? last : first).focus();
  } else if (event.shiftKey && active === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && active === last) {
    event.preventDefault();
    first.focus();
  }
}

async function projectResolves(name: string): Promise<boolean> {
  try {
    await apiGet(`/projects/${encodeURIComponent(name)}/overview`);
    return true;
  } catch { return false; }
}

async function loadLocalShell(): Promise<void> {
  const stored = localStorage.getItem("owcopilot_project");
  const query = new URLSearchParams({ limit: "30" });
  const focus = stored ?? project.value;
  if (focus) query.set("focus", focus);
  const body = await apiGet<{ workspaces: { name: string }[] }>(
    `/workspaces?${query.toString()}`,
  );
  workspaces.value = body.workspaces.map((workspace) => workspace.name);
  if (!stored && workspaces.value.length) {
    setCurrentProject(workspaces.value[0]);
    project.value = workspaces.value[0];
  } else if (stored && (workspaces.value.includes(stored) || await projectResolves(stored))) {
    project.value = stored;
  } else if (workspaces.value.length) {
    setCurrentProject(workspaces.value[0]);
    project.value = workspaces.value[0];
  } else {
    project.value = "";
  }
  if (!project.value && !["/worlds", "/platform", "/settings"].includes(router.currentRoute.value.path)) {
    await router.replace("/worlds");
  }
  await refreshModelBadge();
}

async function bootstrap(): Promise<void> {
  retrying.value = true;
  apiDown.value = false;
  authRequired.value = false;
  try {
    const health = await apiGet<{ profile?: DeploymentProfile }>("/health");
    profile.value = health.profile === "team" ? "team" : "local";
    if (profile.value === "team") {
      project.value = "";
      if (!["/intake", "/canonical"].includes(router.currentRoute.value.path)) {
        await router.replace("/intake");
      }
    } else {
      await loadLocalShell();
    }
  } catch (error) {
    authRequired.value = error instanceof ApiError && error.status === 401;
    apiDown.value = !authRequired.value;
  } finally {
    retrying.value = false;
  }
}
async function unlockService(): Promise<void> {
  setServiceApiKey(accessKey.value, rememberAccessKey.value);
  await bootstrap();
}

onMounted(async () => {
  const started = performance.now();
  applyProMode();
  window.addEventListener("ow-pro-changed", onProChanged);
  window.addEventListener("ow-llm-changed", onLlmChanged);
  window.addEventListener("ow-cost-changed", onCostChanged);
  window.addEventListener("keydown", onGlobalKey);
  if (motionOK) window.addEventListener("pointermove", onPointer, { passive: true });
  await bootstrap();
  const remaining = Math.max(0, 260 - (performance.now() - started));
  window.setTimeout(() => {
    booting.value = false;
    if (!apiDown.value && !authRequired.value && localStorage.getItem("owcopilot_tour_done") !== "1") {
      tourOpen.value = true;
    }
  }, remaining);
});

onUnmounted(() => {
  document.body.classList.remove("nav-open");
  window.cancelAnimationFrame(parallaxRaf);
  window.removeEventListener("ow-pro-changed", onProChanged);
  window.removeEventListener("ow-llm-changed", onLlmChanged);
  window.removeEventListener("ow-cost-changed", onCostChanged);
  window.removeEventListener("keydown", onGlobalKey);
  window.removeEventListener("pointermove", onPointer);
});
</script>

<template>
  <Transition name="splash">
    <div v-if="booting" class="splash" role="status" aria-live="polite">
      <svg width="84" height="84" viewBox="0 0 100 100" fill="none" aria-hidden="true">
        <circle class="orb" cx="50" cy="50" r="44" stroke="#8fd6e8" stroke-opacity=".35" stroke-dasharray="3 6" />
        <circle cx="50" cy="50" r="34" stroke="#d9b56c" stroke-opacity=".4" />
        <path class="core" d="M50 14 L56.5 43.5 L86 50 L56.5 56.5 L50 86 L43.5 56.5 L14 50 L43.5 43.5 Z" fill="#d9b56c" fill-opacity=".25" stroke="#f0d28a" stroke-opacity=".8" />
        <path class="core" d="M50 34 L52.8 47.2 L66 50 L52.8 52.8 L50 66 L47.2 52.8 L34 50 L47.2 47.2 Z" fill="#f0d28a" fill-opacity=".85" />
      </svg>
      <div class="splash-text">正在展卷</div>
      <div class="splash-line"></div>
    </div>
  </Transition>

  <a class="skip-link" href="#main-content">跳到主要内容</a>

  <!-- dynamic background: two parallax aurora layers behind all content. They drift on their own and
       lean toward the cursor (--ow-px/--ow-py), so wide screens read as depth you move through. -->
  <div class="aurora l1" aria-hidden="true"></div>
  <div class="aurora l2" aria-hidden="true"></div>
  <div class="grid-haze" aria-hidden="true"></div>
  <!-- rotating astrolabe + orbiting star trails (dropped in pro mode for a calmer workspace) -->
  <Starfield v-if="!proMode" />

  <header class="mobile-bar">
    <button
      ref="mobileMenuEl"
      type="button"
      class="mobile-menu"
      aria-label="打开导航"
      aria-controls="primary-sidebar"
      :aria-expanded="mobileNavOpen"
      @click="openMobileNav"
    ><span></span><span></span><span></span></button>
    <div class="mobile-brand"><b>OWCopilot</b><span>{{ currentPageLabel }}</span></div>
    <span class="mobile-project">{{ profile === "team" ? "团队" : (project || "未选世界") }}</span>
    <button
      v-if="profile !== 'team'"
      type="button"
      class="mobile-command"
      aria-label="打开全局搜索与命令"
      title="搜索与命令"
      @click="openCommandPalette"
    >
      <svg viewBox="0 0 24 24" width="19" height="19" aria-hidden="true">
        <path d="m20 20-4.4-4.4m2.4-5.1a7.5 7.5 0 1 1-15 0 7.5 7.5 0 0 1 15 0Z" />
      </svg>
    </button>
  </header>
  <button
    v-if="mobileNavOpen"
    type="button"
    class="nav-backdrop"
    aria-label="关闭导航"
    tabindex="-1"
    @click="closeMobileNav()"
  ></button>

  <div class="shell">
    <aside
      id="primary-sidebar"
      ref="sidebarEl"
      class="sidebar pane"
      :class="{ 'mobile-open': mobileNavOpen, gated: authRequired || apiDown }"
      :inert="authRequired || apiDown"
      :role="mobileNavOpen ? 'dialog' : undefined"
      :aria-label="mobileNavOpen ? '主要导航' : undefined"
      :aria-modal="mobileNavOpen || undefined"
      tabindex="-1"
    >
      <div class="brand">
        <svg width="26" height="26" viewBox="0 0 100 100" fill="none" aria-hidden="true">
          <path d="M50 8 L58 42 L92 50 L58 58 L50 92 L42 58 L8 50 L42 42 Z" fill="#f0d28a" fill-opacity=".9" />
        </svg>
        <div>
          <b>OWCopilot</b>
          <span class="muted">世界观工作台</span>
        </div>
        <button type="button" class="sidebar-close" aria-label="关闭导航" @click="closeMobileNav()">×</button>
      </div>

      <nav aria-label="主要导航">
        <div
          v-for="(group, groupIndex) in navGroups"
          :key="group.title"
          class="nav-group"
          :class="{ open: openGroup === group.title }"
          :data-tour="group.title"
        >
          <button
            type="button"
            class="nav-head"
            :aria-expanded="openGroup === group.title"
            :aria-controls="`nav-group-${groupIndex}`"
            @click="toggleGroup(group.title)"
          >
            <span class="nav-title">{{ group.title }}</span>
            <span class="nav-chev" aria-hidden="true"></span>
          </button>
          <div
            :id="`nav-group-${groupIndex}`"
            class="nav-items"
            :aria-hidden="openGroup !== group.title"
            :inert="openGroup !== group.title"
          >
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

      <div v-if="profile !== 'team'" class="side-footer">
        <button
          type="button"
          class="command-button desktop-command"
          aria-label="打开全局搜索与命令"
          @click="openCommandPalette"
        >
          <span aria-hidden="true">⌕</span>
          <span>搜索与命令</span>
          <kbd>Ctrl K</kbd>
        </button>
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
      <div v-else class="side-footer team-footer">
        <span class="profile-badge"><i></i>团队资料库</span>
        <p>来源先经人工复核，再进入不可变正典。</p>
      </div>
    </aside>

    <div class="content">
      <svg class="deco" viewBox="0 0 200 200" aria-hidden="true">
        <circle cx="100" cy="100" r="92" fill="none" stroke="currentColor" stroke-width="0.4" stroke-dasharray="2 7" />
        <circle cx="100" cy="100" r="66" fill="none" stroke="currentColor" stroke-width="0.4" />
        <circle cx="100" cy="100" r="40" fill="none" stroke="currentColor" stroke-width="0.4" stroke-dasharray="1 5" />
        <path d="M100 38 L106 94 L162 100 L106 106 L100 162 L94 106 L38 100 L94 94 Z" fill="currentColor" fill-opacity="0.5" />
      </svg>
      <section v-if="authRequired" class="pane connection-card" role="alert">
        <div class="connection-mark" aria-hidden="true">✦</div>
        <div class="connection-copy">
          <h2>服务需要访问密钥</h2>
          <p>这是工作台自身的访问保护，不是模型供应商的 API Key。</p>
          <form class="access-form" @submit.prevent="unlockService">
            <label>
              <span>服务访问密钥</span>
              <input v-model="accessKey" type="password" autocomplete="current-password" />
            </label>
            <label class="remember-key"><input v-model="rememberAccessKey" type="checkbox" /> 在这台设备记住</label>
            <button class="connect-action" type="submit" :disabled="retrying || !accessKey.trim()">
              {{ retrying ? "正在验证…" : "解锁工作台" }}
            </button>
          </form>
        </div>
      </section>
      <section v-else-if="apiDown" class="pane connection-card" role="alert">
        <div class="connection-mark offline" aria-hidden="true">!</div>
        <div class="connection-copy">
          <h2>暂时连不上工作台服务</h2>
          <p>内容仍安全保存在服务端。启动服务后可直接重试，不必刷新页面。</p>
          <button class="connect-action" type="button" :disabled="retrying" @click="bootstrap">
            {{ retrying ? "正在重连…" : "重新连接" }}
          </button>
          <details>
            <summary>开发者启动方式</summary>
            <code>python -m uvicorn owcopilot.service.api:create_app --factory --port 8000</code>
          </details>
        </div>
      </section>
      <main v-if="!booting && !authRequired && !apiDown" id="main-content" ref="mainEl" tabindex="-1">
        <RouterView v-slot="{ Component }">
          <Transition name="page" mode="out-in">
            <component :is="Component" />
          </Transition>
        </RouterView>
      </main>
    </div>
  </div>

  <GuidedTour v-if="profile !== 'team'" :open="tourOpen" @close="tourOpen = false" />
  <CommandPalette v-if="profile !== 'team'" ref="commandPalette" />
  <ToastHost />
  <ConfirmHost />
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
  pointer-events: none;
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
/* Keep the layered purple subtle so the warm-gold and cool-cyan accents remain distinct. */
.aurora.l1 {
  background:
    radial-gradient(40vmax 32vmax at 16% 10%, rgba(150, 120, 235, 0.13), transparent 60%),
    radial-gradient(36vmax 30vmax at 86% 24%, rgba(143, 214, 232, 0.18), transparent 60%),
    radial-gradient(44vmax 36vmax at 72% 92%, rgba(217, 181, 108, 0.20), transparent 64%);
  filter: blur(10px) saturate(1.1);
  transform: translate3d(calc(var(--px) * 22px), calc(var(--py) * 16px), 0);
  animation: aurora-drift 46s ease-in-out infinite alternate;
}
.aurora.l2 {
  background:
    radial-gradient(30vmax 26vmax at 60% 8%, rgba(120, 96, 220, 0.09), transparent 62%),
    radial-gradient(26vmax 24vmax at 30% 80%, rgba(142, 128, 216, 0.07), transparent 64%),
    radial-gradient(22vmax 20vmax at 88% 72%, rgba(217, 181, 108, 0.12), transparent 60%);
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
  /* FE-2: was 0.45rem (SaaS roundness); 2px aligns with .pane border-radius for geometric consistency */
  border-radius: 2px;
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

.command-button {
  width: 100%;
  min-height: 38px;
  display: flex;
  align-items: center;
  gap: 0.45rem;
  padding: 0.42rem 0.55rem;
  border: 1px solid var(--ow-gold-soft);
  border-radius: 2px;
  color: var(--ow-gold-bright);
  background: linear-gradient(90deg, var(--ow-gold-faint), rgba(143, 214, 232, 0.04));
  font: inherit;
  font-size: 0.8rem;
  cursor: pointer;
}
.command-button:hover {
  border-color: var(--ow-gold);
  box-shadow: 0 0 12px rgba(240, 210, 138, 0.2);
}
.command-button kbd {
  margin-left: auto;
  padding: 0.08rem 0.3rem;
  border: 1px solid var(--ow-line);
  color: var(--ow-muted);
  background: rgba(10, 14, 36, 0.55);
  font: 0.68rem ui-monospace, Consolas, monospace;
}

.model {
  display: inline-flex;
  align-items: center;
  gap: 0.35rem;
  border: 1px solid var(--ow-gold-soft);
  /* FE-2: was 999px pill; micro-cut-corner chip matches .pane + chip geometric language */
  border-radius: 3px;
  clip-path: polygon(4px 0, 100% 0, 100% calc(100% - 4px), calc(100% - 4px) 100%, 0 100%, 0 4px);
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
  /* FE-2: geometric border-radius to match panel language */
  border-radius: 2px;
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
  /* FE-2: geometric to match panel language */
  border-radius: 2px;
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

.skip-link {
  position: fixed;
  top: 0.55rem;
  left: 50%;
  z-index: 10000;
  padding: 0.65rem 1rem;
  color: var(--ow-night-deep);
  background: var(--ow-gold-bright);
  text-decoration: none;
  transform: translate(-50%, -180%);
  transition: transform 0.15s ease;
}
.skip-link:focus { transform: translate(-50%, 0); }
.mobile-bar,
.nav-backdrop,
.sidebar-close { display: none; }
.mobile-command { display: none; }
main:focus { outline: none; }

.team-footer p {
  margin: 0;
  color: var(--ow-muted);
  font-size: 0.74rem;
  line-height: 1.55;
}
.profile-badge {
  display: flex;
  align-items: center;
  gap: 0.45rem;
  color: var(--ow-gold-bright);
  font-size: 0.78rem;
}
.profile-badge i {
  width: 7px;
  height: 7px;
  transform: rotate(45deg);
  background: var(--ow-gold-bright);
  box-shadow: 0 0 8px var(--ow-gold-soft);
}

.connection-card {
  display: flex;
  gap: 1rem;
  align-items: flex-start;
  padding: 1rem 1.15rem;
  margin-bottom: 1rem;
  border-color: var(--ow-cinnabar-soft);
}
.connection-mark {
  flex: none;
  width: 34px;
  height: 34px;
  display: grid;
  place-items: center;
  border: 1px solid var(--ow-gold-soft);
  color: var(--ow-gold-bright);
  background: var(--ow-gold-faint);
  transform: rotate(45deg);
}
.connection-mark::first-letter { display: inline-block; transform: rotate(-45deg); }
.connection-mark.offline { color: #efae8e; border-color: var(--ow-cinnabar-soft); }
.connection-copy { flex: 1; min-width: 0; }
.connection-copy h2 { margin: 0 0 0.25rem; font-size: 1rem; }
.connection-copy > p { margin: 0 0 0.75rem; color: var(--ow-muted); font-size: 0.84rem; line-height: 1.55; }
.connection-copy details { margin-top: 0.7rem; color: var(--ow-muted); font-size: 0.78rem; }
.connection-copy summary { cursor: pointer; }
.connection-copy code { display: block; margin-top: 0.45rem; color: var(--ow-cyan); overflow-wrap: anywhere; }
.access-form {
  display: flex;
  align-items: flex-end;
  flex-wrap: wrap;
  gap: 0.6rem;
}
.access-form > label:first-child { flex: 1 1 260px; }
.access-form label span { display: block; margin-bottom: 0.3rem; color: var(--ow-ink-soft); font-size: 0.76rem; }
.access-form input[type="password"] {
  width: 100%;
  padding: 0.58rem 0.7rem;
  border: 1px solid var(--ow-line);
  color: var(--ow-ink);
  background: var(--ow-panel-2);
}
.remember-key { display: flex; align-items: center; gap: 0.35rem; min-height: 38px; color: var(--ow-muted); font-size: 0.78rem; }
.connect-action {
  min-height: 38px;
  padding: 0.55rem 0.9rem;
  border: 1px solid var(--ow-gold-soft);
  color: var(--ow-gold-bright);
  background: var(--ow-gold-faint);
  cursor: pointer;
}
.connect-action:disabled { opacity: 0.55; cursor: wait; }

@media (max-width: 760px) {
  :global(body.nav-open) { overflow: hidden; }
  .mobile-bar {
    position: fixed;
    inset: 0 0 auto;
    z-index: 70;
    height: 58px;
    display: flex;
    align-items: center;
    gap: 0.75rem;
    padding: 0 0.8rem;
    border-bottom: 1px solid var(--ow-edge);
    background: rgba(10, 14, 36, 0.93);
    backdrop-filter: blur(16px);
    box-shadow: 0 8px 30px rgba(4, 7, 22, 0.36);
  }
  .mobile-menu,
  .sidebar-close,
  .mobile-command {
    width: 44px;
    height: 44px;
    flex: none;
    border: 1px solid var(--ow-line);
    color: var(--ow-gold-bright);
    background: transparent;
    cursor: pointer;
  }
  .mobile-menu { display: grid; place-content: center; gap: 4px; }
  .mobile-menu span { display: block; width: 18px; height: 1.5px; background: currentColor; }
  .mobile-command { display: grid; place-items: center; padding: 0; }
  .mobile-command svg { fill: none; stroke: currentColor; stroke-width: 1.7; stroke-linecap: round; }
  .mobile-brand { min-width: 0; display: flex; flex-direction: column; line-height: 1.2; }
  .mobile-brand b { color: var(--ow-gold-bright); font-size: 0.83rem; letter-spacing: 0.05em; }
  .mobile-brand span { overflow: hidden; color: var(--ow-ink-soft); font-size: 0.72rem; text-overflow: ellipsis; white-space: nowrap; }
  .mobile-project { margin-left: auto; max-width: 34vw; overflow: hidden; color: var(--ow-muted); font-size: 0.7rem; text-overflow: ellipsis; white-space: nowrap; }
  .nav-backdrop {
    position: fixed;
    inset: 0;
    z-index: 79;
    display: block;
    width: 100%;
    height: 100%;
    border: 0;
    background: rgba(4, 7, 22, 0.72);
    backdrop-filter: blur(2px);
  }
  .shell {
    display: block;
    width: 100%;
    padding: 74px 0.75rem 3rem;
  }
  .sidebar {
    position: fixed;
    inset: 0.5rem auto 0.5rem 0.5rem;
    z-index: 80;
    width: min(88vw, 330px);
    max-height: calc(100dvh - 1rem);
    flex-direction: column;
    transform: translateX(calc(-100% - 1rem));
    visibility: hidden;
    pointer-events: none;
    transition: transform 0.24s var(--ow-ease-enter), visibility 0.24s;
  }
  .sidebar.mobile-open { transform: translateX(0); visibility: visible; pointer-events: auto; }
  .sidebar-close { display: grid; place-items: center; margin-left: auto; font-size: 1.4rem; }
  nav { flex-flow: column nowrap; gap: 0.05rem; }
  .nav-head { min-height: 44px; }
  .nav-link { min-height: 44px; }
  .content { width: 100%; }
  .deco { width: 230px; height: 230px; opacity: 0.09; }
  .connection-card { align-items: stretch; padding: 0.9rem; }
  .connection-mark { width: 28px; height: 28px; }
  .access-form { align-items: stretch; flex-direction: column; }
  .access-form > label:first-child { flex-basis: auto; }
  .connect-action { min-height: 44px; }
}
</style>
