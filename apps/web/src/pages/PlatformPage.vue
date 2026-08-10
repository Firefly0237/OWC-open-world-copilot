<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import {
  ApiError,
  currentPlatformBearer,
  humanizeError,
  platformApiDelete,
  platformApiGet,
  platformApiPost,
  setPlatformBearer,
} from "../api";
import PageHead from "../components/PageHead.vue";
import { askConfirm } from "../confirm";
import { notifyOk } from "../toast";

type Role = "owner" | "editor" | "reviewer" | "viewer";

interface Principal {
  user_id: string;
  tenant_id: string;
  role: Role;
  is_loopback: boolean;
}
interface Membership {
  tenant_id: string;
  user_id: string;
  email: string;
  role: Role;
}
interface DevToken {
  id: string;
  tenant_id: string;
  user_id: string;
  role: Role;
  issued_at: string;
  expires_at: string;
  issued_by: string;
  revoked_at: string | null;
  revoked_by: string | null;
}
interface AuditEntry {
  at: string;
  tenant_id: string;
  user_id: string;
  action: string;
  target: string;
}
interface CursorPage<T> {
  total: number;
  next_cursor: string | null;
  snapshot_etag: string;
  memberships?: T[];
  tokens?: T[];
  entries?: T[];
}

const ROLE_LABELS: Record<Role, string> = {
  owner: "所有者",
  editor: "编辑",
  reviewer: "审阅者",
  viewer: "只读",
};
const PAGE_SIZE = 30;

const bearer = ref(currentPlatformBearer());
const rememberBearer = ref(Boolean(localStorage.getItem("owcopilot_platform_bearer")));
const principal = ref<Principal | null>(null);
const meBusy = ref(false);
const meError = ref("");
const targetTenant = ref("");

const tenantForm = ref({ tenant_id: "", name: "", owner_email: "" });
const tenantBusy = ref(false);
const tenantError = ref("");

const memberForm = ref<{ user_id: string; email: string; role: Role }>({
  user_id: "",
  email: "",
  role: "editor",
});
const members = ref<Membership[]>([]);
const memberTotal = ref(0);
const memberCursor = ref<string | null>(null);
const membersBusy = ref(false);
const membersMoreBusy = ref(false);
const memberMutationBusy = ref("");
const membersError = ref("");
let membersLoadSerial = 0;

const tokenForm = ref<{ user_id: string; role: Role; ttl_seconds: number }>({
  user_id: "",
  role: "editor",
  ttl_seconds: 3600,
});
const tokens = ref<DevToken[]>([]);
const tokenTotal = ref(0);
const tokenCursor = ref<string | null>(null);
const tokensBusy = ref(false);
const tokensMoreBusy = ref(false);
const tokenMutationBusy = ref("");
const tokensError = ref("");
const issuedToken = ref<{ id: string; token: string } | null>(null);
let tokensLoadSerial = 0;

const audit = ref<AuditEntry[]>([]);
const auditTotal = ref(0);
const auditCursor = ref<string | null>(null);
const auditBusy = ref(false);
const auditMoreBusy = ref(false);
const auditError = ref("");
let auditLoadSerial = 0;

const isOwner = computed(() => principal.value?.role === "owner");
const canCreateTenant = computed(() => Boolean(principal.value?.is_loopback && isOwner.value));
const canManageTenant = computed(() => Boolean(isOwner.value && targetTenant.value.trim()));

function platformError(error: unknown): string {
  if (error instanceof ApiError && error.status === 401) {
    return "平台凭据无效、已过期或已撤销；请换一个令牌，或清空令牌改用服务 API Key。";
  }
  if (error instanceof ApiError && error.status === 403) {
    return `当前角色没有这项权限。${error.detail}`;
  }
  return humanizeError(error);
}

function formatTime(value: string | null): string {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN");
}

function saveBearer(): void {
  setPlatformBearer(bearer.value, rememberBearer.value);
}

