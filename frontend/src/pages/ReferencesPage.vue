<script setup lang="ts">
import { onMounted, ref } from "vue";
import { apiGet, apiPost, currentProject } from "../api";
import PageHead from "../components/PageHead.vue";
import FilePicker from "../components/FilePicker.vue";
import { notifyError } from "../toast";

interface Source {
  id?: string;
  title: string;
  source_type?: string;
  original_filename?: string;
  language?: string;
  char_count?: number;
  chunk_count?: number;
  [k: string]: unknown;
}

interface Hit {
  ref: string;
  title: string;
  body: string;
}

const sources = ref<Source[]>([]);
const flash = ref("");

const newTitle = ref("");
const newText = ref("");
const adding = ref(false);

const query = ref("");
const searching = ref(false);
const hits = ref<Hit[]>([]);

async function refresh(): Promise<void> {
  const body = await apiGet<{ sources: Source[] }>(`/projects/${currentProject()}/references`);
  sources.value = body.sources;
}

onMounted(async () => {
  try {
    await refresh();
  } catch (e) {
    notifyError(e);
  }
});

function onFile(file: File): void {
  if (!newTitle.value.trim()) newTitle.value = file.name.replace(/\.[^.]+$/, "");
  const reader = new FileReader();
  reader.onload = () => {
    newText.value = String(reader.result ?? "");
  };
  reader.readAsText(file);
}

async function add(): Promise<void> {
  if (!newTitle.value.trim() || !newText.value.trim() || adding.value) return;
  adding.value = true;
  flash.value = "";
  try {
    await apiPost(`/projects/${currentProject()}/references`, {
      title: newTitle.value.trim(),
      text: newText.value,
      source_type: "uploaded_file",
    });
    flash.value = `已收录「${newTitle.value.trim()}」。`;
    newTitle.value = "";
    newText.value = "";
    await refresh();
  } catch (e) {
    notifyError(e);
  } finally {
    adding.value = false;
  }
}

async function search(): Promise<void> {
  if (!query.value.trim() || searching.value) return;
  searching.value = true;
  hits.value = [];
  try {
    const body = await apiPost<{ hits: Hit[] }>(`/projects/${currentProject()}/references:search`, {
      query: query.value.trim(),
    });
    hits.value = body.hits;
  } catch (e) {
    notifyError(e);
  } finally {
    searching.value = false;
  }
}
</script>

<template>
  <section>
    <PageHead overline="REFERENCES" title="灵感库" purpose="收录参考素材，仅用于创世检索，不入正式设定。" />

    <div class="grid">
      <div class="pane block">
        <div class="section"><span class="t">收录素材</span></div>
        <input v-model="newTitle" maxlength="200" placeholder="标题" />
        <FilePicker accept=".txt,.md,.json" hint="选择 .txt / .md / .json，或拖入" @select="onFile" />
        <textarea v-model="newText" rows="5" placeholder="粘贴文本，或选一个 .txt/.md/.json 文件自动读入"></textarea>
        <button class="primary" :disabled="adding || !newTitle.trim() || !newText.trim()" @click="add">
          {{ adding ? "收录中…" : "收录" }}
        </button>
        <p v-if="flash" class="flash">{{ flash }}</p>
      </div>

      <div class="pane block">
        <div class="section"><span class="t">灵感检索</span></div>
        <div class="row">
          <input v-model="query" placeholder="按主题/意象检索素材" @keydown.enter="search" />
          <button class="ghost" :disabled="searching || !query.trim()" @click="search">
            {{ searching ? "检索中…" : "检索" }}
          </button>
        </div>
        <div v-if="hits.length" class="hits">
          <div v-for="h in hits" :key="h.ref" class="hit">
            <span class="mono">{{ h.ref }}</span>
            <b>{{ h.title }}</b>
            <span class="muted body">{{ h.body }}</span>
          </div>
        </div>
        <p v-else-if="query && !searching" class="muted small">敲回车或点检索。</p>
      </div>
    </div>

    <div class="section"><span class="t">全部素材</span></div>
    <p v-if="!sources.length" class="muted">还没有素材——上面收录第一份。</p>
    <TransitionGroup name="list" tag="div" class="sources">
      <div v-for="(s, i) in sources" :key="s.id ?? i" class="pane src">
        <b>{{ s.title }}</b>
        <span class="muted meta">{{ s.source_type ?? "素材" }}<template v-if="s.language"> · {{ s.language }}</template><template v-if="s.char_count"> · {{ s.char_count.toLocaleString() }} 字</template><template v-if="s.chunk_count"> · {{ s.chunk_count }} 块可检索</template></span>
      </div>
    </TransitionGroup>
  </section>
</template>

<style scoped>
.hint {
  font-size: 0.85rem;
}

.grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
  gap: 0.8rem;
  margin-bottom: 1rem;
}

.block {
  padding: 0.9rem 1.1rem;
  display: flex;
  flex-direction: column;
  gap: 0.55rem;
}

.row {
  display: flex;
  gap: 0.5rem;
}

input,
textarea {
  background: var(--ow-panel-2);
  border: 1px solid var(--ow-line);
  border-radius: var(--ow-control-radius);
  color: var(--ow-ink);
  padding: 0.5rem 0.65rem;
  font: inherit;
  font-size: 0.88rem;
}

.row input {
  flex: 1;
}

textarea {
  resize: vertical;
}

input:focus,
textarea:focus {
  outline: none;
  border-color: var(--ow-gold-soft);
}

button {
  border-radius: var(--ow-control-radius);
  cursor: pointer;
  font: inherit;
  font-size: 0.85rem;
  padding: 0.45rem 0.9rem;
  background: var(--ow-panel-2);
  border: 1px solid var(--ow-line);
  color: var(--ow-ink);
}

button.primary {
  background: linear-gradient(180deg, #f0d28a 0%, #b9924a 100%);
  border-color: rgba(240, 210, 138, 0.65);
  color: #241a05;
  font-weight: 600;
  align-self: flex-start;
}

button:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}

.flash {
  color: #8ed4ac;
  font-size: 0.82rem;
}

.error {
  color: #e89a9a;
}

.small {
  font-size: 0.78rem;
}

.hits {
  display: flex;
  flex-direction: column;
  gap: 0.4rem;
  margin-top: 0.5rem;
}

.hit {
  display: flex;
  flex-direction: column;
  gap: 0.15rem;
  border: 1px solid var(--ow-line);
  border-radius: var(--ow-control-radius);
  background: var(--ow-panel-2);
  padding: 0.45rem 0.65rem;
  font-size: 0.83rem;
}

.hit b {
  color: var(--ow-gold-bright);
}

.mono {
  font-family: ui-monospace, Consolas, monospace;
  font-size: 0.72rem;
  color: var(--ow-cyan);
}

.body {
  font-size: 0.8rem;
}

.sources {
  display: flex;
  flex-direction: column;
  gap: 0.45rem;
}

.src {
  display: flex;
  align-items: baseline;
  gap: 0.7rem;
  padding: 0.55rem 0.9rem;
}

.src b {
  color: var(--ow-gold-bright);
  font-family: var(--ow-serif);
}

.meta {
  font-size: 0.78rem;
}

.list-enter-active,
.list-move {
  transition: all 0.3s ease;
}

.list-enter-from {
  opacity: 0;
  transform: translateY(6px);
}
</style>
