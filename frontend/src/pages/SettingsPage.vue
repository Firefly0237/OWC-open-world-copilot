<script setup lang="ts">
import { onMounted, reactive, ref } from "vue";
import { humanizeError, apiGet, apiPost, llmConfig, setLlmConfig } from "../api";
import PageHead from "../components/PageHead.vue";

/** Per-provider API key registration guide. */
const PROVIDER_GUIDE: Record<string, { url: string; hint: string }> = {
  DeepSeek: {
    url: "https://platform.deepseek.com/api_keys",
    hint: "在 DeepSeek 平台注册账号，进入控制台生成一个 Key，复制粘贴进来。通常每次生成消耗几分到几毛钱。Key 只保存在本机，不上传。",
  },
  OpenAI: {
    url: "https://platform.openai.com/api-keys",
    hint: "在 OpenAI 平台注册账号，进入 API Keys 页面生成一个 Key，复制粘贴进来。通常每次生成消耗几分到几毛钱。Key 只保存在本机，不上传。",
  },
  "Anthropic Claude": {
    url: "https://console.anthropic.com/settings/keys",
    hint: "在 Anthropic Console 注册账号，进入 API Keys 生成一个 Key，复制粘贴进来。通常每次生成消耗几分到几毛钱。Key 只保存在本机，不上传。",
  },
  "Moonshot Kimi": {
    url: "https://platform.moonshot.cn/console/api-keys",
    hint: "在 Moonshot 平台注册账号，进入控制台生成 API Key，复制粘贴进来。通常每次生成消耗几分到几毛钱。Key 只保存在本机，不上传。",
  },
  "智谱 GLM": {
    url: "https://open.bigmodel.cn/usercenter/apikeys",
    hint: "在智谱开放平台注册账号，进入 API Keys 页面生成 Key，复制粘贴进来。通常每次生成消耗几分到几毛钱。Key 只保存在本机，不上传。",
  },
  通义千问: {
    url: "https://bailian.console.aliyun.com/",
    hint: "在阿里云百炼平台开通服务，获取 API Key，复制粘贴进来。通常每次生成消耗几分到几毛钱。Key 只保存在本机，不上传。",
  },
  "豆包（火山方舟）": {
    url: "https://console.volcengine.com/ark/region:ark+cn-beijing/apiKey",
    hint: "在火山方舟控制台注册并开通服务，获取 API Key，复制粘贴进来。通常每次生成消耗几分到几毛钱。Key 只保存在本机，不上传。",
  },
};

/** Speed/price hints for known model names (display-only, does not affect form.modelPick value). */
const MODEL_HINTS: Record<string, string> = {
  "deepseek-v4-flash": "（快速·便宜，适合日常生成）",
  "deepseek-v4-pro": "（更强·较贵，适合复杂任务）",
  "gpt-5.5": "（旗舰，贵）",
  "gpt-5.4-mini": "（快速，便宜）",
  "gpt-5.4-nano": "（极速，最便宜）",
  "gpt-5.2": "（均衡）",
  "gpt-5.2-chat-latest": "（均衡，最新）",
  "claude-fable-5": "（旗舰，贵）",
  "claude-opus-4-8": "（旗舰，贵）",
  "claude-sonnet-4-6": "（均衡，中价）",
  "claude-haiku-4-5-20251001": "（快速，便宜）",
  "kimi-k2.6": "（旗舰）",
  "kimi-k2.5": "（均衡）",
  "glm-5.1": "（旗舰）",
  "glm-5": "（均衡）",
  "glm-4.7": "（快速，便宜）",
  "qwen3.7-max": "（旗舰，贵）",
  "qwen3.5-plus": "（均衡，中价）",
  "qwen3.5-flash": "（快速，便宜）",
  "doubao-seed-1.8": "（旗舰）",
  "doubao-seed-1.6": "（均衡）",
  "doubao-seed-1.6-flash": "（快速，便宜）",
};

