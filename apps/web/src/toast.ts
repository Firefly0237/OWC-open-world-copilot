/** A tiny global toast bus. Pages call notifyError / pushToast instead of stranding a raw error in
 * a muted line — failures surface as a calm popup that says what went wrong and clears itself. */
import { reactive } from "vue";
import { humanizeError } from "./api";

export type ToastKind = "error" | "ok" | "info";

export interface Toast {
  id: number;
  kind: ToastKind;
  message: string;
}

export const toasts = reactive<Toast[]>([]);

let seq = 0;
const TIMERS = new Map<number, number>();

export function dismissToast(id: number): void {
  const i = toasts.findIndex((t) => t.id === id);
  if (i >= 0) toasts.splice(i, 1);
  const timer = TIMERS.get(id);
  if (timer) {
    window.clearTimeout(timer);
    TIMERS.delete(id);
  }
}

export function pushToast(message: string, kind: ToastKind = "info", ms = 4800): number {
  const id = ++seq;
  toasts.push({ id, kind, message });
  // cap the stack so a burst of failures can't bury the screen
  while (toasts.length > 4) dismissToast(toasts[0].id);
  if (ms > 0) TIMERS.set(id, window.setTimeout(() => dismissToast(id), ms));
  return id;
}

/** The common path: turn a thrown error into a guided sentence and pop it. */
export function notifyError(e: unknown): number {
  return pushToast(humanizeError(e), "error", 6000);
}

export function notifyOk(message: string): number {
  return pushToast(message, "ok", 3600);
}