async function loadMe(): Promise<void> {
  if (meBusy.value) return;
  meBusy.value = true;
  meError.value = "";
  saveBearer();
  try {
    const body = await platformApiGet<Principal>("/platform/me");
    principal.value = body;
    if (!targetTenant.value || !body.is_loopback) targetTenant.value = body.tenant_id;
    await Promise.all([
      loadAudit(),
      body.role === "owner" ? loadMembers() : Promise.resolve(),
      body.role === "owner" ? loadTokens() : Promise.resolve(),
    ]);
  } catch (error) {
    principal.value = null;
    meError.value = platformError(error);
  } finally {
    meBusy.value = false;
  }
}

async function clearBearer(): Promise<void> {
  bearer.value = "";
  setPlatformBearer("");
  await loadMe();
}

async function createTenant(): Promise<void> {
  const payload = {
    tenant_id: tenantForm.value.tenant_id.trim(),
    name: tenantForm.value.name.trim(),
    owner_email: tenantForm.value.owner_email.trim(),
  };
  if (!payload.tenant_id || !payload.name || !payload.owner_email || tenantBusy.value) return;
  tenantBusy.value = true;
  tenantError.value = "";
  try {
    await platformApiPost("/platform/tenants", payload);
    targetTenant.value = payload.tenant_id;
    tokenForm.value.user_id = `${payload.tenant_id}_owner`;
    tenantForm.value = { tenant_id: "", name: "", owner_email: "" };
    notifyOk("租户已创建，并建立首位所有者成员。可继续签发所有者令牌。");
    await Promise.all([loadMembers(), loadTokens()]);
  } catch (error) {
    tenantError.value = platformError(error);
  } finally {
    tenantBusy.value = false;
  }
}

async function loadMembers(append = false): Promise<void> {
  const tenantId = targetTenant.value.trim();
  const cursor = append ? memberCursor.value : null;
  if (!tenantId || !isOwner.value || (append && (!cursor || membersMoreBusy.value))) return;
  const serial = ++membersLoadSerial;
  if (append) membersMoreBusy.value = true;
  else membersBusy.value = true;
  membersError.value = "";
  const query = new URLSearchParams({ tenant_id: tenantId, limit: String(PAGE_SIZE) });
  if (cursor) query.set("cursor", cursor);
  try {
    const body = await platformApiGet<CursorPage<Membership>>(
      `/platform/memberships?${query.toString()}`,
    );
    if (serial !== membersLoadSerial || targetTenant.value.trim() !== tenantId) return;
    members.value = append
      ? [...members.value, ...(body.memberships ?? [])]
      : body.memberships ?? [];
    memberTotal.value = body.total;
    memberCursor.value = body.next_cursor;
  } catch (error) {
    if (serial !== membersLoadSerial || targetTenant.value.trim() !== tenantId) return;
    if (append && error instanceof ApiError && error.status === 409) {
      membersMoreBusy.value = false;
      await loadMembers();
      return;
    }
    membersError.value = platformError(error);
  } finally {
    if (serial === membersLoadSerial) {
      membersBusy.value = false;
      membersMoreBusy.value = false;
    }
  }
}

async function saveMember(): Promise<void> {
  const tenantId = targetTenant.value.trim();
  const payload = {
    tenant_id: tenantId,
    user_id: memberForm.value.user_id.trim(),
    email: memberForm.value.email.trim(),
    role: memberForm.value.role,
  };
  if (!tenantId || !payload.user_id || !payload.email || memberMutationBusy.value) return;
  memberMutationBusy.value = payload.user_id;
  membersError.value = "";
  try {
    await platformApiPost("/platform/memberships", payload);
    memberForm.value = { user_id: "", email: "", role: "editor" };
    notifyOk("成员及角色已保存。");
    await Promise.all([loadMembers(), loadAudit()]);
  } catch (error) {
    membersError.value = platformError(error);
  } finally {
    memberMutationBusy.value = "";
  }
}