/** Vendor presets verified 2026-06 (same table as the legacy UI); the model dropdown
 * always offers a custom escape hatch. */
const PRESETS: Record<string, { base_url: string; models: string[] }> = {
  DeepSeek: { base_url: "https://api.deepseek.com", models: ["deepseek-v4-flash", "deepseek-v4-pro"] },
  OpenAI: {
    base_url: "https://api.openai.com/v1",
    models: ["gpt-5.5", "gpt-5.4-mini", "gpt-5.4-nano", "gpt-5.2", "gpt-5.2-chat-latest"],
  },
  "Anthropic Claude": {
    base_url: "https://api.anthropic.com/v1/",
    models: ["claude-fable-5", "claude-opus-4-8", "claude-sonnet-4-6", "claude-haiku-4-5-20251001"],
  },
  "Moonshot Kimi": { base_url: "https://api.moonshot.cn/v1", models: ["kimi-k2.6", "kimi-k2.5"] },
  "智谱 GLM": {
    base_url: "https://open.bigmodel.cn/api/paas/v4",
    models: ["glm-5.1", "glm-5", "glm-4.7"],
  },
  通义千问: {
    base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1",
    models: ["qwen3.7-max", "qwen3.5-plus", "qwen3.5-flash"],
  },
  "豆包（火山方舟）": {
    base_url: "https://ark.cn-beijing.volces.com/api/v3",
    models: ["doubao-seed-1.8", "doubao-seed-1.6", "doubao-seed-1.6-flash"],
  },
  自定义: { base_url: "", models: [] },
};

const CUSTOM_MODEL = "自定义输入…";

const form = reactive({
  provider: "DeepSeek",
  baseUrl: PRESETS.DeepSeek.base_url,
  apiKey: "",
  modelPick: PRESETS.DeepSeek.models[0],
  modelCustom: "",
});

const status = ref<{ configured: boolean; base_url: string } | null>(null);
const probeResult = ref("");
const probeOk = ref(false);
const saveFlash = ref("");
const busy = ref(false);

function currentModel(): string {
  return form.modelPick === CUSTOM_MODEL ? form.modelCustom.trim() : form.modelPick;
}

function onProviderChange(): void {
  const preset = PRESETS[form.provider];
  form.baseUrl = preset.base_url;
  form.modelPick = preset.models[0] ?? CUSTOM_MODEL;
  form.modelCustom = "";
}

async function refreshStatus(): Promise<void> {
  status.value = await apiGet<{ configured: boolean; base_url: string }>("/settings/connection");
  if (status.value.configured && status.value.base_url && !form.apiKey) {
    form.baseUrl = status.value.base_url;
    const match = Object.entries(PRESETS).find(([, p]) => p.base_url === status.value!.base_url);
    if (match) {
      form.provider = match[0];
      const saved = llmConfig().model;
      form.modelPick = match[1].models.includes(saved) ? saved : (match[1].models[0] ?? CUSTOM_MODEL);
    }
  }
}

async function probe(): Promise<void> {
  if (!currentModel()) return;
  busy.value = true;
  probeResult.value = "";
  try {
    const body = await apiPost<{ ok: boolean; latency_ms?: number; message?: string; category?: string }>(
      "/settings/connection:probe",
      { base_url: form.baseUrl.trim(), api_key: form.apiKey.trim(), model: currentModel() },
    );
    probeOk.value = body.ok;
    probeResult.value = body.ok
      ? `连接成功 · ${Math.round(body.latency_ms ?? 0)}ms`
      : (body.message ?? "连接失败");
  } catch (e) {
    probeOk.value = false;
    probeResult.value = humanizeError(e);
  } finally {
    busy.value = false;
  }
}

