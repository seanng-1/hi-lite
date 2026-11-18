import type { BrowserWindow, IpcMainInvokeEvent, OpenDialogOptions, OpenDialogReturnValue } from 'electron';

export function trustedAppUrl(value: string, expected: string): boolean {
  try {
    const url = new URL(value);
    return url.origin === new URL(expected).origin && !url.username && !url.password &&
      (url.pathname === '/' || url.pathname === '/index.html');
  } catch { return false; }
}

type SourceLinkingOptions = {
  getWindow: () => BrowserWindow | null;
  appUrl: string;
  backendUrl: string;
  desktopToken: string;
  showOpenDialog: (window: BrowserWindow, options: OpenDialogOptions) => Promise<OpenDialogReturnValue>;
  request?: typeof fetch;
};

/** Main-process only. Injected platform operations make the trust boundary testable without a GUI. */
export function createSourceLinker(options: SourceLinkingOptions) {
  let pickerOpen = false;
  const request = options.request ?? fetch;
  function trustedSender(event: IpcMainInvokeEvent): boolean {
    const window = options.getWindow();
    try {
      return !!window && !window.isDestroyed() && event.sender === window.webContents &&
        event.senderFrame === window.webContents.mainFrame && trustedAppUrl(event.senderFrame.url, options.appUrl);
    } catch { return false; }
  }

  return async function chooseOriginal(event: IpcMainInvokeEvent, projectId: unknown, relink: boolean) {
    if (!trustedSender(event) || typeof projectId !== 'string' || !/^[a-f0-9]{32}$/.test(projectId)) {
      return { status: 'error', message: 'Original-file selection is not available from this window.' };
    }
    if (pickerOpen) return { status: 'error', message: 'A file picker is already open.' };
    pickerOpen = true;
    try {
      const selected = await options.showOpenDialog(options.getWindow()!, {
        title: relink ? 'Relink the same original recording' : 'Link an original recording',
        buttonLabel: relink ? 'Relink original' : 'Link original',
        properties: ['openFile'],
        filters: [{ name: 'Videos', extensions: ['mp4', 'avi', 'mov', 'mkv', 'webm'] }],
      });
      if (selected.canceled || selected.filePaths.length !== 1) return { status: 'cancelled' };
      // Navigation or window replacement during the dialog revokes this request.
      if (!trustedSender(event)) return { status: 'error', message: 'The app window changed. Please try again.' };
      const response = await request(`${options.backendUrl}/api/desktop/${relink ? 'relink' : 'link'}-project`, {
        method: 'POST',
        redirect: 'error',
        headers: { 'Content-Type': 'application/json', 'X-Hi-Lite-Desktop-Token': options.desktopToken },
        body: JSON.stringify({ project_id: projectId, path: selected.filePaths[0] }),
      });
      const result = await response.json() as Record<string, unknown>;
      const source = result.source as Record<string, unknown> | undefined;
      // Allowlist the renderer contract. Never return paths, request headers,
      // capability tokens, or arbitrary fields from the backend.
      return {
        status: response.ok && result.status === 'ok' ? 'ok' : 'error',
        project_id: projectId,
        ...(typeof result.task_id === 'string' && /^task_[a-f0-9]{32}$/.test(result.task_id) ? { task_id: result.task_id } : {}),
        ...(typeof result.message === 'string' ? { message: result.message } : {}),
        ...(source && typeof source.id === 'string' && /^[a-f0-9]{32}$/.test(source.id) &&
          source.url === `/api/projects/${projectId}/api/source/${source.id}`
          ? { source: { id: source.id, url: source.url } } : {}),
      };
    } catch {
      return { status: 'error', message: 'Could not connect to the desktop backend. Please try again.' };
    } finally {
      pickerOpen = false;
    }
  };
}
