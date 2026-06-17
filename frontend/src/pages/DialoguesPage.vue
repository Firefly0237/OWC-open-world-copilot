<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import SvgGraph from "../components/SvgGraph.vue";
import PageHead from "../components/PageHead.vue";
import type { GraphEdge, GraphNode } from "../graphTheme";
import {
  humanizeError,
  apiGet,
  apiPatch,
  currentProject,
} from "../api";

// 对话树 Flow（可编辑）：把分支对话铺成 Detroit 式流程图（卡片节点 + 选择连线），
// 点节点开右侧面板改台词/说话人/选择，拖动重排，新增/删除节点、设起点都即时写入正典。
// flow 端点给布局与边（展示），GET /dialogue_trees/{id} 给完整结构（用于无损编辑）。
interface TreeSummary {
  id: string;
  title: string;
  participants: string[];
  node_count: number;
}
interface DialogueFlow {
  tree_id: string;
  title: string;
  nodes: GraphNode[];
  edges: GraphEdge[];
}

// 结构化节点：PATCH nodes 时整张表回传，故本地维护一份。
interface Choice {
  text: string;
  next_node: string; // "" 表示（结束）
  condition: string;
}
interface EditNode {
  id: string;
  speaker_id: string;
  text: string;
  choices: Choice[];
  next_node: string; // 无选择时的线性下一步；"" 表示结束
}

const trees = ref<TreeSummary[]>([]);
const selected = ref(""); // 当前对话树 id
const flow = ref<DialogueFlow | null>(null);
const rootNode = ref(""); // 当前树的起点节点 id
const localNodes = ref<Record<string, EditNode>>({});
const selectedNode = ref(""); // 当前选中的流程节点 ref（= 节点 id）
const draft = ref<EditNode | null>(null); // 编辑面板草稿（selectedNode 的副本）

const error = ref("");
const flash = ref("");
const loading = ref(false);

const proj = () => currentProject();

const currentTree = computed(() => trees.value.find((t) => t.id === selected.value));

// 说话人候选：本树参与者去重。
const speakerOptions = computed(() => currentTree.value?.participants ?? []);

// 选「下一步」用的节点 id 列表。
const nodeIds = computed(() => Object.keys(localNodes.value));

const flowNode = computed(() => flow.value?.nodes.find((n) => n.ref === selectedNode.value));

function flashOk(msg: string): void {
  flash.value = msg;
  error.value = "";
}

// 结构化树（GET /dialogue_trees/{id}）：完整台词 / 说话人 / 选择，无截断。
interface StructTree {
  id: string;
  title: string;
  root_node: string;
  nodes: Record<
    string,
    {
      id: string;
      speaker_id?: string | null;
      text?: string;
      choices?: { text: string; next_node?: string | null; condition?: string }[];
      next_node?: string | null;
    }
  >;
}

// 从完整结构树播种本地可编辑节点（不再依赖被截断的 flow 显示文本）。
function seedFromTree(tree: StructTree): void {
  const map: Record<string, EditNode> = {};
  for (const [id, n] of Object.entries(tree.nodes)) {
    map[id] = {
      id,
      speaker_id: n.speaker_id ?? "",
      text: n.text ?? "",
      choices: (n.choices ?? []).map((c) => ({
        text: c.text ?? "",
        next_node: c.next_node ?? "",
        condition: c.condition ?? "",
      })),
      next_node: n.next_node ?? "",
    };
  }
  localNodes.value = map;
  rootNode.value = tree.root_node;
}

async function fetchFlow(id: string): Promise<void> {
  // flow 给布局/边（展示），结构树给完整可编辑内容。
  const [flowBody, treeBody] = await Promise.all([
    apiGet<{ flow: DialogueFlow }>(
      `/projects/${proj()}/dialogue_trees/${encodeURIComponent(id)}/flow`,
    ),
    apiGet<{ tree: StructTree }>(`/projects/${proj()}/dialogue_trees/${encodeURIComponent(id)}`),
  ]);
  flow.value = flowBody.flow;
  seedFromTree(treeBody.tree);
  if (selectedNode.value && !localNodes.value[selectedNode.value]) {
    selectedNode.value = "";
    draft.value = null;
  } else if (selectedNode.value) {
    syncDraft(selectedNode.value);
  }
}

async function openTree(id: string): Promise<void> {
  selected.value = id;
  selectedNode.value = "";
  draft.value = null;
  loading.value = true;
  error.value = "";
  flash.value = "";
  try {
    await fetchFlow(id);
  } catch (e) {
    error.value = humanizeError(e);
  } finally {
    loading.value = false;
  }
}

