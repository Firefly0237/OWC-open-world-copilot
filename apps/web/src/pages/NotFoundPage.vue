<script setup lang="ts">
import PageHead from "../components/PageHead.vue";
</script>

<template>
  <section>
    <PageHead overline="LOST CHAPTER" title="这一页不在星图中" purpose="链接可能已经失效，也可能只是写错了地址。" />
    <div class="pane lost-card">
      <div class="lost-sigil" aria-hidden="true"><span /></div>
      <p>没有找到你要打开的页面。已经保存的世界内容不会受到影响。</p>
      <div class="lost-actions">
        <RouterLink class="primary-link" to="/overview">返回世界总览</RouterLink>
        <RouterLink class="secondary-link" to="/worlds">管理工作区</RouterLink>
      </div>
    </div>
  </section>
</template>

<style scoped>
.lost-card {
  min-height: 330px;
  padding: clamp(2rem, 7vw, 5rem) 1.5rem;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  text-align: center;
  gap: 1rem;
}
.lost-card p { max-width: 34rem; margin: 0; color: var(--ow-muted); line-height: 1.8; }
.lost-sigil {
  position: relative;
  width: 88px;
  height: 88px;
  border: 1px solid var(--ow-gold-soft);
  border-radius: 50%;
  transform: rotate(-12deg);
}
.lost-sigil::before {
  content: "";
  position: absolute;
  inset: 14px;
  border: 1px dashed var(--ow-edge-violet);
  border-radius: 50%;
}
.lost-sigil::after {
  content: "";
  position: absolute;
  inset: 38px;
  border-radius: 50%;
  background: var(--ow-gold-bright);
  box-shadow: 0 0 14px var(--ow-gold-soft);
}
.lost-sigil span {
  position: absolute;
  width: 118px;
  height: 1px;
  top: 43px;
  left: -15px;
  background: linear-gradient(90deg, transparent, var(--ow-gold), transparent);
}
.lost-actions { display: flex; flex-wrap: wrap; justify-content: center; gap: 0.7rem; }
.primary-link,
.secondary-link {
  padding: 0.65rem 1rem;
  border: 1px solid var(--ow-gold-soft);
  color: var(--ow-gold-bright);
  text-decoration: none;
  background: var(--ow-gold-faint);
}
.secondary-link { color: var(--ow-ink-soft); border-color: var(--ow-line); background: transparent; }
</style>
