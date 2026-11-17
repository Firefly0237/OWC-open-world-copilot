<script setup lang="ts">
import { computed, ref, watch } from "vue";
import type { StoryTree } from "../storyCreatorApi";

const props = defineProps<{ tree: StoryTree; selected: string; visited?: string[] }>();
const emit = defineEmits<{ select: [nodeId: string] }>();
const search = ref("");
const nodes = computed(() => Object.entries(props.tree.nodes));
const filtered = computed(() => nodes.value.filter(([key, node]) => !search.value || `${key} ${node.text}`.toLowerCase().includes(search.value.toLowerCase())));
const current = computed(() => props.tree.nodes[props.selected]);
const outgoing = computed(() => {
  const node = current.value;
  if (!node) return [];
  const paths = (node.choices ?? []).map((choice) => ({ text: choice.text, target: choice.next_node ?? null, conditional: Boolean(choice.condition), effects: choice.effects?.length ?? 0 }));
  if (node.next_node) paths.push({ text: "继续对白", target: node.next_node, conditional: false, effects: 0 });
  return paths;
});
watch(() => props.tree.id, () => { search.value = ""; });
</script>

<template>
  <section class="branch-map">
    <div class="map-head"><div><h3>分支导航</h3><p>{{ nodes.length }}个对白位置 · 点击节点或去向查看原文</p></div><label>查找对白<input v-model="search" type="search"></label></div>
    <div class="map-layout"><nav class="node-index" aria-label="对白位置"><button v-for="([key, node], index) in filtered" :key="key" :class="{ selected: key === selected, visited: visited?.includes(key) }" @click="emit('select', key)"><span>{{ index + 1 }}</span><div>{{ node.text.slice(0, 60) || '空白位置' }}<small>{{ key === tree.root_node ? '入口' : '' }}{{ visited?.includes(key) ? ' · 本次已走过' : '' }}</small></div></button><p v-if="!filtered.length">没有找到匹配原文。</p></nav>
      <div class="flow-focus"><article v-if="current" class="focus-node"><span class="tiny">当前查看</span><p>{{ current.text }}</p></article><div v-if="outgoing.length" class="branch-paths"><article v-for="(path, index) in outgoing" :key="index"><span class="connector" aria-hidden="true">↳</span><p>{{ path.text }}</p><small>{{ path.conditional ? '带有进入条件' : '未声明额外条件' }}<span v-if="path.effects"> · 声明{{ path.effects }}项状态变化</span></small><button v-if="path.target && tree.nodes[path.target]" @click="emit('select', path.target)">查看后文 →</button><span v-else class="ending">{{ path.target ? '去向尚不存在' : '对白到此汇出或结束' }}</span></article></div><p v-else class="end-note">此处没有声明后续对白分支。是否接入任务阶段，以实际试玩为准。</p></div>
    </div>
    <p class="map-note">这是草稿的结构导航；条件是否成立与实际选择后果，请在试玩中查看，不由静态连线推断。</p>
  </section>
</template>

<style scoped>
h3 { font-size: 1rem; margin: 0 0 .3rem; }.map-head { display: flex; flex-wrap: wrap; justify-content: space-between; gap: .8rem; margin-bottom: 1rem; }
.map-head p, .map-note { color: var(--ow-ink-mute); font-size: .76rem; line-height: 1.7; margin: 0; }
input { background: var(--ow-paper); color: inherit; border: 1px solid var(--ow-paper-edge); border-radius: 6px; padding: .55rem .7rem; font: inherit; font-size: .8rem; }
.map-layout { display: grid; grid-template-columns: minmax(150px,.65fr) minmax(0,1.35fr); gap: 1rem; }
button { font: inherit; color: inherit; cursor: pointer; }
.node-index { max-height: 25rem; overflow: auto; display: flex; flex-direction: column; gap: .4rem; }
.node-index > button { background: transparent; border: 1px solid transparent; border-radius: 6px; padding: .65rem; display: flex; text-align: left; gap: .65rem; align-items: flex-start; font-size: .76rem; line-height: 1.65; }
.node-index > button.selected { border-color: var(--ow-gold-soft); background: var(--ow-gold-faint); }
.node-index > button.visited > span { color: var(--ow-cyan); }.node-index > button > span { color: var(--ow-gold); font-size: .7rem; }.node-index small { display: block; color: var(--ow-ink-mute); font-size: .66rem; }
.focus-node { border: 1px solid var(--ow-gold-soft); border-radius: 8px; padding: 1rem; background: var(--ow-gold-faint); }
.focus-node p { font-family: var(--ow-serif); line-height: 1.9; white-space: pre-wrap; margin: .4rem 0 0; font-size: .92rem; }.tiny { color: var(--ow-gold); font-size: .7rem; }
.branch-paths { display: grid; grid-template-columns: repeat(2,minmax(0,1fr)); gap: .7rem; margin: .8rem 0; }.branch-paths article { position: relative; padding: .9rem; border: 1px solid var(--ow-paper-edge); border-radius: 8px; }.branch-paths p { font-size: .83rem; line-height: 1.75; margin: .3rem 0; }.branch-paths small { color: var(--ow-ink-mute); font-size: .68rem; line-height: 1.65; display: block; }.branch-paths button { margin-top: .7rem; background: none; border: none; color: var(--ow-cyan); padding: 0; font-size: .73rem; }
.connector { position: absolute; top: -.85rem; left: 1rem; color: var(--ow-gold); }.ending { display: block; margin-top: .7rem; color: var(--ow-ink-mute); font-size: .72rem; }.end-note { line-height: 1.8; font-size: .8rem; color: var(--ow-ink-mute); }.map-note { margin-top: .9rem; }
@media (max-width: 680px) { .map-layout { grid-template-columns: minmax(0,1fr); }.node-index { max-height: 10rem; }.branch-paths { grid-template-columns: minmax(0,1fr); } }
</style>