function editMember(member: Membership): void {
  memberForm.value = { user_id: member.user_id, email: member.email, role: member.role };
}

async function removeMember(member: Membership): Promise<void> {
  if (memberMutationBusy.value) return;
  if (!(await askConfirm(`移除成员「${member.user_id}」？`, {
    title: "移除平台成员",
    confirmLabel: "移除成员",
  }))) return;
  memberMutationBusy.value = member.user_id;
  membersError.value = "";
  try {
    await platformApiDelete(
      `/platform/memberships/${encodeURIComponent(member.tenant_id)}/${encodeURIComponent(member.user_id)}`,
    );
    notifyOk("成员已移除。");
    await Promise.all([loadMembers(), loadAudit()]);
  } catch (error) {
    membersError.value = platformError(error);
  } finally {
    memberMutationBusy.value = "";
  }
}

async function loadTokens(append = false): Promise<void> {
  const tenantId = targetTenant.value.trim();
  const cursor = append ? tokenCursor.value : null;
  if (!tenantId || !isOwner.value || (append && (!cursor || tokensMoreBusy.value))) return;
  const serial = ++tokensLoadSerial;
  if (append) tokensMoreBusy.value = true;
  else tokensBusy.value = true;
  tokensError.value = "";
  const query = new URLSearchParams({ tenant_id: tenantId, limit: String(PAGE_SIZE) });
  if (cursor) query.set("cursor", cursor);
  try {
    const body = await platformApiGet<CursorPage<DevToken>>(
      `/platform/auth/dev-tokens?${query.toString()}`,
    );
    if (serial !== tokensLoadSerial || targetTenant.value.trim() !== tenantId) return;
    tokens.value = append ? [...tokens.value, ...(body.tokens ?? [])] : body.tokens ?? [];
    tokenTotal.value = body.total;
    tokenCursor.value = body.next_cursor;
  } catch (error) {
    if (serial !== tokensLoadSerial || targetTenant.value.trim() !== tenantId) return;
    if (append && error instanceof ApiError && error.status === 409) {
      tokensMoreBusy.value = false;
      await loadTokens();
      return;
    }
    tokensError.value = platformError(error);
  } finally {
    if (serial === tokensLoadSerial) {
      tokensBusy.value = false;
      tokensMoreBusy.value = false;
    }
  }
}

async function createToken(): Promise<void> {
  const tenantId = targetTenant.value.trim();
  const userId = tokenForm.value.user_id.trim();
  if (!tenantId || !userId || tokenMutationBusy.value) return;
  tokenMutationBusy.value = "create";
  tokensError.value = "";
  try {
    const body = await platformApiPost<DevToken & { token: string }>(
      "/platform/auth/dev-token",
      {
        tenant_id: tenantId,
        user_id: userId,
        role: tokenForm.value.role,
        ttl_seconds: Math.trunc(Number(tokenForm.value.ttl_seconds)),
      },
    );
    issuedToken.value = { id: body.id, token: body.token };
    notifyOk("开发令牌已签发；原文只在这里显示一次，请妥善保存。");
    await Promise.all([loadTokens(), loadAudit()]);
  } catch (error) {
    tokensError.value = platformError(error);
  } finally {
    tokenMutationBusy.value = "";
  }
}

async function copyIssuedToken(): Promise<void> {
  if (!issuedToken.value) return;
  await navigator.clipboard.writeText(issuedToken.value.token);
  notifyOk("令牌已复制。");
}

async function useIssuedToken(): Promise<void> {
  if (!issuedToken.value) return;
  bearer.value = issuedToken.value.token;
  saveBearer();
  await loadMe();
}

