<script setup lang="ts">
withDefaults(defineProps<{ lines?: number; shape?: "text" | "card" | "ring" }>(), {
  lines: 3,
  shape: "text",
});
</script>

<template>
  <div class="ow-skel" aria-hidden="true">
    <div v-if="shape === 'ring'" class="ow-skel-ring" />
    <div v-else-if="shape === 'card'" class="ow-skel-card" />
    <template v-else>
      <div
        v-for="n in lines"
        :key="n"
        class="ow-skel-line"
        :style="{ width: n === lines ? '62%' : `${92 - (n % 3) * 7}%` }"
      />
    </template>
  </div>
</template>

<style scoped>
.ow-skel {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.ow-skel-line,
.ow-skel-card,
.ow-skel-ring {
  background: linear-gradient(90deg, var(--ow2-paper-2) 25%, rgba(192, 160, 98, 0.14) 50%, var(--ow2-paper-2) 75%);
  background-size: 200% 100%;
  animation: ow-skel-shine 1.4s ease infinite;
}
.ow-skel-line {
  height: 12px;
  border-radius: 6px;
}
.ow-skel-card {
  height: 96px;
  border-radius: var(--ow2-radius-pane);
}
.ow-skel-ring {
  width: 120px;
  height: 120px;
  border-radius: 50%;
  margin: 0 auto;
}
@keyframes ow-skel-shine {
  0% { background-position: 120% 0; }
  100% { background-position: -80% 0; }
}
</style>
