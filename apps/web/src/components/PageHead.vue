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
  position: relative;
  display: flex;
  align-items: flex-end;
  flex-wrap: wrap;
  gap: 0.3rem 0.9rem;
  margin: 0.2rem 0 1.1rem;
  padding-bottom: 0.7rem;
}
/* HSR masthead rule: a gilded line bright at the anchor that fades out, echoing the .section
   divider — more authentic than a flat faint border that reads as generic SaaS. */
.page-head::after {
  content: "";
  position: absolute;
  left: 0;
  right: 0;
  bottom: 0;
  height: 1px;
  background: linear-gradient(90deg, var(--ow-gold) 0, var(--ow-gold-soft) 12%, transparent 68%);
}
.ph-titles {
  position: relative;
  display: flex;
  flex-direction: column;
  gap: 0.15rem;
  padding-left: 0.72rem;
}
/* gilded beveled accent bar — HSR mastheads lead the title with a small angular gold tick */
.ph-titles::before {
  content: "";
  position: absolute;
  left: 0;
  top: 0.12rem;
  bottom: 0.12rem;
  width: 3px;
  background: linear-gradient(180deg, var(--ow-gold-bright), var(--ow-gold-deep));
  clip-path: polygon(0 0, 100% 2px, 100% calc(100% - 2px), 0 100%);
  box-shadow: 0 0 8px rgba(240, 210, 138, 0.35);
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
  width: 44px;
  height: 44px;
  min-width: 44px;
  min-height: 44px;
  flex: none;
  border-radius: 50%;
  border: 1px solid var(--ow-gold-soft);
  background: var(--ow-gold-faint);
  color: var(--ow-gold-bright);
  font-size: 0.86rem;
  font-weight: 700;
  line-height: 1;
  cursor: pointer;
  touch-action: manipulation;
  transition: box-shadow 0.15s ease, transform 0.15s ease;
}
.ph-help:hover {
  box-shadow: 0 0 10px rgba(240, 210, 138, 0.4);
  transform: translateY(-1px);
}
.ph-help:focus-visible {
  outline: 2px solid var(--ow-cyan);
  outline-offset: 3px;
  box-shadow: 0 0 12px rgba(143, 214, 232, 0.28);
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