async function save(): Promise<void> {
  if (!currentModel()) return;
  busy.value = true;
  saveFlash.value = "";
  try {
    const body = await apiPost<{ configured: boolean }>("/settings/connection", {
      base_url: form.baseUrl.trim(),
      api_key: form.apiKey.trim(),
    });
    setLlmConfig(body.configured, currentModel());
    saveFlash.value = body.configured
      ? `已接入：${currentModel()}。现在可用于创世、人物、问答与清查。`
      : "尚未配置 Key。";
    window.dispatchEvent(new CustomEvent("ow-llm-changed"));
    await refreshStatus();
  } catch (e) {
    saveFlash.value = humanizeError(e);
  } finally {
    busy.value = false;
  }
}

// B11 · pro mode: a calmer, lower-motion workspace for daily high-frequency use
const proMode = ref(localStorage.getItem("owcopilot_pro_mode") === "1");
function togglePro(): void {
  localStorage.setItem("owcopilot_pro_mode", proMode.value ? "1" : "0");
  window.dispatchEvent(new CustomEvent("ow-pro-changed"));
}

onMounted(refreshStatus);
</script>

<template>
  <section>
    <PageHead overline="SETTINGS" title="设置 · 模型" purpose="接入你自己的模型，Key 只留在本机。">
      <template #aside>
        <span class="conn" :class="status?.configured ? 'ok' : 'off'">
          {{ status?.configured ? "已接入" : "未接入" }}
        </span>
      </template>
    </PageHead>
    <div class="pane form">
      <div class="grid">
        <label class="field">
          <span class="label">服务商</span>
          <select v-model="form.provider" @change="onProviderChange">
            <option v-for="(_, name) in PRESETS" :key="name" :value="name">{{ name }}</option>
          </select>
        </label>
        <label class="field">
          <span class="label">Base URL</span>
          <input
            v-model="form.baseUrl"
            :readonly="form.provider !== '自定义'"
            :class="{ readonly: form.provider !== '自定义' }"
            :title="form.provider !== '自定义' ? '预设服务商的地址已自动填写，通常无需修改' : ''"
          />
          <span v-if="form.provider !== '自定义'" class="field-hint">预设服务商的地址已自动填写，通常无需修改</span>
        </label>
        <label class="field">
          <span class="label">API Key</span>
          <input v-model="form.apiKey" type="password" placeholder="留空则沿用服务端已有配置" />
        </label>
        <label class="field">
          <span class="label">模型</span>
          <select v-model="form.modelPick">
            <option v-for="m in PRESETS[form.provider].models" :key="m" :value="m">{{ m }}{{ MODEL_HINTS[m] ? ' ' + MODEL_HINTS[m] : '' }}</option>
            <option :value="CUSTOM_MODEL">{{ CUSTOM_MODEL }}</option>
          </select>
          <input
            v-if="form.modelPick === CUSTOM_MODEL"
            v-model="form.modelCustom"
            placeholder="填入该服务商的模型名称"
          />
        </label>
      </div>
      <div v-if="PROVIDER_GUIDE[form.provider]" class="guide-box">
        <span class="guide-text">
          API Key 是 {{ form.provider }} 给你账号的访问凭证。
          <a :href="PROVIDER_GUIDE[form.provider].url" target="_blank" rel="noopener" class="guide-link">前往 {{ form.provider }} 获取 API Key ↗</a>
        </span>
        <span class="guide-hint">{{ PROVIDER_GUIDE[form.provider].hint }}</span>
      </div>
      <div class="actions">
        <button :disabled="busy || !currentModel()" @click="probe">测试连接</button>
        <button class="primary" :disabled="busy || !currentModel()" @click="save">保存并启用</button>
      </div>
      <p v-if="probeResult" :class="probeOk ? 'ok-text' : 'error'">{{ probeResult }}</p>
      <p v-if="saveFlash" class="flash">{{ saveFlash }}</p>
      <p class="muted small">模型列表以厂商文档为准；选「自定义输入…」可填任意模型名。</p>
    </div>

    <div class="pane form pref">
      <div class="section"><span class="t">界面偏好</span></div>
      <label class="toggle">
        <input v-model="proMode" type="checkbox" @change="togglePro" />
        <span>
          <b>专业模式</b>
          <i class="muted">关闭星盘/极光/跃迁等装饰动效，换一个安静、低干扰的工作界面（适合长时间高频使用）。</i>
        </span>
      </label>
    </div>
  </section>
