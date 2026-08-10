<script setup lang="ts">
// 溯源展示（N2.4 / B7）：三处产物（创世/扩写/审阅台）共用一份 reference_report 渲染，
// 别让三页各写一套 JSON.stringify 兜底。字段与 worldgen.models.ReferenceReportItem 对齐——
// 调用方各自本地声明同形状的接口即可（与本仓库其余页面对 API 响应形状的一贯做法一致）。
// N3.1：前四个字段是模型自述，leakage 是代码重算出来的检测结论，所以分开渲染——不能让读者
// 把"模型说它没抄"和"我们查过没越阈值"看成同一句话。老的产物没有这个字段，读作"没查"。
interface ReferenceLeakage {
  checked: boolean;
  detected: boolean;
  verbatim_spans: string[];
  leaked_names: string[];
  threshold_label: string;
  message: string;
}

interface ReferenceReportRow {
  source_ref: string;
  source_title: string;
  used_for: string;
  transformation: string;
  excluded: string[];
  leakage?: ReferenceLeakage | null;
}

defineProps<{ rows: ReferenceReportRow[] }>();
</script>

<template>
  <template v-if="rows.length">
    <div class="section"><span class="t">溯源 · 借鉴自灵感库</span></div>
    <div class="ref-report">
      <div v-for="(row, i) in rows" :key="i" class="ref-row">
        <div class="ref-head">
          <b>{{ row.source_title || row.source_ref }}</b>
          <span class="mono muted small">{{ row.source_ref }}</span>
        </div>
        <p v-if="row.used_for" class="muted line">用于：{{ row.used_for }}</p>
        <p v-if="row.transformation" class="line">转化说明：{{ row.transformation }}</p>
        <p v-if="row.excluded && row.excluded.length" class="warn line">
          未采用：{{ row.excluded.join("、") }}
        </p>
        <p v-if="row.leakage" class="line" :class="row.leakage.detected ? 'alert' : 'muted'">
          <b>{{ row.leakage.detected ? "泄漏检测 · 需人工确认" : "泄漏检测" }}：</b>
          {{ row.leakage.message }}
        </p>
        <p v-if="row.leakage && row.leakage.leaked_names.length" class="alert line">
          疑似照搬的专名：{{ row.leakage.leaked_names.join("、") }}
        </p>
      </div>
    </div>
  </template>
</template>

<style scoped>
.ref-report {
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
  margin-bottom: 0.4rem;
}

.ref-row {
  border: 1px solid var(--ow-line);
  border-radius: 0.6rem;
  background: var(--ow-panel-2);
  padding: 0.55rem 0.75rem;
  display: flex;
  flex-direction: column;
  gap: 0.2rem;
  font-size: 0.85rem;
}

.ref-head {
  display: flex;
  align-items: baseline;
  gap: 0.6rem;
  flex-wrap: wrap;
}

.ref-head b {
  color: var(--ow-gold-bright);
  font-family: var(--ow-serif);
}

.mono {
  font-family: ui-monospace, Consolas, monospace;
  font-size: 0.72rem;
  color: var(--ow-cyan);
}

.small {
  font-size: 0.72rem;
}

.line {
  margin: 0;
  line-height: 1.55;
}

.warn {
  color: #e6c07e;
  font-size: 0.82rem;
}

.alert {
  color: #e08b7a;
  font-size: 0.82rem;
}
</style>
