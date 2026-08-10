import { existsSync, readFileSync, readdirSync } from "node:fs";
import { basename, dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { gzipSync } from "node:zlib";

const KiB = 1024;
const limits = {
  entry: { raw: 190 * KiB, gzip: 70 * KiB },
  nonGraphChunk: { raw: 160 * KiB, gzip: 60 * KiB },
  graphTotal: { raw: 1536 * KiB, gzip: 420 * KiB },
};

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const dist = resolve(root, "dist");
const indexPath = resolve(dist, "index.html");
const assetsPath = resolve(dist, "assets");

if (!existsSync(indexPath) || !existsSync(assetsPath)) {
  throw new Error("Missing frontend build. Run `npm run build` before the bundle budget.");
}

const indexHtml = readFileSync(indexPath, "utf8");
const entryMatch = indexHtml.match(/<script\b[^>]*\bsrc=["']([^"']+\.js)["'][^>]*>/i);
if (!entryMatch) throw new Error("Could not identify the JavaScript entry in dist/index.html.");

const measurements = readdirSync(assetsPath)
  .filter((name) => name.endsWith(".js"))
  .map((name) => {
    const bytes = readFileSync(resolve(assetsPath, name));
    return { name, raw: bytes.byteLength, gzip: gzipSync(bytes).byteLength };
  });
const entryName = basename(entryMatch[1].split(/[?#]/, 1)[0]);
const entry = measurements.find((asset) => asset.name === entryName);
if (!entry) throw new Error(`Entry ${entryName} referenced by index.html is missing.`);

const graphChunks = measurements.filter((asset) => /^GraphPage-[A-Za-z0-9_-]+\.js$/.test(asset.name));
if (graphChunks.length === 0) {
  throw new Error("GraphPage chunk was not found; update its explicit budget classification.");
}
const nonGraphChunks = measurements.filter(
  (asset) => asset.name !== entry.name && !graphChunks.includes(asset),
);
const graphTotal = graphChunks.reduce(
  (total, asset) => ({ raw: total.raw + asset.raw, gzip: total.gzip + asset.gzip }),
  { raw: 0, gzip: 0 },
);

const failures = [];
function check(label, measured, budget) {
  if (measured.raw > budget.raw) {
    failures.push(`${label} raw ${format(measured.raw)} > ${format(budget.raw)}`);
  }
  if (measured.gzip > budget.gzip) {
    failures.push(`${label} gzip ${format(measured.gzip)} > ${format(budget.gzip)}`);
  }
}

function format(bytes) {
  return `${(bytes / KiB).toFixed(1)} KiB`;
}

check(`entry ${entry.name}`, entry, limits.entry);
for (const asset of nonGraphChunks) {
  check(`non-graph chunk ${asset.name}`, asset, limits.nonGraphChunk);
}
check(`graph chunks (${graphChunks.map((asset) => asset.name).join(", ")})`, graphTotal, limits.graphTotal);

if (failures.length > 0) {
  throw new Error(`Bundle budget exceeded:\n- ${failures.join("\n- ")}`);
}

const largestNonGraph = nonGraphChunks.reduce(
  (largest, asset) => (!largest || asset.raw > largest.raw ? asset : largest),
  null,
);
console.log(
  [
    `Bundle budget passed: entry ${format(entry.raw)} raw / ${format(entry.gzip)} gzip`,
    largestNonGraph
      ? `largest non-graph ${largestNonGraph.name} ${format(largestNonGraph.raw)} raw / ${format(largestNonGraph.gzip)} gzip`
      : "no non-graph route chunks",
    `graph total ${format(graphTotal.raw)} raw / ${format(graphTotal.gzip)} gzip`,
  ].join("; "),
);
