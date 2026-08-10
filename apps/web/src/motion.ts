/** 如我所书 · 动效共享工具（P0 冻结，规范 §2.4 / §5）。
 * CSS 侧 token 在 styles/tokens.css（--ow-dur-* / --ow-ease-*）；本模块是 JS 侧同值常量，
 * 给需要毫秒数与 stagger 逻辑的场景（v-reveal 指令、路由过渡、SVG/画布动画）使用。
 * 铁律：只动 transform/opacity；reduced-motion 与 pro mode 下装饰动效全关。 */

export const MOTION = {
  /** 微交互 hover/press（规范区间 120–160ms） */
  micro: 140,
  /** 标准过渡 显隐/淡滑（240–320ms） */
  standard: 280,
  /** 入场编排（400–600ms） */
  enter: 480,
  /** 章节翻页（500–700ms）——仅跨章节路由切换；章内用 standard */
  turn: 600,
  /** 骨架 shimmer 循环（linear） */
  shimmer: 1200,
  easeStandard: "cubic-bezier(0.4, 0, 0.2, 1)",
  easeEnter: "cubic-bezier(0.22, 1, 0.36, 1)",
  /** 入场编排逐项延迟（40–60ms） */
  stagger: 50,
  /** 超过此项数直接出现，不再 stagger（规范铁律 3） */
  staggerCap: 8,
  /** 降级模式（reduced-motion / pro mode）下保留的唯一动效时长 */
  reducedFade: 120,
} as const;

/** 装饰动效是否允许运行：系统 reduced-motion 或用户 pro mode 任一开启即全关。
 * pro mode 的判定与 App.vue/SettingsPage 共用同一把 localStorage 钥匙。 */
export function motionEnabled(): boolean {
  if (typeof window === "undefined") return false;
  if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return false;
  if (document.body.classList.contains("pro-mode")) return false;
  return true;
}

/** 入场编排逐项延迟（ms）：第 index 项延迟 index*stagger；达到 staggerCap 后一律 0（直接出现）。 */
export function staggerDelay(index: number): number {
  return index >= MOTION.staggerCap ? 0 : index * MOTION.stagger;
}

/** 按当前降级状态取动效时长：降级时装饰动效归零，功能淡入保留 reducedFade。 */
export function duration(of: keyof Pick<typeof MOTION, "micro" | "standard" | "enter" | "turn">): number {
  return motionEnabled() ? MOTION[of] : MOTION.reducedFade;
}
