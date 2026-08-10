/**
 * Project-scoped async job channels.
 *
 * Channels survive route changes in memory and hard refreshes in sessionStorage. A page can call
 * `resumeJob` with the same event/result adapters it uses for `startJob`; the server replays its
 * buffered SSE events, so an interrupted browser session reconnects without starting duplicate
 * generation work.
 */
import { reactive } from "vue";
import {
  ApiError,
  addSessionCost,
  apiGet,
  apiPost,
  costOf,
  currentProject,
  streamJobEvents,
  type JobEvent,
} from "./api";
import { notifyError } from "./toast";

export interface Stage {
  key: string;
  label: string;
}

export interface JobChannel<R = unknown> {
  /** Project captured when the channel was created; jobs must not drift after project switching. */
  project: string;
  jobId: string;
  running: boolean;
  /** Has this channel ever started a run, including a restored run. */
  started: boolean;
  /** A browser refresh interrupted observation, while the server job may still be available. */
  recoverable: boolean;
  stageIndex: number;
  elapsed: number;
  stages: Stage[];
  hint: string;
  result: R | null;
  cost: number;
  /** Job whose terminal cost has already been added to the session total. */
  costAccountedJobId: string;
}

interface PersistedChannel<R = unknown> {
  project: string;
  jobId: string;
  running: boolean;
  recoverable: boolean;
  started: boolean;
  stageIndex: number;
  elapsed: number;
  startedAt: number;
  hint: string;
  result: R | null;
  cost: number;
  /** Optional for channels persisted before recovery-safe cost accounting was introduced. */
  costAccountedJobId?: string;
}

const channels = reactive<Record<string, JobChannel>>({});
const timers: Record<string, number> = {};
const startedAt: Record<string, number> = {};
const STORAGE_PREFIX = "owcopilot_job_v1";

function scopedKey(key: string, project = currentProject()): string {
  return `${project}::${key}`;
}

function storageKey(key: string, project = currentProject()): string {
  return `${STORAGE_PREFIX}:${encodeURIComponent(project)}:${encodeURIComponent(key)}`;
}

function readPersisted<R>(key: string, project: string): PersistedChannel<R> | null {
  if (typeof sessionStorage === "undefined") return null;
  try {
    const raw = sessionStorage.getItem(storageKey(key, project));
    if (!raw) return null;
    const parsed = JSON.parse(raw) as PersistedChannel<R>;
    return parsed.project === project ? parsed : null;
  } catch {
    sessionStorage.removeItem(storageKey(key, project));
    return null;
  }
}

function persist<R>(key: string, ch: JobChannel<R>): void {
  if (typeof sessionStorage === "undefined") return;
  const scoped = scopedKey(key, ch.project);
  const value: PersistedChannel<R> = {
    project: ch.project,
    jobId: ch.jobId,
    running: ch.running,
    recoverable: ch.recoverable,
    started: ch.started,
    stageIndex: ch.stageIndex,
    elapsed: ch.elapsed,
    startedAt: startedAt[scoped] ?? 0,
    hint: ch.hint,
    result: ch.result,
    cost: ch.cost,
    costAccountedJobId: ch.costAccountedJobId,
  };
  try {
    sessionStorage.setItem(storageKey(key, ch.project), JSON.stringify(value));
  } catch {
    // A very large generated result may exceed browser quota. Drop only the display result and
    // deliberately mark its job recoverable: the next page load can fetch the terminal payload
    // again instead of silently presenting an empty, non-resumable channel.
    try {
      const recoverable = Boolean(
        value.jobId && (value.running || value.recoverable || value.result !== null),
      );
      sessionStorage.setItem(
        storageKey(key, ch.project),
        JSON.stringify({
          ...value,
          recoverable,
          result: null,
          hint: recoverable ? "结果较大，刷新后将从服务端恢复。" : value.hint,
        }),
      );
    } catch {
      // Storage is an enhancement; the active in-memory job remains fully usable.
    }
  }
}

function startElapsedTimer<R>(key: string, ch: JobChannel<R>): void {
  const scoped = scopedKey(key, ch.project);
  if (timers[scoped]) window.clearInterval(timers[scoped]);
  if (!startedAt[scoped]) startedAt[scoped] = Date.now() - ch.elapsed * 1000;
  ch.elapsed = Math.max(ch.elapsed, Math.floor((Date.now() - startedAt[scoped]) / 1000));
  timers[scoped] = window.setInterval(() => {
    ch.elapsed = Math.floor((Date.now() - startedAt[scoped]) / 1000);
  }, 1000);
}

function stopElapsedTimer<R>(key: string, ch: JobChannel<R>): void {
  const scoped = scopedKey(key, ch.project);
  if (timers[scoped]) {
    window.clearInterval(timers[scoped]);
    delete timers[scoped];
  }
  delete startedAt[scoped];
}

export function getJobChannel<R = unknown>(key: string, stages: Stage[]): JobChannel<R> {
  const project = currentProject();
  const scoped = scopedKey(key, project);
  if (!channels[scoped]) {
    const saved = readPersisted<R>(key, project);
    const elapsed = saved?.running && saved.startedAt
      ? Math.max(saved.elapsed, Math.floor((Date.now() - saved.startedAt) / 1000))
      : (saved?.elapsed ?? 0);
    channels[scoped] = {
      project,
      jobId: saved?.jobId ?? "",
      // Observation is not running until `resumeJob` reconnects it.
      running: false,
      started: saved?.started ?? false,
      recoverable: Boolean((saved?.running || saved?.recoverable) && saved.jobId && !saved.result),
      stageIndex: saved?.stageIndex ?? -1,
      elapsed,
      stages,
      hint: saved?.running || saved?.recoverable
        ? "检测到仍在服务端运行的任务，正在恢复…"
        : (saved?.hint ?? ""),
      result: saved?.result ?? null,
      cost: saved?.cost ?? 0,
      // Older persisted channels wrote `cost` immediately before adding it to the session total.
      // Treat a positive legacy cost as accounted so a quota-recovery fetch cannot charge it twice.
      costAccountedJobId:
        saved?.costAccountedJobId
        ?? ((saved?.cost ?? 0) > 0 && saved?.jobId ? saved.jobId : ""),
    };
    if (saved?.running && saved.startedAt) startedAt[scoped] = saved.startedAt;
  } else {
    channels[scoped].stages = stages;
  }
  return channels[scoped] as JobChannel<R>;
}

