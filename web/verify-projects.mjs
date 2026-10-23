// SPDX-License-Identifier: GPL-3.0-or-later
// Dependency-free persistence contract checks, also usable without Chromium.
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';
import ts from 'typescript';
const source = await readFile(new URL('./src/projects.ts', import.meta.url), 'utf8');
const code = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } }).outputText;
const { Projects } = await import('data:text/javascript;base64,' + Buffer.from(code).toString('base64'));
globalThis.window = { setTimeout, clearTimeout };
const storage = new Map();
globalThis.localStorage = { getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value) };
function fixture() {
  storage.clear();
  const store = {
    first: { id: 'first', name: 'First', revision: 1, state: { selected_ids: [4], edits: [{ source_id: 4 }], instructions: 'Keep this' } },
    second: { id: 'second', name: 'Second', revision: 3, state: { selected_ids: [9] } },
  };
  const requests = [];
  const controls = { fail: 0, hold: null };
  globalThis.fetch = async (url, options = {}) => {
    requests.push({ url, options });
    const json = (body, status = 200) => new Response(JSON.stringify(body), { status });
    if (url === '/api/projects') {
      if (options.method === 'POST') {
        store.third = { id: 'third', name: JSON.parse(options.body).name, revision: 0, state: { selected_ids: [] } };
        return json(store.third, 201);
      }
      return json({ projects: Object.values(store) });
    }
    const id = url.split('/').pop();
    if (!store[id]) return json({}, 404);
    if (options.method === 'PUT') {
      if (controls.hold) await controls.hold;
      if (controls.fail) return json({}, controls.fail);
      const submitted = JSON.parse(options.body);
      if (store[id].revision !== submitted.revision) return json({}, 409);
      store[id] = { ...store[id], revision: submitted.revision + 1, state: submitted.state };
    }
    return json(store[id]);
  };
  return { store, requests, controls, project: new Projects() };
}
await test('reopen saves the current state and preserves extensible edits', async () => {
  const { project, store } = fixture();
  assert.equal(await project.discover(), true);
  assert.equal(project.url('/api/export'), '/api/projects/first/api/export');
  project.update({ selected_ids: [0, 4, 9], instructions: 'More highlights', min_score: 8 });
  await project.open('second');
  assert.deepEqual(store.first.state.edits, [{ source_id: 4 }]);
  assert.deepEqual(store.first.state.selected_ids, [0, 4, 9]);
  assert.deepEqual(project.active.state.selected_ids, [9]);
  await project.open('first');
  assert.equal(project.active.state.instructions, 'More highlights');
  assert.equal(project.active.state.min_score, 8);
  assert.equal(storage.get('hi-lite.active-project'), 'first');
});
await test('new project does not mutate an existing project', async () => {
  const { project, store } = fixture();
  await project.discover();
  await project.create('Third');
  assert.equal(project.active.id, 'third');
  assert.deepEqual(store.first.state.selected_ids, [4]);
});
await test('save failures block navigation and retain local changes for retry', async () => {
  const { project, store, controls } = fixture();
  await project.discover();
  controls.fail = 503;
  project.update({ selected_ids: [0, 4] });
  await assert.rejects(project.open('second'), /Could not save/);
  assert.equal(project.active.id, 'first');
  assert.deepEqual(project.active.state.selected_ids, [0, 4]);
  assert.deepEqual(store.first.state.selected_ids, [4]);
  controls.fail = 0;
  await project.open('second');
  assert.deepEqual(store.first.state.selected_ids, [0, 4]);
});
await test('concurrent flushes serialize snapshots and retain edits arriving during save', async () => {
  const { project, requests, store, controls } = fixture();
  await project.discover();
  let release;
  controls.hold = new Promise(resolve => { release = resolve; });
  project.update({ selected_ids: [0, 4, 9] });
  const saving = project.flush();
  project.update({ selected_ids: [] });
  const switching = project.open('second');
  controls.hold = null;
  release();
  await Promise.all([saving, switching]);
  const saves = requests.filter(request => request.options.method === 'PUT').map(request => JSON.parse(request.options.body));
  assert.deepEqual(saves.map(save => save.revision), [1, 2]);
  assert.deepEqual(saves.map(save => save.state.selected_ids), [[0, 4, 9], []]);
  assert.deepEqual(store.first.state.selected_ids, []);
  assert.equal(project.active.id, 'second');
});
await test('revision conflicts never overwrite another window and require explicit reload', async () => {
  const { project, store } = fixture();
  await project.discover();
  store.first.revision++;
  project.update({ selected_ids: [0] });
  await assert.rejects(project.flush(), /another window/);
  assert.equal(project.conflict, true);
  assert.deepEqual(store.first.state.selected_ids, [4]);
  await assert.rejects(project.open('second'), /another window/);
  await project.open('first', true);
  assert.equal(project.conflict, false);
  assert.deepEqual(project.active.state.selected_ids, [4]);
});
await test('missing project APIs retain legacy compatibility', async () => {
  globalThis.fetch = async () => new Response('', { status: 404 });
  const project = new Projects();
  assert.equal(await project.discover(), false);
  assert.equal(project.url('/api/export'), '/api/export');
  globalThis.fetch = async () => new Response('<html>Static development server</html>');
  assert.equal(await project.discover(), false);
});
