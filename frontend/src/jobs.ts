/** Global job channels. Generation runs (creation, world seed, extraction…) used to live on the
 * page component: navigating away unmounted the component, killed the elapsed timer and dropped the
 * SSE stream, so the progress bar + animation vanished and didn't come back. Here the running job's
 * state (stage, elapsed, running, result) lives in a module-level reactive store and the SSE is
 * consumed here, so it survives navigation — a page just re-reads its channel on mount and the warp
 * picks up exactly where it was. One channel per generation kind. */
import { reactive } from "vue";
import {
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
  jobId: string;
  running: boolean;
  /** has this channel ever started a run (so a returning page knows to show the panel) */
  started: boolean;
  stageIndex: number;
  elapsed: number;
  stages: Stage[];
  hint: string;
  result: R | null;
  cost: number;
}

const channels = reactive<Record<string, JobChannel>>({});
const timers: Record<string, number> = {};

export function getJobChannel<R = unknown>(key: string, stages: Stage[]): JobChannel<R> {
  if (!channels[key]) {
    channels[key] = {
      jobId: "",
      running: false,
      started: false,
      stageIndex: -1,
      elapsed: 0,
      stages,
      hint: "",
      result: null,
      cost: 0,
    };
  } else {
    channels[key].stages = stages;
  }
  return channels[key] as JobChannel<R>;
}

export interface StartOptions<R> {
  kind: string;
  params: Record<string, unknown>;
  stages: Stage[];
  /** page-specific live event handling (stage progression, chunk/judge hints) */
  onEvent?: (ch: JobChannel<R>, event: JobEvent) => void;
  /** turn the terminal job result into the page's display shape */
  parseResult: (raw: Record<string, unknown>) => R;
}

export async function startJob<R>(key: string, opts: StartOptions<R>): Promise<void> {
  const ch = getJobChannel<R>(key, opts.stages);
  if (ch.running) return;
  ch.running = true;
  ch.started = true;
  ch.stageIndex = 0;
  ch.elapsed = 0;
  ch.hint = "";
  ch.result = null;
  ch.cost = 0;
  if (timers[key]) window.clearInterval(timers[key]);
  timers[key] = window.setInterval(() => {
    ch.elapsed += 1;
  }, 1000);
  let failed = false;
  try {
    const job = await apiPost<{ job_id: string }>(`/projects/${currentProject()}/jobs`, {
      kind: opts.kind,
      params: opts.params,
    });
    ch.jobId = job.job_id;
    await streamJobEvents(job.job_id, (event) => {
      if (event.type === "failed") {
        failed = true;
        notifyError(event.data.error ?? "任务失败");
      } else if (opts.onEvent) {
        opts.onEvent(ch, event);
      }
    });
    const status = await apiGet<{
      status: string;
      result: Record<string, unknown> | null;
      error: string | null;
    }>(`/jobs/${job.job_id}`);
    if (status.status === "done" && status.result) {
      ch.stageIndex = opts.stages.length - 1;
      ch.result = opts.parseResult(status.result);
      const used = costOf(status.result as { cost_budget?: { used_usd?: number } });
      ch.cost = used;
      addSessionCost(used);
    } else if (!failed) {
      notifyError(status.error ?? "任务未完成");
    }
  } catch (e) {
    notifyError(e);
  } finally {
    ch.running = false;
    if (timers[key]) {
      window.clearInterval(timers[key]);
      delete timers[key];
    }
  }
}
