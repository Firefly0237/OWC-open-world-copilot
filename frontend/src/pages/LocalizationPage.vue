<script setup lang="ts">
import { onMounted, ref } from "vue";
import { apiGet, apiPost, currentProject } from "../api";
import { notifyError } from "../toast";
import PageHead from "../components/PageHead.vue";

interface Row {
  text_key: string;
  present_locales: string[];
  missing_locales: string[];
  status: Record<string, string>;
}
interface Overview {
  locales: string[];
  keys: number;
  by_status: Record<string, number>;
  missing_total: number;
  rows: Row[];
}

const data = ref<Overview | null>(null);

const STATUS_LABEL: Record<string, string> = {
  untranslated: "待译",
  translated: "已译",
  reviewing: "待校",
  final: "定稿",
};
const NEXT: Record<string, string> = {
  untranslated: "translated",
  translated: "reviewing",
  reviewing: "final",
};
function meName(): string {
  return (localStorage.getItem("owcopilot_operator") ?? "").trim();
}

async function load(): Promise<void> {
  try {
    data.value = (await apiGet<{ overview: Overview }>(
      `/projects/${currentProject()}/localization`,
    )).overview;
  } catch (e) {
    notifyError(e);
  }
}
onMounted(load);

async function advance(key: string, locale: string, current: string): Promise<void> {
  const to = NEXT[current];
  if (!to) return;
  if (!meName()) return void notifyError(new Error("请先在审阅台填写署名"));
  try {
    await apiPost(`/projects/${currentProject()}/localization:transition`, {
      text_key: key,
      locale,
      to,
      by: meName(),
    });
    await load();
  } catch (e) {
    notifyError(e);
  }
}
</script>

<template>
  <section>
    <PageHead
      overline="LOCALIZATION"
      title="本地化"
      purpose="翻译管理全流程：覆盖率、缺口、每条字符串走 待译→已译→待校→定稿。"
    />
    <button class="ghost" @click="load">刷新</button>

    <template v-if="data">
      <div class="summary">
        <span class="s-chip">{{ data.keys }} 条文案 · {{ data.locales.length }} 语言</span>
        <span class="s-chip warn" v-if="data.missing_total">缺口 {{ data.missing_total }}</span>
        <span v-for="(n, st) in data.by_status" :key="st" class="s-chip">
          {{ STATUS_LABEL[st] ?? st }} {{ n }}
        </span>
      </div>

      <table class="loc">
        <thead>
          <tr>
            <th>文案键</th>
            <th v-for="l in data.locales" :key="l">{{ l }}</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="r in data.rows" :key="r.text_key">
            <td class="mono key">{{ r.text_key }}</td>
            <td v-for="l in data.locales" :key="l">
              <button
                class="st"
                :class="r.status[l]"
                :disabled="!NEXT[r.status[l]]"
                :title="NEXT[r.status[l]] ? '点按推进状态' : '已定稿'"
                @click="advance(r.text_key, l, r.status[l])"
              >
                {{ STATUS_LABEL[r.status[l]] ?? r.status[l] }}
              </button>
            </td>
          </tr>
        </tbody>
      </table>
    </template>
    <p v-else class="muted empty">还没打开世界，或正在加载。</p>
  </section>
</template>

<style scoped>
.summary {
  display: flex;
  flex-wrap: wrap;
  gap: 0.5rem;
  margin: 1rem 0;
}
.s-chip {
  font-size: 0.78rem;
  border: 1px solid var(--ow-line);
  border-radius: 0.4rem;
  padding: 0.2rem 0.55rem;
  color: var(--ow-ink-dim);
}
.s-chip.warn {
  color: var(--ow-flag, #e0653a);
}
.loc {
  width: 100%;
  border-collapse: collapse;
  font-size: 0.82rem;
}
.loc th,
.loc td {
  text-align: left;
  padding: 0.4rem 0.6rem;
  border-bottom: 1px solid var(--ow-line);
}
.loc .key {
  color: var(--ow-ink-dim);
}
.st {
  background: var(--ow-panel-2);
  border: 1px solid var(--ow-line);
  border-radius: 0.35rem;
  padding: 0.15rem 0.5rem;
  cursor: pointer;
  font-size: 0.74rem;
  color: var(--ow-ink);
}
.st:disabled {
  cursor: default;
  opacity: 0.7;
}
.st.untranslated {
  color: var(--ow-flag, #e0653a);
}
.st.final {
  color: #6fcf97;
  border-color: #6fcf97;
}
.empty {
  padding: 2rem 0;
}
</style>