// 把 localNodes 里某节点拷进可编辑草稿。
function syncDraft(id: string): void {
  const n = localNodes.value[id];
  if (!n) {
    draft.value = null;
    return;
  }
  draft.value = {
    id: n.id,
    speaker_id: n.speaker_id,
    text: n.text,
    next_node: n.next_node,
    choices: n.choices.map((c) => ({ ...c })),
  };
}

function onSelect(ref: string): void {
  selectedNode.value = ref;
  syncDraft(ref);
}

// 拖动累积到 node_pos，move-end 时 PATCH metadata_updates.node_pos。
async function onMove(ref: string, x: number, y: number): Promise<void> {
  // 本地即时反映，避免回弹。
  const node = flow.value?.nodes.find((n) => n.ref === ref);
  if (node) {
    node.x = x;
    node.y = y;
  }
  const pos: Record<string, [number, number]> = {};
  for (const n of flow.value?.nodes ?? []) pos[n.ref] = [n.x, n.y];
  try {
    await apiPatch(`/projects/${proj()}/dialogue_trees/${encodeURIComponent(selected.value)}`, {
      metadata_updates: { node_pos: pos },
    });
  } catch (e) {
    error.value = humanizeError(e);
  }
}

// 把 localNodes 组装成后端要的整张 nodes 表。
function buildNodesPayload(): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const n of Object.values(localNodes.value)) {
    const entry: Record<string, unknown> = { id: n.id, text: n.text };
    if (n.speaker_id) entry.speaker_id = n.speaker_id;
    if (n.choices.length) {
      entry.choices = n.choices.map((c) => {
        const choice: Record<string, unknown> = { text: c.text };
        if (c.next_node) choice.next_node = c.next_node;
        if (c.condition) choice.condition = c.condition;
        return choice;
      });
    } else if (n.next_node) {
      entry.next_node = n.next_node;
    }
    out[n.id] = entry;
  }
  return out;
}

async function patchNodes(okMsg: string): Promise<void> {
  loading.value = true;
  error.value = "";
  try {
    await apiPatch(`/projects/${proj()}/dialogue_trees/${encodeURIComponent(selected.value)}`, {
      nodes: buildNodesPayload(),
    });
    await fetchFlow(selected.value); // 刷新布局/边
    flashOk(okMsg);
  } catch (e) {
    error.value = humanizeError(e);
  } finally {
    loading.value = false;
  }
}

// 保存当前节点：把草稿写回 localNodes，再整表 PATCH。
async function saveNode(): Promise<void> {
  if (!draft.value) return;
  const id = draft.value.id;
  localNodes.value[id] = {
    id,
    speaker_id: draft.value.speaker_id,
    text: draft.value.text,
    next_node: draft.value.choices.length ? "" : draft.value.next_node,
    choices: draft.value.choices.map((c) => ({ ...c })),
  };
  await patchNodes("已保存。");
}

function addChoice(): void {
  if (!draft.value) return;
  draft.value.choices.push({ text: "", next_node: "", condition: "" });
}

function removeChoice(i: number): void {
  draft.value?.choices.splice(i, 1);
}

// 新增节点：id = n{maxIndex+1}，整表 PATCH 后选中它。
async function addNode(): Promise<void> {
  let max = 0;
  for (const id of nodeIds.value) {
    const m = /^n(\d+)$/.exec(id);
    if (m) max = Math.max(max, Number(m[1]));
  }
  const id = `n${max + 1}`;
  localNodes.value[id] = {
    id,
    speaker_id: "",
    text: "新节点",
    choices: [],
    next_node: "",
  };
  loading.value = true;
  error.value = "";
  try {
    await apiPatch(`/projects/${proj()}/dialogue_trees/${encodeURIComponent(selected.value)}`, {
      nodes: buildNodesPayload(),
    });
    await fetchFlow(selected.value);
    flashOk("已新增节点。");
    onSelect(id);
  } catch (e) {
    error.value = humanizeError(e);
  } finally {
    loading.value = false;
  }
}

// 删除节点：禁删起点；从表里移除，并清掉指向它的选择/next。
async function deleteNode(): Promise<void> {
  const id = selectedNode.value;
  if (!id || !localNodes.value[id]) return;
  if (id === rootNode.value) {
    error.value = "起点节点不能删除。请先把另一个节点「设为起点」。";
    return;
  }
  if (!window.confirm(`删除节点「${id}」？指向它的选择会一并清除。`)) return;
  delete localNodes.value[id];
  for (const node of Object.values(localNodes.value)) {
    node.choices = node.choices.filter((c) => c.next_node !== id);
    for (const c of node.choices) if (c.next_node === id) c.next_node = "";
    if (node.next_node === id) node.next_node = "";
  }
  selectedNode.value = "";
  draft.value = null;
  await patchNodes("已删除。");
}

