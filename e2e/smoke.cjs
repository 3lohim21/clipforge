// Post-deploy smoke test: drives the real UI in Chromium through the whole clipping journey.
//
//   BASE=https://clips.example.com EMAIL=you@example.com PASSWORD=... \
//   VIDEO_URL=https://<a short public video link> node e2e/smoke.cjs
//
// Requires Playwright (`npm i -D playwright && npx playwright install chromium`).
// Creates a project named "Smoke test <timestamp>" — delete it afterwards in Settings.
const { chromium } = require("playwright");

const BASE = process.env.BASE ?? "http://localhost:8000";
const EMAIL = process.env.EMAIL;
const PASSWORD = process.env.PASSWORD;
const VIDEO_URL = process.env.VIDEO_URL;
const SHOTS = process.env.SHOTS; // optional directory for screenshots

(async () => {
  if (!EMAIL || !PASSWORD || !VIDEO_URL) throw new Error("set EMAIL, PASSWORD and VIDEO_URL");
  const errors = [];
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  page.on("pageerror", (e) => errors.push(`pageerror: ${e.message}`));
  page.on("response", (r) => {
    // The signed-out probe of /api/auth/me is expected to 401 before login.
    if (r.status() >= 400 && !(r.status() === 401 && r.url().endsWith("/api/auth/me"))) {
      errors.push(`HTTP ${r.status()} ${r.request().method()} ${r.url()}`);
    }
  });
  const step = (s) => console.log(`✓ ${s}`);
  const shot = async (name) => SHOTS && page.screenshot({ path: `${SHOTS}/${name}.png` });

  await page.goto(`${BASE}/`);
  await page.waitForURL(/\/login/);
  await page.getByLabel("Email").fill(EMAIL);
  await page.getByLabel("Password").fill(PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.getByRole("heading", { name: "Clients & campaigns" }).waitFor();
  step("signed in");

  const name = `Smoke test ${new Date().toISOString().slice(0, 16)}`;
  await page.getByRole("button", { name: "New project" }).first().click();
  await page.getByLabel("Client or campaign name").fill(name);
  await page.getByRole("button", { name: "Create" }).click();
  await page.getByRole("heading", { name }).waitFor();
  step("project created");

  await page.getByLabel(/Video links/).fill(VIDEO_URL);
  await page.getByRole("button", { name: /Import & find clips/ }).click();
  const t0 = Date.now();
  await page.getByRole("link", { name: /Open editor/ }).waitFor({ timeout: 20 * 60_000 });
  step(`imported + analysed in ${((Date.now() - t0) / 1000).toFixed(1)}s`);
  await shot("1-project");

  await page.getByRole("link", { name: /Open editor/ }).click();
  await page.locator(".moment").first().waitFor();
  step(`editor: ${await page.locator(".moment").count()} AI moments`);
  await page.locator(".moment").first().click();
  await page.locator(".preview-916 canvas").waitFor();
  await page.waitForTimeout(2000);
  await shot("2-editor");

  // Looping must survive editor re-renders during playback (regression guard).
  const videoId = new URL(page.url()).pathname.split("/").pop();
  const clipId = new URL(page.url()).searchParams.get("clip");
  const clip = (await (await page.request.get(`${BASE}/api/videos/${videoId}/clips`)).json()).find((c) => c.id === clipId);
  await page.evaluate((t) => { document.querySelector("video").currentTime = t; }, clip.end_ms / 1000 - 1.5);
  await page.waitForTimeout(400);
  if (await page.evaluate(() => document.querySelector("video").paused)) await page.keyboard.press("Space");
  await page.keyboard.press("i");
  await page.waitForTimeout(2600);
  const t = await page.evaluate(() => document.querySelector("video").currentTime * 1000);
  if (t < clip.start_ms || t > clip.end_ms) throw new Error(`clip did not loop (playhead ${Math.round(t)} ms)`);
  await page.keyboard.press("Space");
  step("clip preview loops");

  await page.keyboard.press("e");
  await page.locator(".export-row a[download]").first().waitFor({ timeout: 10 * 60_000 });
  const href = await page.locator(".export-row a[download]").first().getAttribute("href");
  const res = await page.request.get(`${BASE}${href}`);
  if (res.status() !== 200 || !res.headers()["content-type"]?.includes("video/mp4")) throw new Error("download failed");
  step(`export downloaded (${((await res.body()).length / 1024 / 1024).toFixed(1)} MB)`);
  await shot("3-exported");

  await browser.close();
  if (errors.length) {
    console.error(`\nUnexpected errors:\n${errors.join("\n")}`);
    process.exit(1);
  }
  console.log("\nAll smoke checks passed.");
})().catch((e) => {
  console.error(e);
  process.exit(2);
});