async function revokeToken(token: DevToken): Promise<void> {
  if (token.revoked_at || tokenMutationBusy.value) return;
  if (!(await askConfirm(`撤销「${token.user_id}」的开发令牌？撤销后立即不能再访问平台接口。`, {
    title: "撤销开发令牌",
    confirmLabel: "撤销令牌",
  }))) return;
  tokenMutationBusy.value = token.id;
  tokensError.value = "";
  try {
    await platformApiPost(
      `/platform/auth/dev-tokens/${encodeURIComponent(token.id)}:revoke?tenant_id=${encodeURIComponent(token.tenant_id)}`,
      {},
    );
    if (issuedToken.value?.id === token.id && bearer.value === issuedToken.value.token) {
      bearer.value = "";
      setPlatformBearer("");
    }
    notifyOk("开发令牌已撤销。");
    // Re-resolve the principal too: the revoked row may be the credential currently in use.
    await loadMe();
  } catch (error) {
    tokensError.value = platformError(error);
  } finally {
    tokenMutationBusy.value = "";
  }
}

async function loadAudit(append = false): Promise<void> {
  const tenantId = isOwner.value ? targetTenant.value.trim() : "";
  const cursor = append ? auditCursor.value : null;
  if (append && (!cursor || auditMoreBusy.value)) return;
  const serial = ++auditLoadSerial;
  if (append) auditMoreBusy.value = true;
  else auditBusy.value = true;
  auditError.value = "";
  const query = new URLSearchParams({ limit: String(PAGE_SIZE) });
  if (tenantId) query.set("tenant_id", tenantId);
  if (cursor) query.set("cursor", cursor);
  try {
    const body = await platformApiGet<CursorPage<AuditEntry>>(
      `/platform/audit?${query.toString()}`,
    );
    if (serial !== auditLoadSerial || (tenantId && targetTenant.value.trim() !== tenantId)) return;
    audit.value = append ? [...audit.value, ...(body.entries ?? [])] : body.entries ?? [];
    auditTotal.value = body.total;
    auditCursor.value = body.next_cursor;
  } catch (error) {
    if (serial !== auditLoadSerial || (tenantId && targetTenant.value.trim() !== tenantId)) return;
    if (append && error instanceof ApiError && error.status === 409) {
      auditMoreBusy.value = false;
      await loadAudit();
      return;
    }
    auditError.value = platformError(error);
  } finally {
    if (serial === auditLoadSerial) {
      auditBusy.value = false;
      auditMoreBusy.value = false;
    }
  }
}

function invalidateTenantControl(): void {
  membersLoadSerial += 1;
  tokensLoadSerial += 1;
  auditLoadSerial += 1;
  members.value = [];
  tokens.value = [];
  audit.value = [];
  memberTotal.value = 0;
  tokenTotal.value = 0;
  auditTotal.value = 0;
  memberCursor.value = null;
  tokenCursor.value = null;
  auditCursor.value = null;
  membersError.value = "";
  tokensError.value = "";
  auditError.value = "";
}

async function refreshTenantControl(): Promise<void> {
  invalidateTenantControl();
  await Promise.all([
    loadAudit(),
    isOwner.value && targetTenant.value.trim() ? loadMembers() : Promise.resolve(),
    isOwner.value && targetTenant.value.trim() ? loadTokens() : Promise.resolve(),
  ]);
}

onMounted(loadMe);
</script>