// 设为起点。
async function setRoot(): Promise<void> {
  const id = selectedNode.value;
  if (!id) return;
  loading.value = true;
  error.value = "";
  try {
    await apiPatch(`/projects/${proj()}/dialogue_trees/${encodeURIComponent(selected.value)}`, {
      root_node: id,
    });
    await fetchFlow(selected.value);
    flashOk("已设为起点。");
  } catch (e) {
    error.value = humanizeError(e);
  } finally {
    loading.value = false;
  }
}

onMounted(async () => {
  try {
    const body = await apiGet<{ dialogues: { trees: TreeSummary[] } }>(
      `/projects/${proj()}/dialogue_trees`,
    );
    trees.value = body.dialogues.trees;
    if (trees.value.length) await openTree(trees.value[0].id);
  } catch (e) {
    error.value = humanizeError(e);
  }
});
</script>

<template>
  <section>
    <PageHead overline="DIALOGUE" title="对话流" purpose="分支对话铺成流程图，点节点改台词与选择。" />

    <p v-if="error" class="error">{{ error }}</p>
    <p v-if="flash" class="flash">{{ flash }}</p>

    <p v-if="!trees.length && !error" class="pane empty muted">
      还没有对话树。去「创作工坊 · 对话树」生成一棵，这里就能编辑它的分支流程。
    </p>

    <div v-else-if="trees.length" class="layout">
      <ul class="list">
        <li
          v-for="tree in trees"
          :key="tree.id"
          class="pane row"
          :class="{ on: selected === tree.id }"
          @click="openTree(tree.id)"
        >
          <b>{{ tree.title }}</b>
          <span class="meta muted">
            {{ tree.node_count }} 节点 · {{ tree.participants.join("、") || "无指定说话人" }}
          </span>
        </li>
      </ul>

      <div class="canvas-col">
        <div class="stage pane" :class="{ loading }">
          <div v-if="flow" class="stage-head">
            <b>{{ flow.title }}</b>
            <span class="muted small">{{ flow.nodes.length }} 个台词节点</span>
            <button class="ghost head-btn" @click="addNode">+ 新增节点</button>
          </div>
          <p v-if="flow && !flow.nodes.length" class="muted empty">这棵树还没有节点。点「新增节点」开始。</p>
          <SvgGraph
            v-else-if="flow"
            shape="card"
            draggable
            :nodes="flow.nodes"
            :edges="flow.edges"
            :selected="selectedNode"
            @select="onSelect"
            @move="onMove"
          />
        </div>
        <p class="muted tiny">拖动卡片重排，点卡片在右侧编辑。带光环的卡片是起点。</p>
      </div>

      <div v-if="draft" class="pane detail">
        <div class="d-head">
          <b>节点 {{ draft.id }}</b>
          <span v-if="selectedNode === rootNode" class="root-tag">起点</span>
        </div>
        <label class="f">
          <span>说话人</span>
          <select v-if="speakerOptions.length" v-model="draft.speaker_id">
            <option value="">（未指定）</option>
            <option v-for="s in speakerOptions" :key="s" :value="s">{{ s }}</option>
          </select>
          <input v-else v-model="draft.speaker_id" placeholder="说话人 id（可留空）" />
        </label>

        <label class="f">
          <span>台词</span>
          <textarea v-model="draft.text" rows="3"></textarea>
        </label>

        <div class="section sub"><span class="t">选择</span></div>
        <div v-if="draft.choices.length" class="choices">
          <div v-for="(c, i) in draft.choices" :key="i" class="choice-row">
            <input v-model="c.text" class="ch-text" placeholder="选项文案" />
            <select v-model="c.next_node" class="ch-next">
              <option value="">（结束）</option>
              <option v-for="id in nodeIds" :key="id" :value="id" :disabled="id === draft.id">
                {{ id }}
              </option>
            </select>
            <button class="x" title="移除选项" @click="removeChoice(i)">×</button>
          </div>
        </div>
        <p v-else class="muted tiny">没有分支选择。</p>

        <label v-if="!draft.choices.length" class="f next-line">
          <span>无选择时的下一步</span>
          <select v-model="draft.next_node">
            <option value="">（结束）</option>
            <option v-for="id in nodeIds" :key="id" :value="id" :disabled="id === draft.id">
              {{ id }}
            </option>
          </select>
        </label>

        <div class="d-actions">
          <button class="ghost" @click="addChoice">+ 加选择</button>
          <button class="primary" @click="saveNode">保存节点</button>
        </div>
        <div class="d-actions">
          <button
            class="ghost"
            :disabled="selectedNode === rootNode"
            @click="setRoot"
          >
            设为起点
          </button>
          <button class="danger" :disabled="selectedNode === rootNode" @click="deleteNode">
            删除节点
          </button>
        </div>
      </div>

      <div v-else-if="flow" class="pane detail hint-panel muted">
        点一张卡片，在这里编辑它的台词、说话人和选择。
      </div>
    </div>
  </section>