</template>

<style scoped>
.pref {
  margin-top: 0.8rem;
}
.toggle {
  display: flex;
  align-items: flex-start;
  gap: 0.6rem;
  cursor: pointer;
}
.toggle input {
  margin-top: 0.2rem;
  width: 1.05rem;
  height: 1.05rem;
  accent-color: var(--ow-gold-bright);
}
.toggle span {
  display: flex;
  flex-direction: column;
  gap: 0.15rem;
}
.toggle i {
  font-size: 0.8rem;
  font-style: normal;
}

.hint {
  font-size: 0.85rem;
}

.hint .ok {
  color: #8ed4ac;
}

.hint .off {
  color: #e6c07e;
}

/* connection chip in the page header */
.conn {
  font-size: 0.74rem;
  letter-spacing: 0.06em;
  border-radius: 3px;
  clip-path: polygon(
    var(--ow-chip-nip) 0, 100% 0, 100% calc(100% - var(--ow-chip-nip)),
    calc(100% - var(--ow-chip-nip)) 100%, 0 100%, 0 var(--ow-chip-nip)
  );
  padding: 0.16rem 0.66rem;
  border: 1px solid var(--ow-line);
}
.conn.ok {
  color: #8ed4ac;
  border-color: rgba(142, 212, 172, 0.5);
}
.conn.off {
  color: #e6c07e;
  border-color: rgba(224, 180, 106, 0.45);
}

.form {
  padding: 1.1rem 1.2rem;
  display: flex;
  flex-direction: column;
  gap: 0.9rem;
}

.field {
  display: flex;
  flex-direction: column;
  gap: 0.35rem;
}

.label {
  font-size: 0.82rem;
  color: var(--ow-muted);
}

.field-hint {
  font-size: 0.75rem;
  color: var(--ow-muted);
  opacity: 0.75;
}

input.readonly {
  opacity: 0.65;
  cursor: default;
}

.guide-box {
  border-left: 2px solid var(--ow-gold-soft);
  background: var(--ow-gold-faint);
  padding: 0.6rem 0.8rem;
  border-radius: 0 3px 3px 0;
  display: flex;
  flex-direction: column;
  gap: 0.3rem;
}

.guide-text {
  font-size: 0.83rem;
  color: var(--ow-ink);
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.4rem;
}

.guide-link {
  color: var(--ow-gold-bright);
  text-decoration: none;
  font-weight: 500;
}

.guide-link:hover {
  text-decoration: underline;
}

.guide-hint {
  font-size: 0.78rem;
  color: var(--ow-muted);
  line-height: 1.5;
}

input,
select {
  background: var(--ow-panel-2);
  border: 1px solid var(--ow-line);
  border-radius: var(--ow-control-radius);
  color: var(--ow-ink);
  padding: 0.5rem 0.65rem;
  font: inherit;
  font-size: 0.88rem;
}

input:focus,
select:focus {
  outline: none;
  border-color: var(--ow-gold-soft);
}

.grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
  gap: 0.8rem;
}

.actions {
  display: flex;
  gap: 0.6rem;
}

button {
  border-radius: var(--ow-control-radius);
  cursor: pointer;
  font: inherit;
  font-size: 0.88rem;
  padding: 0.5rem 1rem;
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

button:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}

.ok-text {
  color: #8ed4ac;
}

.error {
  color: #e89a9a;
}

.flash {
  color: #8ed4ac;
}

.small {
  font-size: 0.78rem;
}
</style>