<template>
  <section>
    <PageHead
      overline="PLATFORM"
      title="平台管理"
      purpose="管理租户、成员、开发令牌与租户审计；所有写操作都要求所有者角色。"
    />

    <div class="pane auth-card">
      <div class="section"><span class="t">平台凭据</span></div>
      <p class="muted small">留空时使用服务 API Key / 本机所有者；填写后以 Bearer 令牌身份访问。</p>
      <div class="auth-row">
        <input v-model="bearer" type="password" autocomplete="current-password" placeholder="开发 Bearer 令牌（可留空）" />
        <label class="remember"><input v-model="rememberBearer" type="checkbox" /> 长期记住</label>
        <button class="primary" :disabled="meBusy" @click="loadMe">{{ meBusy ? "验证中…" : "连接 / 验证" }}</button>
        <button v-if="bearer" class="ghost" :disabled="meBusy" @click="clearBearer">清空令牌</button>
      </div>
      <p v-if="meError" class="error" role="alert">{{ meError }}</p>
      <div v-if="principal" class="principal-line">
        <span class="role">{{ ROLE_LABELS[principal.role] }}</span>
        <b>@{{ principal.user_id }}</b>
        <span class="mono">{{ principal.tenant_id }}</span>
        <span v-if="principal.is_loopback" class="local">本机/API Key</span>
      </div>
    </div>

    <template v-if="principal">
      <div v-if="canCreateTenant" class="pane block">
        <div class="section"><span class="t">创建租户</span></div>
        <div class="form-grid tenant-grid">
          <label><span>租户 ID</span><input v-model="tenantForm.tenant_id" maxlength="64" placeholder="studio_a" /></label>
          <label><span>显示名称</span><input v-model="tenantForm.name" maxlength="120" placeholder="Studio A" /></label>
          <label><span>首位所有者邮箱</span><input v-model="tenantForm.owner_email" type="email" placeholder="owner@example.com" /></label>
          <button class="primary" :disabled="tenantBusy || !tenantForm.tenant_id.trim() || !tenantForm.name.trim() || !tenantForm.owner_email.trim()" @click="createTenant">
            {{ tenantBusy ? "创建中…" : "创建租户" }}
          </button>
        </div>
        <p v-if="tenantError" class="error" role="alert">{{ tenantError }}</p>
      </div>

      <div class="pane block">
        <div class="section"><span class="t">管理目标</span></div>
        <div class="target-row">
          <input v-model="targetTenant" maxlength="64" :disabled="!principal.is_loopback" placeholder="租户 ID" @input="invalidateTenantControl" @keydown.enter="refreshTenantControl" />
          <button class="ghost" :disabled="!canManageTenant || membersBusy || tokensBusy || auditBusy" @click="refreshTenantControl">读取租户控制面</button>
        </div>
        <p v-if="!isOwner" class="notice">当前角色为「{{ ROLE_LABELS[principal.role] }}」：可以查看自己的审计范围，但不能管理租户成员或令牌。</p>
      </div>

      <div v-if="canManageTenant" class="two-cols">
        <div class="pane block">
          <div class="section"><span class="t">成员管理</span><span class="count">{{ members.length }} / {{ memberTotal }}</span></div>
          <div class="form-grid">
            <label><span>用户 ID</span><input v-model="memberForm.user_id" maxlength="120" /></label>
            <label><span>邮箱</span><input v-model="memberForm.email" type="email" /></label>
            <label><span>角色</span><select v-model="memberForm.role"><option v-for="(label, role) in ROLE_LABELS" :key="role" :value="role">{{ label }}</option></select></label>
            <button class="primary" :disabled="Boolean(memberMutationBusy) || !memberForm.user_id.trim() || !memberForm.email.trim()" @click="saveMember">保存成员</button>
          </div>
          <p v-if="membersError" class="error" role="alert">{{ membersError }}</p>
          <p v-if="membersBusy" class="muted small">正在读取成员…</p>
          <div v-else class="rows">
            <div v-for="member in members" :key="member.user_id" class="data-row">
              <div><b>{{ member.user_id }}</b><span>{{ member.email }}</span></div>
              <span class="role">{{ ROLE_LABELS[member.role] }}</span>
              <button class="ghost xs" :disabled="Boolean(memberMutationBusy)" @click="editMember(member)">编辑</button>
              <button class="ghost xs danger" :disabled="Boolean(memberMutationBusy)" @click="removeMember(member)">移除</button>
            </div>
          </div>
          <button v-if="memberCursor" class="ghost load-more" :disabled="membersMoreBusy" @click="loadMembers(true)">{{ membersMoreBusy ? "读取中…" : "加载更多成员" }}</button>
        </div>

        <div class="pane block">
          <div class="section"><span class="t">开发令牌</span><span class="count">{{ tokens.length }} / {{ tokenTotal }}</span></div>
          <div class="form-grid">
            <label><span>用户 ID</span><input v-model="tokenForm.user_id" maxlength="120" /></label>
            <label><span>角色</span><select v-model="tokenForm.role"><option v-for="(label, role) in ROLE_LABELS" :key="role" :value="role">{{ label }}</option></select></label>
            <label><span>有效秒数（60–86400）</span><input v-model.number="tokenForm.ttl_seconds" type="number" min="60" max="86400" /></label>
            <button class="primary" :disabled="Boolean(tokenMutationBusy) || !tokenForm.user_id.trim() || tokenForm.ttl_seconds < 60 || tokenForm.ttl_seconds > 86400" @click="createToken">签发令牌</button>
          </div>
          <div v-if="issuedToken" class="issued-token">
            <b>令牌原文（只显示到离开本页）</b>
            <textarea :value="issuedToken.token" readonly rows="3"></textarea>
            <div><button class="ghost xs" @click="copyIssuedToken">复制</button><button class="ghost xs" @click="useIssuedToken">用此令牌连接</button></div>
          </div>
          <p v-if="tokensError" class="error" role="alert">{{ tokensError }}</p>
          <p v-if="tokensBusy" class="muted small">正在读取令牌…</p>
          <div v-else class="rows">
            <div v-for="token in tokens" :key="token.id" class="data-row token-row">
              <div><b class="mono">{{ token.id }}</b><span>@{{ token.user_id }} · {{ ROLE_LABELS[token.role] }}</span><span>到期 {{ formatTime(token.expires_at) }}</span></div>
              <span :class="token.revoked_at ? 'revoked' : 'active'">{{ token.revoked_at ? "已撤销" : "有效" }}</span>
              <button v-if="!token.revoked_at" class="ghost xs danger" :disabled="Boolean(tokenMutationBusy)" @click="revokeToken(token)">撤销</button>
            </div>
          </div>
          <button v-if="tokenCursor" class="ghost load-more" :disabled="tokensMoreBusy" @click="loadTokens(true)">{{ tokensMoreBusy ? "读取中…" : "加载更多令牌" }}</button>
        </div>
      </div>

      <div class="pane block">
        <div class="section"><span class="t">租户审计</span><span class="count">{{ audit.length }} / {{ auditTotal }}</span></div>
        <p class="muted small">审计严格按当前身份或本机所有者所选目标租户隔离，最新记录在前。</p>
        <p v-if="auditError" class="error" role="alert">{{ auditError }}</p>
        <p v-if="auditBusy" class="muted small">正在读取审计…</p>
        <div v-else class="audit-table">
          <div v-for="(entry, index) in audit" :key="`${entry.at}-${entry.action}-${index}`" class="audit-row">
            <time :datetime="entry.at">{{ formatTime(entry.at) }}</time>
            <b>{{ entry.action }}</b>
            <span>@{{ entry.user_id }}</span>
            <span class="mono">{{ entry.target || "—" }}</span>
          </div>
          <p v-if="!audit.length" class="muted small">当前租户还没有审计记录。</p>
        </div>
        <button v-if="auditCursor" class="ghost load-more" :disabled="auditMoreBusy" @click="loadAudit(true)">{{ auditMoreBusy ? "读取中…" : "加载更早审计" }}</button>
      </div>
    </template>
  </section>
