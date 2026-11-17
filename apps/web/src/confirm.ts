import { shallowRef } from "vue";

export interface ConfirmRequest {
  title: string;
  message: string;
  confirmLabel: string;
  cancelLabel: string;
  danger: boolean;
}

interface PendingConfirm extends ConfirmRequest {
  resolve: (accepted: boolean) => void;
}

export const activeConfirm = shallowRef<PendingConfirm | null>(null);
const queue: PendingConfirm[] = [];

function showNext(): void {
  if (!activeConfirm.value) activeConfirm.value = queue.shift() ?? null;
}

export function askConfirm(
  message: string,
  options: Partial<Omit<ConfirmRequest, "message">> = {},
): Promise<boolean> {
  return new Promise((resolve) => {
    queue.push({
      title: options.title ?? "确认这项操作",
      message,
      confirmLabel: options.confirmLabel ?? "确认",
      cancelLabel: options.cancelLabel ?? "取消",
      danger: options.danger ?? true,
      resolve,
    });
    showNext();
  });
}

export function settleConfirm(accepted: boolean): void {
  const pending = activeConfirm.value;
  if (!pending) return;
  activeConfirm.value = null;
  pending.resolve(accepted);
  showNext();
}
