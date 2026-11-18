import { app, BrowserWindow, dialog, ipcMain, shell } from 'electron';
import { randomBytes } from 'node:crypto';
import { createSourceLinker, trustedAppUrl } from './source-linking.js';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawn, spawnSync, ChildProcessWithoutNullStreams } from 'node:child_process';
import kill from 'tree-kill';
import waitOn from 'wait-on';

// ESM path helpers
const __filename = fileURLToPath(import.meta.url);
const hereDir = path.dirname(__filename);

let mainWindow: BrowserWindow | null = null;
let backendProcess: ChildProcessWithoutNullStreams | null = null;

const BACKEND_PORT = 8000;
const isDev = !app.isPackaged;
// This capability never enters the renderer or preload. Browser-only instances
// have no token and cannot use the filesystem-selection endpoints.
const desktopToken = randomBytes(32).toString('hex');
const startUrl = isDev ? 'http://localhost:5173/' : `http://127.0.0.1:${BACKEND_PORT}/`;
const chooseOriginal = createSourceLinker({
  getWindow: () => mainWindow,
  appUrl: startUrl,
  backendUrl: `http://127.0.0.1:${BACKEND_PORT}`,
  desktopToken,
  showOpenDialog: (window, options) => dialog.showOpenDialog(window, options),
});

function openExternalLink(value: string): void {
  try {
    const url = new URL(value);
    if (url.protocol === 'https:' || url.protocol === 'http:') void shell.openExternal(url.href);
  } catch { /* Ignore malformed links and local/custom protocols. */ }
}

ipcMain.handle('hi-lite:link-project', (event, projectId: unknown) => chooseOriginal(event, projectId, false));
ipcMain.handle('hi-lite:relink-project', (event, projectId: unknown) => chooseOriginal(event, projectId, true));

function resolveBackendExecutable(): { cmd: string; args: string[] } {
  const resourcesBase = process.resourcesPath;
  const packagedBackendWin = path.join(resourcesBase, 'backend.exe');
  const packagedBackendUnix = path.join(resourcesBase, 'backend');

  if (!isDev) {
    if (process.platform === 'win32') {
      return { cmd: packagedBackendWin, args: [] };
    }
    return { cmd: packagedBackendUnix, args: [] };
  }

  // Dev: run python against repo root's web_interface.py
  const script = path.resolve(process.cwd(), '..', 'web_interface.py');
  const pythonCmd = process.platform === 'win32' ? 'python' : 'python3';
  return { cmd: pythonCmd, args: [script] };
}

async function startBackend(): Promise<void> {
  const { cmd, args } = resolveBackendExecutable();

  const resourcesBase = process.resourcesPath;
  const ffmpegDir = path.join(resourcesBase, 'bin');
  const ffmpegPath = path.join(ffmpegDir, process.platform === 'win32' ? 'ffmpeg.exe' : 'ffmpeg');
  const ffprobePath = path.join(ffmpegDir, process.platform === 'win32' ? 'ffprobe.exe' : 'ffprobe');

  const mergedEnv: NodeJS.ProcessEnv = {
    ...process.env,
    FLASK_ENV: isDev ? 'development' : 'production',
    // Backend exits when its stdin pipe hits EOF (i.e. this process died),
    // covering crashes and force-kills where our quit handlers never run.
    HI_LITE_WATCHDOG: '1',
    HI_LITE_DESKTOP_TOKEN: desktopToken,
  };

  if (!isDev) {
    // All backend data (uploads, highlights, whisper models) goes to a
    // per-user writable folder, not the install directory.
    mergedEnv.HI_LITE_DATA_DIR = app.getPath('userData');
    mergedEnv.PATH = `${ffmpegDir}${path.delimiter}${process.env.PATH ?? ''}`;
    mergedEnv.FFMPEG_PATH = ffmpegPath;
    mergedEnv.FFPROBE_PATH = ffprobePath;
    // MoviePy reads FFMPEG_BINARY; point it at the shipped ffmpeg too.
    mergedEnv.FFMPEG_BINARY = ffmpegPath;
  }

  // In production, run from resourcesBase (where backend.exe is)
  const spawnCwd = isDev 
    ? path.resolve(process.cwd(), '..') 
    : resourcesBase;

  backendProcess = spawn(cmd, args, {
    cwd: spawnCwd,
    env: mergedEnv,
    stdio: 'pipe'
  });

  backendProcess.stdout?.on('data', (data) => {
    console.log(`[backend] ${data}`.trim());
  });
  backendProcess.stderr?.on('data', (data) => {
    console.error(`[backend:err] ${data}`.trim());
  });
  backendProcess.on('exit', (code) => {
    console.log(`[backend] exited with code ${code}`);
  });

  // Wait until backend is reachable
  await waitOn({
    resources: [
      `http://127.0.0.1:${BACKEND_PORT}/data/segments_manifest.json`,
      `http://127.0.0.1:${BACKEND_PORT}/`
    ],
    timeout: 60000,
    validateStatus: (status: number) => [200, 404].includes(status),
  });
}

async function createWindow(): Promise<void> {
  mainWindow = new BrowserWindow({
    width: 1200,
    height: 800,
    webPreferences: {
      contextIsolation: true,
      sandbox: true,
      nodeIntegration: false,
      preload: path.join(hereDir, 'preload.cjs'),
    },
    show: false,
  });

  mainWindow.once('ready-to-show', () => mainWindow?.show());

  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    openExternalLink(url);
    return { action: 'deny' };
  });
  mainWindow.webContents.on('will-navigate', (event, url) => {
    if (!trustedAppUrl(url, startUrl)) {
      event.preventDefault();
      openExternalLink(url);
    }
  });
  await mainWindow.loadURL(startUrl);

  mainWindow.on('closed', () => {
    mainWindow = null;
  });
}

async function init(): Promise<void> {
  await app.whenReady();
  // Transcription now runs inside the backend (faster-whisper); no Docker needed.
  await startBackend();
  await createWindow();

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      createWindow();
    }
  });
}

function shutdownBackend(): void {
  if (backendProcess && backendProcess.pid) {
    const pid = backendProcess.pid;
    try {
      if (process.platform === 'win32') {
        // Synchronous tree kill: must finish before Electron exits, otherwise
        // backend.exe (and any pipeline children) outlive the app.
        spawnSync('taskkill', ['/pid', String(pid), '/T', '/F'], { stdio: 'ignore' });
      } else {
        kill(pid);
      }
    } catch (e) {
      // ignore
    }
    backendProcess = null;
  }
}

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') {
    app.quit();
  }
});

app.on('before-quit', () => {
  shutdownBackend();
});

// Ensure backend is killed on unexpected exits in dev
process.on('exit', () => shutdownBackend());
process.on('SIGINT', () => { shutdownBackend(); process.exit(0); });
process.on('SIGTERM', () => { shutdownBackend(); process.exit(0); });
process.on('uncaughtException', () => { shutdownBackend(); process.exit(1); });

init().catch((err) => {
  console.error(err);
  app.quit();
});