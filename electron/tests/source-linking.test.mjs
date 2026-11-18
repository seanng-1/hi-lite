import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import vm from 'node:vm';
import { createSourceLinker, trustedAppUrl } from '../dist/source-linking.js';

const projectId = 'a'.repeat(32);
const sourceId = 'b'.repeat(32);
function setup(overrides = {}) {
  const frame = { url: 'http://127.0.0.1:8000/' };
  const window = { isDestroyed: () => false, webContents: { mainFrame: frame } };
  const calls = [];
  const dialogs = [];
  const options = {
    getWindow: () => window,
    appUrl: frame.url,
    backendUrl: 'http://127.0.0.1:8000',
    desktopToken: 'only-in-main',
    showOpenDialog: async (...args) => {
      dialogs.push(args);
      return { canceled: false, filePaths: ['/chosen/original.mp4'] };
    },
    request: async (...args) => {
      calls.push(args);
      return { ok: true, json: async () => ({ status: 'ok', task_id: 'task_' + sourceId, path: '/secret', token: 'must-not-leak' }) };
    },
    ...overrides,
  };
  return { window, frame, calls, dialogs, options, event: { sender: window.webContents, senderFrame: frame }, link: createSourceLinker(options) };
}

test('app URL allowlist rejects remote, frame, file, credential and asset destinations', () => {
  for (const url of ['https://evil.example/', 'http://127.0.0.1:8001/', 'http://localhost:8000/',
    'http://127.0.0.1:8000/api/source/id', 'file:///etc/passwd', 'http://user@127.0.0.1:8000/', 'bad']) {
    assert.equal(trustedAppUrl(url, 'http://127.0.0.1:8000/'), false, url);
  }
  assert.equal(trustedAppUrl('http://127.0.0.1:8000/#editor', 'http://127.0.0.1:8000/'), true);
});

test('only the trusted top-level app frame may select an original', async () => {
  const h = setup();
  for (const event of [{ sender: {}, senderFrame: h.frame },
    { sender: h.window.webContents, senderFrame: { url: h.frame.url } },
    { sender: h.window.webContents, senderFrame: null }]) {
    assert.equal((await h.link(event, projectId, false)).status, 'error');
  }
  h.frame.url = 'https://evil.example/';
  assert.equal((await h.link(h.event, projectId, false)).status, 'error');
  assert.equal(h.dialogs.length, 0);
  assert.equal(h.calls.length, 0);
});

test('renderer cannot supply a path/object instead of a project ID', async () => {
  const h = setup();
  for (const id of ['/etc/passwd', '../secret', { projectId, path: '/etc/passwd' }, null, 12]) {
    assert.equal((await h.link(h.event, id, false)).status, 'error');
  }
  assert.equal(h.dialogs.length, 0);
});

test('main sends native picker path plus private token, exposes only public results', async () => {
  const h = setup();
  const result = await h.link(h.event, projectId, false);
  assert.deepEqual(result, { status: 'ok', project_id: projectId, task_id: 'task_' + sourceId });
  assert.equal(h.calls[0][0], 'http://127.0.0.1:8000/api/desktop/link-project');
  assert.equal(h.calls[0][1].headers['X-Hi-Lite-Desktop-Token'], 'only-in-main');
  assert.equal(h.calls[0][1].redirect, 'error');
  assert.deepEqual(JSON.parse(h.calls[0][1].body), { project_id: projectId, path: '/chosen/original.mp4' });
});

test('navigation during native picker revokes authorization', async () => {
  const h = setup();
  h.options.showOpenDialog = async () => { h.frame.url = 'https://evil.example/'; return { canceled: false, filePaths: ['/chosen/original.mp4'] }; };
  const link = createSourceLinker(h.options);
  assert.equal((await link(h.event, projectId, false)).status, 'error');
  assert.equal(h.calls.length, 0);
});

test('cancelled picker sends no request and can be reopened', async () => {
  const h = setup({ showOpenDialog: async () => ({ canceled: true, filePaths: [] }) });
  assert.equal((await h.link(h.event, projectId, true)).status, 'cancelled');
  assert.equal((await h.link(h.event, projectId, true)).status, 'cancelled');
  assert.equal(h.calls.length, 0);
});

test('only one pending picker is allowed', async () => {
  let finish;
  const h = setup({ showOpenDialog: () => new Promise(resolve => { finish = resolve; }) });
  const first = h.link(h.event, projectId, false);
  assert.equal((await h.link(h.event, projectId, false)).status, 'error');
  finish({ canceled: true, filePaths: [] });
  assert.equal((await first).status, 'cancelled');
});

test('relink exposes only same-project source URL, no nested private fields', async () => {
  const h = setup({ request: async () => ({ ok: true, json: async () => ({ status: 'ok', source: {
    id: sourceId, url: `/api/projects/${projectId}/api/source/${sourceId}`, path: '/private', token: 'private',
  } }) }) });
  assert.deepEqual((await h.link(h.event, projectId, true)).source, { id: sourceId, url: `/api/projects/${projectId}/api/source/${sourceId}` });
});

test('sandbox preload exposes only link/relink project methods, never raw IPC', async () => {
  let bridge;
  const calls = [];
  const code = await readFile(new URL('../dist/preload.cjs', import.meta.url), 'utf8');
  vm.runInNewContext(code, {
    exports: {},
    require: name => {
      assert.equal(name, 'electron');
      return { contextBridge: { exposeInMainWorld: (name, value) => { assert.equal(name, 'hiLiteDesktop'); bridge = value; } },
        ipcRenderer: { invoke: (...args) => { calls.push(args); return Promise.resolve({ status: 'cancelled' }); } } };
    },
  });
  assert.deepEqual(Object.keys(bridge).sort(), ['linkProject', 'relinkProject']);
  assert.equal((await bridge.linkProject('/private.mp4')).status, 'error');
  await bridge.relinkProject(projectId);
  assert.deepEqual(calls, [['hi-lite:relink-project', projectId]]);
});