</template>

<style scoped>
.auth-card, .block { padding: 0.9rem; margin-bottom: 0.8rem; }
.section { display: flex; align-items: center; gap: 0.6rem; }
.count { margin-left: auto; color: var(--ow-muted); font: 0.72rem ui-monospace, Consolas, monospace; }
.small { margin: 0.35rem 0; font-size: 0.76rem; }
.auth-row, .target-row { display: flex; align-items: center; gap: 0.45rem; flex-wrap: wrap; }
.auth-row > input, .target-row > input { flex: 1; min-width: min(100%, 18rem); }
.remember { display: flex; align-items: center; gap: 0.3rem; color: var(--ow-muted); font-size: 0.76rem; }
.principal-line { display: flex; align-items: center; gap: 0.55rem; margin-top: 0.6rem; flex-wrap: wrap; }
.role, .local, .active, .revoked { padding: 0.12rem 0.45rem; border: 1px solid var(--ow-line); color: var(--ow-cyan); font-size: 0.7rem; }
.local, .active { color: #8ed4ac; border-color: rgba(142, 212, 172, 0.4); }
.revoked { color: #e89a9a; border-color: rgba(224, 133, 133, 0.45); }
.form-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 0.55rem; }
.tenant-grid { grid-template-columns: repeat(3, minmax(0, 1fr)) auto; align-items: end; }
.form-grid label { display: flex; flex-direction: column; gap: 0.25rem; }
.form-grid label span { color: var(--ow-muted); font-size: 0.72rem; }
input, select, textarea {
  min-height: 38px; padding: 0.45rem 0.58rem; border: 1px solid var(--ow-line);
  border-radius: var(--ow-control-radius); color: var(--ow-ink); background: var(--ow-panel-2); font: inherit;
}
textarea { width: 100%; resize: vertical; font: 0.72rem/1.45 ui-monospace, Consolas, monospace; }
button { padding: 0.42rem 0.75rem; border: 1px solid var(--ow-line); border-radius: var(--ow-control-radius); color: var(--ow-ink); background: var(--ow-panel-2); cursor: pointer; font: inherit; }
button.primary { border-color: var(--ow-gold-soft); color: #241a05; background: linear-gradient(180deg, #f0d28a, #b9924a); font-weight: 600; }
button:disabled { opacity: 0.5; cursor: not-allowed; }
.xs { padding: 0.2rem 0.45rem; font-size: 0.7rem; }
.danger { color: #e89a9a; }
.error, .notice { margin: 0.5rem 0 0; padding: 0.45rem 0.6rem; border-left: 2px solid #e89a9a; color: #e89a9a; font-size: 0.76rem; }
.notice { color: var(--ow-gold-bright); border-color: var(--ow-gold-soft); }
.two-cols { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 0.8rem; }
.rows { display: flex; flex-direction: column; gap: 0.35rem; margin-top: 0.65rem; }
.data-row { display: flex; align-items: center; gap: 0.45rem; padding: 0.45rem; border: 1px solid var(--ow-line); background: rgba(12, 18, 37, 0.35); }
.data-row > div { min-width: 0; display: flex; flex: 1; flex-direction: column; gap: 0.1rem; }
.data-row > div span { color: var(--ow-muted); font-size: 0.7rem; overflow-wrap: anywhere; }
.issued-token { display: flex; flex-direction: column; gap: 0.35rem; margin-top: 0.65rem; padding: 0.55rem; border: 1px solid var(--ow-gold-soft); background: var(--ow-gold-faint); }
.issued-token > div { display: flex; gap: 0.35rem; }
.load-more { width: 100%; margin-top: 0.55rem; }
.audit-table { overflow-x: auto; }
.audit-row { display: grid; grid-template-columns: minmax(10rem, max-content) minmax(8rem, 1fr) minmax(6rem, max-content) minmax(8rem, 1fr); gap: 0.6rem; padding: 0.42rem 0; border-bottom: 1px solid var(--ow-line); font-size: 0.75rem; }
.audit-row time, .audit-row span { color: var(--ow-muted); }
.mono { font-family: ui-monospace, Consolas, monospace; overflow-wrap: anywhere; }
@media (max-width: 900px) {
  .two-cols, .tenant-grid { grid-template-columns: 1fr; }
}
@media (max-width: 600px) {
  .form-grid { grid-template-columns: 1fr; }
  .auth-row > *, .target-row > * { width: 100%; min-height: 44px; }
  .data-row { align-items: flex-start; flex-wrap: wrap; }
  .audit-row { grid-template-columns: 1fr; gap: 0.15rem; }
}
</style>