</template>

<style scoped>
.hint {
  font-size: 0.85rem;
}

.empty {
  padding: 1.4rem;
  text-align: center;
}

.layout {
  display: grid;
  grid-template-columns: 13rem 1fr 17rem;
  gap: 0.9rem;
  align-items: start;
}

.list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}

.row {
  padding: 0.6rem 0.8rem;
  cursor: pointer;
  display: flex;
  flex-direction: column;
  gap: 0.2rem;
  border-left: 2px solid transparent;
}

.row.on {
  border-left-color: var(--ow-gold);
  background: var(--ow-gold-faint);
}

.row b {
  color: var(--ow-gold-bright);
  font-family: var(--ow-serif);
  font-size: 0.92rem;
}

.meta {
  font-size: 0.76rem;
}

.canvas-col {
  display: flex;
  flex-direction: column;
  gap: 0.3rem;
}

.stage {
  padding: 0.7rem 0.9rem;
  min-height: 14rem;
  transition: opacity 0.15s ease;
}

.stage.loading {
  opacity: 0.5;
}

.stage-head {
  display: flex;
  align-items: baseline;
  gap: 0.6rem;
  margin-bottom: 0.4rem;
}

.stage-head b {
  font-family: var(--ow-serif);
  color: var(--ow-ink);
}

.head-btn {
  margin-left: auto;
}

.small {
  font-size: 0.76rem;
}

.tiny {
  font-size: 0.74rem;
}

/* ---- buttons / fields (matches GraphPage) ---- */
button {
  border-radius: 0.5rem;
  cursor: pointer;
  font: inherit;
  font-size: 0.84rem;
  padding: 0.45rem 0.85rem;
  background: var(--ow-panel-2);
  border: 1px solid var(--ow-line);
  color: var(--ow-ink);
}
button.primary {
  background: linear-gradient(180deg, #f0d28a 0%, #b9924a 100%);
  border-color: rgba(240, 210, 138, 0.65);
  color: #241a05;
  font-weight: 600;
}
button.ghost {
  color: var(--ow-muted);
}
button.danger {
  border-color: rgba(224, 133, 133, 0.45);
  color: #e89a9a;
}
button:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

select,
input,
textarea {
  background: var(--ow-panel-2);
  border: 1px solid var(--ow-line);
  border-radius: 0.5rem;
  color: var(--ow-ink);
  padding: 0.45rem 0.6rem;
  font: inherit;
  font-size: 0.86rem;
}
select:focus,
input:focus,
textarea:focus {
  outline: none;
  border-color: var(--ow-gold-soft);
}

.detail {
  padding: 0.8rem 0.9rem;
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}
.hint-panel {
  font-size: 0.84rem;
  text-align: center;
  padding: 1.6rem 1rem;
}
.d-head {
  display: flex;
  align-items: center;
  gap: 0.5rem;
}
.d-head b {
  color: var(--ow-gold-bright);
  font-family: var(--ow-serif);
}
.root-tag {
  font-size: 0.72rem;
  color: #241a05;
  background: linear-gradient(180deg, #f0d28a 0%, #b9924a 100%);
  border-radius: 0.4rem;
  padding: 0.05rem 0.4rem;
}
.f {
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
  font-size: 0.8rem;
  color: var(--ow-muted);
}
.next-line {
  margin-top: 0.2rem;
}
.sub {
  margin: 0.5rem 0 0.2rem;
}
.choices {
  display: flex;
  flex-direction: column;
  gap: 0.4rem;
}
.choice-row {
  display: flex;
  align-items: center;
  gap: 0.35rem;
}
.ch-text {
  flex: 1;
  min-width: 0;
}
.ch-next {
  width: 5.2rem;
}
.x {
  padding: 0 0.45rem;
  color: #e89a9a;
  border-color: transparent;
  background: transparent;
}
.d-actions {
  display: flex;
  gap: 0.5rem;
  margin-top: 0.2rem;
}
.d-actions .primary,
.d-actions .ghost,
.d-actions .danger {
  flex: 1;
}

.error {
  color: #e89a9a;
}
.flash {
  color: #8ed4ac;
}

@media (prefers-reduced-motion: reduce) {
  .stage {
    transition: none;
  }
}

@media (max-width: 920px) {
  .layout {
    grid-template-columns: 1fr;
  }
}
</style>
