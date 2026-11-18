// Sandboxed preloads require CommonJS, even though the main process is ESM.
// Keep this bridge intentionally narrow: renderer code never chooses paths,
// sees the launch capability, or obtains a generic IPC/network primitive.
const { contextBridge, ipcRenderer } = require('electron');

function selectOriginal(channel: string, projectId: unknown) {
  if (typeof projectId !== 'string' || !/^[a-f0-9]{32}$/.test(projectId)) {
    return Promise.resolve({ status: 'error', message: 'Select a valid project first.' });
  }
  return ipcRenderer.invoke(channel, projectId);
}

contextBridge.exposeInMainWorld('hiLiteDesktop', Object.freeze({
  linkProject: (projectId: unknown) => selectOriginal('hi-lite:link-project', projectId),
  relinkProject: (projectId: unknown) => selectOriginal('hi-lite:relink-project', projectId),
}));
