export type SemanticScanStatus =
  | "IDLE"
  | "ERROR"
  | "NOT_AVAILABLE"
  | "NO_CANDIDATE"
  | "RISK"
  | "INCOMPLETE"
  | "CLEAN";

export interface SemanticScanResult<TFinding = unknown> {
  contradictions: TFinding[];
  review_suggested: TFinding[];
  candidate_count: number;
  judged_count: number;
  judge_skipped: number;
  semantic_used: boolean;
  llm_used: boolean;
  // A future backend may explicitly attest that every recalled candidate went
  // through the complete exclusion protocol.  The current backend never sends
  // this, which intentionally makes CLEAN unreachable today.
  protocol_complete_exclusion?: boolean;
}

export interface SemanticScanState<TFinding = unknown> {
  status: SemanticScanStatus;
  result: SemanticScanResult<TFinding>;
}

export function emptySemanticScanResult<TFinding = unknown>(): SemanticScanResult<TFinding> {
  return {
    contradictions: [],
    review_suggested: [],
    candidate_count: 0,
    judged_count: 0,
    judge_skipped: 0,
    semantic_used: false,
    llm_used: false,
  };
}

export function semanticScanStatus(result: SemanticScanResult): SemanticScanStatus {
  if (
    !Number.isInteger(result.candidate_count) ||
    result.candidate_count < 0 ||
    !Number.isInteger(result.judged_count) ||
    result.judged_count < 0 ||
    !Number.isInteger(result.judge_skipped) ||
    result.judge_skipped < 0
  ) {
    return "ERROR";
  }
  if (!result.semantic_used) return "NOT_AVAILABLE";
  if (result.candidate_count === 0) return "NO_CANDIDATE";
  if (result.contradictions.length || result.review_suggested.length) return "RISK";
  if (
    result.protocol_complete_exclusion === true &&
    result.judged_count === result.candidate_count &&
    result.judge_skipped === 0
  ) {
    return "CLEAN";
  }
  return "INCOMPLETE";
}

export function successfulSemanticScan<TFinding>(
  result: SemanticScanResult<TFinding>,
): SemanticScanState<TFinding> {
  return { status: semanticScanStatus(result), result };
}

export function failedSemanticScan<TFinding = unknown>(): SemanticScanState<TFinding> {
  // Always allocate a fresh empty result: an error must not leave a previous
  // successful CLEAN/risk payload visible.
  return { status: "ERROR", result: emptySemanticScanResult<TFinding>() };
}

export const SEMANTIC_STATUS_MESSAGE: Record<SemanticScanStatus, string> = {
  IDLE: "尚未运行语义检测。",
  ERROR: "语义检测失败；上一次结果已清除。",
  NOT_AVAILABLE: "语义能力未运行或已降级，不能据此判断设定干净。",
  NO_CANDIDATE: "语义层已运行，但没有召回候选；这不等于已证明没有风险。",
  RISK: "已召回需要处理或人工复查的语义风险。",
  INCOMPLETE: "已有候选，但完整排除协议尚未完成，不能判定干净。",
  CLEAN: "有候选且已按完整协议逐一排除，未发现语义风险。",
};
