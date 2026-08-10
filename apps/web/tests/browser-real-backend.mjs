import assert from "node:assert/strict";
import { once } from "node:events";
import {
  cpSync,
  existsSync,
  mkdtempSync,
  mkdirSync,
  readdirSync,
  statSync,
} from "node:fs";
import { rm } from "node:fs/promises";
import { createServer } from "node:net";
import { dirname, join, resolve } from "node:path";
import process from "node:process";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";

import puppeteer from "puppeteer-core";

const webRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const repoRoot = resolve(webRoot, "..", "..");
const distRoot = resolve(webRoot, "dist");
const sourceProject = resolve(repoRoot, "demo", "honglou");

function newestMtimeMs(target) {
  const metadata = statSync(target);
  if (!metadata.isDirectory()) return metadata.mtimeMs;
  return readdirSync(target, { withFileTypes: true }).reduce((newest, entry) => {
    const child = resolve(target, entry.name);
    return Math.max(newest, entry.isDirectory() ? newestMtimeMs(child) : statSync(child).mtimeMs);
  }, metadata.mtimeMs);
}

function requireFreshBuild() {
  const index = resolve(distRoot, "index.html");
  assert.ok(existsSync(index), "Missing dist/index.html; run `npm run build` first.");
  assert.ok(existsSync(sourceProject), `Missing real-browser fixture: ${sourceProject}`);
  const newestSource = Math.max(
    newestMtimeMs(resolve(webRoot, "src")),
    statSync(resolve(webRoot, "package.json")).mtimeMs,
  );
  assert.ok(
    statSync(index).mtimeMs >= newestSource,
    "Frontend build is stale; run `npm run build` before the real-backend browser smoke.",
  );
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
  if (!executable) throw new Error("No Chrome/Edge executable found; set PUPPETEER_EXECUTABLE_PATH.");
  return executable;
}

function pythonExecutable() {
  const candidates = [
    process.env.OWCOPILOT_PYTHON,
    resolve(repoRoot, ".tmp", "fastapi-venv", "Scripts", "python.exe"),
    resolve(repoRoot, ".venv", "Scripts", "python.exe"),
    resolve(repoRoot, ".tmp", "fastapi-venv", "bin", "python"),
    resolve(repoRoot, ".venv", "bin", "python"),
  ].filter(Boolean);
  return candidates.find((candidate) => existsSync(candidate))
    ?? (process.platform === "win32" ? "python" : "python3");
}

async function reservePort() {
  const probe = createServer();
  await new Promise((resolveListen, rejectListen) => {
    probe.once("error", rejectListen);
    probe.listen(0, "127.0.0.1", resolveListen);
  });
  const address = probe.address();
  assert.ok(address && typeof address !== "string");
  await new Promise((resolveClose, rejectClose) => {
    probe.close((error) => error ? rejectClose(error) : resolveClose());
  });
  return address.port;
}

function delay(milliseconds) {
  return new Promise((resolveDelay) => setTimeout(resolveDelay, milliseconds));
}

async function waitForHealth(origin, child, output) {
  const deadline = Date.now() + 30_000;
  while (Date.now() < deadline) {
    if (child.exitCode !== null) {
      throw new Error(`FastAPI exited early (${child.exitCode}).\n${output()}`);
    }
    try {
      const response = await fetch(`${origin}/health`);
      if (response.ok) return;
    } catch {
      // The TCP listener is not ready yet.
    }
    await delay(150);
  }
  throw new Error(`FastAPI did not become healthy within 30 seconds.\n${output()}`);
}

async function stopChild(child) {
  if (!child || child.exitCode !== null) return;
  // Register before signalling: on Windows ChildProcess can emit `exit` synchronously enough for
  // an `once()` installed after kill() to miss it, leaving top-level await unsettled even though
  // the browser assertions passed.
  const gracefulExit = Promise.race([
    once(child, "exit").then(() => true),
    delay(5_000).then(() => false),
  ]);
  child.kill("SIGTERM");
  if (await gracefulExit || child.exitCode !== null) return;
  const forcedExit = Promise.race([
    once(child, "exit").then(() => true),
    delay(5_000).then(() => false),
  ]);
  child.kill("SIGKILL");
  assert.ok(await forcedExit || child.exitCode !== null, "FastAPI child did not exit after SIGKILL");
}

requireFreshBuild();
// Keep disposable state inside the repository workspace. The host-global temp directory can be
// owned by a different Windows sandbox identity, which allows file creation but denies deleting
// the root directory and turns an otherwise successful smoke into a false cleanup failure.
const tempBase = resolve(repoRoot, ".tmp", "browser-real");
mkdirSync(tempBase, { recursive: true });
const tempRoot = mkdtempSync(join(tempBase, "run-"));
const projectRoot = resolve(tempRoot, "project");
cpSync(sourceProject, projectRoot, { recursive: true });
const port = await reservePort();
const origin = `http://127.0.0.1:${port}`;
let server;
let browser;
let serverOutput = "";

