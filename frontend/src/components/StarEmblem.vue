<script setup lang="ts">
defineProps<{ size?: number }>();
</script>

<template>
  <svg :width="size ?? 56" :height="size ?? 56" viewBox="0 0 100 100" fill="none">
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
  </svg>
</template>

<style scoped>
.orb {
  animation: emblem-spin 10s linear infinite;
  transform-origin: center;
  transform-box: fill-box;
}

.core {
  animation: emblem-pulse 2s ease-in-out infinite;
  transform-origin: center;
  transform-box: fill-box;
}

@keyframes emblem-spin {
  to {
    transform: rotate(360deg);
  }
}

@keyframes emblem-pulse {
  0%,
  100% {
    opacity: 0.7;
    transform: scale(0.96);
  }

  50% {
    opacity: 1;
    transform: scale(1.05);
  }
}
</style>
