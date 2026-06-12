<script setup lang="ts">
import { ref } from "vue";
import { currentProject, setCurrentProject } from "./api";

const project = ref(currentProject());

function applyProject(): void {
  setCurrentProject(project.value.trim() || "demo");
  window.location.reload();
}
</script>

<template>
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
          <span class="muted">世界观工作台 · Vue 预览版</span>
        </div>
      </div>
      <nav>
        <RouterLink to="/overview">世界总览</RouterLink>
        <RouterLink to="/archive">设定档案</RouterLink>
        <RouterLink to="/genesis">创世工坊</RouterLink>
        <RouterLink to="/ask">世界问答</RouterLink>
        <RouterLink to="/review">审阅台</RouterLink>
      </nav>
      <div class="project">
        <input v-model="project" placeholder="项目名（如 demo）" @keydown.enter="applyProject" />
        <button @click="applyProject">切换</button>
      </div>
    </header>
    <main>
      <RouterView />
    </main>
  </div>
</template>

<style scoped>
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
  gap: 0.9rem;
  flex: 1;
}

nav a {
  color: var(--ow-muted);
  text-decoration: none;
  padding: 0.25rem 0.1rem;
  border-bottom: 2px solid transparent;
}

nav a.router-link-active {
  color: var(--ow-gold-bright);
  border-bottom-color: var(--ow-gold);
  text-shadow: 0 0 12px rgba(240, 210, 138, 0.4);
}

.project {
  display: flex;
  gap: 0.4rem;
}

.project input {
  background: var(--ow-panel-2);
  border: 1px solid var(--ow-line);
  border-radius: 0.5rem;
  color: var(--ow-ink);
  padding: 0.35rem 0.6rem;
  width: 11rem;
}

.project button {
  background: linear-gradient(180deg, #f0d28a 0%, #b9924a 100%);
  border: 1px solid rgba(240, 210, 138, 0.65);
  border-radius: 0.5rem;
  color: #241a05;
  font-weight: 600;
  padding: 0.35rem 0.8rem;
  cursor: pointer;
}
</style>
