// SPDX-License-Identifier: GPL-3.0-or-later
// Exercise the built UI in Chromium using its DevTools pipe; no test dependency.
// Run `npm test`. Set CHROME_BIN if Chromium is not in the Playwright cache.
import assert from "node:assert/strict";
import { spawn, execFileSync } from "node:child_process";
import { existsSync, readFileSync, readdirSync } from "node:fs";
import { mkdtemp, mkdir, rm, writeFile } from "node:fs/promises";
import { createServer } from "node:http";
import { homedir, tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

const fixture = JSON.parse(readFileSync(new URL("../tests/fixtures/segments.json", import.meta.url), "utf8"));
const sourceId = 'a'.repeat(32);
const sourceUrl = `/api/source/${sourceId}`;
const mediaFolder = await mkdtemp(join(tmpdir(), 'hi-lite-timeline-media-'));
const mediaPath = join(mediaFolder, 'source.mp4');
execFileSync(process.env.FFMPEG_PATH || 'ffmpeg', ['-v', 'error', '-f', 'lavfi', '-i',
  'testsrc2=s=160x90:r=10', '-t', '20', '-c:v', 'libx264', '-preset', 'ultrafast',
  '-pix_fmt', 'yuv420p', '-movflags', '+faststart', '-y', mediaPath]);
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
  if (path === sourceUrl) {
    const content = readFileSync(mediaPath);
    const range = /bytes=(\d+)-(\d*)/.exec(request.headers.range || '');
    const first = range ? Number(range[1]) : 0;
    const last = range?.[2] ? Math.min(Number(range[2]), content.length - 1) : content.length - 1;
    response.writeHead(range ? 206 : 200, { 'Content-Type': 'video/mp4', 'Accept-Ranges': 'bytes',
      'Content-Length': last - first + 1, ...(range ? { 'Content-Range': `bytes ${first}-${last}/${content.length}` } : {}) });
    response.end(content.subarray(first, last + 1));
    return;
  }
  const file = join(dist, path === "/" ? "index.html" : path);
  if (!file.startsWith(dist) || !existsSync(file)) {
    response.writeHead(404).end();
    return;
  }
  response.setHeader("Content-Type", file.endsWith(".js") ? "text/javascript" : file.endsWith(".css") ? "text/css" : file.endsWith(".png") ? "image/png" : "text/html");
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
  await page("Emulation.setDeviceMetricsOverride", { width: 1736, height: 864, deviceScaleFactor: 1, mobile: false });
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
  async function viewport(width, height = 800) {
    await page("Emulation.setDeviceMetricsOverride", { width, height, deviceScaleFactor: 1, mobile: false });
    await evaluate("new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))");
  }
  async function tabTo(selector) {
    for (let attempt = 0; attempt < 100; attempt++) {
      if (await evaluate(`document.activeElement === document.querySelector(${JSON.stringify(selector)})`)) return;
      await press('Tab', 'Tab', 9);
    }
    assert.fail(`Keyboard could not reach ${selector}`);
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
    llmSettings, llmStatusError = false, criteria = 'Great plays', llmStatusDelayed = false, source = true } = {}) {
    if (!raw && source && body?.segments?.length) body = { ...body, source: { id: sourceId, url: sourceUrl } };
    if (scriptId) await page("Page.removeScriptToEvaluateOnNewDocument", { identifier: scriptId });
    caseId++;
    const { identifier } = await page("Page.addScriptToEvaluateOnNewDocument", { source: `
      window.__case = ${caseId};
      window.__fixture = ${JSON.stringify({ ...fixture, source: { id: sourceId, url: sourceUrl } })};
      window.__manifest = ${JSON.stringify({ body: raw ? body : JSON.stringify(body), status, networkError })};
      window.__requests = [];
      window.__uploads = 0;
      window.__confirmCount = 0;
      window.__injected = false;
      window.__alerts = [];
      window.__llmStatusError = ${JSON.stringify(llmStatusError)};
      window.__holdLlmStatus = ${JSON.stringify(llmStatusDelayed)};
      window.__llmSettings = ${JSON.stringify(llmSettings ?? {
        resolved_backend: llmAvailable ? "api" : null, backend_override: "", base_url: "https://fixture.invalid/v1",
        model: "fixture-model", has_api_key: llmAvailable, local_url: "http://127.0.0.1:11434",
        local_model: "fixture-local", local_available: false
      })};
      window.confirm = () => { window.__confirmCount++; return true; };
      window.alert = message => {
        window.__alerts.push(message);
        throw new Error('Unexpected blocking alert: ' + message);
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
          if (options?.method === 'POST') {
            const saved = JSON.parse(options.body);
            if (window.__holdLlmSave) await new Promise(resolve => { window.__finishLlmSave = resolve; });
            for (const key of ['base_url', 'model', 'local_url', 'local_model']) {
              if (key in saved) window.__llmSettings[key] = saved[key];
            }
            if (saved.api_key) window.__llmSettings.has_api_key = true;
            return json({ status: 'ok' });
          }
          if (window.__holdLlmStatus) await new Promise(resolve => { window.__finishLlmStatus = resolve; });
          if (window.__llmStatusError === 'network') throw new TypeError('Status unavailable');
          if (window.__llmStatusError) return json({ message: 'Status unavailable' }, 503);
          return json(window.__llmSettings);
        }
        if (path === "/api/llm-test") {
          if (window.__holdLlmTest) await new Promise(resolve => { window.__finishLlmTest = resolve; });
          return json(window.__llmTestResult ?? { status: 'ok', backend: 'api', model: 'fixture-model' });
        }
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
          if (window.__holdUploadPoll) {
            if (!window.__importStageShown) {
              window.__importStageShown = true;
              return json({ status: 'processing', progress: 42, message: 'Preparing transcript…' });
            }
            return new Promise(resolve => {
              window.__finishUpload = () => resolve(json({ status: 'complete', progress: 100 }));
            });
          }
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
    await page("Page.navigate", { url });
    await until(`window.__case === ${caseId} && !!document.querySelector('.upload-container, .main-container')`);
    assert.equal(await evaluate('location.search'), '', 'Every screen loads at the default URL');
    if (await evaluate("!!document.getElementById('aiInstructions')")) await setScoring(criteria, 7);
    if (await evaluate("document.getElementById('mainVideo')?.getAttribute('src') === " + JSON.stringify(sourceUrl)))
      await until("document.getElementById('sourceScrubber').getAttribute('aria-disabled') === 'false'");
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
      await click('#mediaTrigger');
      await click(selector);
      for (let retry = 0; retry < 200 && !existsSync(file); retry++) {
        await new Promise(resolve => setTimeout(resolve, 20));
      }
      assert.ok(existsSync(file), "Browser saved the subtitle attachment");
      assert.equal(readFileSync(file, "utf8"), format === "vtt"
        ? 'WEBVTT\n\n00:00:00.000 --> 00:00:01.000\n日本 &amp; text\n\n'
        : '1\n00:00:00,000 --> 00:00:01,000\n日本 & text\n\n');
      await press('Escape', 'Escape', 27);
      await click("#selectKeywords");
      await setScoring("different criteria", 9);
    }
    await evaluate(`window.__errors = { '/api/subtitles': { status: 404, body: { available: false, message: 'No captions available' } } }`);
    await until(`document.querySelector('.subtitles-panel [role="status"]')?.textContent === 'No captions available'`);
    assert.equal(await evaluate(`document.querySelector('button[data-format="srt"]').disabled`), true);
    await evaluate(`window.__errors = {}`);
    await until(`document.querySelector('button[data-format="srt"]').disabled === false`);
    await evaluate(`window.__errors = { '/api/subtitles/srt': { status: 422, body: { message: 'Invalid transcript timing' } } }`);
    await click('#mediaTrigger');
    await click('button[data-format="srt"]');
    await until(`document.querySelector('.subtitles-panel [role="status"]').textContent === 'Invalid transcript timing'`);
    await press('Escape', 'Escape', 27);
    assert.match(await evaluate("document.getElementById('activityErrors').textContent"), /Subtitle download.*Invalid transcript timing.*Retry SRT or VTT/);
  });

  await test("missing and empty manifests open a styled, enabled import screen", async () => {
    for (const [body, status] of [[{ segments: [] }, 200], [{ segments: [] }, 404]]) {
      await load(body, { status });
      await until("document.querySelector('.brand-logo')?.naturalWidth > 0");
      assert.equal(await evaluate("!!document.querySelector('.upload-container')"), true);
      assert.equal(await evaluate("document.getElementById('uploadButton').disabled"), false);
      assert.equal(await evaluate("getComputedStyle(document.querySelector('.upload-container')).display"), "flex");
      assert.equal(await evaluate("document.querySelectorAll('#activityErrors [role=\"alert\"]').length > 0"), false);
      assert.equal(await evaluate("document.styleSheets.length"), 1);
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
      assert.match(await evaluate("document.getElementById('activityErrors').textContent"), /Invalid manifest/);
      assert.equal(await evaluate("document.querySelector('#activityErrors [role=\"alert\"]').getAttribute('role')"), "alert");
    }
    for (const body of ["", "{broken", '{"segments":[{"id":1e400}]}']) {
      await load(body, { raw: true });
      assert.equal(await evaluate("!!document.querySelector('.upload-container') && !document.getElementById('uploadButton').disabled"), true);
      assert.match(await evaluate("document.getElementById('activityErrors').textContent"), /manifest/);
    }
    await load({}, { status: 500 });
    assert.match(await evaluate("document.getElementById('activityErrors').textContent"), /HTTP 500/);
    await load({}, { networkError: true });
    assert.match(await evaluate("document.getElementById('activityErrors').textContent"), /Network unavailable/);
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
        assert.equal(await evaluate("window.__reviews.length"), 0);
        assert.equal(await evaluate("window.__plays"), repeat * 3 + index);
        assert.equal(await evaluate("document.querySelector('.segment-row.active .segment-id').textContent"), String(segment.id));
        await click(`${row} .play-btn`);
        assert.equal(await evaluate("window.__reviews.length"), 0);
        assert.equal(await evaluate("window.__plays"), repeat * 3 + index + 1);
        assert.equal(await evaluate("document.getElementById('mainVideo').getAttribute('src')"), sourceUrl);
        assert.equal(await evaluate("window.__seeks.at(-1)"), segment.start);
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
    assert.equal(await evaluate("!!document.querySelector('#app script, #app img:not(.shell-brand > .brand-logo), #app svg, .segment-text b, [onerror], [onload]')"), false);
    assert.equal(await evaluate("document.getElementById('mainVideo').getAttribute('src')"), sourceUrl);
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
      assert.equal(await evaluate("document.getElementById('aiInstructions').value.trim()"), criteria.trim());
      assert.equal(await evaluate("document.getElementById('aiInstructions').checkVisibility()"), true);
      assert.equal(await evaluate("document.getElementById('generationThreshold').textContent"), `Score threshold: ${threshold}+`);
      assert.equal(await evaluate("document.querySelector('#generationControls b')"), null);
      await click('#advancedToggle');
      assert.deepEqual(await evaluate("[document.getElementById('minScoreSlider').value, document.getElementById('scoreValue').textContent]"), [String(threshold), String(threshold)]);
      await click('#previewBtn');
      await until("document.getElementById('previewInfo').classList.contains('visible') && !document.getElementById('previewBtn').disabled");
      assert.match(await evaluate("document.getElementById('previewInfo').textContent"), /Selection Statistics:/);
      assert.equal(await evaluate("document.getElementById('previewBtn').textContent"), 'Selection Statistics');
      assert.doesNotMatch(await evaluate("document.getElementById('app').textContent"), /Preview Selection|playable preview/i);
      await click('#advancedToggle');
      await click('#autoGenerateBtn');
      await until(`document.getElementById('statusMessage').textContent.includes('Ready!') && !document.getElementById('autoGenerateBtn').disabled`);
      assert.match(await evaluate("document.getElementById('statusMessage').textContent"), new RegExp(`score >= ${threshold}`));
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
    await until("document.getElementById('statusMessage').textContent.includes('Ready!') && !document.getElementById('autoGenerateBtn').disabled");
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
      await until("document.querySelectorAll('#activityErrors [role=\"alert\"]').length > 0 && !document.getElementById('previewBtn').disabled");
      assert.match(await evaluate("document.getElementById('activityErrors').textContent"), status === 409 ? /busy.*wait/i : /Run AI scoring/);
      assert.deepEqual(await selected(), [0, 9]);
      assert.deepEqual(await evaluate("[document.getElementById('selectedCount').textContent, document.getElementById('selectedDuration').textContent, document.getElementById('aiInstructions').value, document.getElementById('minScoreSlider').value, document.getElementById('scoreValue').textContent]"), ['2', '3.0s', 'Goals', '8', '8']);
      assert.equal(await evaluate("document.getElementById('autoDownloadRow').outerHTML"), before);
      assert.equal(await evaluate("document.getElementById('autoGenerateBtn').disabled"), false);
      await click('#advancedToggle');
      await click('#autoGenerateBtn');
      await until("document.getElementById('statusMessage').textContent.includes('Ready!') && !document.getElementById('autoGenerateBtn').disabled");
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
      await until("document.querySelectorAll('#activityErrors [role=\"alert\"]').length > 0 && !document.getElementById('autoGenerateBtn').disabled");
      assert.match(await evaluate("document.getElementById('activityErrors').textContent"), stage === 'admission' ? /Run AI scoring/ : /Please try again/);
      assert.deepEqual(await selected(), [0, 9]);
      assert.deepEqual(await evaluate("[document.getElementById('aiInstructions').value, document.getElementById('minScoreSlider').value, document.getElementById('scoreValue').textContent, document.getElementById('rescoreCheck').checked]"), ['Saves', '9', '9', true]);
      assert.equal(await evaluate("document.getElementById('advancedSection').classList.contains('visible')"), false);
      assert.equal(await evaluate("document.getElementById('uploadNewBtn').disabled"), false);
      if (stage === 'admission') assert.equal(await evaluate("document.getElementById('autoDownloadRow').outerHTML"), before);
      await evaluate("window.__errors = {}; window.__autoError = null;");
      await click('#autoGenerateBtn');
      await until("document.getElementById('statusMessage').textContent.includes('Ready!') && !document.getElementById('autoGenerateBtn').disabled");
      assert.match(await evaluate("document.getElementById('statusMessage').textContent"), /Ready!/);
      assert.deepEqual((await scoringRequests()).map(r => [r.body.instructions, r.body.min_score, r.body.rescore]), [['Saves', 9, true], ['Saves', 9, true]]);
    }
  });

  await test("provider and model status stays visible near Generate with Advanced closed", async () => {
    for (const [backend, model] of [['api', 'fixture-api'], ['local', 'fixture-local'], ['g4f', null]]) {
      await load(fixture, { llmSettings: { resolved_backend: backend, model, local_model: model, base_url: 'https://fixture.invalid/v1' } });
      await until("!document.getElementById('autoGenerateBtn').disabled");
      assert.equal(await evaluate("document.getElementById('advancedSection').classList.contains('visible')"), false);
      assert.equal(await evaluate("document.getElementById('llmStatus').closest('#generationControls') === document.getElementById('autoGenerateBtn').closest('#generationControls')"), true);
      assert.equal(await evaluate("document.getElementById('llmStatus').checkVisibility() && document.getElementById('llmNotice').checkVisibility() && document.getElementById('aiInstructions').checkVisibility()"), true);
      assert.match(await evaluate("document.getElementById('llmStatus').textContent"), new RegExp(backend === 'api' ? 'API' : backend));
      if (model) assert.match(await evaluate("document.getElementById('llmStatus').textContent"), new RegExp(model));
      await click('#llmSettingsToggle');
      assert.equal(await evaluate("document.getElementById('llmSettingsPanel').checkVisibility()"), true);
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
      await evaluate("document.getElementById('autoGenerateBtn').dispatchEvent(new Event('click'))");
      assert.equal((await scoringRequests()).length, 0);
      assert.equal(await evaluate("document.querySelectorAll('#activityErrors [data-operation=\"Auto-generation\"]').length"), 1);
      assert.deepEqual(await selected(), [0, 9]);
      assert.deepEqual(await evaluate("[document.getElementById('aiInstructions').value, document.getElementById('minScoreSlider').value]"), ['Goals', '8']);
      assert.equal(await evaluate("document.getElementById('exportBtn').disabled"), false);
    }
  });

  const draftFields = ['llmBaseUrl', 'llmModel', 'llmLocalUrl', 'llmLocalModel', 'llmApiKey'];
  async function editProvider(values) {
    await evaluate(`(${JSON.stringify(values)}).forEach((value, i) => {
      const field = document.getElementById(${JSON.stringify(draftFields)}[i]);
      field.value = value;
      field.dispatchEvent(new Event('input', { bubbles: true }));
    })`);
  }
  const providerDraft = () => evaluate(`${JSON.stringify(draftFields)}.map(id => document.getElementById(id).value)`);

  await test("provider drafts require Save before Test and survive failed saves", async () => {
    await load(fixture, { llmAvailable: true });
    await until("!document.getElementById('autoGenerateBtn').disabled");
    await click('#llmSettingsToggle');
    const draft = ['https://draft.invalid/v1', 'draft-model', 'http://localhost:1234', 'draft-local', 'draft-key'];
    await editProvider(draft);
    assert.equal(await evaluate("document.getElementById('llmTestBtn').disabled"), true);
    assert.match(await evaluate("document.getElementById('llmSettingsHint').textContent"), /Unsaved.*Save before testing/);
    await evaluate("document.getElementById('llmTestBtn').dispatchEvent(new Event('click'))");
    assert.equal(await evaluate("window.__requests.filter(r => r.path === '/api/llm-test').length"), 0);
    await click('#llmSettingsToggle');
    await click('#llmSettingsToggle');
    assert.deepEqual(await providerDraft(), draft);
    await evaluate("window.__errors = { '/api/llm-settings': { status: 500, body: { status: 'error', message: 'Save failed' } } }");
    await click('#llmSaveBtn');
    await until("!document.getElementById('llmSaveBtn').disabled");
    assert.deepEqual(await providerDraft(), draft);
    assert.equal(await evaluate("document.getElementById('llmTestBtn').disabled"), true);
    await evaluate("window.__errors = {};");
    await click('#llmSaveBtn');
    await until("!document.getElementById('llmTestBtn').disabled");
    assert.deepEqual(await providerDraft(), [...draft.slice(0, 4), '']);
    assert.match(await evaluate("document.getElementById('llmApiKey').placeholder"), /saved/);
    await click('#llmTestBtn');
    await until("!document.getElementById('llmTestBtn').disabled");
    const requests = await evaluate("window.__requests.filter(r => r.options?.method === 'POST').map(r => [r.path, JSON.parse(r.options.body)])");
    assert.deepEqual(requests.at(-1), ['/api/llm-test', {}], 'Test still uses the existing saved-settings endpoint');
    assert.deepEqual(requests.at(-2)[1], { base_url: draft[0], model: draft[1], local_url: draft[2], local_model: draft[3], api_key: draft[4] });
    assert.equal('backend' in requests.at(-2)[1], false, 'Provider resolution/extension override stays untouched');
  });

  await test("status refresh preserves edits made during initial load, Save and Test", async () => {
    const draft = ['https://newer.invalid/v1', 'newer-model', 'http://localhost:4321', 'newer-local', 'newer-key'];
    await load(fixture, { llmAvailable: true, llmStatusDelayed: true });
    await until("typeof window.__finishLlmStatus === 'function'");
    await editProvider(draft);
    await evaluate("window.__holdLlmStatus = false; window.__finishLlmStatus()");
    await until("!document.getElementById('autoGenerateBtn').disabled");
    assert.deepEqual(await providerDraft(), draft, 'Initial status cannot overwrite typing');

    await load(fixture, { llmAvailable: true });
    await until("!document.getElementById('autoGenerateBtn').disabled");
    await click('#llmSettingsToggle');
    await evaluate("window.__holdLlmTest = true");
    await click('#llmTestBtn');
    await until("typeof window.__finishLlmTest === 'function'");
    await editProvider(draft);
    await evaluate("window.__finishLlmTest()");
    await until("!document.getElementById('llmSaveBtn').disabled");
    assert.deepEqual(await providerDraft(), draft, 'Successful Test refresh preserves the newer draft');
    assert.equal(await evaluate("document.getElementById('llmTestBtn').disabled"), true);

    await evaluate("window.__holdLlmSave = true");
    await click('#llmSaveBtn');
    await until("typeof window.__finishLlmSave === 'function'");
    const newer = draft.map(value => value + '-edited');
    await editProvider(newer);
    await evaluate("window.__finishLlmSave()");
    await until("!document.getElementById('llmSaveBtn').disabled");
    assert.deepEqual(await providerDraft(), newer, 'Save refresh cannot clear a newer key or other edits');
    assert.equal(await evaluate("document.getElementById('llmTestBtn').disabled"), true);
  });

  await test("required criteria stay visible with scoring options closed and prevent blank generation", async () => {
    await load(fixture, { llmAvailable: true, criteria: '' });
    await until("document.getElementById('llmStatus').textContent.includes('API')");
    assert.equal(await evaluate("document.getElementById('aiInstructions').checkVisibility() && !document.getElementById('advancedSection').checkVisibility()"), true);
    assert.equal(await evaluate("document.getElementById('aiInstructions').required"), true);
    for (const value of ['', '   \n  ']) {
      await setScoring(value, 7);
      assert.equal(await evaluate("document.getElementById('autoGenerateBtn').disabled"), true);
      await click('#autoGenerateBtn');
      assert.equal((await scoringRequests()).length, 0, 'Blank criteria never reach the backend');
    }
    assert.match(await evaluate("document.getElementById('generationHint').textContent"), /Describe your moments/);
    await click('#selectKeywords');
    await setScoring('  Funny exchanges and big plays  ', 7);
    assert.equal(await evaluate("document.getElementById('autoGenerateBtn').disabled"), false);
    assert.match(await evaluate("document.querySelector('.generation-outcome').textContent"), /separate AI reel.*checked clips stay unchanged/);
    await click('#autoGenerateBtn');
    await until("document.getElementById('autoDownloadRow').classList.contains('visible')");
    assert.deepEqual((await scoringRequests()).map(r => [r.body.instructions, r.body.min_score, r.body.rescore]), [['Funny exchanges and big plays', 7, false]]);
    assert.deepEqual(await selected(), [0, 9]);
    assert.equal(await evaluate("document.getElementById('advancedToggle').getAttribute('aria-expanded')"), 'false');
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
      assert.equal(await evaluate("document.styleSheets.length"), 1);
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
      await until(`document.querySelectorAll('#activityErrors [role=\"alert\"]').length > 0 && !document.getElementById(${JSON.stringify(button)}).disabled`);
      assert.match(await evaluate("document.getElementById('activityErrors').textContent"), /busy.*wait/i);
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
    await until("document.querySelectorAll('#activityErrors [role=\"alert\"]').length === 2 && !document.getElementById('exportBtn').disabled && !document.getElementById('previewBtn').disabled");
    assert.match(await evaluate("document.getElementById('activityErrors').textContent"), /busy/);
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
    await until("document.querySelectorAll('#activityErrors [role=\"alert\"]').length > 0 && !document.getElementById('autoGenerateBtn').disabled");
    assert.match(await evaluate("document.getElementById('activityErrors').textContent"), /busy/);
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
    assert.match(await evaluate("document.getElementById('activityErrors').textContent"), /busy.*wait/i);
    assert.equal(await evaluate("window.__requests.filter(r => r.path === '/api/upload').length"), 1);
    assert.equal(await evaluate("window.__requests.some(r => r.path.startsWith('/api/processing-status/'))"), false);
    assert.equal(await evaluate("document.getElementById('uploadButton').disabled"), false);
  });

  await test("default workspace contains decoded landscape, portrait and wide footage and retains native playback", async () => {
    const body = { segments: Array.from({ length: 30 }, (_, id) => ({
      ...fixture.segments[0], id, start: id * 2, end: id * 2 + 1.5, is_keyword: false
    })) };
    await load(body);
    await click('#selectAll');
    for (const [videoWidth, videoHeight] of [[640, 360], [360, 640], [840, 360]]) {
      // Generate tiny, decoded media in Chromium; no external encoder or test dependency.
      await evaluate(`(async () => {
        const canvas = document.createElement('canvas');
        canvas.width = ${videoWidth}; canvas.height = ${videoHeight};
        const context = canvas.getContext('2d');
        const draw = () => {
          context.fillStyle = '#408060'; context.fillRect(0, 0, canvas.width, canvas.height);
          context.fillStyle = '#e0e0e0'; context.fillRect(0, 0, 20, canvas.height);
        };
        draw();
        const stream = canvas.captureStream(15);
        const recorder = new MediaRecorder(stream, { mimeType: 'video/webm;codecs=vp8' });
        const chunks = [];
        recorder.ondataavailable = event => chunks.push(event.data);
        const done = new Promise(resolve => recorder.onstop = resolve);
        const timer = setInterval(draw, 50);
        recorder.start();
        await new Promise(resolve => setTimeout(resolve, 400));
        recorder.stop(); await done; clearInterval(timer);
        stream.getTracks().forEach(track => track.stop());
        if (window.__previewBlob) URL.revokeObjectURL(window.__previewBlob);
        window.__previewBlob = URL.createObjectURL(new Blob(chunks, { type: 'video/webm' }));
        document.getElementById('mainVideo').src = window.__previewBlob;
      })()`);
      await until(`document.getElementById('mainVideo').readyState >= 2 && document.getElementById('mainVideo').videoWidth === ${videoWidth}`);
      for (const [width, height] of [[1736, 864], [1366, 768], [1280, 800], [960, 768], [768, 800], [320, 800], [1200, 500], [320, 500]]) {
        await viewport(width, height);
        const geometry = await evaluate(`(() => {
          const video = document.getElementById('mainVideo');
          const rect = video.getBoundingClientRect();
          const scale = Math.min(rect.width / video.videoWidth, rect.height / video.videoHeight);
          const container = video.parentElement.getBoundingClientRect();
          const list = document.getElementById('segmentsList');
          return { pictureWidth: video.videoWidth * scale, pictureHeight: video.videoHeight * scale,
            elementWidth: rect.width, elementHeight: rect.height, containerHeight: container.height,
            fit: getComputedStyle(video).objectFit, controls: video.controls,
            listHeight: list.clientHeight, scrollable: list.scrollHeight > list.clientHeight };
        })()`);
        assert.equal(geometry.fit, 'contain');
        assert.equal(geometry.controls, true);
        assert.ok(Math.abs(geometry.pictureWidth / geometry.pictureHeight - videoWidth / videoHeight) < .001);
        assert.ok(geometry.pictureWidth <= geometry.elementWidth + 1 && geometry.pictureHeight <= geometry.elementHeight + 1);
        assert.ok(Math.abs(geometry.elementHeight - geometry.containerHeight) < 1, 'Video fills its available height');
        assert.ok(geometry.listHeight >= 100 && geometry.scrollable, 'Transcript retains its own usable scroll area');
        if (videoWidth === 640) {
          const minimumWidth = height < 600 ? width >= 960 ? 600 : width >= 768 ? 310 : 270
              : width === 1736 ? 820 : width >= 1280 ? 735 : width >= 960 ? 540 : width >= 768 ? 310 : 270;
          assert.ok(geometry.pictureWidth >= minimumWidth, `${width}×${height}: actual picture is ${geometry.pictureWidth}px wide`);
        }
      }
      await evaluate("document.getElementById('mainVideo').play()");
      await until("document.getElementById('mainVideo').currentTime > .05");
      await evaluate("document.getElementById('mainVideo').pause(); document.getElementById('mainVideo').currentTime = .1");
      await until("!document.getElementById('mainVideo').seeking");
      assert.deepEqual(await selected(), body.segments.map(segment => segment.id), 'Native playback and seek never change inclusion');
    }
    await evaluate("URL.revokeObjectURL(window.__previewBlob)");
    await viewport(1736, 864);
  });

  await test("enlarged checkbox labels select once without reviewing", async () => {
    await viewport(320, 800);
    await load();
    await observeReview();
    const target = '.segment-selection';
    assert.deepEqual(await evaluate(`(() => {
      const rect = document.querySelector('${target}').getBoundingClientRect();
      return [rect.width, rect.height];
    })()`), [44, 44]);
    // Click the label padding, away from the checkbox itself.
    const { x, y } = await evaluate(`(() => {
      const rect = document.querySelector('${target}').getBoundingClientRect();
      return { x: rect.x + 3, y: rect.y + 3 };
    })()`);
    await page('Input.dispatchMouseEvent', { type: 'mousePressed', x, y, button: 'left', clickCount: 1 });
    await page('Input.dispatchMouseEvent', { type: 'mouseReleased', x, y, button: 'left', clickCount: 1 });
    assert.deepEqual(await selected(), [0]);
    assert.deepEqual(await evaluate('window.__reviews'), []);
    assert.equal(await evaluate('window.__plays'), 0);
    await viewport(1736, 864);
  });

  await test("U1 shell regions and breakpoint geometry keep the central editor usable", async () => {
    const body = { segments: Array.from({ length: 36 }, (_, id) => ({
      ...fixture.segments[0], id, start: id * 10, end: id * 10 + 1.5, is_keyword: id % 3 === 0,
      text: ('A longer transcript passage with readable wrapping and ordinary punctuation. ').repeat(4)
    })) };
    await load(body, { llmAvailable: true });
    await until("document.querySelector('.brand-logo')?.naturalWidth > 0");
    assert.equal(await evaluate("document.querySelector('link[rel=\"icon\"]').href === document.querySelector('.brand-logo').src"), true);
    await evaluate("document.getElementById('selectAll').click()");
    await setScoring('Long criteria '.repeat(50), 9);
    assert.deepEqual(await evaluate(`[
      document.getElementById('exportBtn').closest('header') !== null,
      document.getElementById('uploadNewBtn').closest('#mediaPanel') !== null,
      document.querySelector('.subtitles-panel').closest('#mediaPanel') !== null,
      document.getElementById('mainVideo').closest('#centralEditor') !== null,
      document.getElementById('segmentsList').closest('#centralEditor') !== null,
      document.getElementById('generationControls').closest('#contextPanel') !== null,
      document.getElementById('previewBtn').closest('#contextPanel') !== null,
      document.getElementById('downloadLink').closest('footer') !== null,
      document.getElementById('autoDownloadRow').closest('footer') !== null
    ]`), Array(9).fill(true));
    for (const [width, height] of [[1736, 864], [1600, 864], [1599, 864], [1366, 768], [1200, 800], [1280, 800],
      [960, 768], [959, 768], [768, 800], [320, 800], [1200, 500], [320, 500]]) {
      await viewport(width, height);
      const geometry = await evaluate(`(() => {
        const box = id => document.getElementById(id).getBoundingClientRect();
        return {
          media: document.getElementById('mediaPanel').checkVisibility(),
          context: document.getElementById('contextPanel').checkVisibility(),
          mediaTrigger: document.getElementById('mediaTrigger').checkVisibility(),
          contextTrigger: document.getElementById('contextTrigger').checkVisibility(),
          sideways: document.documentElement.scrollWidth > innerWidth,
          preview: document.querySelector('.preview-panel').getBoundingClientRect().toJSON(),
          transcript: document.querySelector('.segments-panel').getBoundingClientRect().toJSON(),
          centralWidth: box('centralEditor').width, listHeight: box('segmentsList').height,
          previewHeight: document.querySelector('.preview-panel').getBoundingClientRect().height,
          footerBottom: document.querySelector('footer').getBoundingClientRect().bottom,
          exportRight: box('exportBtn').right,
          duplicateIds: [...document.querySelectorAll('[id]')].length !== new Set([...document.querySelectorAll('[id]')].map(el => el.id)).size,
          overflowRows: [...document.querySelectorAll('.segment-row')].some(row => row.scrollWidth > row.clientWidth)
        };
      })()`);
      assert.equal(geometry.media, false, `${width}: Media remains in its drawer`);
      assert.equal(geometry.context, width >= 1600, `${width}: AI docking`);
      assert.equal(geometry.mediaTrigger, true);
      assert.equal(geometry.contextTrigger, true);
      assert.equal(geometry.sideways, false, `${width}×${height}: no horizontal page scroll`);
      assert.equal(geometry.overflowRows, false, `${width}: transcript actions fit`);
      assert.equal(geometry.duplicateIds, false);
      const pageWidth = await evaluate("document.documentElement.clientWidth");
      assert.ok(geometry.centralWidth >= pageWidth - (width >= 1600 ? 336 : 24), `${width}×${height}: central width ${geometry.centralWidth}`);
      assert.ok(geometry.listHeight >= 100, `${width}×${height}: transcript has usable scroll space (${geometry.listHeight})`);
      assert.ok(geometry.previewHeight >= 210, `${width}×${height}: usable preview height`);
      if (width >= 960) {
        assert.ok(Math.abs(geometry.preview.top - geometry.transcript.top) < 1, 'Preview and Transcript are beside each other');
        assert.ok(Math.abs(geometry.preview.height - geometry.transcript.height) < 1, 'Transcript uses the full workspace height');
        const split = geometry.preview.width / (geometry.preview.width + geometry.transcript.width);
        assert.ok(Math.abs(split - .6) < .01, 'Preview/Transcript retain the approved 60/40 split');
      } else {
        assert.ok(geometry.transcript.top >= geometry.preview.bottom, 'Below 960px Transcript stacks below Preview');
      }
      if (height >= 600 && width > 480 && width < 960) {
        const centralHeight = await evaluate("document.getElementById('centralEditor').clientHeight");
        assert.ok(geometry.previewHeight / centralHeight >= .45 && geometry.previewHeight / centralHeight <= .55,
          `${width}×${height}: preview uses about half the central height`);
      }
      assert.ok(geometry.footerBottom <= height + 1, `${width}×${height}: Activity remains visible`);
      assert.ok(geometry.exportRight <= width, `${width}: export is reachable`);
      if (height >= 600) {
        const before = await evaluate("document.querySelector('.preview-panel').getBoundingClientRect().top");
        await evaluate("document.getElementById('segmentsList').scrollTop = 600");
        assert.equal(await evaluate("document.querySelector('.preview-panel').getBoundingClientRect().top"), before);
      }
      if (process.env.SELECTOR_SCREENSHOTS) {
        await mkdir(process.env.SELECTOR_SCREENSHOTS, { recursive: true });
        const capture = await page('Page.captureScreenshot', { format: 'png' });
        await writeFile(join(process.env.SELECTOR_SCREENSHOTS, `${width}x${height}.png`), Buffer.from(capture.data, 'base64'));
      }
    }
    await viewport(1736, 864);
  });

  await test("Media and Context drawers trap/restore focus and preserve the same live session controls", async () => {
    await viewport(1200, 800);
    await load(fixture, { llmAvailable: true });
    await until("!document.getElementById('autoGenerateBtn').disabled");
    await click('#selectKeywords');
    await click('#contextTrigger');
    await click('#advancedToggle');
    await setScoring('Keep these goals', 8);
    await click('#rescoreCheck');
    await click('#previewBtn');
    await until("document.getElementById('previewInfo').classList.contains('visible')");
    await evaluate(`
      window.__mediaNode = document.getElementById('mediaPanel');
      window.__contextNode = document.getElementById('contextPanel');
      window.__criteriaNode = document.getElementById('aiInstructions');
      window.__pauses = 0;
      document.getElementById('mainVideo').pause = () => window.__pauses++;
      document.getElementById('mainVideo').currentTime = 0.5;
    `);
    const state = () => evaluate(`[
      document.getElementById('selectedCount').textContent, document.getElementById('selectedDuration').textContent,
      document.getElementById('aiInstructions').value, document.getElementById('minScoreSlider').value,
      document.getElementById('rescoreCheck').checked, document.getElementById('previewInfo').outerHTML,
      document.getElementById('mainVideo').getAttribute('src'), document.getElementById('mainVideo').currentTime
    ]`);
    await press('Escape', 'Escape', 27);
    const before = await state();
    for (const [width, height] of [[1200, 800], [768, 800], [320, 800], [1200, 500], [320, 500]]) {
      await viewport(width, height);
      for (const region of ['media', 'context']) {
        const pane = `#${region}Panel`;
        await click(`#${region}Trigger`);
        assert.equal(await evaluate(`document.activeElement.id`), `${region}Heading`);
        assert.equal(await evaluate(`document.querySelector('${pane}').getAttribute('aria-modal')`), 'true');
        assert.equal(await evaluate(`(() => {
          const rect = document.querySelector('${pane}').getBoundingClientRect();
          return rect.top >= document.querySelector('header').getBoundingClientRect().bottom &&
            rect.bottom <= document.querySelector('footer').getBoundingClientRect().top;
        })()`), true, `${width}×${height}: drawer leaves header and Activity visible`);
        assert.equal(await evaluate("document.getElementById('centralEditor').inert"), true);
        assert.equal(await evaluate("document.getElementById('statusMessage').closest('[inert]')"), null);
        await press('Tab', 'Tab', 9);
        assert.equal(await evaluate(`document.activeElement.matches('${pane} .drawer-close')`), true);
        const last = region === 'media' ? 'button[data-format="vtt"]' : '#resetSettingsBtn';
        await until(`!document.querySelector(${JSON.stringify(`${pane} ${last}`)}).disabled`);
        await evaluate(`document.querySelector(${JSON.stringify(`${pane} ${last}`)}).focus()`);
        await press('Tab', 'Tab', 9);
        assert.equal(await evaluate(`document.activeElement.matches('${pane} .drawer-close')`), true);
        await page('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Tab', code: 'Tab', windowsVirtualKeyCode: 9, modifiers: 8 });
        await page('Input.dispatchKeyEvent', { type: 'keyUp', key: 'Tab', code: 'Tab', windowsVirtualKeyCode: 9, modifiers: 8 });
        assert.equal(await evaluate(`document.activeElement.matches(${JSON.stringify(`${pane} ${last}`)})`), true);
        await press('Escape', 'Escape', 27);
        assert.equal(await evaluate('document.activeElement.id'), `${region}Trigger`);
        assert.equal(await evaluate(`document.querySelector('${pane}').checkVisibility()`), false);
        assert.equal(await evaluate("document.getElementById('centralEditor').inert"), false);
        await click(`#${region}Trigger`);
        await click(`${pane} .drawer-close`);
        assert.equal(await evaluate('document.activeElement.id'), `${region}Trigger`);
        assert.deepEqual(await state(), before);
      }
    }
    await viewport(768);
    await click('#contextTrigger');
    await evaluate("document.getElementById('mediaTrigger').click()");
    assert.equal(await evaluate("document.querySelectorAll('.drawer-open').length"), 1);
    assert.equal(await evaluate('document.activeElement.id'), 'mediaHeading');
    await click('#drawerBackdrop');
    assert.equal(await evaluate('document.activeElement.id'), 'mediaTrigger');
    assert.equal(await evaluate("window.__mediaNode === document.getElementById('mediaPanel') && window.__contextNode === document.getElementById('contextPanel') && window.__criteriaNode === document.getElementById('aiInstructions')"), true);
    assert.equal(await evaluate("window.__pauses"), 0);
    assert.deepEqual(await selected(), [0, 9]);
    await viewport(320, 500);
    await evaluate("window.scrollTo(0, document.documentElement.scrollHeight)");
    await click('#contextTrigger');
    assert.equal(await evaluate("document.getElementById('contextHeading').getBoundingClientRect().top >= document.querySelector('header').getBoundingClientRect().bottom"), true);
    await press('Escape', 'Escape', 27);
    assert.equal(await evaluate('document.activeElement.id'), 'contextTrigger');
    await viewport(1736, 864);
  });

  await test("focused panes survive responsive docking and disclosures never hide the active focus", async () => {
    await viewport(1736, 864);
    await load(fixture, { llmAvailable: true });
    await click('#llmSettingsToggle');
    assert.equal(await evaluate('document.activeElement.id'), 'llmBaseUrl');
    await viewport(1599, 864);
    assert.equal(await evaluate('document.activeElement.id'), 'llmBaseUrl');
    assert.equal(await evaluate("document.getElementById('contextPanel').classList.contains('drawer-open')"), true);
    await press('Escape', 'Escape', 27);
    assert.equal(await evaluate('document.activeElement.id'), 'contextTrigger');
    await viewport(1600, 864);
    assert.equal(await evaluate('document.activeElement.id'), 'contextHeading');
    assert.equal(await evaluate("document.getElementById('contextPanel').hasAttribute('aria-modal')"), false);
    await viewport(1599, 864);
    await click('#contextTrigger');
    await evaluate("document.querySelector('#contextPanel .drawer-close').focus()");
    await viewport(1600, 864);
    assert.equal(await evaluate("document.activeElement.matches('#contextPanel .drawer-close')"), true);
    assert.equal(await evaluate('document.activeElement.textContent'), 'Hide');
    await evaluate("document.getElementById('llmBaseUrl').focus(); document.getElementById('llmSettingsToggle').click()");
    assert.equal(await evaluate('document.activeElement.id'), 'llmSettingsToggle');
    assert.equal(await evaluate("document.getElementById('llmSettingsToggle').getAttribute('aria-expanded')"), 'false');
    await evaluate("document.getElementById('advancedToggle').click(); document.getElementById('minScoreSlider').focus(); document.getElementById('advancedToggle').click()");
    assert.equal(await evaluate('document.activeElement.id'), 'advancedToggle');
    assert.equal(await evaluate("document.getElementById('advancedToggle').getAttribute('aria-expanded')"), 'false');
    await viewport(1280, 768);
    // Resizing the focused AI dock opens its drawer; close it before opening Media.
    await press('Escape', 'Escape', 27);
    await click('#mediaTrigger');
    await evaluate("document.getElementById('uploadNewBtn').focus()");
    await viewport(1279, 768);
    assert.equal(await evaluate('document.activeElement.id'), 'uploadNewBtn');
    assert.equal(await evaluate("document.getElementById('mediaPanel').classList.contains('drawer-open')"), true);
    await viewport(1280, 768);
    assert.equal(await evaluate('document.activeElement.id'), 'uploadNewBtn');
    assert.equal(await evaluate("document.getElementById('mediaPanel').getAttribute('aria-modal')"), 'true');
    assert.equal(await evaluate("document.getElementById('centralEditor').inert"), true);
    await viewport(1279, 768);
    await press('Escape', 'Escape', 27);
    await viewport(1280, 768);
    assert.equal(await evaluate('document.activeElement.id'), 'mediaTrigger');
    await evaluate("document.getElementById('transcriptHeading').focus()");
    await viewport(959, 768);
    await setScoring('', 7);
    await evaluate("document.getElementById('autoGenerateBtn').dispatchEvent(new Event('click'))");
    assert.equal(await evaluate('document.activeElement.id'), 'aiInstructions');
    assert.equal(await evaluate("document.getElementById('aiInstructions').getAttribute('aria-invalid')"), 'true');
    assert.equal(await evaluate("document.getElementById('advancedToggle').getAttribute('aria-expanded')"), 'false');
    assert.equal(await evaluate("document.getElementById('contextPanel').getAttribute('aria-modal')"), 'true');
    assert.equal((await scoringRequests()).length, 0);
    await press('Escape', 'Escape', 27);
    assert.equal(await evaluate('document.activeElement.id'), 'contextTrigger');
    await viewport(1736, 864);
  });

  await test("row keyboard review and rebuild/filter focus preserve explicit selection semantics", async () => {
    await load(fixture);
    await observeReview();
    await evaluate("document.querySelector('.segment-review[data-id=\"4\"]').focus()");
    await press('Enter', 'Enter', 13);
    assert.deepEqual(await selected(), []);
    assert.equal(await evaluate('window.__seeks.length'), 1);
    await evaluate("document.querySelector('.play-btn[data-id=\"4\"]').focus()");
    await press(' ', 'Space', 32);
    assert.deepEqual(await selected(), []);
    assert.equal(await evaluate('window.__plays'), 1);
    await evaluate("document.querySelector('.segment-checkbox[data-id=\"4\"]').focus(); document.getElementById('selectAll').click()");
    assert.equal(await evaluate("document.activeElement.matches('.segment-checkbox[data-id=\"4\"]')"), true);
    assert.deepEqual(await selected(), [0, 4, 9]);
    await evaluate("document.getElementById('showOnlyKeywords').checked = true; document.getElementById('showOnlyKeywords').dispatchEvent(new Event('change'))");
    assert.equal(await evaluate('document.activeElement.id'), 'showOnlyKeywords');
    assert.match(await evaluate("document.getElementById('filterSummary').textContent"), /2 shown · 1 selected hidden/);
    assert.equal(await evaluate("document.getElementById('headerSelectedCount').textContent"), '3 selected');
    await click('#selectKeywords');
    assert.equal(await evaluate('document.activeElement.id'), 'selectKeywords');
    assert.equal(await evaluate('window.__seeks.length'), 2);
  });

  await test("Activity keeps jobs, independent errors and results visible through drawers and disclosures", async () => {
    await viewport(768);
    await load(fixture, { llmAvailable: true });
    await until("!document.getElementById('autoGenerateBtn').disabled");
    await setScoring('Goals', 8);
    await evaluate(`
      document.getElementById('selectKeywords').click();
      window.__holdAutoPoll = true;
      window.__busyPaths = ['/api/export', '/api/preview-auto-export'];
      document.getElementById('autoGenerateBtn').click();
    `);
    await until("typeof window.__finishAuto === 'function'");
    await click('#mediaTrigger');
    await evaluate("document.getElementById('exportBtn').click(); document.getElementById('previewBtn').click()");
    await until("document.querySelectorAll('#activityErrors [role=\"alert\"]').length === 2");
    assert.match(await evaluate("document.getElementById('statusMessage').textContent"), /Scoring fixture/);
    assert.equal(await evaluate("document.getElementById('statusMessage').checkVisibility() && !document.getElementById('statusMessage').closest('[inert]')"), true);
    assert.equal(await evaluate("[...document.querySelectorAll('#activityErrors [role=\"alert\"]')].every(node => node.checkVisibility() && !node.closest('[inert]'))"), true);
    assert.ok(await evaluate("document.querySelector('footer').getBoundingClientRect().bottom <= innerHeight"));
    await press('Escape', 'Escape', 27);
    await click('#contextTrigger');
    await evaluate("document.getElementById('advancedToggle').click()");
    assert.equal(await evaluate("document.querySelectorAll('#activityErrors [role=\"alert\"]').length"), 2);
    await press('Escape', 'Escape', 27);
    await evaluate("window.__finishAuto()");
    await until("!document.getElementById('autoGenerateBtn').disabled");
    assert.equal(await evaluate("document.getElementById('autoDownloadLink').checkVisibility()"), true);
    await evaluate("document.getElementById('discardAutoBtn').focus(); document.getElementById('activityToggle').click()");
    assert.equal(await evaluate('document.activeElement.id'), 'activityToggle');
    assert.match(await evaluate("document.getElementById('activityErrorSummary').textContent"), /2 request errors.*Details/);
    assert.match(await evaluate("document.getElementById('statusMessage').textContent"), /Ready!/);
    await click('#activityToggle');
    await evaluate("document.querySelector('#activityErrors button').click()");
    assert.equal(await evaluate("document.querySelectorAll('#activityErrors [role=\"alert\"]').length"), 1);
    assert.equal(await evaluate('document.activeElement.id'), 'activityToggle');
    await evaluate("window.__busyPaths = []; document.getElementById('previewBtn').click()");
    await until("!document.getElementById('previewBtn').disabled");
    assert.equal(await evaluate("document.querySelectorAll('#activityErrors [role=\"alert\"]').length"), 0);
    await viewport(1736, 864);
  });

  await test("keyboard import, review, selection, export and original subtitles work wide and narrow", async () => {
    const downloads = join(profile, 'downloads');
    await mkdir(downloads, { recursive: true });
    await send('Browser.setDownloadBehavior', { behavior: 'allow', downloadPath: downloads });
    const source = join(profile, 'keyboard-recording.mp4');
    await writeFile(source, 'fixture');
    await page('Page.setInterceptFileChooserDialog', { enabled: true });
    for (const width of [1366, 320]) {
      await viewport(width);
      await load({ segments: [] });
      assert.equal(await evaluate('document.activeElement.id'), 'uploadButton');
      assert.equal(await evaluate("!!document.getElementById('exportBtn') || !!document.getElementById('contextPanel')"), false);
      await until("!document.querySelector('button[data-format=\"vtt\"]').disabled");
      await tabTo('button[data-format="vtt"]');
      const vtt = join(downloads, 'original-recording.vtt');
      await rm(vtt, { force: true });
      await press('Enter', 'Enter', 13);
      for (let retry = 0; retry < 200 && !existsSync(vtt); retry++) await new Promise(resolve => setTimeout(resolve, 20));
      assert.ok(existsSync(vtt), 'Original VTT downloads on the import screen');
      await tabTo('#uploadButton');
      await evaluate("window.__holdUploadPoll = true");
      await press('Enter', 'Enter', 13);
      const { root } = await page('DOM.getDocument');
      const { nodeId } = await page('DOM.querySelector', { nodeId: root.nodeId, selector: '#videoInput' });
      await page('DOM.setFileInputFiles', { nodeId, files: [source] });
      await until("typeof window.__finishUpload === 'function'");
      assert.equal(await evaluate("document.getElementById('progressBar').getAttribute('aria-valuenow')"), '42');
      assert.match(await evaluate("document.getElementById('statusMessage').textContent"), /Preparing transcript/);
      assert.equal(await evaluate("[...document.querySelectorAll('button[data-format]')].every(button => button.disabled)"), true);
      await evaluate("window.__finishUpload()");
      await until("!!document.getElementById('transcriptHeading')");
      assert.equal(await evaluate('document.activeElement.id'), 'transcriptHeading');
      assert.equal(await evaluate("document.getElementById('recordingContext').textContent"), 'keyboard-recording.mp4');
      await observeReview();
      await press('Tab', 'Tab', 9);
      assert.equal(await evaluate('document.activeElement.id'), 'selectAll');
      await press('Enter', 'Enter', 13);
      assert.deepEqual(await selected(), [0, 4, 9]);
      await tabTo('#selectKeywords');
      await press('Enter', 'Enter', 13);
      assert.deepEqual(await selected(), [0, 9]);
      await tabTo('.segment-review[data-id="0"]');
      await press('Enter', 'Enter', 13);
      assert.deepEqual(await selected(), [0, 9]);
      await tabTo('#exportBtn');
      await press('Enter', 'Enter', 13);
      await until("document.getElementById('downloadLink').checkVisibility()");
      assert.deepEqual(await evaluate("JSON.parse(window.__requests.find(r => r.path === '/api/export').options.body).selected_ids"), [0, 9]);
      await tabTo('#mediaTrigger'); await press('Enter', 'Enter', 13);
      await until("!document.querySelector('button[data-format=\"srt\"]').disabled");
      await tabTo('button[data-format="srt"]');
      const srt = join(downloads, 'original-recording.srt');
      await rm(srt, { force: true });
      await press('Enter', 'Enter', 13);
      for (let retry = 0; retry < 200 && !existsSync(srt); retry++) await new Promise(resolve => setTimeout(resolve, 20));
      assert.ok(existsSync(srt), 'Original SRT downloads during review');
      await press('Escape', 'Escape', 27);
      assert.equal(await evaluate("window.__alerts.length"), 0, 'Feedback never opens blocking alerts');
    }
    await page('Page.setInterceptFileChooserDialog', { enabled: false });
    await viewport(1736, 864);
  });

  await test("default workspace drawers and optional AI docking preserve live nodes, focus, drafts and selection", async () => {
    await viewport(1736, 864);
    await load(fixture, { llmAvailable: true });
    await observeReview();
    await click('#selectKeywords');
    await click('.segment-review[data-id="4"]');
    assert.deepEqual(await selected(), [0, 9]);
    assert.match(await evaluate("document.getElementById('clipContextText').textContent"), /ordinary play/);
    await click('#llmSettingsToggle');
    await evaluate(`
      window.__videoNode = document.getElementById('mainVideo');
      window.__criteriaNode = document.getElementById('aiInstructions');
      window.__contextNode = document.getElementById('contextPanel');
      document.getElementById('llmModel').value = 'unsaved-draft';
      document.getElementById('llmModel').dispatchEvent(new Event('input'));
    `);
    assert.equal(await evaluate("document.getElementById('llmTestBtn').disabled"), true);
    const before = await evaluate("document.querySelector('.preview-panel').getBoundingClientRect().width");
    await click('#contextPanel .drawer-close');
    assert.equal(await evaluate('document.activeElement.id'), 'contextTrigger');
    assert.equal(await evaluate("document.getElementById('contextPanel').checkVisibility()"), false);
    assert.ok(await evaluate("document.querySelector('.preview-panel').getBoundingClientRect().width") > before + 150);
    await click('#contextTrigger');
    assert.equal(await evaluate("document.getElementById('contextPanel').hasAttribute('aria-modal')"), false);
    await evaluate("document.getElementById('llmModel').focus()");
    await viewport(1599, 864);
    assert.equal(await evaluate('document.activeElement.id'), 'llmModel');
    assert.equal(await evaluate("document.getElementById('contextPanel').getAttribute('aria-modal')"), 'true');
    await press('Escape', 'Escape', 27);
    assert.equal(await evaluate('document.activeElement.id'), 'contextTrigger');
    for (const [width, height] of [[1736,864], [1366,768], [1280,800], [960,720], [768,800], [1366,500], [320,500]]) {
      await viewport(width, height);
      assert.equal(await evaluate('document.documentElement.scrollWidth > innerWidth'), false);
      await click('#mediaTrigger');
      assert.equal(await evaluate('document.activeElement.id'), 'mediaHeading');
      await press('Tab', 'Tab', 9);
      assert.equal(await evaluate("document.activeElement.matches('#mediaPanel .drawer-close')"), true);
      await evaluate("document.querySelector('#mediaPanel button[data-format=\"vtt\"]').focus()");
      await press('Tab', 'Tab', 9);
      assert.equal(await evaluate("document.activeElement.matches('#mediaPanel .drawer-close')"), true);
      assert.equal(await evaluate("document.getElementById('centralEditor').inert"), true);
      assert.equal(await evaluate("document.getElementById('statusMessage').closest('[inert]')"), null);
      assert.equal(await evaluate(`(() => { const r=document.getElementById('mediaPanel').getBoundingClientRect(); return r.top >= document.querySelector('header').getBoundingClientRect().bottom && r.bottom <= document.querySelector('footer').getBoundingClientRect().top; })()`), true);
      await press('Escape', 'Escape', 27);
      assert.equal(await evaluate('document.activeElement.id'), 'mediaTrigger');
      if (width < 1600) {
        await click('#contextTrigger');
        assert.equal(await evaluate('document.activeElement.id'), 'contextHeading');
        await evaluate("document.getElementById('resetSettingsBtn').focus()");
        // Provider draft remains the final visible control while its form is open.
        await evaluate("document.getElementById('llmSaveBtn').focus()");
        await press('Tab', 'Tab', 9);
        assert.equal(await evaluate("document.activeElement.matches('#contextPanel .drawer-close')"), true);
        await press('Escape', 'Escape', 27);
        assert.equal(await evaluate('document.activeElement.id'), 'contextTrigger');
      }
      assert.equal(await evaluate("document.getElementById('llmModel').value"), 'unsaved-draft');
      assert.equal(await evaluate("document.getElementById('llmTestBtn').disabled"), true);
      assert.equal(await evaluate("window.__videoNode === document.getElementById('mainVideo') && window.__criteriaNode === document.getElementById('aiInstructions') && window.__contextNode === document.getElementById('contextPanel')"), true);
      assert.deepEqual(await selected(), [0, 9]);
      assert.equal(await evaluate("window.__seeks.length"), 1);
    }
    await viewport(1736,864);
    await click('#contextPanel .drawer-close');
    await setScoring('', 7);
    await evaluate("document.getElementById('autoGenerateBtn').dispatchEvent(new Event('click'))");
    assert.equal(await evaluate('document.activeElement.id'), 'aiInstructions');
    assert.equal(await evaluate("document.getElementById('contextPanel').checkVisibility()"), true);
    assert.equal((await scoringRequests()).length, 0);
    await viewport(1736,864);
  });

  await test("default workspace manual and automatic reels retain criteria snapshots, statistics and independent Activity errors", async () => {
    await viewport(1366,768);
    await load(fixture, { llmAvailable: true });
    await click('#selectKeywords');
    await click('#contextTrigger');
    await setScoring('  Great goals  ', 8);
    await click('#advancedToggle');
    await click('#previewBtn');
    await until("document.getElementById('previewInfo').classList.contains('visible')");
    await evaluate("window.__holdAutoPoll = true; window.__busyPaths = ['/api/export'];");
    await click('#autoGenerateBtn');
    await until("typeof window.__finishAuto === 'function'");
    await evaluate("document.getElementById('exportBtn').click()");
    await until("document.querySelector('#activityErrors [role=\"alert\"]')?.textContent.includes('busy')");
    assert.match(await evaluate("document.getElementById('statusMessage').textContent"), /Scoring fixture/);
    assert.equal(await evaluate("document.getElementById('statusMessage').checkVisibility() && !document.getElementById('statusMessage').closest('[inert]')"), true);
    await evaluate("window.__finishAuto()");
    await until("!document.getElementById('autoGenerateBtn').disabled");
    await press('Escape','Escape',27);
    assert.equal(await evaluate("document.getElementById('contextPanel').checkVisibility()"), false);
    assert.equal(await evaluate("document.getElementById('autoDownloadLink').checkVisibility()"), true);
    const auto = (await scoringRequests()).find(request => request.path === '/api/auto-generate');
    assert.equal(auto.body.instructions, 'Great goals');
    assert.equal(auto.body.min_score, 8);
    assert.deepEqual(await selected(), [0,9]);
    await evaluate("window.__busyPaths = []");
    await click('#exportBtn');
    await until("document.getElementById('downloadLink').checkVisibility()");
    assert.equal(await evaluate("document.getElementById('autoDownloadLink').checkVisibility()"), true);
    await click('#discardAutoBtn');
    await until("!document.getElementById('autoDownloadRow').classList.contains('visible')");
    assert.deepEqual(await selected(), [0,9]);
    await viewport(1736,864);
  });

  await test("default workspace keyboard import, filter, review, manual export and original downloads remain usable", async () => {
    const downloads = join(profile,'downloads');
    await mkdir(downloads,{recursive:true});
    await send('Browser.setDownloadBehavior',{behavior:'allow',downloadPath:downloads});
    for (const width of [1736,768]) {
      await viewport(width,800);
      await load({segments:[]},{});
      assert.equal(await evaluate('document.activeElement.id'),'uploadButton');
      await upload();
      assert.equal(await evaluate('document.activeElement.id'),'transcriptHeading');
      assert.equal(await evaluate("location.search === '' && !!document.querySelector('.editor-shell')"),true);
      await observeReview();
      await press('Tab','Tab',9);
      assert.equal(await evaluate('document.activeElement.id'),'selectAll');
      await press('Enter','Enter',13);
      await tabTo('#showOnlyKeywords');
      await press(' ','Space',32);
      assert.deepEqual(await ids(),[0,9]);
      assert.deepEqual(await selected(),[0,9]);
      await click('#selectNone');
      assert.deepEqual(await selected(),[]);
      await tabTo('#selectKeywords');
      await press('Enter','Enter',13);
      await tabTo('.segment-review[data-id="9"]');
      await press('Enter','Enter',13);
      assert.deepEqual(await selected(),[0,9]);
      await tabTo('#exportBtn');
      await press('Enter','Enter',13);
      await until("document.getElementById('downloadLink').checkVisibility()");
      await tabTo('#mediaTrigger');
      await press('Enter','Enter',13);
      for (const format of ['srt','vtt']) {
        const file=join(downloads,`original-recording.${format}`);
        await rm(file,{force:true});
        await until(`!document.querySelector('button[data-format="${format}"]').disabled`);
        await tabTo(`button[data-format="${format}"]`);
        await press('Enter','Enter',13);
        for(let retry=0;retry<200&&!existsSync(file);retry++) await new Promise(resolve=>setTimeout(resolve,20));
        assert.ok(existsSync(file));
      }
      await press('Escape','Escape',27);
      assert.deepEqual(await selected(),[0,9]);
      await click('#mediaTrigger');
      await click('#uploadNewBtn');
      await until("!!document.getElementById('uploadButton')");
      assert.equal(await evaluate("location.search === '' && !!document.querySelector('.editor-shell')"),true);
    }
    await viewport(1736,864);
  });

  await test('source timeline seeks gaps, drags, follows native playback and never changes inclusion', async () => {
    await viewport(1736, 864);
    await load(fixture);
    await click('#selectKeywords');
    assert.equal(await evaluate("document.getElementById('mainVideo').duration"), 20);
    assert.equal(await evaluate("document.getElementById('sourceScrubber').getAttribute('aria-valuemax')"), '20');
    const point = await evaluate(`(() => {const r=document.getElementById('sourceScrubber').getBoundingClientRect();return {x:r.left+r.width*.5,y:r.top+30,width:r.width};})()`);
    await page('Input.dispatchMouseEvent', { type:'mousePressed',x:point.x,y:point.y,button:'left',clickCount:1 });
    await page('Input.dispatchMouseEvent', { type:'mouseReleased',x:point.x,y:point.y,button:'left',clickCount:1 });
    await until("!document.getElementById('mainVideo').seeking && Math.abs(document.getElementById('mainVideo').currentTime - 10) < .05");
    assert.equal(await evaluate("document.querySelectorAll('.segment-row.active').length"), 0);
    await page('Input.dispatchMouseEvent', { type:'mousePressed',x:point.x,y:point.y,button:'left',clickCount:1 });
    await page('Input.dispatchMouseEvent', { type:'mouseMoved',x:point.x+point.width*.25,y:point.y,button:'left',buttons:1 });
    assert.ok(Math.abs(await evaluate("document.getElementById('mainVideo').currentTime") - 15) < .05);
    await page('Input.dispatchMouseEvent', { type:'mouseReleased',x:point.x+point.width*.25,y:point.y,button:'left',clickCount:1 });
    await until("!document.getElementById('mainVideo').seeking");
    await press('ArrowLeft','ArrowLeft',37);
    assert.ok(Math.abs(await evaluate("document.getElementById('mainVideo').currentTime") - 14) < .05);
    await click('.segment-review[data-id="4"]');
    await until("!document.getElementById('mainVideo').seeking");
    assert.equal(await evaluate("document.getElementById('mainVideo').currentTime"), 1.5);
    assert.equal(await evaluate("document.querySelector('.timeline-segment.active').dataset.id"), '4');
    await evaluate("document.getElementById('mainVideo').play()");
    await until("document.getElementById('mainVideo').currentTime > 1.7");
    await evaluate("document.getElementById('mainVideo').pause()");
    assert.ok(Math.abs(await evaluate("Number(document.getElementById('sourceScrubber').getAttribute('aria-valuenow')) - document.getElementById('mainVideo').currentTime")) < .3);
    assert.deepEqual(await selected(), [0,9]);
    assert.deepEqual(await evaluate("[...document.querySelectorAll('.timeline-segment.selected')].map(n=>Number(n.dataset.id))"), [0,9]);
    await click('.segment-checkbox[data-id="4"]');
    assert.deepEqual(await evaluate("[...document.querySelectorAll('.timeline-segment.selected')].map(n=>Number(n.dataset.id))"), [0,4,9]);
    const buffer = await evaluate(`(() => {const v=document.getElementById('mainVideo'),n=document.querySelector('#timelineBuffer span');return {count:v.buffered.length,marks:document.querySelectorAll('#timelineBuffer span').length,width:n?parseFloat(n.style.width):0,end:v.buffered.length?v.buffered.end(0):0,buffer:n?getComputedStyle(n).backgroundColor:null,selection:getComputedStyle(document.querySelector('.timeline-segment.selected')).backgroundColor};})()`);
    assert.equal(buffer.count, buffer.marks);
    assert.ok(buffer.count > 0);
    assert.ok(Math.abs(buffer.width - buffer.end / 20 * 100) < .01);
    assert.notEqual(buffer.buffer, buffer.selection);
    await click('#showOnlyKeywords');
    assert.equal(await evaluate("document.querySelectorAll('.timeline-segment.selected').length"),3,'Filtering keeps hidden selected source regions');
  });

  await test('empty and overlapping source intervals, unavailable sources and decode failures recover safely', async () => {
    await load({segments:[],source:{id:sourceId,url:sourceUrl}});
    assert.equal(await evaluate("document.getElementById('mainVideo').duration"),20);
    assert.equal(await evaluate("document.querySelectorAll('.timeline-segment').length"),0);
    await evaluate("document.getElementById('mainVideo').currentTime=10");
    await until("document.getElementById('sourceScrubber').getAttribute('aria-valuenow') === '10'");
    const overlapping={...fixture,segments:fixture.segments.map(s=>({...s,start:0,end:5,duration:5}))};
    await load(overlapping);
    await click('.segment-review[data-id="9"]');
    await evaluate("document.getElementById('mainVideo').currentTime=2");
    await until("!document.getElementById('mainVideo').seeking");
    assert.equal(await evaluate("document.querySelector('.segment-row.active').dataset.id"),'9');
    await load(fixture,{source:false});
    assert.match(await evaluate("document.getElementById('timelineStatus').textContent"),/Reimport/);
    assert.equal(await evaluate("document.getElementById('mainVideo').getAttribute('src')"),null);
    await click('#selectAll');
    assert.deepEqual(await selected(),[0,4,9]);
    await click('#exportBtn');
    await until("!document.getElementById('exportBtn').disabled");
    await load(fixture);
    await evaluate("document.getElementById('mainVideo').src = 'data:video/mp4;base64,aW52YWxpZA=='");
    await until("document.getElementById('sourceScrubber').getAttribute('aria-disabled') === 'true'");
    assert.match(await evaluate("document.getElementById('sourcePreviewStatus').textContent"),/unsupported browser codec/);
  });

  await test('Iris Garden computed text, selected, disabled, input and focus colors retain contrast', async () => {
    await load(fixture);
    const pairs = await evaluate(`(() => {
      const lum = color => {const rgb=color.match(/[\\d.]+/g).slice(0,3).map(Number).map(v=>v/255).map(v=>v<=.04045?v/12.92:((v+.055)/1.055)**2.4);return rgb.reduce((a,v,i)=>a+v*[.2126,.7152,.0722][i],0);};
      const contrast=(a,b)=>{const x=lum(a),y=lum(b);return (Math.max(x,y)+.05)/(Math.min(x,y)+.05);};
      const result=[];
      for(const selector of ['.shell-header','.segments-panel','.side-body','button.primary','#exportBtn','#autoGenerateBtn','#aiInstructions','#sourceClock']) {
        const node=document.querySelector(selector),style=getComputedStyle(node);
        let background=style.backgroundColor,parent=node;
        while(background==='rgba(0, 0, 0, 0)'&&parent.parentElement){parent=parent.parentElement;background=getComputedStyle(parent).backgroundColor;}
        result.push({selector,ratio:contrast(style.color,background)});
      }
      document.querySelector('.segment-checkbox').click();
      const row=getComputedStyle(document.querySelector('.segment-row.selected'));
      result.push({selector:'.segment-row.selected',ratio:contrast(row.color,row.backgroundColor)});
      document.querySelector('button.primary').focus();
      const focus=getComputedStyle(document.querySelector('button.primary'));
      result.push({selector:'primary focus ring',ratio:contrast(focus.outlineColor,focus.backgroundColor),minimum:3});
      return result;
    })()`);
    for (const pair of pairs) assert.ok(pair.ratio >= (pair.minimum || 4.5),`${pair.selector}: ${pair.ratio.toFixed(2)}:1`);
    const hover=await evaluate(`(() => {const r=document.querySelector('button.primary').getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2};})()`);
    await page('Input.dispatchMouseEvent',{type:'mouseMoved',...hover});
    assert.notEqual(await evaluate("getComputedStyle(document.querySelector('button.primary')).backgroundColor"),'rgb(105, 36, 117)','Primary hover uses its lighter plum variant');
  });

  assert.deepEqual(exceptions, [], "No uncaught JavaScript exceptions or unhandled rejections");
} finally {
  browser.kill();
  await new Promise(resolve => browser.exitCode !== null || browser.signalCode !== null ? resolve() : browser.once("exit", resolve));
  await new Promise(resolve => server.close(resolve));
  await rm(profile, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 });
  await rm(mediaFolder, { recursive: true, force: true });
}
