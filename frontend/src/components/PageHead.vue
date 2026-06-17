<script setup lang="ts">
// Shared HSR masthead: English overline + CJK title + one concise purpose line, plus a per-page "?"
// that opens this page's onboarding tips. Keeps every page's top band consistent and self-teaching.
import { computed, ref } from "vue";
import { useRoute } from "vue-router";
import Modal from "./Modal.vue";
import { PAGE_HELP } from "../pageHelp";

defineProps<{ overline: string; title: string; purpose?: string }>();

const route = useRoute();
const help = computed(() => PAGE_HELP[route.path]);
const helpOpen = ref(false);
</script>

<template>
  <header class="page-head">
    <div class="ph-titles">
      <span class="overline">{{ overline }}</span>
      <h1 class="ph-title">
        {{ title }}
        <button
          v-if="help"
          class="ph-help"
          type="button"
          aria-label="本页说明"
          title="本页说明"
          @click="helpOpen = true"
        >?</button>
      </h1>
    </div>
    <p v-if="purpose" class="ph-purpose muted">{{ purpose }}</p>
    <div v-if="$slots.aside" class="ph-aside"><slot name="aside" /></div>
  </header>

  <Modal
    v-if="help"
    :open="helpOpen"
    overline="GUIDE"
    :title="help.title"
    @close="helpOpen = false"
  >
    <ul class="ph-tips reveal">
      <li v-for="(tip, i) in help.tips" :key="i">{{ tip }}</li>
    </ul>
  </Modal>
</template>

<style scoped>
.page-head {
  display: flex;
  align-items: flex-end;
  flex-wrap: wrap;
  gap: 0.3rem 0.9rem;
  margin: 0.2rem 0 1.1rem;
  padding-bottom: 0.7rem;
  border-bottom: 1px solid var(--ow-gold-faint);
}
.ph-titles {
  display: flex;
  flex-direction: column;
  gap: 0.15rem;
}
.ph-title {
  margin: 0;
  font-size: 1.5rem;
  letter-spacing: 0.03em;
  display: flex;
  align-items: center;
  gap: 0.55rem;
}
.ph-help {
  width: 20px;
  height: 20px;
  flex: none;
  border-radius: 50%;
  border: 1px solid var(--ow-gold-soft);
  background: var(--ow-gold-faint);
  color: var(--ow-gold-bright);
  font-size: 0.78rem;
  font-weight: 700;
  line-height: 1;
  cursor: pointer;
  transition: box-shadow 0.15s ease, transform 0.15s ease;
}
.ph-help:hover {
  box-shadow: 0 0 10px rgba(240, 210, 138, 0.4);
  transform: translateY(-1px);
}
.ph-purpose {
  font-size: 0.86rem;
  margin: 0;
  padding-bottom: 0.2rem;
}
.ph-aside {
  margin-left: auto;
  padding-bottom: 0.1rem;
}
.ph-tips {
  margin: 0;
  padding: 0;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 0.55rem;
}
.ph-tips li {
  position: relative;
  padding: 0.55rem 0.7rem 0.55rem 1.6rem;
  border-left: 2px solid var(--ow-gold-soft);
  background: rgba(143, 214, 232, 0.045);
  border-radius: 0 0.4rem 0.4rem 0;
  font-size: 0.88rem;
  line-height: 1.6;
  color: var(--ow-ink);
}
.ph-tips li::before {
  content: "";
  position: absolute;
  left: 0.7rem;
  top: 0.95rem;
  width: 6px;
  height: 6px;
  background: var(--ow-gold-bright);
  clip-path: polygon(50% 0, 60% 40%, 100% 50%, 60% 60%, 50% 100%, 40% 60%, 0 50%, 40% 40%);
}
</style>
