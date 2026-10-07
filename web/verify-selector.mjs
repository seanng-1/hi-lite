// SPDX-License-Identifier: GPL-3.0-or-later
// Exercise the built UI in Chromium using its DevTools pipe; no test dependency.
// Run `npm test`. Set CHROME_BIN if Chromium is not in the Playwright cache.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { existsSync, readFileSync, readdirSync } from "node:fs";
import { mkdtemp, mkdir, rm } from "node:fs/promises";
import { createServer } from "node:http";
import { homedir, tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

const fixture = JSON.parse(readFileSync(new URL("../tests/fixtures/segments.json", import.meta.url), "utf8"));
const cache = join(homedir(), ".cache", "ms-playwright");
const candidates = [process.env.CHROME_BIN, "/usr/bin/chromium", "/usr/bin/chromium-browser", "/usr/bin/google-chrome"];
if (existsSync(cache)) {
  for (const dir of readdirSync(cache).filter(dir => dir.startsWith("chromium-"))) {
    candidates.push(join(cache, dir, "chrome-linux64", "chrome"));
  }
}
const chrome = candidates.find(path => path && existsSync(path));
assert.ok(chrome, "Set CHROME_BIN to a Chromium executable to run browser checks.");
const dist = fileURLToPath(new URL("dist/", import.meta.url));
assert.ok(existsSync(join(dist, "index.html")), "Run npm run build first.");
const server = createServer((request, response) => {
  const path = new URL(request.url, "http://localhost").pathname;
  const file = join(dist, path === "/" ? "index.html" : path);
  if (!file.startsWith(dist) || !existsSync(file)) {
    response.writeHead(404).end();
    return;
  }
  response.setHeader("Content-Type", file.endsWith(".js") ? "text/javascript" : "text/html");
  response.end(readFileSync(file));
});
await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
const url = `http://127.0.0.1:${server.address().port}/`;
const profile = await mkdtemp(join(tmpdir(), "hi-lite-a1-browser-"));
const browser = spawn(chrome, ["--headless", "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
  "--no-first-run", "--no-default-browser-check", "--remote-debugging-pipe", `--user-data-dir=${profile}`],
  { stdio: ["ignore", "ignore", "pipe", "pipe", "pipe"] });
const pending = new Map();
const exceptions = [];
let sequence = 0;
let buffer = "";
let stderr = "";
browser.stderr.on("data", chunk => { stderr = (stderr + chunk).slice(-4000); });
browser.stdio[4].setEncoding("utf8");
browser.stdio[4].on("data", chunk => {
  buffer += chunk;
  let end;
  while ((end = buffer.indexOf("\0")) >= 0) {
    const message = JSON.parse(buffer.slice(0, end));
    buffer = buffer.slice(end + 1);
    if (message.id) {
      const task = pending.get(message.id);
      if (!task) continue;
      pending.delete(message.id);
      clearTimeout(task.timer);
      if (message.error) task.reject(new Error(message.error.message));
      else task.resolve(message.result);
    } else if (message.method === "Runtime.exceptionThrown") {
      exceptions.push(message.params.exceptionDetails);
    }
  }
});
browser.on("exit", code => {
  for (const task of pending.values()) {
    clearTimeout(task.timer);
    task.reject(new Error(`Chromium exited (${code}): ${stderr}`));
  }
  pending.clear();
});
function send(method, params = {}, sessionId) {
  return new Promise((resolve, reject) => {
    const id = ++sequence;
    const timer = setTimeout(() => {
      pending.delete(id);
      reject(new Error(`DevTools timeout: ${method}. ${stderr}`));
    }, 15000);
    pending.set(id, { resolve, reject, timer });
    browser.stdio[3].write(JSON.stringify({ id, method, params, sessionId }) + "\0");
  });
}

try {
  const { targetId } = await send("Target.createTarget", { url: "about:blank" });
  const { sessionId } = await send("Target.attachToTarget", { targetId, flatten: true });
  const page = (method, params) => send(method, params, sessionId);
  await page("Page.enable");
  await page("Runtime.enable");
  await page("Emulation.setDeviceMetricsOverride", { width: 1440, height: 1100, deviceScaleFactor: 1, mobile: false });
  async function evaluate(expression) {
    const result = await page("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true });
    if (result.exceptionDetails) throw new Error(JSON.stringify(result.exceptionDetails));
    return result.result.value;
  }
  async function until(expression) {
    for (let attempt = 0; attempt < 500; attempt++) {
      if (await evaluate(expression)) return;
      await new Promise(resolve => setTimeout(resolve, 20));
    }
    throw new Error(`UI did not settle: ${expression}`);
  }
  async function click(selector) {
    const { x, y } = await evaluate(`(() => {
      const element = document.querySelector(${JSON.stringify(selector)});
      element.scrollIntoView({ block: 'center' });
      const rect = element.getBoundingClientRect();
      return { x: rect.x + rect.width / 2, y: rect.y + rect.height / 2 };
    })()`);
    await page("Input.dispatchMouseEvent", { type: "mousePressed", x, y, button: "left", clickCount: 1 });
    await page("Input.dispatchMouseEvent", { type: "mouseReleased", x, y, button: "left", clickCount: 1 });
  }
  async function press(key, code, windowsVirtualKeyCode) {
    await page("Input.dispatchKeyEvent", { type: "keyDown", key, code, windowsVirtualKeyCode,
      text: key === 'Enter' ? '\r' : key.length === 1 ? key : undefined });
    await page("Input.dispatchKeyEvent", { type: "keyUp", key, code, windowsVirtualKeyCode });
  }
  async function setScoring(instructions, threshold) {
    await evaluate(`
      document.getElementById('aiInstructions').value = ${JSON.stringify(instructions)};
      document.getElementById('aiInstructions').dispatchEvent(new Event('input', { bubbles: true }));
      document.getElementById('minScoreSlider').value = ${JSON.stringify(String(threshold))};
      document.getElementById('minScoreSlider').dispatchEvent(new Event('input', { bubbles: true }));
    `);
  }
  const scoringRequests = () => evaluate(`window.__requests
    .filter(r => ['/api/preview-auto-export', '/api/auto-generate'].includes(r.path))
    .map(r => ({ path: r.path, body: JSON.parse(r.options.body) }))`);
  async function observeReview() {
    // Keep the real source/seek setters, but don't play missing fixture media.
    await evaluate(`
      window.__reviews = [];
      window.__plays = 0;
      window.__seeks = [];
      const video = document.getElementById('mainVideo');
      const source = Object.getOwnPropertyDescriptor(HTMLMediaElement.prototype, 'src');
      const time = Object.getOwnPropertyDescriptor(HTMLMediaElement.prototype, 'currentTime');
      Object.defineProperty(video, 'src', {
        get() { return source.get.call(this); },
        set(value) { window.__reviews.push(value); source.set.call(this, value); }
      });
      Object.defineProperty(video, 'currentTime', {
        get() { return time.get.call(this); },
        set(value) { window.__seeks.push(value); time.set.call(this, value); }
      });
      video.play = () => { window.__plays++; return Promise.resolve(); };
    `);
  }
  let scriptId;
  let caseId = 0;
  async function load(body = fixture, { raw = false, status = 200, networkError = false, llmAvailable = false,
    llmSettings, llmStatusError = false } = {}) {
    if (scriptId) await page("Page.removeScriptToEvaluateOnNewDocument", { identifier: scriptId });
    caseId++;
    const { identifier } = await page("Page.addScriptToEvaluateOnNewDocument", { source: `
      window.__case = ${caseId};
      window.__fixture = ${JSON.stringify(fixture)};
      window.__manifest = ${JSON.stringify({ body: raw ? body : JSON.stringify(body), status, networkError })};
      window.__requests = [];
      window.__uploads = 0;
      window.__confirmCount = 0;
      window.__injected = false;
      window.__alerts = [];
      window.__allowAlerts = ${llmAvailable};
      window.__llmStatusError = ${JSON.stringify(llmStatusError)};
      window.__llmSettings = ${JSON.stringify(llmSettings ?? {
        resolved_backend: llmAvailable ? "api" : null, backend_override: "", base_url: "https://fixture.invalid/v1",
        model: "fixture-model", has_api_key: llmAvailable, local_url: "http://127.0.0.1:11434",
        local_model: "fixture-local", local_available: false
      })};
      window.confirm = () => { window.__confirmCount++; return true; };
      window.alert = message => {
        if (!window.__allowAlerts) throw new Error(message);
        window.__alerts.push(message);
      };
      const nativeTimeout = window.setTimeout;
      window.setTimeout = (fn, delay, ...args) => nativeTimeout(fn, delay === 2000 || delay === 3000 ? 0 : delay, ...args);
      const nativeFetch = window.fetch;
      window.fetch = async (input, options) => {
        const path = new URL(input, location.href).pathname;
        window.__requests.push({ path, options });
        const json = (data, status = 200) => new Response(JSON.stringify(data), { status, headers: { "Content-Type": "application/json" } });
        if (window.__busyPaths?.includes(path)) return json({ status: "error", message: "Hi-Lite is busy processing another operation. Please wait for it to finish." }, 409);
        if (window.__errors?.[path]) return json(window.__errors[path].body, window.__errors[path].status);
        if (path === "/data/segments_manifest.json") {
          if (window.__manifest.networkError) throw new TypeError("Network unavailable");
          return new Response(window.__manifest.body, { status: window.__manifest.status });
        }
        if (path === "/api/subtitles") return json({ available: true, message: "Original recording subtitles ready" });
        if (path.startsWith("/api/subtitles/")) return new Response(path.endsWith("vtt")
          ? 'WEBVTT\\n\\n00:00:00.000 --> 00:00:01.000\\n日本 &amp; text\\n\\n'
          : '1\\n00:00:00,000 --> 00:00:01,000\\n日本 & text\\n\\n');
        if (path === "/api/llm-settings") {
          if (options?.method === 'POST') return json({ status: 'ok' });
          if (window.__llmStatusError === 'network') throw new TypeError('Status unavailable');
          if (window.__llmStatusError) return json({ message: 'Status unavailable' }, 503);
          return json(window.__llmSettings);
        }
        if (path === "/api/llm-test") return json(window.__llmTestResult ?? { status: 'ok', backend: 'api', model: 'fixture-model' });
        if (path === "/api/auto-generate") return json({ status: "ok", task_id: "fixture-auto-task" });
        if (path === "/api/export") {
          if (window.__holdExport) return new Promise(resolve => {
            window.__finishExport = () => resolve(json({ status: "ok", output_file: "/final/manual.mp4" }));
          });
          return json({ status: "ok", output_file: "/final/manual.mp4" });
        }
        if (path === "/api/discard-reel") return json({ status: "ok" });
        if (path === "/api/processing-status/fixture-auto-task") {
          if (window.__autoError) return json({ status: "error", message: window.__autoError });
          if (window.__holdAutoPoll) {
            if (!window.__scoringShown) {
              window.__scoringShown = true;
              return json({ status: "processing", message: "Scoring fixture..." });
            }
            return new Promise(resolve => {
              window.__finishAuto = () => resolve(json({ status: "complete", output_file: "/final/new-auto.mp4", highlights_used: 1, total_available: 3, file_size_mb: 1 }));
            });
          }
          return json({ status: "complete", output_file: "/final/auto.mp4", highlights_used: 1, total_available: 3, file_size_mb: 1 });
        }
        if (path === "/api/upload") {
          window.__uploads++;
          if (window.__uploadError) return json({ status: "error", message: "Upload rejected" });
          window.__manifest = { body: JSON.stringify(window.__nextManifest ?? window.__fixture), status: 200 };
          return json({ status: "ok", task_id: "fixture-task" });
        }
        if (path === "/api/processing-status/fixture-task") {
          if (window.__pollError === "network") throw new TypeError("Polling failed");
          if (window.__pollError === "json") return new Response("{broken");
          if (window.__pollError === "http") return new Response("error", { status: 500 });
          return json(window.__processingStatus ?? { status: "complete", progress: 100 });
        }
        if (path === "/api/preview-auto-export") {
          const result = { status: "ok", count: '<img onerror="window.__injected=true">', total_available: "<b>literal</b>", estimated_duration_seconds: 4.5 };
          if (window.__holdStatistics) return new Promise(resolve => {
            window.__finishStatistics = () => resolve(json(result));
          });
          return json(result);
        }
        return nativeFetch(input, options);
      };
    ` });
    scriptId = identifier;
    await page("Page.navigate", { url: `${url}?case=${caseId}` });
    await until(`window.__case === ${caseId} && !!document.querySelector('.upload-container, .main-container')`);
  }
  const ids = () => evaluate("[...document.querySelectorAll('.segment-row .segment-id')].map(el => Number(el.textContent))");
  const selected = () => evaluate("[...document.querySelectorAll('.segment-checkbox:checked')].map(el => Number(el.dataset.id))");
  async function upload() {
    await evaluate(`(() => {
      const input = document.getElementById('videoInput');
      const files = new DataTransfer();
      files.items.add(new File(['fixture'], 'fixture.mp4', { type: 'video/mp4' }));
      input.files = files.files;
      input.dispatchEvent(new Event('change'));
      input.dispatchEvent(new Event('change'));
    })()`);
    await until("!!document.querySelector('.main-container') || document.getElementById('uploadButton')?.disabled === false");
  }

  await test("original recording subtitles download repeatedly and ignore highlight controls", async () => {
    const downloads = join(profile, "downloads");
    await mkdir(downloads, { recursive: true });
    await send("Browser.setDownloadBehavior", { behavior: "allow", downloadPath: downloads });
    await load();
    for (const format of ["srt", "vtt", "srt"]) {
      const selector = `button[data-format="${format}"]`;
      await until(`document.querySelector('${selector}')?.disabled === false`);
      const file = join(downloads, `original-recording.${format}`);
      await rm(file, { force: true });
      await click(selector);
      for (let retry = 0; retry < 200 && !existsSync(file); retry++) {
        await new Promise(resolve => setTimeout(resolve, 20));
      }
      assert.ok(existsSync(file), "Browser saved the subtitle attachment");
      assert.equal(readFileSync(file, "utf8"), format === "vtt"
        ? 'WEBVTT\n\n00:00:00.000 --> 00:00:01.000\n日本 &amp; text\n\n'
        : '1\n00:00:00,000 --> 00:00:01,000\n日本 & text\n\n');
      await click("#selectKeywords");
      await setScoring("different criteria", 9);
    }
    await evaluate(`window.__errors = { '/api/subtitles': { status: 404, body: { available: false, message: 'No captions available' } } }`);
    await until(`document.querySelector('section [role="status"]')?.textContent === 'No captions available'`);
    assert.equal(await evaluate(`document.querySelector('button[data-format="srt"]').disabled`), true);
    await evaluate(`window.__errors = {}`);
    await until(`document.querySelector('button[data-format="srt"]').disabled === false`);
    await evaluate(`window.__errors = { '/api/subtitles/srt': { status: 422, body: { message: 'Invalid transcript timing' } } }`);
    await click('button[data-format="srt"]');
    await until(`document.querySelector('section [role="status"]').textContent === 'Invalid transcript timing'`);
  });

  await test("missing and empty manifests open a styled, enabled import screen", async () => {
    for (const [body, status] of [[{ segments: [] }, 200], [{ segments: [] }, 404]]) {
      await load(body, { status });
      assert.equal(await evaluate("!!document.querySelector('.upload-container')"), true);
      assert.equal(await evaluate("document.getElementById('uploadButton').disabled"), false);
      assert.equal(await evaluate("getComputedStyle(document.querySelector('.upload-container')).display"), "flex");
      assert.equal(await evaluate("document.getElementById('statusMessage').classList.contains('visible')"), false);
      assert.equal(await evaluate("document.querySelectorAll('#selectorStyles').length"), 1);
    }
  });

  await test("invalid manifests recover without partial review or uncaught exceptions", async () => {
    const bad = [null, [], {}, { segments: {} }, { segments: [null] }, { segments: [1] }];
    const valid = fixture.segments[0];
    for (const field of ["id", "start", "end", "duration", "filename", "text"]) {
      const missing = { ...valid }; delete missing[field];
      bad.push({ segments: [missing] });
    }
    for (const fields of [{ id: true }, { id: -1 }, { id: 0.5 }, { id: "0" }, { id: 1e20 },
      { start: "0" }, { start: -1 }, { end: 0 }, { end: null }, { duration: 0 }, { duration: "1.5" },
      { filename: " " }, { filename: "\ud800" }, { text: {} }, { is_keyword: "false" }, { is_keyword: null }]) {
      bad.push({ segments: [valid, { ...fixture.segments[1], ...fields }] });
    }
    bad.push({ segments: [valid, valid] }, { segments: [valid], keyword_highlights: {} },
      { segments: [valid], keyword_highlights: [null] }, { segments: [valid], keyword_highlights: [{ id: 99 }] });
    for (const body of bad) {
      await load(body);
      assert.equal(await evaluate("!!document.querySelector('.upload-container') && !document.querySelector('.segment-row') && !document.getElementById('uploadButton').disabled"), true);
      assert.match(await evaluate("document.getElementById('statusMessage').textContent"), /Invalid manifest/);
      assert.equal(await evaluate("document.getElementById('statusMessage').getAttribute('role')"), "alert");
    }
    for (const body of ["", "{broken", '{"segments":[{"id":1e400}]}']) {
      await load(body, { raw: true });
      assert.equal(await evaluate("!!document.querySelector('.upload-container') && !document.getElementById('uploadButton').disabled"), true);
      assert.match(await evaluate("document.getElementById('statusMessage').textContent"), /manifest/);
    }
    await load({}, { status: 500 });
    assert.match(await evaluate("document.getElementById('statusMessage').textContent"), /HTTP 500/);
    await load({}, { networkError: true });
    assert.match(await evaluate("document.getElementById('statusMessage').textContent"), /Network unavailable/);
  });

  await test("keyword filtering and selection agree for legacy and explicit flags", async () => {
    const modern = { ...fixture, segments: fixture.segments.map(seg => ({ ...seg, is_keyword: seg.id === 4 })) };
    const mixed = { ...fixture, segments: fixture.segments.map(seg => seg.id === 0 ? { ...seg, is_keyword: false } : seg) };
    for (const [body, expected] of [[fixture, [0, 9]], [modern, [4]], [mixed, [9]], [{ segments: fixture.segments }, []]]) {
      await load(body);
      assert.deepEqual(await ids(), [0, 4, 9]);
      await evaluate("document.getElementById('selectAll').click(); document.getElementById('selectKeywords').click()");
      assert.deepEqual(await selected(), expected);
      const filter = "document.getElementById('showOnlyKeywords')";
      await evaluate(`${filter}.checked = true; ${filter}.dispatchEvent(new Event('change'))`);
      assert.deepEqual(await ids(), expected);
      assert.deepEqual(await selected(), expected);
      await evaluate(`${filter}.checked = false; ${filter}.dispatchEvent(new Event('change'))`);
      assert.deepEqual(await selected(), expected);
    }
  });

  await test("repeated row review, play and seeking leave selection and totals unchanged", async () => {
    await load(fixture);
    await click('#selectKeywords');
    await observeReview();
    for (let repeat = 0; repeat < 3; repeat++) {
      for (const [index, segment] of fixture.segments.entries()) {
        const row = `.segment-row:nth-child(${index + 1})`;
        await click(row);
        assert.equal(await evaluate("window.__reviews.length"), repeat * 6 + index * 2 + 1);
        assert.equal(await evaluate("window.__plays"), repeat * 3 + index);
        assert.equal(await evaluate("document.querySelector('.segment-row.active .segment-id').textContent"), String(segment.id));
        await click(`${row} .play-btn`);
        assert.equal(await evaluate("window.__reviews.length"), repeat * 6 + index * 2 + 2);
        assert.equal(await evaluate("window.__plays"), repeat * 3 + index + 1);
        assert.equal(await evaluate("document.getElementById('mainVideo').getAttribute('src')"), `/segments/${segment.filename}`);
        assert.equal(await evaluate("window.__seeks.at(-1)"), 0);
        await evaluate("document.getElementById('mainVideo').currentTime = 0.5; document.getElementById('mainVideo').dispatchEvent(new Event('seeking'))");
        assert.deepEqual(await selected(), [0, 9]);
        assert.deepEqual(await evaluate("[document.getElementById('selectedCount').textContent, document.getElementById('selectedDuration').textContent]"), ['2', '3.0s']);
      }
    }
    await click('#exportBtn');
    await until("!document.getElementById('exportBtn').disabled");
    assert.deepEqual(await evaluate("JSON.parse(window.__requests.find(r => r.path === '/api/export').options.body).selected_ids"), [0, 9]);
  });

  await test("checkbox, keyboard and bulk controls select once without reviewing", async () => {
    await load(fixture);
    await click('#selectKeywords');
    await observeReview();
    await evaluate(`
      window.__changes = 0;
      document.getElementById('segmentsList').addEventListener('change', () => window.__changes++, true);
    `);
    for (const [expected, count, duration] of [[[0, 4, 9], 1, '4.5s'], [[0, 9], 2, '3.0s']]) {
      await click('.segment-checkbox[data-id="4"]');
      assert.deepEqual(await selected(), expected);
      assert.equal(await evaluate("window.__changes"), count);
      assert.equal(await evaluate("document.getElementById('selectedCount').textContent"), String(expected.length));
      assert.equal(await evaluate("document.getElementById('selectedDuration').textContent"), duration);
    }
    await evaluate("document.querySelector('.segment-checkbox[data-id=\"0\"]').focus()");
    await press(' ', 'Space', 32);
    assert.deepEqual(await selected(), [9]);
    assert.equal(await evaluate("window.__changes"), 3);
    await press(' ', 'Space', 32);
    assert.deepEqual(await selected(), [0, 9]);
    assert.equal(await evaluate("window.__changes"), 4);
    for (const [control, expected] of [['selectAll', [0, 4, 9]], ['selectNone', []], ['selectKeywords', [0, 9]]]) {
      await click(`#${control}`);
      assert.deepEqual(await selected(), expected);
      assert.equal(await evaluate("document.getElementById('selectedCount').textContent"), String(expected.length));
      assert.equal(await evaluate("document.getElementById('selectedDuration').textContent"), (expected.length * 1.5).toFixed(1) + 's');
    }
    await evaluate("document.getElementById('selectAll').focus()");
    await press('Enter', 'Enter', 13);
    assert.deepEqual(await selected(), [0, 4, 9]);
    await click('#showOnlyKeywords');
    assert.deepEqual(await ids(), [0, 9]);
    assert.equal(await evaluate("document.getElementById('selectedCount').textContent"), '3');
    await click('#exportBtn');
    await until("!document.getElementById('exportBtn').disabled");
    assert.deepEqual(await evaluate("JSON.parse(window.__requests.find(r => r.path === '/api/export').options.body).selected_ids.sort((a, b) => a - b)"), [0, 4, 9]);
    assert.deepEqual(await evaluate("window.__reviews"), []);
    assert.equal(await evaluate("window.__plays"), 0);
    assert.equal(await evaluate("document.querySelector('.segment-row.active .segment-id').textContent"), '0');
  });

  await test("transcripts, filenames and statistics values cannot create markup", async () => {
    const body = structuredClone(fixture);
    body.segments[0].filename = 'clip.mp4\"><img src=x onerror="window.__injected=true">';
    await load(body);
    assert.deepEqual(await evaluate("[...document.querySelectorAll('.segment-text')].map(el => el.textContent)"), fixture.segments.map(seg => seg.text));
    assert.equal(await evaluate("!!document.querySelector('#app script, #app img, #app svg, .segment-text b, [onerror], [onload]')"), false);
    assert.equal(await evaluate("document.getElementById('mainVideo').getAttribute('src')"), `/segments/${encodeURIComponent(body.segments[0].filename)}`);
    await evaluate("document.getElementById('advancedToggle').click(); document.getElementById('previewBtn').click()");
    await until("document.getElementById('previewInfo').classList.contains('visible')");
    assert.equal(await evaluate("document.getElementById('previewInfo').children.length"), 2);
    assert.match(await evaluate("document.getElementById('previewInfo').textContent"), /<img onerror=/);
    assert.equal(await evaluate("window.__injected"), false);
  });

  await test("statistics and hidden-panel generation share current criteria and persistent threshold", async () => {
    await load(fixture, { llmAvailable: true });
    await until("!document.getElementById('autoGenerateBtn').disabled");
    await click('#advancedToggle');
    await evaluate("document.getElementById('minScoreSlider').focus()");
    await press('ArrowRight', 'ArrowRight', 39);
    assert.equal(await evaluate("document.getElementById('minScoreSlider').value"), '8');
    for (const [index, [criteria, threshold]] of [['  Great plays  ', 8], ['  Saves <b>and goals</b>  ', 9]].entries()) {
      await setScoring(criteria, threshold);
      await click('#advancedToggle');
      assert.equal(await evaluate("document.getElementById('advancedSection').classList.contains('visible')"), false);
      assert.equal(await evaluate("document.getElementById('generationCriteria').textContent"), `Criteria: ${criteria.trim()}`);
      assert.equal(await evaluate("document.getElementById('generationThreshold').textContent"), `Score threshold: ${threshold}+`);
      assert.equal(await evaluate("document.querySelector('#generationCriteria b')"), null);
      await click('#advancedToggle');
      assert.deepEqual(await evaluate("[document.getElementById('minScoreSlider').value, document.getElementById('scoreValue').textContent]"), [String(threshold), String(threshold)]);
      await click('#previewBtn');
      await until("document.getElementById('previewInfo').classList.contains('visible') && !document.getElementById('previewBtn').disabled");
      assert.match(await evaluate("document.getElementById('previewInfo').textContent"), /Selection Statistics:/);
      assert.equal(await evaluate("document.getElementById('previewBtn').textContent"), 'Selection Statistics');
      assert.doesNotMatch(await evaluate("document.getElementById('app').textContent"), /Preview Selection|playable preview/i);
      await click('#advancedToggle');
      await click('#autoGenerateBtn');
      await until(`window.__alerts.length === ${index + 1} && !document.getElementById('autoGenerateBtn').disabled`);
      assert.match(await evaluate("window.__alerts.at(-1)"), new RegExp(`score >= ${threshold}`));
      if (index === 0) await click('#advancedToggle');
    }
    const requests = await scoringRequests();
    assert.equal(requests.length, 4);
    assert.deepEqual(requests.map(r => [r.body.instructions, r.body.min_score]), [
      ['Great plays', 8], ['Great plays', 8], ['Saves <b>and goals</b>', 9], ['Saves <b>and goals</b>', 9]
    ]);
    assert.equal(requests[1].body.sort_chronological, true);
    assert.equal(requests[1].body.rescore, false);
  });

  await test("Reset Threshold updates the control, summary and both requests without resetting criteria or selection", async () => {
    await load(fixture, { llmAvailable: true });
    await until("!document.getElementById('autoGenerateBtn').disabled");
    await click('#selectKeywords');
    await click('#advancedToggle');
    await setScoring('Keep goals', 9);
    await click('#rescoreCheck');
    await click('#previewBtn');
    await until("document.getElementById('previewInfo').classList.contains('visible')");
    await click('#resetSettingsBtn');
    assert.equal(await evaluate("document.getElementById('resetSettingsBtn').textContent"), 'Reset Threshold');
    assert.deepEqual(await evaluate("[document.getElementById('minScoreSlider').value, document.getElementById('scoreValue').textContent, document.getElementById('generationThreshold').textContent]"), ['7', '7', 'Score threshold: 7+']);
    assert.equal(await evaluate("document.getElementById('previewInfo').classList.contains('visible')"), false);
    assert.equal(await evaluate("document.getElementById('aiInstructions').value"), 'Keep goals');
    assert.equal(await evaluate("document.getElementById('rescoreCheck').checked"), true);
    assert.deepEqual(await selected(), [0, 9]);
    await click('#previewBtn');
    await until("document.getElementById('previewInfo').classList.contains('visible')");
    await click('#advancedToggle');
    await click('#autoGenerateBtn');
    await until("window.__alerts.length === 1 && !document.getElementById('autoGenerateBtn').disabled");
    const requests = await scoringRequests();
    assert.deepEqual(requests.map(r => [r.body.instructions, r.body.min_score]), [['Keep goals', 9], ['Keep goals', 7], ['Keep goals', 7]]);
    assert.equal(requests[2].body.rescore, true);
  });

  await test("editing criteria or threshold invalidates visible and pending statistics", async () => {
    for (const [criteria, threshold] of [['Different goals', 8], ['Great plays', 9]]) {
      await load(fixture, { llmAvailable: true });
      await click('#advancedToggle');
      await setScoring('Great plays', 8);
      await click('#previewBtn');
      await until("document.getElementById('previewInfo').classList.contains('visible')");
      await setScoring(criteria, threshold);
      assert.equal(await evaluate("document.getElementById('previewInfo').classList.contains('visible')"), false);
      await setScoring('Great plays', 8);
      await evaluate("window.__holdStatistics = true");
      await click('#previewBtn');
      await until("typeof window.__finishStatistics === 'function'");
      await evaluate("document.getElementById('previewBtn').dispatchEvent(new Event('click'))");
      await setScoring(criteria, threshold);
      await evaluate("window.__finishStatistics()");
      await until("!document.getElementById('previewBtn').disabled");
      assert.equal(await evaluate("document.getElementById('previewInfo').classList.contains('visible')"), false);
      assert.equal((await scoringRequests()).length, 2);
    }
  });

  await test("busy, missing and stale scores preserve controls and selection and allow generation to recover", async () => {
    for (const [status, category, message] of [
      [409, 'busy', 'Hi-Lite is busy processing another operation. Please wait for it to finish.'],
      [404, undefined, 'sorted_segments.json not found. Run AI scoring first.'],
      [400, 'cache_miss', 'Cached AI scores do not match this scoring request. Run AI scoring again.'],
      [400, 'validation', 'No usable AI scores remain after validation. Run AI scoring again.']
    ]) {
      await load(fixture, { llmAvailable: true });
      await until("!document.getElementById('autoGenerateBtn').disabled");
      await click('#selectKeywords');
      await click('#advancedToggle');
      await setScoring('Goals', 8);
      await evaluate(`
        document.getElementById('autoDownloadRow').classList.add('visible');
        document.getElementById('autoDownloadLink').setAttribute('href', '/final/previous.mp4');
        window.__errors = { '/api/preview-auto-export': ${JSON.stringify({ status, body: { status: 'error', category, message } })} };
      `);
      const before = await evaluate("document.getElementById('autoDownloadRow').outerHTML");
      await click('#previewBtn');
      await until("window.__alerts.length === 1 && !document.getElementById('previewBtn').disabled");
      assert.match(await evaluate("window.__alerts[0]"), status === 409 ? /busy.*wait/i : /Run AI scoring/);
      assert.deepEqual(await selected(), [0, 9]);
      assert.deepEqual(await evaluate("[document.getElementById('selectedCount').textContent, document.getElementById('selectedDuration').textContent, document.getElementById('aiInstructions').value, document.getElementById('minScoreSlider').value, document.getElementById('scoreValue').textContent]"), ['2', '3.0s', 'Goals', '8', '8']);
      assert.equal(await evaluate("document.getElementById('autoDownloadRow').outerHTML"), before);
      assert.equal(await evaluate("document.getElementById('autoGenerateBtn').disabled"), false);
      await click('#advancedToggle');
      await click('#autoGenerateBtn');
      await until("window.__alerts.length === 2 && !document.getElementById('autoGenerateBtn').disabled");
      const requests = await scoringRequests();
      assert.deepEqual(requests.map(r => [r.body.instructions, r.body.min_score]), [['Goals', 8], ['Goals', 8]]);
      assert.equal(requests[1].body.rescore, false);
      assert.deepEqual(await selected(), [0, 9]);
    }
  });

  await test("recoverable generation admission and scoring errors preserve selection and current controls", async () => {
    for (const stage of ['admission', 'scoring']) {
      await load(fixture, { llmAvailable: true });
      await until("!document.getElementById('autoGenerateBtn').disabled");
      await click('#selectKeywords');
      await click('#advancedToggle');
      await setScoring('Saves', 9);
      await click('#rescoreCheck');
      await evaluate(`
        document.getElementById('autoDownloadRow').classList.add('visible');
        document.getElementById('autoDownloadLink').setAttribute('href', '/final/existing.mp4');
        ${stage === 'admission'
          ? "window.__errors = { '/api/auto-generate': { status: 400, body: { status: 'error', category: 'cache_miss', message: 'Run AI scoring again.' } } };"
          : "window.__autoError = 'The scoring endpoint did not respond. Please try again.';"}
      `);
      const before = await evaluate("document.getElementById('autoDownloadRow').outerHTML");
      await click('#advancedToggle');
      await click('#autoGenerateBtn');
      await until("window.__alerts.length === 1 && !document.getElementById('autoGenerateBtn').disabled");
      assert.match(await evaluate("window.__alerts[0]"), stage === 'admission' ? /Run AI scoring/ : /Please try again/);
      assert.deepEqual(await selected(), [0, 9]);
      assert.deepEqual(await evaluate("[document.getElementById('aiInstructions').value, document.getElementById('minScoreSlider').value, document.getElementById('scoreValue').textContent, document.getElementById('rescoreCheck').checked]"), ['Saves', '9', '9', true]);
      assert.equal(await evaluate("document.getElementById('advancedSection').classList.contains('visible')"), false);
      assert.equal(await evaluate("document.getElementById('uploadNewBtn').disabled"), false);
      if (stage === 'admission') assert.equal(await evaluate("document.getElementById('autoDownloadRow').outerHTML"), before);
      await evaluate("window.__errors = {}; window.__autoError = null;");
      await click('#autoGenerateBtn');
      await until("window.__alerts.length === 2 && !document.getElementById('autoGenerateBtn').disabled");
      assert.match(await evaluate("window.__alerts[1]"), /Ready!/);
      assert.deepEqual((await scoringRequests()).map(r => [r.body.instructions, r.body.min_score, r.body.rescore]), [['Saves', 9, true], ['Saves', 9, true]]);
    }
  });

  await test("provider and model status stays visible near Generate with Advanced closed", async () => {
    for (const [backend, model] of [['api', 'fixture-api'], ['local', 'fixture-local'], ['g4f', null]]) {
      await load(fixture, { llmSettings: { resolved_backend: backend, model, local_model: model, base_url: 'https://fixture.invalid/v1' } });
      await until("!document.getElementById('autoGenerateBtn').disabled");
      assert.equal(await evaluate("document.getElementById('advancedSection').classList.contains('visible')"), false);
      assert.equal(await evaluate("document.getElementById('llmStatus').closest('#generationControls') === document.getElementById('autoGenerateBtn').closest('#generationControls')"), true);
      assert.equal(await evaluate("document.getElementById('llmStatus').checkVisibility() && document.getElementById('llmNotice').checkVisibility() && document.getElementById('generationCriteria').checkVisibility()"), true);
      assert.match(await evaluate("document.getElementById('llmStatus').textContent"), new RegExp(backend === 'api' ? 'API' : backend));
      if (model) assert.match(await evaluate("document.getElementById('llmStatus').textContent"), new RegExp(model));
      await click('#llmSettingsToggle');
      assert.equal(await evaluate("document.getElementById('advancedSection').classList.contains('visible') && document.getElementById('llmSettingsPanel').checkVisibility()"), true);
    }
  });

  await test("missing or unreadable provider status disables generation with an actionable message", async () => {
    for (const llmStatusError of [false, true, 'network']) {
      await load(fixture, { llmStatusError });
      await until("!document.getElementById('llmStatus').textContent.includes('Checking')");
      assert.equal(await evaluate("document.getElementById('autoGenerateBtn').disabled"), true);
      assert.equal(await evaluate("document.getElementById('llmNotice').checkVisibility()"), true);
      assert.match(await evaluate("document.getElementById('llmNotice').textContent"), /disabled.*AI Settings/i);
      await click('#selectKeywords');
      await setScoring('Goals', 8);
      await evaluate("window.__allowAlerts = true; document.getElementById('autoGenerateBtn').dispatchEvent(new Event('click'))");
      assert.equal((await scoringRequests()).length, 0);
      assert.equal(await evaluate("window.__alerts.length"), 1);
      assert.deepEqual(await selected(), [0, 9]);
      assert.deepEqual(await evaluate("[document.getElementById('aiInstructions').value, document.getElementById('minScoreSlider').value]"), ['Goals', '8']);
      assert.equal(await evaluate("document.getElementById('exportBtn').disabled"), false);
    }
  });

  await test("a failed public provider test is visible near Generate and can recover through existing Settings", async () => {
    await load(fixture, { llmAvailable: true });
    await until("!document.getElementById('autoGenerateBtn').disabled");
    await click('#selectKeywords');
    await setScoring('Goals', 8);
    await click('#llmSettingsToggle');
    await evaluate("window.__errors = { '/api/llm-test': { status: 502, body: { status: 'error', category: 'config', message: 'Authentication failed. Check your API key.' } } }");
    await click('#llmTestBtn');
    await until("!document.getElementById('llmTestBtn').disabled");
    await click('#advancedToggle');
    assert.equal(await evaluate("document.getElementById('autoGenerateBtn').disabled"), true);
    assert.match(await evaluate("document.getElementById('llmNotice').textContent"), /Authentication failed.*Check.*API key.*AI Settings/);
    assert.equal(await evaluate("document.getElementById('llmNotice').checkVisibility()"), true);
    await evaluate("document.getElementById('autoGenerateBtn').dispatchEvent(new Event('click'))");
    assert.equal((await scoringRequests()).length, 0);
    await evaluate("window.__errors = {};");
    await click('#llmTestBtn');
    await until("!document.getElementById('llmTestBtn').disabled && !document.getElementById('autoGenerateBtn').disabled");
    await evaluate("window.__llmStatusError = true");
    await click('#llmSaveBtn');
    await until("!document.getElementById('llmSaveBtn').disabled");
    assert.equal(await evaluate("document.getElementById('autoGenerateBtn').disabled"), true);
    assert.match(await evaluate("document.getElementById('llmStatus').textContent"), /status unavailable/);
    await evaluate("window.__llmStatusError = false");
    await click('#llmSaveBtn');
    await until("!document.getElementById('llmSaveBtn').disabled && !document.getElementById('autoGenerateBtn').disabled");
    assert.deepEqual(await selected(), [0, 9]);
    assert.deepEqual(await evaluate("[document.getElementById('aiInstructions').value, document.getElementById('minScoreSlider').value]"), ['Goals', '8']);
  });

  await test("repeated review/import transitions reset state and bind controls once", async () => {
    await load(fixture);
    for (let repeat = 0; repeat < 3; repeat++) {
      await evaluate("window.__previousVideo = document.getElementById('mainVideo'); document.getElementById('selectAll').click(); document.getElementById('showOnlyKeywords').checked = true; document.getElementById('advancedToggle').click(); document.getElementById('uploadNewBtn').click()");
      assert.equal(await evaluate("!!document.querySelector('.upload-container') && !document.querySelector('video')"), true);
      assert.equal(await evaluate("window.__previousVideo.paused && window.__previousVideo.getAttribute('src') === null"), true);
      assert.equal(await evaluate("document.getElementById('progressFill').style.width"), "");
      assert.equal(await evaluate(`(() => {
        let clicks = 0;
        document.getElementById('videoInput').addEventListener('click', () => clicks++);
        document.getElementById('uploadButton').click();
        return clicks;
      })()`), 1);
      await upload();
      assert.equal(await evaluate("window.__uploads"), repeat + 1);
      assert.equal(await evaluate("window.__confirmCount"), repeat + 1);
      assert.deepEqual(await selected(), []);
      assert.deepEqual(await ids(), [0, 4, 9]);
      assert.equal(await evaluate("document.getElementById('showOnlyKeywords').checked"), false);
      assert.equal(await evaluate("document.getElementById('advancedSection').classList.contains('visible')"), false);
      assert.equal(await evaluate("document.getElementById('exportBtn').disabled"), true);
      assert.equal(await evaluate("document.querySelectorAll('#selectorStyles').length"), 1);
    }
    assert.equal(await evaluate("window.__requests.filter(r => r.path === '/data/segments_manifest.json').every(r => r.options.cache === 'no-store')"), true);
  });

  await test("import completion revalidates corrupt and empty manifests", async () => {
    for (const body of [{ segments: [] }, { segments: [null] }]) {
      await load({ segments: [] });
      await evaluate(`window.__nextManifest = ${JSON.stringify(body)}`);
      await upload();
      await until("!!document.querySelector('.upload-container') && !document.getElementById('uploadButton').disabled");
      assert.equal(await evaluate("!!document.querySelector('.segment-row')"), false);
      await evaluate("window.__nextManifest = window.__fixture");
      await upload();
      assert.deepEqual(await ids(), [0, 4, 9]);
    }
  });

  await test("upload and polling failures leave retry usable, including the same file", async () => {
    for (const error of ["network", "json", "http", "processing", "invalid", "upload"]) {
      await load({ segments: [] });
      await evaluate(`window.__pollError = ${JSON.stringify(error)};
        window.__processingStatus = ${JSON.stringify(error === "processing" ? { status: "error", message: "Fixture failure" } : error === "invalid" ? {} : null)};
        window.__uploadError = ${error === "upload"}`);
      await upload();
      assert.equal(await evaluate("!!document.querySelector('.upload-container') && !document.getElementById('uploadButton').disabled && document.getElementById('videoInput').value === ''"), true);
      await evaluate("window.__pollError = null; window.__processingStatus = null; window.__uploadError = false");
      await upload();
      assert.deepEqual(await ids(), [0, 4, 9]);
      assert.equal(await evaluate("window.__uploads"), 2);
    }
  });

  await test("busy export, generation and discard preserve selections and existing downloads", async () => {
    for (const [path, button] of [["/api/export", "exportBtn"], ["/api/auto-generate", "autoGenerateBtn"], ["/api/preview-auto-export", "previewBtn"], ["/api/discard-reel", "discardAutoBtn"]]) {
      await load(fixture, { llmAvailable: true });
      await until("!document.getElementById('autoGenerateBtn').disabled");
      await setScoring('fixture', 8);
      await evaluate(`
        window.__busyPaths = [${JSON.stringify(path)}];
        document.getElementById('selectKeywords').click();
        document.getElementById('aiInstructions').value = 'fixture';
        document.getElementById('autoDownloadRow').classList.add('visible');
        document.getElementById('autoDownloadLink').setAttribute('href', '/final/existing-auto.mp4');
        document.getElementById('autoDownloadLink').textContent = 'Existing automatic reel';
        document.getElementById('downloadLink').setAttribute('href', '/final/existing-manual.mp4');
        document.getElementById('downloadLink').classList.add('visible');
      `);
      const before = await evaluate("[document.getElementById('autoDownloadRow').outerHTML, document.getElementById('downloadLink').outerHTML]");
      await evaluate(`document.getElementById(${JSON.stringify(button)}).click()`);
      await until(`window.__alerts.length === 1 && !document.getElementById(${JSON.stringify(button)}).disabled`);
      assert.match(await evaluate("window.__alerts[0]"), /busy.*wait/i);
      assert.deepEqual(await selected(), [0, 9]);
      assert.deepEqual(await evaluate("[document.getElementById('aiInstructions').value, document.getElementById('minScoreSlider').value, document.getElementById('scoreValue').textContent]"), ['fixture', '8', '8']);
      assert.deepEqual(await evaluate("[document.getElementById('autoDownloadRow').outerHTML, document.getElementById('downloadLink').outerHTML]"), before);
      assert.equal(await evaluate("window.__requests.some(r => r.path.startsWith('/api/processing-status/'))"), false);
    }
  });

  await test("busy manual export/statistics and repeated clicks leave the active automatic job visible", async () => {
    await load(fixture, { llmAvailable: true });
    await until("!document.getElementById('autoGenerateBtn').disabled");
    await setScoring('fixture', 8);
    await evaluate(`
      document.getElementById('selectKeywords').click();
      document.getElementById('aiInstructions').value = 'fixture';
      window.__holdAutoPoll = true;
      window.__busyPaths = ['/api/export', '/api/preview-auto-export'];
      document.getElementById('autoGenerateBtn').click();
    `);
    await until("typeof window.__finishAuto === 'function'");
    assert.equal(await evaluate("document.getElementById('autoGenerateBtn').textContent"), "Scoring fixture...");
    await evaluate(`
      document.getElementById('autoGenerateBtn').dispatchEvent(new Event('click'));
      document.getElementById('exportBtn').click();
      document.getElementById('previewBtn').click();
    `);
    await until("window.__alerts.length === 2 && !document.getElementById('exportBtn').disabled && !document.getElementById('previewBtn').disabled");
    assert.match(await evaluate("window.__alerts[0]"), /busy/);
    assert.deepEqual(await selected(), [0, 9]);
    assert.deepEqual(await evaluate("[document.getElementById('aiInstructions').value, document.getElementById('minScoreSlider').value]"), ['fixture', '8']);
    assert.equal(await evaluate("document.getElementById('autoGenerateBtn').disabled"), true);
    assert.equal(await evaluate("document.getElementById('autoGenerateBtn').textContent"), "Scoring fixture...");
    assert.equal(await evaluate("window.__requests.filter(r => r.path === '/api/auto-generate').length"), 1);
    assert.equal(await evaluate("document.getElementById('uploadNewBtn').disabled"), true);
    await evaluate("document.getElementById('uploadNewBtn').dispatchEvent(new Event('click'))");
    assert.equal(await evaluate("!!document.querySelector('.main-container')"), true);
    await evaluate("window.__finishAuto()");
    await until("document.getElementById('autoDownloadRow').classList.contains('visible') && !document.getElementById('autoGenerateBtn').disabled");
    assert.equal(await evaluate("document.getElementById('autoDownloadLink').getAttribute('href')"), "/final/new-auto.mp4");
  });

  await test("selection changes and a busy automatic request preserve an active manual export", async () => {
    await load(fixture, { llmAvailable: true });
    await until("!document.getElementById('autoGenerateBtn').disabled");
    await evaluate(`
      document.getElementById('selectKeywords').click();
      document.getElementById('aiInstructions').value = 'fixture';
      window.__holdExport = true;
      window.__busyPaths = ['/api/auto-generate'];
      document.getElementById('exportBtn').click();
    `);
    await until("typeof window.__finishExport === 'function'");
    await evaluate(`
      document.getElementById('selectAll').click();
      document.getElementById('exportBtn').dispatchEvent(new Event('click'));
      document.getElementById('autoGenerateBtn').click();
    `);
    await until("window.__alerts.length === 1 && !document.getElementById('autoGenerateBtn').disabled");
    assert.match(await evaluate("window.__alerts[0]"), /busy/);
    assert.deepEqual(await selected(), [0, 4, 9]);
    assert.equal(await evaluate("document.getElementById('exportBtn').disabled"), true);
    assert.equal(await evaluate("document.getElementById('exportBtn').textContent"), "Generating...");
    assert.equal(await evaluate("document.getElementById('uploadNewBtn').disabled"), true);
    assert.equal(await evaluate("window.__requests.filter(r => r.path === '/api/export').length"), 1);
    await evaluate("window.__finishExport()");
    await until("!document.getElementById('exportBtn').disabled && document.getElementById('downloadLink').classList.contains('visible')");
    assert.equal(await evaluate("document.getElementById('uploadNewBtn').disabled"), false);
  });

  await test("a busy import displays the server message and never polls a rejected task", async () => {
    await load({ segments: [] });
    await evaluate("window.__busyPaths = ['/api/upload']");
    await upload();
    assert.match(await evaluate("document.getElementById('statusMessage').textContent"), /busy.*wait/i);
    assert.equal(await evaluate("window.__requests.filter(r => r.path === '/api/upload').length"), 1);
    assert.equal(await evaluate("window.__requests.some(r => r.path.startsWith('/api/processing-status/'))"), false);
    assert.equal(await evaluate("document.getElementById('uploadButton').disabled"), false);
  });

  assert.deepEqual(exceptions, [], "No uncaught JavaScript exceptions or unhandled rejections");
} finally {
  browser.kill();
  await new Promise(resolve => browser.exitCode !== null || browser.signalCode !== null ? resolve() : browser.once("exit", resolve));
  await new Promise(resolve => server.close(resolve));
  await rm(profile, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 });
}