try {
  const currentPythonPath = process.env.PYTHONPATH;
  server = spawn(
    pythonExecutable(),
    [
      "-m",
      "uvicorn",
      "owcopilot.service.api:app",
      "--host",
      "127.0.0.1",
      "--port",
      String(port),
      "--log-level",
      "warning",
    ],
    {
      cwd: repoRoot,
      env: {
        ...process.env,
        PYTHONPATH: [resolve(repoRoot, "src"), currentPythonPath].filter(Boolean).join(process.platform === "win32" ? ";" : ":"),
        OWCOPILOT_DEPLOYMENT_PROFILE: "local",
        OWCOPILOT_PROJECTS_JSON: JSON.stringify({ demo: projectRoot }),
        OWCOPILOT_PLATFORM_DB: resolve(tempRoot, "platform.sqlite"),
        OWCOPILOT_FRONTEND_DIST: distRoot,
        OWCOPILOT_LLM_MODE: "offline",
        OWCOPILOT_ALLOW_OFFLINE_LLM: "1",
        HF_HUB_OFFLINE: "1",
        TRANSFORMERS_OFFLINE: "1",
      },
      stdio: ["ignore", "pipe", "pipe"],
    },
  );
  const capture = (chunk) => {
    serverOutput = `${serverOutput}${chunk.toString("utf8")}`.slice(-32_000);
  };
  server.stdout.on("data", capture);
  server.stderr.on("data", capture);
  await waitForHealth(origin, server, () => serverOutput);

  browser = await puppeteer.launch({
    executablePath: browserExecutable(),
    headless: true,
    args: ["--no-sandbox", "--disable-dev-shm-usage"],
  });
  const page = await browser.newPage();
  page.setDefaultTimeout(20_000);
  await page.setViewport({ width: 1440, height: 900, deviceScaleFactor: 1 });
  const runtimeErrors = [];
  const apiResponses = [];
  page.on("pageerror", (error) => runtimeErrors.push(error.message));
  page.on("console", (message) => {
    if (message.type() === "error") runtimeErrors.push(message.text());
  });
  page.on("response", (response) => {
    const url = new URL(response.url());
    if (url.origin !== origin || response.request().resourceType() === "document") return;
    if (url.pathname.startsWith("/assets/")) return;
    apiResponses.push({ method: response.request().method(), path: url.pathname, status: response.status() });
  });
  await page.evaluateOnNewDocument(() => {
    localStorage.setItem("owcopilot_project", "demo");
    localStorage.setItem("owcopilot_tour_done", "1");
  });

  await page.goto(`${origin}/overview`, { waitUntil: "networkidle0" });
  await page.waitForFunction(() => document.querySelector("h1")?.textContent?.includes("世界总览"));
  assert.equal(await page.$eval("main", (element) => element.getAttribute("aria-busy")), null);
  assert.ok(
    await page.$eval("main", (element) => element.textContent?.includes("实体")),
    "overview should render real project counts",
  );

  await page.goto(`${origin}/archive`, { waitUntil: "networkidle0" });
  await page.waitForFunction(() => document.querySelector("h1")?.textContent?.includes("设定档案"));
  await page.waitForFunction(() => document.querySelectorAll("table tbody tr").length > 0);
  assert.ok(await page.$$eval("table tbody tr", (rows) => rows.length) > 0);

  const requiredPaths = ["/health", "/workspaces", "/settings/connection", "/projects/demo/overview", "/projects/demo/archive"];
  for (const path of requiredPaths) {
    assert.ok(apiResponses.some((entry) => entry.path === path), `browser never reached real API ${path}`);
  }
  const failed = apiResponses.filter((entry) => entry.status >= 400);
  assert.deepEqual(failed, [], `real API failures: ${JSON.stringify(failed)}`);
  assert.deepEqual(runtimeErrors, [], `browser runtime errors:\n${runtimeErrors.join("\n")}`);

  const schemaResponse = await fetch(`${origin}/openapi.json`);
  assert.equal(schemaResponse.status, 200);
  const schema = await schemaResponse.json();
  const untagged = Object.entries(schema.paths).flatMap(([path, item]) =>
    Object.entries(item)
      .filter(([method, operation]) => ["get", "post", "put", "patch", "delete"].includes(method) && !operation.tags?.length)
      .map(([method]) => `${method.toUpperCase()} ${path}`),
  );
  assert.deepEqual(untagged, [], `OpenAPI operations without domain tags: ${untagged.join(", ")}`);

  console.log(
    `Real-backend browser smoke passed on ${origin}: built Vue → Uvicorn/FastAPI → `
      + `temporary Honglou project (${apiResponses.length} API responses).`,
  );
} finally {
  await browser?.close();
  await stopChild(server);
  // SQLite/PDFium handles can take a short moment to disappear after a forced Windows process
  // exit. Node's bounded retry handles EPERM/EBUSY without hiding a permanently leaked handle.
  await rm(tempRoot, { recursive: true, force: true, maxRetries: 20, retryDelay: 250 });
}
