<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from "vue";
import { humanizeError } from "../api";
import type { EvolutionVersion } from "../evolutionStudioApi";
import { storyCreatorClient, type MachineRequirements, type MethodTrialScope } from "../storyCreatorApi";
import type { StudioTask } from "../studioApi";

const props = defineProps<{
  project: string;
  task: StudioTask | null;
  version: EvolutionVersion | null;
  familyHash: string;
  requirements: MachineRequirements;
  disabled: boolean;
  executionActive?: boolean;
}>();
type Selection = { version_id: string; scope_id: string; permit_hash: string } | null;
const emit = defineEmits<{ selection: [value: Selection]; changed: [] }>();
const scopes = ref<MethodTrialScope[]>([]);
const reading = ref(false);
const working = ref(false);
const error = ref("");
const notice = ref("");
const permissionUncertain = ref(false);
let generation = 0;
let readSequence = 0;
let alive = true;
let grantOperationId = "";

function stable(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(stable);
  if (value && typeof value === "object") {
    return Object.fromEntries(Object.entries(value as Record<string, unknown>)
      .filter(([key, item]) => key !== "schema_version" && item != null)
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([key, item]) => [key, stable(item)]));
  }
  return value;
}
function requirementKey(value: MachineRequirements | undefined): string {
  return JSON.stringify(stable({
    participant_ids: [], game_state_variables: [], business_roles: [], business_relations: [],
    ...value,
  }));
}
const requirementsKey = computed(() => requirementKey(props.requirements));
const contextKey = computed(() => JSON.stringify([
  props.project, props.task?.id, props.task?.revision,
  props.version?.id, props.version?.version_hash, requirementsKey.value,
]));
const actorKind = computed(() => props.task?.origin_kind === "development"
  ? "development_operator" as const : "author" as const);
const matching = computed(() => scopes.value.find((scope) => (
  scope.status === "active" && props.task != null && props.version != null
  && scope.permit.task_id === props.task.id
  && scope.permit.task_revision === props.task.revision
  && scope.permit.allowed_versions[props.version.id] === props.version.version_hash
  && requirementKey(scope.permit.requirements) === requirementsKey.value
)) ?? null);
const usable = computed(() => !permissionUncertain.value && props.version?.state.trial_status === "allowed"
  && props.version.state.author_acceptance !== "rejected" ? matching.value : null);
const permittedVersions = computed(() => matching.value
  ? Object.keys(matching.value.permit.allowed_versions) : []);
const unavailable = computed(() => props.disabled || props.executionActive || working.value || reading.value
  || permissionUncertain.value
  || !props.task || !props.version || !props.familyHash
  || props.version.state.author_acceptance === "rejected");
const authorityLabel = computed(() => {
  const labels: Record<string, string> = {
    explicit_author_action: "本次明确许可",
    development_technical_trial: "开发记录中的试用许可",
    preauthorized_policy: "按任务约定许可",
  };
  return matching.value ? labels[matching.value.permit.authority] ?? "已记录的许可" : "";
});

watch(usable, (scope, previous) => {
  if (!scope || scope.permit.id !== previous?.permit.id
    || scope.permit.permit_hash !== previous?.permit.permit_hash) emit("selection", null);
}, { immediate: true });

async function readScopes(): Promise<void> {
  const stamp = generation;
  const sequence = ++readSequence;
  const taskId = props.task?.id;
  const project = props.project;
  if (!taskId) { scopes.value = []; return; }
  reading.value = true;
  error.value = "";
  try {
    const response = await storyCreatorClient(project).trialScopes(taskId);
    if (alive && stamp === generation && sequence === readSequence) {
      scopes.value = response.scopes;
      permissionUncertain.value = false;
      if (matching.value) grantOperationId = "";
    }
  } catch (cause) {
    if (alive && stamp === generation && sequence === readSequence) {
      scopes.value = [];
      error.value = humanizeError(cause);
    }
  } finally {
    if (alive && stamp === generation && sequence === readSequence) reading.value = false;
  }
}

async function grant(): Promise<void> {
  if (unavailable.value || !props.task || !props.version) return;
  const stamp = generation;
  const task = props.task;
  const version = props.version;
  const project = props.project;
  const familyHash = props.familyHash;
  const actor = actorKind.value;
  const requirements = JSON.parse(JSON.stringify(props.requirements)) as MachineRequirements;
  const client = storyCreatorClient(project);
  grantOperationId ||= crypto.randomUUID();
  const operationId = grantOperationId;
  working.value = true; error.value = ""; notice.value = "";
  try {
    if (version.state.trial_status !== "allowed") {
      await client.allowTrial(version.id, familyHash, true, actor);
      emit("changed");
      if (!alive || stamp !== generation) return;
    }
    const result = await client.grantTaskTrial(task.id, {
      expected_revision: task.revision, version_ids: [version.id],
      machine_requirements: requirements, operation_id: operationId,
      reason: "仅在当前任务中试用此流程", actor_kind: actor,
    });
    if (!alive || stamp !== generation) return;
    scopes.value = [result, ...scopes.value.filter((row) => row.permit.id !== result.permit.id)];
    notice.value = result.status === "active" ? "已许可本任务试用" : "此前许可已撤回";
    grantOperationId = "";
    emit("changed");
  } catch (cause) {
    if (alive && stamp === generation) {
      error.value = humanizeError(cause);
      permissionUncertain.value = true;
      emit("changed");
    }
  } finally {
    if (alive && stamp === generation) working.value = false;
  }
}