export interface ResumeOptions<R> {
  stages: Stage[];
  /** Page-specific live event handling (stage progression, chunk/judge hints). */
  onEvent?: (ch: JobChannel<R>, event: JobEvent) => void;
  /** Turn the terminal job result into the page's display shape. */
  parseResult: (raw: Record<string, unknown>) => R;
}

export interface StartOptions<R> extends ResumeOptions<R> {
  kind: string;
  params: Record<string, unknown>;
}

function isMissingJobError(error: unknown): boolean {
  return error instanceof ApiError && (error.status === 404 || error.status === 410);
}

function clearMissingJob<R>(ch: JobChannel<R>): void {
  ch.jobId = "";
  ch.running = false;
  ch.started = false;
  ch.recoverable = false;
  ch.stageIndex = -1;
  ch.elapsed = 0;
  ch.result = null;
  ch.cost = 0;
  ch.costAccountedJobId = "";
  ch.hint = "上次任务已过期或服务已重启，请重新开始。";
}

async function consumeJob<R>(key: string, ch: JobChannel<R>, opts: ResumeOptions<R>): Promise<void> {
  const jobId = ch.jobId;
  if (!jobId || ch.running) return;
  ch.running = true;
  ch.started = true;
  ch.recoverable = false;
  startElapsedTimer(key, ch);
  persist(key, ch);

  let failed = false;
  let terminal = false;
  try {
    await streamJobEvents(jobId, (event) => {
      if (event.type === "failed") {
        failed = true;
        terminal = true;
        notifyError(event.data.error ?? "任务失败");
      } else {
        opts.onEvent?.(ch, event);
      }
      persist(key, ch);
    });
    const status = await apiGet<{
      status: string;
      result: Record<string, unknown> | null;
      error: string | null;
    }>(`/jobs/${jobId}`);
    terminal = ["done", "failed", "cancelled"].includes(status.status);
    if (status.status === "done" && status.result) {
      const parsed = opts.parseResult(status.result);
      const used = costOf(status.result as { cost_budget?: { used_usd?: number } });
      if (ch.costAccountedJobId !== jobId) {
        addSessionCost(used);
        ch.costAccountedJobId = jobId;
      }
      ch.stageIndex = opts.stages.length - 1;
      ch.cost = used;
      ch.result = parsed;
    } else if (!failed) {
      notifyError(status.error ?? "任务未完成");
    }
  } catch (error) {
    if (isMissingJobError(error)) {
      // Jobs live in the service process and may disappear after a restart or bounded-history prune.
      // A 404/410 is terminal for this id, not a network interruption to retry forever.
      terminal = true;
      clearMissingJob(ch);
      notifyError("上次任务已过期或服务已重启，请重新开始。");
    } else {
      // Network loss does not cancel server work; preserve the id for a later reconnect.
      notifyError(error);
    }
  } finally {
    ch.running = false;
    ch.recoverable = Boolean(ch.jobId && !ch.result && !terminal);
    if (ch.recoverable) ch.hint = "连接已中断，可刷新本页或稍后回来继续恢复。";
    stopElapsedTimer(key, ch);
    persist(key, ch);
  }
}

export async function resumeJob<R>(key: string, opts: ResumeOptions<R>): Promise<void> {
  const ch = getJobChannel<R>(key, opts.stages);
  if (!ch.recoverable || ch.running || !ch.jobId) return;
  await consumeJob(key, ch, opts);
}

export async function startJob<R>(key: string, opts: StartOptions<R>): Promise<void> {
  const ch = getJobChannel<R>(key, opts.stages);
  if (ch.running) return;
  if (ch.recoverable && ch.jobId) {
    // Never overwrite the only handle to server work that may still be running. A user's next
    // click doubles as an explicit reconnect attempt; only a later click, after a confirmed 404/410
    // cleared the stale id, may create a replacement job.
    ch.hint = "检测到未完成任务，正在恢复；没有启动重复任务。";
    persist(key, ch);
    await consumeJob(key, ch, opts);
    return;
  }

  ch.jobId = "";
  ch.running = true;
  ch.started = true;
  ch.recoverable = false;
  ch.stageIndex = 0;
  ch.elapsed = 0;
  ch.hint = "";
  ch.result = null;
  ch.cost = 0;
  ch.costAccountedJobId = "";
  const scoped = scopedKey(key, ch.project);
  startedAt[scoped] = Date.now();
  startElapsedTimer(key, ch);
  persist(key, ch);

  try {
    const job = await apiPost<{ job_id: string }>(`/projects/${ch.project}/jobs`, {
      kind: opts.kind,
      params: opts.params,
    });
    ch.jobId = job.job_id;
    // `consumeJob` owns the running flag so hand observation over to it explicitly.
    ch.running = false;
    persist(key, ch);
    await consumeJob(key, ch, opts);
  } catch (error) {
    ch.running = false;
    ch.recoverable = Boolean(ch.jobId);
    stopElapsedTimer(key, ch);
    persist(key, ch);
    notifyError(error);
  }
}
