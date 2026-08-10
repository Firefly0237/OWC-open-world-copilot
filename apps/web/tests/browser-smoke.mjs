import assert from "node:assert/strict";
import { existsSync, readdirSync, statSync } from "node:fs";
import process from "node:process";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import puppeteer from "puppeteer-core";
import { preview } from "vite";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
let jobStatusCalls = 0;
let timelineSaveCalls = 0;
let brokenDialogueCalls = 0;

function newestMtimeMs(target) {
  const metadata = statSync(target);
  if (!metadata.isDirectory()) return metadata.mtimeMs;
  return readdirSync(target, { withFileTypes: true }).reduce((newest, entry) => {
    const child = resolve(target, entry.name);
    return Math.max(newest, entry.isDirectory() ? newestMtimeMs(child) : statSync(child).mtimeMs);
  }, metadata.mtimeMs);
}

function requireFreshBuild() {
  const distIndex = resolve(root, "dist", "index.html");
  if (!existsSync(distIndex)) {
    throw new Error("Missing dist/index.html. Run `npm run build` before the browser smoke.");
  }
  const buildInputs = [
    resolve(root, "src"),
    resolve(root, "index.html"),
    resolve(root, "package.json"),
    resolve(root, "package-lock.json"),
    resolve(root, "tsconfig.json"),
    resolve(root, "vite.config.ts"),
  ];
  const newestInput = Math.max(...buildInputs.filter(existsSync).map(newestMtimeMs));
  const builtAt = statSync(distIndex).mtimeMs;
  if (builtAt < newestInput) {
    throw new Error(
      "Stale frontend build: source/config is newer than dist/index.html. " +
      "Run `npm run build` before the browser smoke.",
    );
  }
}

function browserExecutable() {
  const candidates = [
    process.env.PUPPETEER_EXECUTABLE_PATH,
    process.env.CHROME_PATH,
    "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
    "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
  ].filter(Boolean);
  const executable = candidates.find((candidate) => existsSync(candidate));
  if (!executable) {
    throw new Error("No Chrome/Edge executable found. Set PUPPETEER_EXECUTABLE_PATH.");
  }
  return executable;
}