async function revoke(): Promise<void> {
  const scope = matching.value;
  if (props.disabled || working.value || reading.value || !scope) return;
  const stamp = generation;
  const client = storyCreatorClient(props.project);
  working.value = true; error.value = ""; notice.value = "";
  permissionUncertain.value = true;
  try {
    const result = await client.revokeTaskTrial(
      scope.permit.id, scope.permit.permit_hash, "撤回本任务的流程试用许可",
    );
    if (!alive || stamp !== generation) return;
    scopes.value = scopes.value.map((row) => row.permit.id === result.permit.id ? result : row);
    permissionUncertain.value = false; grantOperationId = "";
    emit("selection", null); emit("changed");
    notice.value = "本任务的试用许可已撤回";
  } catch (cause) {
    if (alive && stamp === generation) error.value = humanizeError(cause);
  } finally {
    if (alive && stamp === generation) working.value = false;
  }
}

function selectCurrent(): void {
  const scope = usable.value;
  if (scope && props.version && !unavailable.value) emit("selection", {
    version_id: props.version.id, scope_id: scope.permit.id, permit_hash: scope.permit.permit_hash,
  });
}

watch(contextKey, () => {
  generation += 1; readSequence += 1; grantOperationId = "";
  scopes.value = []; error.value = ""; notice.value = ""; permissionUncertain.value = false;
  working.value = false; reading.value = false;
  emit("selection", null);
  void readScopes();
}, { immediate: true });
onBeforeUnmount(() => { alive = false; generation += 1; });
</script>

<template>
  <section v-if="task && version" class="method-trial" aria-label="当前任务的流程试用">
    <header><h4>本任务试用 · 版本 {{ version.version }}</h4><span>{{ reading ? '读取许可中' : permissionUncertain ? '许可状态待刷新' : usable ? '已许可' : matching ? '版本试用已停用' : '未许可' }}</span></header>
    <p>范围：当前故事 v{{ task.revision }}，按当前草稿要求使用此流程。</p>
    <p v-if="matching" class="hint">{{ authorityLabel }} · 试用不会设为后续默认。</p>
    <p v-if="permittedVersions.length > 1" class="hint">这份许可包含{{ permittedVersions.length }}个版本，撤回会一并停用。</p>
    <div class="actions">
      <button v-if="!usable" :disabled="unavailable" @click="grant">{{ working ? '保存许可中…' : '许可本任务试用' }}</button>
      <button v-else :disabled="unavailable" @click="selectCurrent">用于本任务</button>
      <button v-if="matching" :disabled="disabled || working || reading" @click="revoke">撤回本任务许可</button>
      <button :disabled="working || reading" @click="readScopes">刷新许可</button>
    </div>
    <p v-if="notice" role="status">{{ notice }}</p>
    <p v-if="error" role="alert" class="error">{{ error }}</p>
    <details v-if="matching"><summary>许可详情</summary><dl>
      <dt>任务</dt><dd>{{ matching.permit.task_id }} · v{{ matching.permit.task_revision }}</dd>
      <dt>流程版本</dt><dd>{{ version.id }}</dd>
      <dt>许可包含的版本</dt><dd>{{ permittedVersions.join('、') }}</dd>
      <dt>许可</dt><dd>{{ matching.permit.id }}</dd>
      <dt>用途</dt><dd>{{ matching.permit.reason }}</dd>
    </dl></details>
  </section>
</template>

<style scoped>
.method-trial { display: grid; gap: .55rem; padding: .9rem; border: 1px solid var(--ow-paper-edge); border-radius: 7px; }
header, .actions { display: flex; flex-wrap: wrap; align-items: center; gap: .6rem; }
header { justify-content: space-between; } h4, p { margin: 0; }
h4 { font-size: .9rem; } p, summary, header > span { font-size: .8rem; line-height: 1.65; }
.hint { color: var(--ow-ink-mute); } .error { color: var(--ow-red, #f2a0a0); }
button { padding: .4rem .65rem; color: inherit; font: inherit; font-size: .8rem; border: 1px solid var(--ow-paper-edge); border-radius: 5px; background: transparent; cursor: pointer; }
button:disabled { opacity: .45; cursor: default; } summary { cursor: pointer; }
dl { display: grid; grid-template-columns: auto 1fr; gap: .4rem .7rem; font-size: .75rem; }
dd { margin: 0; overflow-wrap: anywhere; }
</style>
