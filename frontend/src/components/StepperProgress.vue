<script setup lang="ts">
import StarEmblem from "./StarEmblem.vue";

defineProps<{
  stages: { key: string; label: string }[];
  index: number;
  running: boolean;
  elapsed: number;
  hint?: string;
}>();
</script>

<template>
  <div class="pane progress">
    <StarEmblem v-if="running" :size="52" />
    <div class="stages">
      <div class="steps">
        <template v-for="(stage, i) in stages" :key="stage.key">
          <span class="step" :class="{ done: i < index, active: i === index && running }">
            {{ stage.label }}
          </span>
          <span v-if="i < stages.length - 1" class="step-line"></span>
        </template>
      </div>
      <span v-if="running" class="muted elapsed">
        已用时 {{ elapsed }}s{{ hint ? ` · ${hint}` : "" }}
      </span>
    </div>
  </div>
</template>

<style scoped>
.progress {
  margin-top: 0.9rem;
  padding: 0.8rem 1rem;
  display: flex;
  align-items: center;
  gap: 1rem;
}

.stages {
  display: flex;
  flex-direction: column;
  gap: 0.45rem;
}

.steps {
  display: flex;
  align-items: center;
  gap: 0.45rem;
  flex-wrap: wrap;
}

.step {
  font-size: 0.84rem;
  color: var(--ow-muted);
  border: 1px solid var(--ow-line);
  border-radius: 999px;
  padding: 0.18rem 0.7rem;
  transition:
    color 0.25s ease,
    border-color 0.25s ease,
    box-shadow 0.25s ease;
}

.step.done {
  color: var(--ow-cyan);
  border-color: rgba(143, 214, 232, 0.4);
}

.step.active {
  color: var(--ow-gold-bright);
  border-color: var(--ow-gold-soft);
  box-shadow: 0 0 12px rgba(240, 210, 138, 0.35);
  animation: step-breathe 1.6s ease-in-out infinite;
}

@keyframes step-breathe {
  0%,
  100% {
    box-shadow: 0 0 8px rgba(240, 210, 138, 0.25);
  }

  50% {
    box-shadow: 0 0 16px rgba(240, 210, 138, 0.45);
  }
}

.step-line {
  width: 1.1rem;
  height: 1px;
  background: linear-gradient(90deg, var(--ow-gold-soft), transparent);
}

.elapsed {
  font-size: 0.78rem;
}
</style>
