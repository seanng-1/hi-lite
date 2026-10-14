// SPDX-License-Identifier: GPL-3.0-or-later
// Capture the default workspace using the existing local recording and a read-only
// fixture server. Build first, then run `node web/capture-workspace.mjs`.
// --live-smoke checks existing Vite/Flask servers without mutating API requests.
// Set CHROME_BIN if Chromium is not in the Playwright cache.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { existsSync, readFileSync, readdirSync } from "node:fs";
import { mkdtemp, mkdir, rm, writeFile } from "node:fs/promises";
import { createServer, request as httpRequest } from "node:http";
import { homedir, tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

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
// Read-only local recording; API readiness is explicitly a fixture.
const recording = JSON.parse(readFileSync(new URL('../highlights/segments_manifest.json', import.meta.url), 'utf8'));
const sourceSmoke = process.argv.includes('--url');
const evidence = fileURLToPath(new URL(sourceSmoke ? '../.impeccable/source-timeline/evidence/' : '../.impeccable/default-workspace/evidence/', import.meta.url));
const server = createServer((request, response) => {
  const path = new URL(request.url, 'http://localhost').pathname;
  if (sourceSmoke && ['/api/', '/data/', '/segments/', '/final/'].some(prefix => path.startsWith(prefix))) {
    const target = new URL(request.url, process.argv[process.argv.indexOf('--url') + 1]);
    const proxy = httpRequest(target, { method:request.method, headers:request.headers }, upstream => {
      response.writeHead(upstream.statusCode, upstream.headers);
      upstream.pipe(response);
    });
    proxy.on('error', error => response.writeHead(502).end(error.message));
    request.pipe(proxy);
    return;
  }
  const json = value => { response.setHeader('Content-Type', 'application/json'); response.end(JSON.stringify(value)); };
  if (path === '/data/segments_manifest.json') return json(recording);
  if (path === '/api/subtitles') return json({ available: true, message: 'Original recording subtitles ready' });
  if (path === '/api/llm-settings') return json({ resolved_backend: 'api', base_url: 'https://fixture.invalid/v1', model: 'fixture-model', has_api_key: true });
  const file = path.startsWith('/segments/')
    ? fileURLToPath(new URL('../highlights/segments/' + path.split('/').pop(), import.meta.url))
    : join(dist, path === '/' ? 'index.html' : path);
  if (!existsSync(file)) { response.writeHead(404).end(); return; }
  const content = readFileSync(file);
  response.setHeader('Content-Type', file.endsWith('.mp4') ? 'video/mp4' : file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : file.endsWith('.png') ? 'image/png' : 'text/html');
  // Chromium needs range delivery for native seek.
  if (request.headers.range) {
    const match = /bytes=(\d+)-(\d*)/.exec(request.headers.range);
    const first = Number(match[1]); const last = match[2] ? Number(match[2]) : content.length - 1;
    response.writeHead(206, { 'Content-Range': `bytes ${first}-${last}/${content.length}`, 'Accept-Ranges': 'bytes', 'Content-Length': last - first + 1 });
    response.end(content.subarray(first, last + 1));
  } else response.end(content);
});
await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
const liveSmoke = process.argv.includes('--live-smoke');
const url = liveSmoke ? 'http://127.0.0.1:5173/' : `http://127.0.0.1:${server.address().port}/`;
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
    const result = await page("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true, userGesture: true });
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
  await mkdir(evidence, { recursive: true });
  const measurements = [];
  await page('Page.navigate', { url });
  await until("document.getElementById('mainVideo')?.readyState >= 1 && document.getElementById('llmStatus')?.textContent && !document.getElementById('llmStatus').textContent.includes('Checking')");
  if (sourceSmoke) {
    const duration = Number(process.argv[process.argv.indexOf('--duration') + 1]);
    assert.ok(Math.abs(await evaluate("document.getElementById('mainVideo').duration") - duration) < .2);
    assert.ok(await evaluate("document.getElementById('mainVideo').getAttribute('src').startsWith('/api/source/')"));
    const point = await evaluate(`(() => {const r=document.getElementById('sourceScrubber').getBoundingClientRect();return {x:r.left+r.width*12/document.getElementById('mainVideo').duration,y:r.top+30,width:r.width,duration:document.getElementById('mainVideo').duration};})()`);
    await page('Input.dispatchMouseEvent',{type:'mousePressed',x:point.x,y:point.y,button:'left',clickCount:1});
    await page('Input.dispatchMouseEvent',{type:'mouseMoved',x:point.x+point.width*20/point.duration,y:point.y,button:'left',buttons:1});
    assert.ok(Math.abs(await evaluate("document.getElementById('mainVideo').currentTime")-32)<.05,'Dragging seeks real original footage');
    await page('Input.dispatchMouseEvent',{type:'mouseMoved',x:point.x,y:point.y,button:'left',buttons:1});
    await page('Input.dispatchMouseEvent',{type:'mouseReleased',x:point.x,y:point.y,button:'left',clickCount:1});
    await until("!document.getElementById('mainVideo').seeking && Math.abs(document.getElementById('mainVideo').currentTime-12)<.05 && document.getElementById('mainVideo').readyState>=2");
    assert.equal(await evaluate("document.querySelectorAll('.segment-row.active').length"),0,'Real footage plays in a transcript gap');
    await evaluate("document.getElementById('mainVideo').play()");
    await until("document.getElementById('mainVideo').currentTime>12.2");
    await evaluate("document.getElementById('mainVideo').pause();document.getElementById('mainVideo').currentTime=6");
    await until("!document.getElementById('mainVideo').seeking");
  }
  // Prime the native player before the paused frame, clearing its
  // initial buffering overlay as well as proving real playback advancement.
  assert.equal(await evaluate('location.pathname + location.search'), '/', 'Capture the default route');
  await evaluate("document.getElementById('mainVideo').play()");
  await until("document.getElementById('mainVideo').currentTime > .1");
  await evaluate("document.getElementById('mainVideo').pause()");
  await evaluate(`
    const input = document.getElementById('aiInstructions');
    input.value = 'Find the goals, close chances and standout reactions. Skip routine build-up.';
    input.dispatchEvent(new Event('input'));
    for (const id of [1, 3, 5]) document.querySelector('.segment-checkbox[data-id="' + id + '"]').click();
    document.getElementById('transcriptHeading').blur();
    document.getElementById('mainVideo').currentTime = ${sourceSmoke ? 6 : 2};
  `);
  await until(`Math.abs(document.getElementById('mainVideo').currentTime - ${sourceSmoke ? 6 : 2}) < .05 && !document.getElementById('mainVideo').seeking && document.getElementById('mainVideo').readyState >= 4`);
  await new Promise(resolve => setTimeout(resolve, 400));
  for (const [width, height] of liveSmoke ? [[1736,864]] : [[1736,864],[1366,768],[1280,800],[960,720],[768,800],[1366,500],[320,800],[320,500]]) {
    await viewport(width, height);
    await evaluate("document.activeElement?.blur(); window.scrollTo(0,0); document.getElementById('segmentsList').scrollTop = 0");
    const measure = await evaluate(`(() => {
      const rect = selector => { const r = document.querySelector(selector).getBoundingClientRect(); return {x:r.x,y:r.y,width:r.width,height:r.height}; };
      const v = document.getElementById('mainVideo'), b = v.getBoundingClientRect();
      const scale = Math.min(b.width/v.videoWidth, b.height/v.videoHeight);
      const list = document.getElementById('segmentsList').getBoundingClientRect();
      const timeline = document.querySelector('.source-timeline');
      const visibleBottom = timeline && getComputedStyle(timeline).position === 'sticky'
        ? Math.min(list.bottom, timeline.getBoundingClientRect().top) : Math.min(list.bottom, innerHeight);
      const rows = [...document.querySelectorAll('.segment-row')];
      return { picture: { width:v.videoWidth*scale, height:v.videoHeight*scale }, intrinsic:[v.videoWidth,v.videoHeight], readyState:v.readyState,
        preview:rect('.preview-panel'), transcript:rect('.segments-panel'), list:rect('#segmentsList'),
        ai:document.getElementById('contextPanel').checkVisibility() ? rect('#contextPanel') : null,
        media:document.getElementById('mediaPanel').checkVisibility() ? rect('#mediaPanel') : null,
        rowsFullyVisible:rows.filter(row=>{const r=row.getBoundingClientRect();return r.top>=list.top && r.bottom<=visibleBottom;}).length,
        rowsInScrollArea:rows.filter(row=>{const r=row.getBoundingClientRect();return r.top>=list.top && r.bottom<=list.bottom;}).length,
        horizontalOverflow:document.documentElement.scrollWidth>innerWidth,
        timeline:document.querySelector('.source-timeline') ? rect('.source-timeline') : null,
        nativeControls:v.controls, selected:document.getElementById('selectedCount').textContent };
    })()`);
    assert.equal(measure.horizontalOverflow, false, `${width}×${height}`);
    const { data } = await page('Page.captureScreenshot', { format:'png', captureBeyondViewport:false });
    await writeFile(join(evidence, `${liveSmoke ? 'live-' : ''}default-${width}x${height}.png`), Buffer.from(data,'base64'));
    measurements.push({route:'/',width,height,...measure});
  }
  await viewport(1736,864);
  await click('.play-btn[data-id="1"]');
  await until("document.getElementById('mainVideo').currentTime > .1 && !document.getElementById('mainVideo').paused");
  assert.equal(await evaluate("document.getElementById('selectedCount').textContent"), '3');
  await evaluate("document.getElementById('mainVideo').pause(); document.getElementById('mainVideo').currentTime = .5;");
  await until("!document.getElementById('mainVideo').seeking");
  await evaluate("document.getElementById('mainVideo').focus()");
  await press(' ', 'Space', 32);
  await until("!document.getElementById('mainVideo').paused && document.getElementById('mainVideo').currentTime > .55");
  await evaluate("document.getElementById('mainVideo').pause()");
  assert.equal(await evaluate("document.getElementById('selectedCount').textContent"), '3');
  await click('.segment-review[data-id="0"]');
  await until("document.getElementById('mainVideo').readyState >= 4");
  await evaluate("document.getElementById('mainVideo').play()");
  await until("document.getElementById('mainVideo').currentTime > .1");
  await evaluate("document.getElementById('mainVideo').pause()");
  await evaluate(`document.getElementById('mainVideo').currentTime=${sourceSmoke ? 6 : 2}`);
  await until("!document.getElementById('mainVideo').seeking");
  await new Promise(resolve=>setTimeout(resolve,400));
  if (!liveSmoke) {
    await viewport(1736,864);
    await click('#contextPanel .drawer-close');
    const v = await evaluate(`(() => { const v=document.getElementById('mainVideo'),r=v.getBoundingClientRect(),s=Math.min(r.width/v.videoWidth,r.height/v.videoHeight);return {width:v.videoWidth*s,height:v.videoHeight*s}; })()`);
    const { data } = await page('Page.captureScreenshot', {format:'png'});
    await writeFile(join(evidence, 'default-ai-hidden-1736x864.png'),Buffer.from(data,'base64'));
    measurements.push({route:'/',scenario:'ai-hidden',width:1736,height:864,picture:v});
    await viewport(768,800);
    await click('#contextTrigger');
    const ai = await page('Page.captureScreenshot',{format:'png'});
    await writeFile(join(evidence,'default-ai-drawer-768x800.png'),Buffer.from(ai.data,'base64'));
    await press('Escape','Escape',27);
    await click('#mediaTrigger');
    const media = await page('Page.captureScreenshot',{format:'png'});
    await writeFile(join(evidence,'default-media-drawer-768x800.png'),Buffer.from(media.data,'base64'));
    await press('Escape','Escape',27);
    await viewport(1736,864);
    await click('#contextTrigger');
    await click('#llmSettingsToggle');
    await evaluate("document.getElementById('llmModel').value='unsaved-creator-model'; document.getElementById('llmModel').dispatchEvent(new Event('input')); document.getElementById('llmModel').focus()");
    const draft=await page('Page.captureScreenshot',{format:'png'});
    await writeFile(join(evidence,'default-provider-draft-1736x864.png'),Buffer.from(draft.data,'base64'));
  }
  await writeFile(join(evidence,liveSmoke ? 'live-measurements.json' : 'measurements.json'),JSON.stringify(measurements,null,2)+'\n');
  console.log(JSON.stringify(measurements.map(({route,scenario,width,height,picture,rowsFullyVisible,timeline,horizontalOverflow})=>({route,scenario,width,height,picture,rowsFullyVisible,timelineHeight:timeline?.height,horizontalOverflow})),null,2));
  assert.deepEqual(exceptions, []);
} finally {
  browser.kill();
  await new Promise(resolve => browser.exitCode !== null || browser.signalCode !== null ? resolve() : browser.once("exit", resolve));
  await new Promise(resolve => server.close(resolve));
  await rm(profile, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 });
}
