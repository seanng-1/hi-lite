// SPDX-License-Identifier: GPL-3.0-or-later
// DOM interactions only: real browser layout/media tests remain in verify-selector.mjs.
import { JSDOM } from 'jsdom';
import { readFile, readdir } from 'node:fs/promises';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
const base = fileURLToPath(new URL('../', import.meta.url));
const fixture = JSON.parse(await readFile(base + '/tests/fixtures/segments.json', 'utf8'));
const js = (await readdir(base + '/web/dist/assets')).find(name => name.endsWith('.js'));
const code = await readFile(base + '/web/dist/assets/' + js, 'utf8');
const a = 'a'.repeat(32), b = 'b'.repeat(32);
const store = {
  [a]: { id: a, name: 'Project A', revision: 1, has_media: true, state: { selected_ids: [4], instructions: 'Saved criteria', min_score: 9 } },
  [b]: { id: b, name: 'Project B', revision: 0, has_media: false, state: { selected_ids: [] } },
};
const errors = [];
const dom = new JSDOM('<!DOCTYPE html><div id="app"></div>', { url: 'http://localhost/', runScripts: 'outside-only', pretendToBeVisual: true });
const w = dom.window;
w.addEventListener('error', event => errors.push(event.error));
w.ResizeObserver = class { observe() {} disconnect() {} };
w.HTMLElement.prototype.checkVisibility = function () { return !this.hidden; };
w.HTMLElement.prototype.scrollIntoView = function () {};
w.HTMLMediaElement.prototype.load = function () {};
w.HTMLMediaElement.prototype.pause = function () {};
w.HTMLMediaElement.prototype.play = function () { return Promise.resolve(); };
w.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
w.HTMLDialogElement.prototype.close = function () { this.open = false; this.dispatchEvent(new w.Event('close')); };
w.confirm = () => true;
let saveFailure = 0;
let uploads = 0;
w.fetch = async (input, options = {}) => {
 const path = String(input);
 const json = (value, status = 200) => new Response(JSON.stringify(value), { status });
 if (path === '/api/projects') {
   if (options.method === 'POST') {
     const id = 'c'.repeat(32);
     store[id] = { id, name: JSON.parse(options.body).name, revision: 0, has_media: false, state: { selected_ids: [] } };
     return json(store[id], 201);
   }
   return json({ projects: Object.values(store) });
 }
 const match = path.match(/^\/api\/projects\/([^/]+)(.*)$/);
 if (match) {
   const project = store[match[1]];
   if (!project) return json({}, 404);
   if (!match[2]) {
     if (options.method === 'PUT') {
       if (saveFailure) return json({}, saveFailure);
       const body = JSON.parse(options.body);
       if (project.revision !== body.revision) return json({}, 409);
       project.state = body.state; project.revision++;
     }
     return json(project);
   }
   if (match[2] === '/data/segments_manifest.json') return project.has_media ? json({ ...fixture, source: { id: a, url: `/api/projects/${project.id}/api/source/${a}` } }) : json({}, 404);
   if (match[2] === '/api/upload') uploads++;
 }
 if (path.endsWith('/api/subtitles')) return json({ available: false, message: 'Unavailable' });
 if (path === '/api/llm-settings') return json({ resolved_backend: null });
 return json({}, 404);
};
w.eval(code.replaceAll('import.meta.url', JSON.stringify('http://localhost/assets/app.js')));
const until = async (predicate) => { for (let i=0;i<200;i++) { if(predicate()) return; await new Promise(r=>setTimeout(r,10)); } throw Error('UI timeout'); };
const $ = selector => w.document.querySelector(selector);
const selected = () => [...w.document.querySelectorAll('.segment-checkbox:checked')].map(node=>Number(node.dataset.id));
await until(() => $('#selectAll'));
assert.deepEqual(selected(), [4]);
assert.equal($('#aiInstructions').value, 'Saved criteria');
assert.equal($('#mainVideo').getAttribute('src'), `/api/projects/${a}/api/source/${a}`);
$('#selectAll').click();
$('#projectsTrigger').click();
await until(() => $(`[data-project-id="${b}"]`));
saveFailure = 503;
$(`[data-project-id="${b}"]`).click();
await until(() => $('#projectDialog [role=alert]').textContent.includes('Could not save'));
assert.equal($('#recordingContext').textContent, 'Project A');
assert.deepEqual(selected(), [0,4,9]);
saveFailure = 0;
$(`[data-project-id="${b}"]`).click();
await until(() => $('#recordingContext').textContent === 'Project B' && $('#uploadButton'));
assert.deepEqual(store[a].state.selected_ids, [0,4,9]);
assert.equal($('#uploadButton').textContent, 'Choose Video File');
$('#projectsTrigger').click();
$(`[data-project-id="${a}"]`).click();
await until(() => $('#selectAll'));
assert.deepEqual(selected(), [0,4,9]);
$('#projectsTrigger').click();
$('#newProjectName').value = '<script>never execute</script>';
$('#newProjectForm').dispatchEvent(new w.Event('submit', { bubbles: true, cancelable: true }));
await until(() => $('#recordingContext').textContent === '<script>never execute</script>');
assert.equal(uploads, 0);
assert.equal(store[a].has_media, true);
assert.deepEqual(errors, []);
console.log('DOM smoke passed: load, scoped source, selection restore, save-failure blocking, reopen, safe create, no media overwrite.');
w.close();
