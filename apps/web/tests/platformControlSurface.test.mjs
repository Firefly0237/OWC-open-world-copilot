import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import ts from "typescript";

const [page, api, router, app, help] = await Promise.all([
  readFile(new URL("../src/pages/PlatformPage.vue", import.meta.url), "utf8"),
  readFile(new URL("../src/api.ts", import.meta.url), "utf8"),
  readFile(new URL("../src/router.ts", import.meta.url), "utf8"),
  readFile(new URL("../src/App.vue", import.meta.url), "utf8"),
  readFile(new URL("../src/pageHelp.ts", import.meta.url), "utf8"),
]);

function extractFunction(source, name) {
  const match = new RegExp(`(?:async\\s+)?function\\s+${name}(?:<[^>]+>)?\\s*\\(`).exec(source);
  assert.ok(match, `${name} must be defined`);
  const paramsOpen = source.indexOf("(", match.index);
  let paramsDepth = 0;
  let paramsClose = -1;
  for (let index = paramsOpen; index < source.length; index += 1) {
    if (source[index] === "(") paramsDepth += 1;
    if (source[index] === ")") paramsDepth -= 1;
    if (paramsDepth === 0) {
      paramsClose = index;
      break;
    }
  }
  assert.notEqual(paramsClose, -1, `${name} parameters must close`);
  const open = source.indexOf("{", paramsClose);
  let depth = 0;
  for (let index = open; index < source.length; index += 1) {
    if (source[index] === "{") depth += 1;
    if (source[index] === "}") depth -= 1;
    if (depth === 0) return source.slice(match.index, index + 1);
  }
  throw new Error(`Could not find the end of ${name}`);
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

test("platform control plane is routed, locally discoverable, and role gated", () => {
  assert.match(router, /path:\s*"\/platform"[\s\S]*PlatformPage\.vue/);
  assert.match(help, /"\/platform":\s*\{/);

  const localNav = app.slice(app.indexOf("const LOCAL_NAV"), app.indexOf("const TEAM_NAV"));
  const teamNav = app.slice(app.indexOf("const TEAM_NAV"), app.indexOf("const profile"));
  assert.match(localNav, /to:\s*"\/platform"/);
  assert.doesNotMatch(teamNav, /to:\s*"\/platform"/);
  assert.match(app, /\["\/worlds", "\/platform", "\/settings"\]/);

  assert.match(page, /const isOwner = computed\(\(\) => principal\.value\?\.role === "owner"\)/);
  assert.match(page, /const canCreateTenant = computed[\s\S]*is_loopback/);
  assert.match(page, /v-if="canManageTenant"/);
  assert.match(page, /v-if="!isOwner"/);
});

test("platform page covers identity, tenant, members, tokens, revocation, and bounded audit", () => {
  const expectedPaths = [
    "/platform/me",
    "/platform/tenants",
    "/platform/memberships",
    "/platform/auth/dev-token",
    "/platform/auth/dev-tokens",
    "/platform/audit",
  ];
  for (const path of expectedPaths) assert.ok(page.includes(path), `missing ${path}`);
  assert.match(page, /dev-tokens\/\$\{encodeURIComponent\(token\.id\)\}:revoke/);

  for (const name of ["loadMembers", "loadTokens", "loadAudit"]) {
    const body = extractFunction(page, name);
    assert.match(body, /limit:\s*String\(PAGE_SIZE\)/);
    assert.match(body, /query\.set\("cursor", cursor\)/);
    assert.match(body, /error instanceof ApiError && error\.status === 409/);
  }
  assert.match(extractFunction(page, "loadAudit"), /query\.set\("tenant_id", tenantId\)/);
  assert.match(page, /:disabled="meBusy"/);
  assert.match(page, /:disabled="Boolean\(memberMutationBusy\)"/);
  assert.match(page, /:disabled="Boolean\(tokenMutationBusy\)"/);
  assert.match(page, /issuedToken\.value = \{ id: body\.id, token: body\.token \}/);
  assert.match(page, /@input="invalidateTenantControl"/);
  assert.match(extractFunction(page, "revokeToken"), /await loadMe\(\)/);
});

test("platform requests combine optional bearer identity with the service API-key fallback", () => {
  const headers = extractFunction(api, "platformAuthHeaders");
  assert.match(headers, /const headers = authHeaders\(\)/);
  assert.match(headers, /currentPlatformBearer\(\)\.trim\(\)/);
  assert.match(headers, /headers\.Authorization = `Bearer \$\{token\}`/);
  assert.match(extractFunction(api, "platformApiGet"), /platformAuthHeaders\(\)/);
  assert.match(extractFunction(api, "platformApiPost"), /platformAuthHeaders\(\)/);
  assert.match(extractFunction(api, "platformApiDelete"), /platformAuthHeaders\(\)/);
});

let harnessSequence = 0;

async function loadAuditHarness(runtime) {
  globalThis.__owPlatformAuditRuntime = runtime;
  harnessSequence += 1;
  const typescript = `
    const runtime = globalThis.__owPlatformAuditRuntime;
    const ref = (value) => ({ value });
    class ApiError extends Error {
      constructor(status, detail) { super(detail); this.status = status; }
    }
    const PAGE_SIZE = 30;
    const audit = ref([{ action: "existing" }]);
    const auditTotal = ref(2);
    const auditCursor = ref("old cursor");
    const auditBusy = ref(false);
    const auditMoreBusy = ref(false);
    const auditError = ref("");
    const isOwner = ref(false);
    const targetTenant = ref("");
    let auditLoadSerial = 0;
    const platformApiGet = (path) => runtime.apiGet(path);
    const platformError = (error) => String(error);

    ${extractFunction(page, "loadAudit")}

    export {
      ApiError,
      loadAudit,
      audit,
      auditTotal,
      auditCursor,
      auditBusy,
      auditMoreBusy,
      auditError,
    };
    // isolated harness ${harnessSequence}
  `;
  const javascript = ts.transpileModule(typescript, {
    compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  return import(`data:text/javascript,${encodeURIComponent(javascript)}`);
}

function auditRuntime(...responses) {
  return {
    responses: [...responses],
    requests: [],
    apiGet(path) {
      this.requests.push(path);
      const response = this.responses.shift();
      assert.ok(response, `unexpected GET: ${path}`);
      return response.promise;
    },
  };
}

test("a fresh audit page fences an older continuation that resolves last", async () => {
  const oldPage = deferred();
  const freshPage = deferred();
  const runtime = auditRuntime(oldPage, freshPage);
  const platform = await loadAuditHarness(runtime);

  const loadingOld = platform.loadAudit(true);
  const refreshing = platform.loadAudit();
  freshPage.resolve({
    entries: [{ action: "fresh" }],
    total: 1,
    next_cursor: null,
    snapshot_etag: "fresh-snapshot",
  });
  await refreshing;
  oldPage.resolve({
    entries: [{ action: "stale" }],
    total: 2,
    next_cursor: null,
    snapshot_etag: "stale-snapshot",
  });
  await loadingOld;

  assert.deepEqual(platform.audit.value.map((entry) => entry.action), ["fresh"]);
  assert.equal(platform.auditTotal.value, 1);
  assert.equal(platform.auditCursor.value, null);
  assert.equal(platform.auditBusy.value, false);
  assert.equal(platform.auditMoreBusy.value, false);
  assert.match(runtime.requests[0], /limit=30&cursor=old\+cursor$/);
  assert.match(runtime.requests[1], /limit=30$/);
});

test("a stale audit cursor silently restarts from page one", async () => {
  const stalePage = deferred();
  const freshPage = deferred();
  const runtime = auditRuntime(stalePage, freshPage);
  const platform = await loadAuditHarness(runtime);

  const loading = platform.loadAudit(true);
  stalePage.reject(new platform.ApiError(409, "snapshot stale"));
  freshPage.resolve({
    entries: [{ action: "newest" }],
    total: 1,
    next_cursor: null,
    snapshot_etag: "new-snapshot",
  });
  await loading;

  assert.deepEqual(platform.audit.value.map((entry) => entry.action), ["newest"]);
  assert.equal(platform.auditError.value, "");
  assert.deepEqual(runtime.requests, [
    "/platform/audit?limit=30&cursor=old+cursor",
    "/platform/audit?limit=30",
  ]);
});
