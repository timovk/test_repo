#!/usr/bin/env node
/**
 * Headless UI smoke-test / screenshot tool (development aid).
 *
 *   node scripts/ui_snapshot.mjs http://127.0.0.1:8000 out_dir "#/night" "#/house" ...
 *
 * For every route: loads the page, waits for network idle, saves <out_dir>/<name>.png and prints
 * console errors / failed requests as JSON lines.  Exit code 1 if any page logged an error.
 * Optional env: WIDTH, HEIGHT, THEME=light|dark, WAIT_MS (extra settle time, default 1500).
 */
import fs from "node:fs";
import path from "node:path";
import { createRequire } from "node:module";
import { execSync } from "node:child_process";

// Resolve Playwright from a local install or the global npm root (ESM ignores NODE_PATH).
const require = createRequire(import.meta.url);
let pw;
try {
  pw = require("playwright");
} catch {
  pw = require(path.join(execSync("npm root -g").toString().trim(), "playwright"));
}
const { chromium } = pw;

const [base, outDir, ...routes] = process.argv.slice(2);
if (!base || !outDir) {
  console.error("usage: ui_snapshot.mjs <base-url> <out-dir> [#/route ...]");
  process.exit(2);
}
fs.mkdirSync(outDir, { recursive: true });
const browser = await chromium.launch();
const context = await browser.newContext({
  viewport: { width: Number(process.env.WIDTH || 1600), height: Number(process.env.HEIGHT || 1000) },
  deviceScaleFactor: 1,
});
if (process.env.THEME) await context.addInitScript((t) => localStorage.setItem("nlfed.theme", t), process.env.THEME);
let failed = false;
for (const route of routes.length ? routes : ["#/night"]) {
  const page = await context.newPage();
  const problems = [];
  page.on("console", (m) => m.type() === "error" && problems.push({ type: "console", text: m.text() }));
  page.on("pageerror", (e) => problems.push({ type: "pageerror", text: String(e) }));
  page.on("requestfailed", (r) => problems.push({ type: "requestfailed", url: r.url(), text: r.failure()?.errorText }));
  page.on("response", (r) => r.status() >= 400 && problems.push({ type: "http", url: r.url(), status: r.status() }));
  const url = `${base.replace(/\/$/, "")}/${route.startsWith("#") ? route : `#${route}`}`;
  await page.goto(url, { waitUntil: "networkidle", timeout: 60000 }).catch((e) => problems.push({ type: "goto", text: String(e) }));
  await page.waitForTimeout(Number(process.env.WAIT_MS || 1500));
  const name = route.replace(/[^a-z0-9]+/gi, "_").replace(/^_|_$/g, "") || "root";
  await page.screenshot({ path: path.join(outDir, `${name}.png`), fullPage: true });
  console.log(JSON.stringify({ route, screenshot: path.join(outDir, `${name}.png`), problems }));
  if (problems.some((p) => p.type !== "http" || p.status >= 500)) failed = true;
  await page.close();
}
await browser.close();
process.exit(failed ? 1 : 0);