const mocks = new Map([
  // The smoke intentionally exercises the deterministic agent job. Production hides this option
  // unless the FastAPI runtime advertises the explicit development/CI capability.
  ["/health", { status: "ok", profile: "local", offline_llm_allowed: true }],
  ["/workspaces", { workspaces: [{ name: "demo" }] }],
  ["/settings/connection", { configured: false, prices_configured: false }],
  [
    "/projects/demo/overview",
    {
      overview: {
        counts: { entities: 3, quests: 1, regions: 1, pois: 2, dialogues: 0 },
        graph: { nodes: 4, edges: 2 },
        provenance: { by_origin: { human: 3 }, by_review_status: { approved: 3 } },
      },
    },
  ],
  [
    "/projects/demo/readiness",
    {
      readiness: {
        standard_version: "smoke",
        total_items: 0,
        ready_items: 0,
        overall_score: 0,
        ready_rate: 0,
        by_kind: [],
        items: [],
      },
    },
  ],
  [
    "/projects/demo/timeline",
    {
      timeline: {
        items: [
          {
            ref: "event:smoke-event",
            kind: "event",
            label: "烟港停电",
            order: 1,
            rank: 0,
            flags: [],
          },
        ],
        dependencies: [],
        unsequenced: [],
        rank_count: 1,
      },
    },
  ],
  [
    "/projects/demo/dialogue_trees",
    {
      dialogues: {
        trees: [
          {
            id: "dialogue-ok",
            title: "旧树：港口问讯",
            participants: ["守卫"],
            node_count: 1,
          },
          {
            id: "dialogue-broken",
            title: "失败树：断线支路",
            participants: ["向导"],
            node_count: 1,
          },
        ],
      },
    },
  ],
  [
    "/projects/demo/dialogue_trees/dialogue-ok/flow",
    {
      flow: {
        tree_id: "dialogue-ok",
        title: "旧树：港口问讯",
        nodes: [
          {
            ref: "opening",
            x: 140,
            y: 100,
            kind: "root",
            label: "守卫：通行证？",
          },
        ],
        edges: [],
      },
    },
  ],
  [
    "/projects/demo/dialogue_trees/dialogue-ok",
    {
      tree: {
        id: "dialogue-ok",
        title: "旧树：港口问讯",
        root_node: "opening",
        nodes: {
          opening: {
            id: "opening",
            speaker_id: "guard",
            text: "通行证？",
            choices: [],
            next_node: null,
          },
        },
      },
    },
  ],
  [
    "/projects/demo/archive",
    {
      inventory: {
        entities: [
          {
            id: "smoke-archivist",
            name: "雾港档案员",
            type: "npc",
            description: "负责记录港区航行日志。",
            tags: "港口,档案",
            origin: "human",
            review_status: "approved",
            metadata: {},
          },
        ],
        quests: [
          {
            id: "smoke-quest",
            title: "雾港通行证",
            giver_npc: "smoke-archivist",
            location: "smoke-region",
            objective: "取得港区通行许可。",
            stages: 2,
            timeline_order: 1,
            origin: "human",
            review_status: "approved",
          },
        ],
        regions: [
          {
            id: "smoke-region",
            name: "雾港",
            level_min: 1,
            level_max: 10,
            themes: "潮汐与旧航路",
            banned_content: "",
          },
        ],
        pois: [
          {
            id: "smoke-poi",
            name: "航路档案馆",
            region_id: "smoke-region",
            purpose: "保存航行记录",
            controlling_faction: "港务署",
          },
        ],
        terms: [
          {
            id: "smoke-term",
            canonical: "雾灯",
            aliases: "引航灯",
            forbidden: "",
            description: "港区的夜间引航设施。",
          },
        ],
        dialogues: [
          {
            id: "smoke-line",
            text_key: "dialogue.guard.opening",
            speaker_id: "guard",
            quest_id: "smoke-quest",
            text: "通行证？",
          },
        ],
        dialogue_trees: [
          {
            id: "dialogue-ok",
            title: "旧树：港口问讯",
            quest_id: "smoke-quest",
            participants: "guard",
            nodes: 1,
          },
        ],
        relations: [
          {
            source: "smoke-archivist",
            kind: "documents",
            target: "smoke-region",
            valid_from: 1,
            valid_until: null,
            origin: "human",
            review_status: "approved",
            metadata: {},
          },
        ],
        style_guides: [
          {
            id: "smoke-style-guide",
            body: "港区叙述保持克制。",
            rules: ["避免网络梗"],
            origin: "human",
            review_status: "approved",
          },
        ],
        quest_event_refs: [
          {
            id: "smoke-qer",
            quest_id: "smoke-quest",
            event_id: "smoke-event",
            ref_kind: "mentions_event",
            note: "通行证任务提及停电事件。",
            origin: "human",
            review_status: "approved",
            metadata: {},
          },
        ],
        style_rubrics: [
          {
            id: "smoke-rubric",
            label: "烟港叙事量表",
            abstract_tag: "克制",
            version: "v1",
            frozen_at: null,
            author: "smoke",
            elements: [{ id: "concise", text: "表达是否简洁" }],
            element_count: 1,
            origin: "human",
            review_status: "approved",
            metadata: {},
          },
        ],
        dialogue_tree_payloads: {
          "dialogue-ok": {
            id: "dialogue-ok",
            nodes: {
              opening: { id: "opening", text_key: "dialogue.guard.opening" },
            },
          },
        },
      },
    },
  ],
  ["/projects/demo/assets", { assets: [] }],
  [
    "/projects/demo/collab",
    {
      assignment: null,
      comments: [],
      lock: null,
      total: 0,
      next_cursor: null,
      snapshot_etag: "smoke-collab",
    },
  ],
]);

