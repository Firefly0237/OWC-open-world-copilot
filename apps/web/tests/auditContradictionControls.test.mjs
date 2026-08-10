import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  buildContradictionScanPayload,
  contradictionActualModeLabel,
  contradictionScanControlError,
} from "../src/contradictionScanControls.ts";

const page = await readFile(new URL("../src/pages/AuditPage.vue", import.meta.url), "utf8");

test("audit contradiction scan is offline by default and exposes an explicit real-model gate", () => {
  assert.match(page, /const contraUseRealJudge = ref\(false\)/);
  assert.match(page, /v-model="contraUseRealJudge"[\s\S]*:disabled="!llmReady \|\| contraScanning"/);
  assert.match(page, /to="\/settings"/);
  assert.match(page, /\$0 扫描设定矛盾/);
  assert.match(page, /\/contradictions:scan/);
  assert.match(page, /\/audits\?include_details=false/);

  const offline = buildContradictionScanPayload({
    semanticThreshold: 0.6,
    maxJudge: 200,
    requestRealJudge: false,
    llmReady: true,
    llmModel: "configured-model",
  });
  assert.deepEqual(offline, {
    use_llm: false,
    semantic_threshold: 0.6,
    max_judge: 200,
    llm_mode: "offline",
  });

  const notReady = buildContradictionScanPayload({
    semanticThreshold: 0.6,
    maxJudge: 200,
    requestRealJudge: true,
    llmReady: false,
    llmModel: "stale-model",
  });
  assert.equal(notReady.use_llm, false);
  assert.equal(notReady.llm_mode, "offline");
  assert.equal("llm_model" in notReady, false);
});

test("real-model mode requires both explicit consent and a ready model", () => {
  const payload = buildContradictionScanPayload({
    semanticThreshold: 0.75,
    maxJudge: 37,
    requestRealJudge: true,
    llmReady: true,
    llmModel: "provider/model",
  });
  assert.deepEqual(payload, {
    use_llm: true,
    semantic_threshold: 0.75,
    max_judge: 37,
    llm_mode: "real",
    llm_model: "provider/model",
  });
});

test("semantic threshold and judge cap enforce the FastAPI request bounds", () => {
  assert.equal(contradictionScanControlError(0, 1), "");
  assert.equal(contradictionScanControlError(1, 1000), "");
  assert.match(contradictionScanControlError(-0.01, 200), /0 到 1/);
  assert.match(contradictionScanControlError(1.01, 200), /0 到 1/);
  assert.match(contradictionScanControlError("", 200), /0 到 1/);
  assert.match(contradictionScanControlError(0.6, 0), /1 到 1000/);
  assert.match(contradictionScanControlError(0.6, 1001), /1 到 1000/);
  assert.match(contradictionScanControlError(0.6, 1.5), /整数/);
  assert.throws(
    () =>
      buildContradictionScanPayload({
        semanticThreshold: Number.NaN,
        maxJudge: 200,
        requestRealJudge: false,
        llmReady: false,
      }),
    RangeError,
  );

  assert.match(page, /v-model\.number="contraSemanticThreshold"[\s\S]*min="0"[\s\S]*max="1"/);
  assert.match(page, /v-model\.number="contraMaxJudge"[\s\S]*min="1"[\s\S]*max="1000"/);
  assert.match(page, /semanticThreshold: contraSemanticThreshold\.value/);
  assert.match(page, /maxJudge: contraMaxJudge\.value/);
});

test("result copy reports backend evidence instead of the requested mode alone", () => {
  assert.match(contradictionActualModeLabel({ llm_used: false }, false), /\$0 离线扫描/);
  assert.match(
    contradictionActualModeLabel({ llm_used: false }, true),
    /真实模型未实际判定/,
  );
  assert.match(
    contradictionActualModeLabel({ llm_used: true }, true),
    /后端报告 llm_used=true/,
  );
  assert.match(
    contradictionActualModeLabel({ judge_mode: "offline_double", llm_used: true }, true),
    /离线判定替身/,
  );
  assert.match(contradictionActualModeLabel({}, true), /不宣称已调用模型/);
  assert.match(page, /data-testid="contradiction-actual-mode"/);
  assert.match(page, /contra\.semantic_used/);
  assert.match(page, /contra\.judged_count/);
  assert.match(page, /contra\.candidate_count/);
});
