export interface ContradictionScanPayload {
  use_llm: boolean;
  semantic_threshold: number;
  max_judge: number;
  llm_mode: "offline" | "real";
  llm_model?: string;
}

export interface ContradictionModeEvidence {
  llm_used?: boolean;
  judge_mode?: "disabled" | "offline_double" | "real_model" | string;
}

export function contradictionScanControlError(
  semanticThreshold: unknown,
  maxJudge: unknown,
): string {
  if (
    typeof semanticThreshold !== "number" ||
    !Number.isFinite(semanticThreshold) ||
    semanticThreshold < 0 ||
    semanticThreshold > 1
  ) {
    return "语义召回阈值必须是 0 到 1 之间的数字。";
  }
  if (
    typeof maxJudge !== "number" ||
    !Number.isInteger(maxJudge) ||
    maxJudge < 1 ||
    maxJudge > 1000
  ) {
    return "模型判定上限必须是 1 到 1000 之间的整数。";
  }
  return "";
}

export function buildContradictionScanPayload(options: {
  semanticThreshold: unknown;
  maxJudge: unknown;
  requestRealJudge: boolean;
  llmReady: boolean;
  llmModel?: string;
}): ContradictionScanPayload {
  const validationError = contradictionScanControlError(
    options.semanticThreshold,
    options.maxJudge,
  );
  if (validationError) throw new RangeError(validationError);

  const useRealJudge = options.requestRealJudge && options.llmReady;
  return {
    use_llm: useRealJudge,
    semantic_threshold: options.semanticThreshold as number,
    max_judge: options.maxJudge as number,
    llm_mode: useRealJudge ? "real" : "offline",
    ...(useRealJudge && options.llmModel ? { llm_model: options.llmModel } : {}),
  };
}

export function contradictionActualModeLabel(
  evidence: ContradictionModeEvidence,
  requestedRealJudge: boolean,
): string {
  if (evidence.judge_mode === "real_model") {
    return "实际模式：真实模型判定（后端报告 judge_mode=real_model）。";
  }
  if (evidence.judge_mode === "offline_double") {
    return "实际模式：离线判定替身（$0；不是真实模型）。";
  }
  if (evidence.judge_mode === "disabled") {
    return "实际模式：$0 离线扫描，未使用模型判定。";
  }
  if (evidence.llm_used === true) {
    return requestedRealJudge
      ? "实际模式：真实模型判定已运行（后端报告 llm_used=true）。"
      : "实际模式：后端报告模型判定已运行，但未返回模型类型。";
  }
  if (evidence.llm_used === false) {
    return requestedRealJudge
      ? "实际模式：真实模型未实际判定（后端报告 llm_used=false）；候选保留人工复查。"
      : "实际模式：$0 离线扫描，未使用模型判定。";
  }
  return "实际模式：后端未返回判定模式；不宣称已调用模型。";
}
