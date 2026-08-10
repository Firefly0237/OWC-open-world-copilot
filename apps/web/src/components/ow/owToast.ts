import { reactive } from "vue";

export interface OwToastItem {
  id: number;
  text: string;
  tone: "success" | "error" | "info";
}

let seq = 0;
export const owToasts = reactive<OwToastItem[]>([]);

export function owToast(text: string, tone: OwToastItem["tone"] = "info"): void {
  const id = ++seq;
  owToasts.push({ id, text, tone });
  window.setTimeout(() => {
    const index = owToasts.findIndex((t) => t.id === id);
    if (index >= 0) owToasts.splice(index, 1);
  }, 4000);
}