requireFreshBuild();
const preferredPort = Number(process.env.OWCOPILOT_SMOKE_PORT ?? "4273");
if (!Number.isInteger(preferredPort) || preferredPort < 1 || preferredPort > 65_535) {
  throw new Error("OWCOPILOT_SMOKE_PORT must be an integer between 1 and 65535.");
}
const server = await preview({
  root,
  // Let Vite advance to a free port when the preferred one is already occupied, then derive the
  // origin from the bound server rather than assuming the requested port won the race.
  preview: { host: "127.0.0.1", port: preferredPort, strictPort: false },
  logLevel: "error",
});
const address = server.httpServer.address();
assert.ok(address && typeof address !== "string", "preview server must expose its bound TCP port");
const origin = `http://127.0.0.1:${address.port}`;
let browser;

try {
  browser = await puppeteer.launch({
    executablePath: browserExecutable(),
    headless: true,
    args: ["--no-sandbox", "--disable-dev-shm-usage"],
  });
  const page = await browser.newPage();
  page.setDefaultTimeout(12_000);
  const runtimeErrors = [];
  const expectedHttpFailurePaths = new Set([
    "/projects/demo/entities/smoke-event",
    "/projects/demo/dialogue_trees/dialogue-broken",
    "/projects/demo/dialogue_trees/dialogue-broken/flow",
  ]);
  page.on("pageerror", (error) => runtimeErrors.push(error.message));
  page.on("console", (message) => {
    if (message.type() === "error") {
      const source = message.location().url;
      if (
        source
        && message.text() === "Failed to load resource: the server responded with a status of 500 (Internal Server Error)"
      ) {
        const sourceUrl = new URL(source);
        if (sourceUrl.origin === origin && expectedHttpFailurePaths.has(sourceUrl.pathname)) return;
      }
      runtimeErrors.push(source ? `${message.text()} (${source})` : message.text());
    }
  });
  await page.setRequestInterception(true);
  const observedApiUrls = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.pathname === "/projects/demo/entities/smoke-event" && request.method() === "PATCH") {
      observedApiUrls.push(url.href);
      timelineSaveCalls += 1;
      void request.respond({
        status: 500,
        contentType: "application/json; charset=utf-8",
        body: JSON.stringify({ detail: "时间线保存失败（smoke）" }),
      });
      return;
    }
    if (
      request.method() === "GET"
      && (
        url.pathname === "/projects/demo/dialogue_trees/dialogue-broken"
        || url.pathname === "/projects/demo/dialogue_trees/dialogue-broken/flow"
      )
    ) {
      observedApiUrls.push(url.href);
      brokenDialogueCalls += 1;
      void request.respond({
        status: 500,
        contentType: "application/json; charset=utf-8",
        body: JSON.stringify({ detail: "对话树读取失败（smoke）" }),
      });
      return;
    }
    if (url.pathname === "/projects/demo/jobs" && request.method() === "POST") {
      observedApiUrls.push(url.href);
      void request.respond({
        status: 202,
        contentType: "application/json; charset=utf-8",
        body: JSON.stringify({ job_id: "smoke-job", kind: "agent_diagnose", status: "running" }),
      });
      return;
    }
    if (url.pathname === "/jobs/smoke-job/events") {
      observedApiUrls.push(url.href);
      void request.respond({
        status: 200,
        contentType: "text/event-stream; charset=utf-8",
        body: 'event: stage\ndata: {"stage":"reason","max_steps":6}\n\nevent: done\ndata: {}\n\n',
      });
      return;
    }
    if (url.pathname === "/jobs/smoke-job") {
      observedApiUrls.push(url.href);
      jobStatusCalls += 1;
      const done = jobStatusCalls > 1;
      void request.respond({
        status: 200,
        contentType: "application/json; charset=utf-8",
        body: JSON.stringify(done
          ? {
              status: "done",
              error: null,
              result: {
                goal: "恢复测试",
                final_answer: "刷新后恢复成功",
                stop_reason: "completed",
                steps: [],
                step_count: 0,
                interrupted: false,
                interrupt_request: null,
                status: "completed",
                thread_id: "smoke-thread",
                skills: [],
                cost_budget: { used_usd: 0 },
              },
            }
          : { status: "running", error: null, result: null }),
      });
      return;
    }
    if (
      url.pathname === "/projects/demo/archive"
      && request.method() === "GET"
      && request.resourceType() !== "document"
      && url.searchParams.has("kind")
    ) {
      observedApiUrls.push(url.href);
      const legacy = mocks.get(url.pathname).inventory;
      const kind = url.searchParams.get("kind") ?? "entity";
      const keys = {
        entity: "entities",
        quest: "quests",
        region: "regions",
        poi: "pois",
        term: "terms",
        dialogue: "dialogues",
        dialogue_tree: "dialogue_trees",
        relation: "relations",
        style_guide: "style_guides",
        quest_event_ref: "quest_event_refs",
        style_rubric: "style_rubrics",
      };
      const q = (url.searchParams.get("q") ?? "").trim().toLocaleLowerCase();
      const rows = [...(legacy[keys[kind]] ?? [])]
        .filter((row) => !q || JSON.stringify(row).toLocaleLowerCase().includes(q));
      const limit = Number(url.searchParams.get("limit") ?? 20);
      const focusId = (url.searchParams.get("focus") ?? "").split(":").slice(1).join(":");
      const focusIndex = focusId
        ? rows.findIndex((row) => (
            (row.id ?? `${row.source}:${row.kind}:${row.target}`) === focusId
          ))
        : -1;
      const offset = focusIndex >= 0 ? Math.floor(focusIndex / limit) * limit : 0;
      const items = rows.slice(offset, offset + limit);
      const payloadIds = new Set(
        kind === "dialogue_tree"
          ? items.map((row) => row.id)
          : Object.keys(legacy.dialogue_tree_payloads),
      );
      const dialogue_tree_payloads = Object.fromEntries(
        Object.entries(legacy.dialogue_tree_payloads)
          .filter(([id]) => (kind === "dialogue" || kind === "dialogue_tree") && payloadIds.has(id)),
      );
      const counts = Object.fromEntries(
        Object.entries(keys).map(([name, key]) => [name, legacy[key]?.length ?? 0]),
      );
      void request.respond({
        status: 200,
        contentType: "application/json; charset=utf-8",
        body: JSON.stringify({
          project: "demo",
          kind,
          items,
          counts,
          total: rows.length,
          limit,
          offset,
          page_cursor: null,
          previous_cursor: null,
          next_cursor: null,
          snapshot_etag: "smoke-archive",
          dialogue_tree_payloads,
          cost_budget: { used_usd: 0 },
        }),
      });
      return;
    }
    const body = mocks.get(url.pathname);
    if (body && request.method() === "GET" && request.resourceType() !== "document") {
      observedApiUrls.push(url.href);
      void request.respond({
        status: 200,
        contentType: "application/json; charset=utf-8",
        body: JSON.stringify(body),
      });
    } else {
      void request.continue();
    }
  });
  await page.evaluateOnNewDocument(() => {
    localStorage.setItem("owcopilot_project", "demo");
    localStorage.setItem("owcopilot_tour_done", "1");
  });

  await page.setViewport({ width: 1440, height: 900, deviceScaleFactor: 1 });
  await page.goto(`${origin}/overview`, { waitUntil: "networkidle0" });
  await page.waitForFunction(() => document.querySelector("h1")?.textContent?.includes("世界总览"));
  const isVisible = (selector) => page.$eval(selector, (element) => {
    const style = getComputedStyle(element);
    const box = element.getBoundingClientRect();
    return style.visibility !== "hidden" && style.display !== "none" && box.width > 0 && box.height > 0;
  });
  const count = (selector) => page.$$eval(selector, (elements) => elements.length);
  const attr = (selector, name) => page.$eval(selector, (element, key) => element.getAttribute(key), name);
  assert.equal(await isVisible("main"), true, "main content should mount");
  assert.equal(await isVisible("aside"), true, "desktop navigation should be visible");
  assert.equal(await count(".connection-card"), 0, "healthy API must not show a gate");
  assert.equal(await count("h1"), 1, "page should expose one primary heading");
  assert.equal(await isVisible(".desktop-command"), true, "desktop sidebar should expose search without a shortcut");
  await page.click(".desktop-command");
  await page.waitForSelector('.cp[role="dialog"][aria-label="全局检索与安全重命名"]', { visible: true });
  assert.equal(
    await page.$eval(".cp input", (input) => input === document.activeElement),
    true,
    "desktop search button should open and focus the command palette",
  );
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector('.cp[role="dialog"]'));

  await page.setViewport({ width: 390, height: 844, deviceScaleFactor: 1 });
  await page.waitForFunction(() => getComputedStyle(document.querySelector(".mobile-bar")).display !== "none");
  const mobileGeometry = await page.evaluate(() => ({
    top: document.querySelector("main")?.getBoundingClientRect().top ?? 999,
    overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
  }));
  assert.ok(mobileGeometry.top < 120, `mobile content starts too low (${mobileGeometry.top}px)`);
  assert.ok(mobileGeometry.overflow <= 1, `mobile layout overflows by ${mobileGeometry.overflow}px`);

  const mobileCommandSize = await page.$eval(".mobile-command", (button) => {
    const box = button.getBoundingClientRect();
    return { width: box.width, height: box.height };
  });
  assert.ok(
    mobileCommandSize.width >= 44 && mobileCommandSize.height >= 44,
    `mobile search target is too small (${mobileCommandSize.width}x${mobileCommandSize.height})`,
  );
  await page.click(".mobile-command");
  await page.waitForSelector('.cp[role="dialog"][aria-label="全局检索与安全重命名"]', { visible: true });
  assert.equal(
    await page.$eval(".cp input", (input) => input === document.activeElement),
    true,
    "mobile search button should focus the command palette",
  );
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector('.cp[role="dialog"]'));

  const menuHitTarget = await page.$eval(".mobile-menu", (button) => {
    const box = button.getBoundingClientRect();
    const target = document.elementFromPoint(box.left + box.width / 2, box.top + box.height / 2);
    return target?.closest("button")?.className ?? target?.className ?? target?.tagName ?? "";
  });
  assert.equal(menuHitTarget, "mobile-menu", `mobile menu is covered by ${menuHitTarget}`);
  await page.click(".mobile-menu");
  await page.waitForFunction(() => document.querySelector(".mobile-menu")?.getAttribute("aria-expanded") === "true");
  assert.equal(await attr(".mobile-menu", "aria-expanded"), "true");
  assert.equal(await isVisible("aside"), true, "mobile drawer should open");
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => document.querySelector(".mobile-menu")?.getAttribute("aria-expanded") === "false");
  assert.equal(await attr(".mobile-menu", "aria-expanded"), "false");

  await page.setViewport({ width: 1440, height: 900, deviceScaleFactor: 1 });
  await page.goto(`${origin}/missing-chapter`, { waitUntil: "networkidle0" });
  await page.waitForFunction(() => document.querySelector("h1")?.textContent?.includes("不在星图中"));
  assert.equal(await count("h1"), 1, "not-found route should remain a complete page");

  // A loaded timeline must remain usable when a mutation fails: keyboard users can open an SVG
  // node, and the failed PATCH is surfaced as a visible in-context alert without dropping the view.
  await page.goto(`${origin}/timeline`, { waitUntil: "networkidle0" });
  await page.waitForFunction(() => document.querySelector("h1")?.textContent?.includes("时间线"));
  const timelineNode = ".scroll svg.tl g.node[role='button']";
  await page.waitForSelector(timelineNode, { visible: true });
  await page.$eval(timelineNode, (node) => node.focus());
  assert.equal(
    await page.$eval(timelineNode, (node) => document.activeElement === node),
    true,
    "timeline SVG node should accept keyboard focus",
  );
  await page.keyboard.press("Enter");
  await page.waitForFunction(
    () => document.querySelector(".detail .d-head b")?.textContent?.includes("烟港停电"),
  );
  assert.equal(await attr(timelineNode, "aria-pressed"), "true", "Enter should select the node");
  await page.click(".detail .d-actions .primary");
  await page.waitForFunction(() =>
    [...document.querySelectorAll("[role='alert']")]
      .some((element) => element.textContent?.includes("时间线保存失败（smoke）")),
  );
  assert.equal(await isVisible(".mutation-error[role='alert']"), true, "PATCH failure should be visible");
  assert.equal(await count(timelineNode), 1, "failed save must preserve the loaded timeline view");
  assert.equal(timelineSaveCalls, 1, "timeline save should issue exactly one PATCH");

  // Switching to a tree whose requests fail must clear the previous flow instead of presenting a
  // stale title beside the new selection, while retaining an actionable visible error.
  await page.goto(
    `${origin}/dialogues?kind=dialogue&focus=${encodeURIComponent("dialogue_tree:dialogue-ok")}&node=opening`,
    { waitUntil: "networkidle0" },
  );
  await page.waitForFunction(
    () => document.querySelector(".stage-head input")?.value?.includes("旧树：港口问讯"),
  );
  await page.waitForFunction(
    () => document.querySelector(".detail .d-head b")?.textContent?.includes("节点 opening"),
  );
  await page.$$eval(".list .row", (rows, title) => {
    const target = rows.find((row) => row.textContent?.includes(title));
    if (!target) throw new Error(`Missing dialogue tree row: ${title}`);
    target.click();
  }, "失败树：断线支路");
  await page.waitForFunction(() =>
    [...document.querySelectorAll("[role='alert']")]
      .some((element) => element.textContent?.includes("对话树读取失败（smoke）")),
  );
  assert.equal(await isVisible(".visible-error[role='alert']"), true, "tree load error should be visible");
  assert.equal(await count(".stage-head"), 0, "failed tree must not retain the previous flow title");
  assert.ok(brokenDialogueCalls >= 1, "selecting the broken tree should request its data");

  // Every object kind exposed by the archive contract must be addressable through URL state.
  const archiveDeepLinks = [
    ["entity", "entity:smoke-archivist", "雾港档案员"],
    ["quest", "quest:smoke-quest", "雾港通行证"],
    ["region", "region:smoke-region", "雾港"],
    ["poi", "poi:smoke-poi", "航路档案馆"],
    ["term", "term:smoke-term", "雾灯"],
    ["dialogue", "dialogue:smoke-line", "dialogue.guard.opening"],
    ["dialogue_tree", "dialogue_tree:dialogue-ok", "旧树：港口问讯"],
    [
      "relation",
      "relation:smoke-archivist:documents:smoke-region",
      "smoke-archivist → smoke-region",
    ],
    ["style_guide", "style_guide:smoke-style-guide", "smoke-style-guide"],
    ["quest_event_ref", "quest_event_ref:smoke-qer", "smoke-quest → smoke-event"],
    ["style_rubric", "style_rubric:smoke-rubric", "烟港叙事量表"],
  ];
  for (const [kind, focus, expectedTitle] of archiveDeepLinks) {
    await page.goto(
      `${origin}/archive?kind=${encodeURIComponent(kind)}&focus=${encodeURIComponent(focus)}&page=1`,
      { waitUntil: "networkidle0" },
    );
    await page.waitForFunction(
      (title) => document.querySelector("[role='dialog'] .mh-title")?.textContent?.trim() === title,
      {},
      expectedTitle,
    );
    await page.keyboard.press("Escape");
    await page.waitForFunction(() => document.querySelectorAll("[role='dialog']").length === 0);
  }

  // Confirm overlays are stacked modals. Escape closes only the top confirm, preserving the
  // underlying detail and its body scroll lock until a second Escape closes that detail.
  await page.goto(`${origin}/archive`, { waitUntil: "networkidle0" });
  await page.waitForFunction(() => document.querySelector("h1")?.textContent?.includes("设定档案"));
  await page.click("table tbody tr.row[role='button']");
  await page.waitForFunction(() => document.querySelectorAll("[role='dialog']").length === 1);
  assert.deepEqual(
    await page.$$eval("[role='dialog'] .mh-title", (titles) => titles.map((title) => title.textContent?.trim())),
    ["雾港档案员"],
    "archive row should open its detail dialog",
  );
  await page.click("[role='dialog'] .modal-foot .danger");
  await page.waitForFunction(() => document.querySelectorAll("[role='dialog']").length === 2);
  const stackedModalState = await page.evaluate(() => ({
    count: document.body.dataset.owModalCount,
    locked: document.body.classList.contains("modal-open"),
    overflow: getComputedStyle(document.body).overflow,
    titles: [...document.querySelectorAll("[role='dialog'] .mh-title")]
      .map((title) => title.textContent?.trim()),
  }));
  assert.deepEqual(stackedModalState.titles, ["雾港档案员", "删除设定实体"]);
  assert.equal(stackedModalState.count, "2", "two open modals should hold two body locks");
  assert.equal(stackedModalState.locked, true, "stacked modals should lock body scrolling");
  assert.equal(stackedModalState.overflow, "hidden", "stacked modals should hide body overflow");

  await page.keyboard.press("Escape");
  await page.waitForFunction(() => document.querySelectorAll("[role='dialog']").length === 1);
  const detailModalState = await page.evaluate(() => ({
    count: document.body.dataset.owModalCount,
    locked: document.body.classList.contains("modal-open"),
    overflow: getComputedStyle(document.body).overflow,
    title: document.querySelector("[role='dialog'] .mh-title")?.textContent?.trim(),
  }));
  assert.equal(detailModalState.title, "雾港档案员", "first Escape should keep the detail open");
  assert.equal(detailModalState.count, "1", "closing the confirm should retain one body lock");
  assert.equal(detailModalState.locked, true, "detail modal should keep body scrolling locked");
  assert.equal(detailModalState.overflow, "hidden", "detail modal should keep overflow hidden");

  await page.keyboard.press("Escape");
  await page.waitForFunction(() => document.querySelectorAll("[role='dialog']").length === 0);
  const closedModalState = await page.evaluate(() => ({
    count: document.body.dataset.owModalCount ?? null,
    locked: document.body.classList.contains("modal-open"),
    overflow: getComputedStyle(document.body).overflow,
  }));
  assert.equal(closedModalState.count, null, "last modal should release the body lock count");
  assert.equal(closedModalState.locked, false, "last modal should unlock body scrolling");
  assert.notEqual(closedModalState.overflow, "hidden", "body overflow should be restored");

  // A stream can disconnect while the server job keeps running. The channel must survive a hard
  // reload and reconnect to the same job id instead of submitting duplicate work.
  await page.goto(`${origin}/agent`, { waitUntil: "networkidle0" });
  await page.type(".form textarea", "验证刷新恢复");
  await page.click(".form > .primary");
  await page.waitForFunction(() => {
    const key = "owcopilot_job_v1:demo:agent_diagnose";
    const saved = JSON.parse(sessionStorage.getItem(key) ?? "null");
    return saved?.jobId === "smoke-job" && saved?.recoverable === true;
  });
  await page.reload({ waitUntil: "networkidle0" });
  await page.waitForFunction(() => document.body.textContent?.includes("刷新后恢复成功"));
  assert.equal(jobStatusCalls, 2, "recovery should query the existing job exactly once after reload");

  assert.ok(observedApiUrls.length > 0, "browser smoke must observe API traffic");
  const crossOriginApiUrls = observedApiUrls.filter((url) => new URL(url).origin !== origin);
  assert.deepEqual(
    crossOriginApiUrls,
    [],
    `API requests must remain same-origin with the workbench (${origin})`,
  );
  assert.deepEqual(runtimeErrors, [], `browser runtime errors:\n${runtimeErrors.join("\n")}`);
  console.log(
    `Browser smoke passed on ${origin}: same-origin API, desktop/mobile shell, SPA 404, ` +
    "timeline/dialogue failure UX, modal stacking, and hard-refresh job recovery.",
  );
} finally {
  await browser?.close();
  await new Promise((resolveClose, rejectClose) => {
    server.httpServer.close((error) => error ? rejectClose(error) : resolveClose());
  });
}
