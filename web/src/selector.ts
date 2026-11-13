// SPDX-License-Identifier: GPL-3.0-or-later
import "./selector.css";
import { Projects } from "./projects";
import { mountTranscriptEditor } from "./transcript-editor";
import { formatSourceTime as formatTime, readSource, setupSourceTimeline, timelineMarkup, type SourceReference } from './source-timeline';

interface DesktopSourceResult {
  status: 'ok' | 'cancelled' | 'error';
  task_id?: string;
  message?: string;
}
declare global {
  interface Window {
    hiLiteDesktop?: {
      linkProject(projectId: string): Promise<DesktopSourceResult>;
      relinkProject(projectId: string): Promise<DesktopSourceResult>;
    };
  }
}

const logoUrl = new URL("./assets/Hi-Lite-Logo.png", import.meta.url).href;

interface Segment {
    id: number;
    start: number;
    end: number;
    duration: number;
    filename: string;
    text: string;
    is_keyword: boolean;
  }
  
  function isRecord(value: unknown): value is Record<string, unknown> {
    return typeof value === "object" && value !== null && !Array.isArray(value);
  }

  function readSegments(data: unknown): Segment[] {
    if (!isRecord(data) || !Array.isArray(data.segments)) {
      throw new Error("Invalid manifest: expected an object with a segments array.");
    }

    const ids = new Set<number>();
    const segments = data.segments.map((value: unknown, index: number) => {
      const invalid = () => new Error(`Invalid manifest: segment ${index + 1} has missing or invalid fields.`);
      if (!isRecord(value)) throw invalid();
      const { id, start, end, duration, filename, text, is_keyword } = value;
      if (typeof id !== "number" || !Number.isSafeInteger(id) || id < 0 || ids.has(id) ||
          typeof start !== "number" || !Number.isFinite(start) || start < 0 ||
          typeof end !== "number" || !Number.isFinite(end) || end <= start ||
          typeof duration !== "number" || !Number.isFinite(duration) || duration <= 0 ||
          typeof filename !== "string" || !filename.trim() || typeof text !== "string" ||
          (is_keyword !== undefined && typeof is_keyword !== "boolean")) {
        throw invalid();
      }
      // Invalid Unicode in a filename must recover here, before URL encoding
      // during review rendering can throw.
      try { encodeURIComponent(filename); } catch { throw invalid(); }
      ids.add(id);
      return { id, start, end, duration, filename, text, is_keyword };
    });

    // Older manifests store copies of matching segments instead of flags.
    // Only the canonical id links those records to the displayed segments.
    const keywordIds = new Set<number>();
    if (segments.some(segment => segment.is_keyword === undefined) && data.keyword_highlights !== undefined) {
      if (!Array.isArray(data.keyword_highlights)) {
        throw new Error("Invalid manifest: expected a keyword_highlights array.");
      }
      for (const match of data.keyword_highlights) {
        if (!isRecord(match) || typeof match.id !== "number" || !ids.has(match.id)) {
          throw new Error("Invalid manifest: keyword highlight has an unknown or missing segment id.");
        }
        keywordIds.add(match.id);
      }
    }
    return segments.map(segment => ({
      ...segment,
      is_keyword: segment.is_keyword ?? keywordIds.has(segment.id)
    }));
  }

  let disposeShell = () => {};
  const projects = new Projects();
  const projectOperations = new Set<string>();
  let navigatingProject = false;
  let projectNavigationError = "";

  function projectBusy(operation: string, busy: boolean) {
    if (busy) projectOperations.add(operation); else projectOperations.delete(operation);
    updateProjectChrome();
  }

  function updateProjectChrome() {
    const status = document.getElementById('projectSaveStatus');
    if (status) {
      status.textContent = projects.active ? projects.status : 'No project open';
      status.title = projects.error;
      status.classList.toggle('save-error', !!projects.error);
    }
    const retry = document.getElementById('projectSaveRetry') as HTMLButtonElement | null;
    if (retry) retry.hidden = !projects.error || projects.conflict;
    const reload = document.getElementById('projectReload') as HTMLButtonElement | null;
    if (reload) reload.hidden = !projects.conflict;
    const trigger = document.getElementById('projectsTrigger') as HTMLButtonElement | null;
    if (trigger) trigger.disabled = navigatingProject || projectOperations.size > 0;
    document.querySelectorAll<HTMLButtonElement>('[data-project-operation]').forEach(button => button.disabled = navigatingProject || projectOperations.size > 0);
    const error = document.getElementById('projectSaveError');
    if (error) { error.textContent = projects.error || projectNavigationError; error.hidden = !error.textContent; }
  }
  projects.onChange = updateProjectChrome;

  async function navigateProject(action: () => Promise<void>, dialog?: HTMLDialogElement) {
    if (navigatingProject || projectOperations.size) return;
    navigatingProject = true;
    projectNavigationError = "";
    const previousShell = document.querySelector<HTMLElement>('.editor-shell');
    if (previousShell) previousShell.inert = true;
    updateProjectChrome();
    dialog?.querySelectorAll<HTMLButtonElement>('button').forEach(button => button.disabled = true);
    try {
      await action();
      dialog?.close();
      dialog?.remove();
      disposeShell();
      document.querySelector<HTMLVideoElement>('#mainVideo')?.pause();
      document.getElementById('app')!.replaceChildren();
      await main();
    } catch (error) {
      const message = error instanceof Error ? error.message : 'Could not open the project.';
      const target = dialog?.querySelector<HTMLElement>('[role="alert"]');
      if (target) target.textContent = message;
      else projectNavigationError = message;
    } finally {
      navigatingProject = false;
      if (previousShell) previousShell.inert = false;
      dialog?.querySelectorAll<HTMLButtonElement>('button').forEach(button => button.disabled = false);
      updateProjectChrome();
    }
  }

  function openProjectPicker() {
    if (!projects.enabled || projectOperations.size || navigatingProject || document.getElementById('projectDialog')) return;
    const dialog = document.createElement('dialog');
    dialog.id = 'projectDialog';
    dialog.className = 'project-dialog';
    dialog.setAttribute('aria-labelledby', 'projectsHeading');
    dialog.innerHTML = `<div class="panel-heading"><h2 id="projectsHeading">Projects</h2><button id="closeProjects" aria-label="Close projects">Close</button></div>
      <div class="project-dialog-body"><form id="newProjectForm"><label for="newProjectName">New project name</label>
        <input id="newProjectName" type="text" maxlength="120" required placeholder="e.g. Weekend highlights" autocomplete="off">
        <button type="submit" class="primary">Create new project</button></form>
        <p class="field-help">Each project keeps its own recording, transcript, selected clips, and scoring options. Your current project is saved before switching.</p>
        <h3>Recent projects</h3><div id="recentProjects"></div><p role="alert"></p></div>`;
    function renderRecent() {
      const list = dialog.querySelector('#recentProjects')!;
      list.replaceChildren();
      if (!projects.recent.length) list.textContent = 'No saved projects yet.';
      for (const project of projects.recent) {
        const button = document.createElement('button');
        button.className = 'recent-project';
        button.dataset.projectId = project.id;
        const title = document.createElement('strong');
        title.textContent = project.name;
        const detail = document.createElement('span');
        detail.textContent = `${project.id === projects.active?.id ? 'Current project · ' : ''}${project.has_media ? 'Recording saved' : 'Ready to import'}`;
        button.append(title, detail);
        button.addEventListener('click', () => { void navigateProject(() => projects.open(project.id), dialog); });
        list.append(button);
      }
    }
    dialog.querySelector('#closeProjects')!.addEventListener('click', () => { dialog.close(); });
    dialog.addEventListener('cancel', event => { if (navigatingProject) event.preventDefault(); });
    dialog.addEventListener('close', () => dialog.remove());
    dialog.querySelector('#newProjectForm')!.addEventListener('submit', event => {
      event.preventDefault();
      const input = dialog.querySelector<HTMLInputElement>('#newProjectName')!;
      const name = input.value.trim();
      if (!name) { input.setCustomValidity('Enter a project name.'); input.reportValidity(); return; }
      void navigateProject(() => projects.create(name), dialog);
    });
    dialog.querySelector('#newProjectName')!.addEventListener('input', event => (event.target as HTMLInputElement).setCustomValidity(''));
    document.body.append(dialog);
    renderRecent();
    dialog.showModal();
    void projects.refresh().then(() => { if (dialog.isConnected) renderRecent(); }).catch(error => {
      if (dialog.isConnected) dialog.querySelector('[role="alert"]')!.textContent = error.message;
    });
  }

  function setupProjectChrome() {
    if (!projects.enabled) return;
    document.getElementById('projectsTrigger')!.addEventListener('click', openProjectPicker);
    document.getElementById('checkpointsTrigger')?.addEventListener('click', openCheckpoints);
    document.getElementById('projectSaveRetry')!.addEventListener('click', () => { void projects.flush().catch(() => {}); });
    document.getElementById('projectReload')!.addEventListener('click', () => {
      if (projects.active && confirm('Reload the saved project? Unsaved changes in this window will be discarded.')) {
        void navigateProject(() => projects.open(projects.active!.id, true));
      }
    });
    if (projects.active) {
      document.getElementById('recordingContext')!.textContent = projects.active.name;
      document.getElementById('recordingContext')!.title = projects.active.name;
    }
    updateProjectChrome();
  }

  function openCheckpoints() {
    if (!projects.active?.has_media || projectOperations.size || navigatingProject || document.getElementById('checkpointDialog')) return;
    const dialog = document.createElement('dialog');
    dialog.id = 'checkpointDialog'; dialog.className = 'project-dialog';
    dialog.setAttribute('aria-labelledby', 'checkpointHeading');
    dialog.innerHTML = `<div class="panel-heading"><h2 id="checkpointHeading">Edit checkpoints</h2><button id="closeCheckpoints">Close</button></div>
      <div class="project-dialog-body"><p class="field-help">Save named versions of your selections, edits, captions and settings. Restoring first saves a recovery checkpoint. Original media and exports are not copied or deleted.</p>
      <form id="checkpointForm"><label for="checkpointName">Checkpoint name</label><input id="checkpointName" type="text" maxlength="120" required placeholder="e.g. Before trying a shorter cut" autocomplete="off"><button class="primary" type="submit">Save checkpoint</button></form>
      <div id="checkpointStatus" role="status"></div><div id="checkpointList"></div><p role="alert"></p></div>`;
    let busy = false, requestVersion = 0;
    const alert = dialog.querySelector<HTMLElement>('[role="alert"]')!;
    const status = dialog.querySelector<HTMLElement>('#checkpointStatus')!;
    async function refresh() {
      const version = ++requestVersion;
      const items = await projects.checkpoints();
      if (!dialog.isConnected || version !== requestVersion) return;
      const list = dialog.querySelector('#checkpointList')!; list.replaceChildren();
      if (!items.length) list.textContent = 'No checkpoints yet. Save one before experimenting.';
      for (const item of items) {
        const row = document.createElement('div'); row.className = 'checkpoint-row';
        const title = document.createElement('strong'); title.textContent = item.name;
        const detail = document.createElement('span'); detail.textContent = `${new Date(item.created_at).toLocaleString()} · revision ${item.project_revision}`;
        const restore = document.createElement('button'); restore.type = 'button'; restore.textContent = 'Restore'; restore.dataset.restoreCheckpoint = item.id;
        restore.addEventListener('click', () => {
          if (!busy && confirm(`Restore “${item.name}”? Your current saved edit will be kept as a recovery checkpoint.`)) void navigateProject(() => projects.restoreCheckpoint(item.id), dialog);
        });
        const remove = document.createElement('button'); remove.type = 'button'; remove.textContent = 'Delete'; remove.dataset.deleteCheckpoint = item.id;
        remove.addEventListener('click', () => {
          if (!busy && confirm(`Permanently delete checkpoint “${item.name}”? Your current edit and original media are unaffected.`)) void run(async () => { await projects.deleteCheckpoint(item.id); status.textContent = 'Checkpoint deleted. Current edit unchanged.'; });
        });
        row.append(title, detail, restore, remove); list.append(row);
      }
    }
    async function run(action: () => Promise<void>) {
      if (busy || navigatingProject || projectOperations.size) return;
      busy = true; navigatingProject = true; alert.textContent = '';
      const shell = document.querySelector<HTMLElement>('.editor-shell'); if (shell) shell.inert = true;
      updateProjectChrome();
      dialog.querySelectorAll<HTMLButtonElement | HTMLInputElement>('button,input').forEach(node => node.disabled = true);
      try { await action(); await refresh(); }
      catch (error) { alert.textContent = error instanceof Error ? error.message : 'Checkpoint could not be saved.'; }
      finally {
        busy = false; navigatingProject = false;
        if (shell) shell.inert = false;
        dialog.querySelectorAll<HTMLButtonElement | HTMLInputElement>('button,input').forEach(node => node.disabled = false);
        updateProjectChrome();
      }
    }
    dialog.querySelector('#checkpointForm')!.addEventListener('submit', event => {
      event.preventDefault();
      const input = dialog.querySelector<HTMLInputElement>('#checkpointName')!;
      const name = input.value.trim();
      if (!name) { input.setCustomValidity('Enter a checkpoint name.'); input.reportValidity(); return; }
      void run(async () => { await projects.checkpoint(name); input.value = ''; status.textContent = 'Checkpoint saved. You can keep experimenting.'; });
    });
    dialog.querySelector('#checkpointName')!.addEventListener('input', event => (event.target as HTMLInputElement).setCustomValidity(''));
    dialog.querySelector('#closeCheckpoints')!.addEventListener('click', () => { if (!busy && !navigatingProject) dialog.close(); });
    dialog.addEventListener('cancel', event => { if (busy || navigatingProject) event.preventDefault(); });
    dialog.addEventListener('close', () => { requestVersion++; dialog.remove(); });
    document.body.append(dialog); dialog.showModal();
    void refresh().catch(error => { if (dialog.isConnected) alert.textContent = error.message; });
  }

  window.addEventListener('beforeunload', event => {
    if (projects.dirty) { event.preventDefault(); event.returnValue = ''; }
  });
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden') void projects.flush().catch(() => {});
  });

  function shellMarkup(workspace: string, review = false) {
    return `<div class="editor-shell ${review ? 'review-shell main-container' : 'import-shell'}">
      ${review ? '<a class="skip-link" href="#transcriptHeading">Skip to transcript</a>' : ''}
      <header class="shell-header">
        <h1 class="shell-brand"><img class="brand-logo" src="${logoUrl}" alt="" width="32" height="32">Hi-Lite</h1>
        ${projects.enabled ? `<div class="project-navigation"><button id="projectsTrigger">Projects</button>${projects.active?.has_media ? '<button id="checkpointsTrigger" data-project-operation>Checkpoints</button>' : ''}<span id="projectSaveStatus" role="status"></span><button id="projectSaveRetry" hidden>Retry save</button><button id="projectReload" hidden>Reload saved version</button></div>` : ''}
        <span class="recording-context" id="recordingContext">${review ? 'Current recording' : 'Import a recording'}</span>
        ${review ? `<div class="header-actions">
          <button class="drawer-trigger" id="mediaTrigger" aria-controls="mediaPanel" aria-expanded="false">Media</button>
          <button class="drawer-trigger" id="contextTrigger" aria-controls="contextPanel" aria-expanded="false">AI Highlights
            <span id="contextProviderSummary">Checking AI backend…</span></button>
          <span class="header-selection" id="headerSelectedCount">0 selected</span>
          <button id="exportBtn" disabled>Create Reel from Selected Clips</button>
        </div>` : ''}
      </header>
      <div class="workspace ${review ? '' : 'import-workspace upload-container'}">${workspace}</div>
      ${review ? timelineMarkup : ''}
      <footer class="activity-footer" aria-label="Activity">
        <div class="activity-summary"><strong>Activity</strong>
          <span id="statusMessage" role="status">${review ? 'Ready to review' : 'Choose a video file to begin'}</span>
          <button id="activityCancel" hidden>Cancel task</button>
          <button id="activityToggle" aria-expanded="false" aria-controls="activityDetails" hidden>Details</button></div>
        <div class="progress-bar" id="progressBar" role="progressbar" aria-label="Operation progress" hidden><div class="progress-fill" id="progressFill"></div></div>
        <div id="activityErrorSummary" hidden></div>
        ${projects.enabled ? '<p id="projectSaveError" class="project-save-error" role="alert" hidden></p>' : ''}
        <div id="activityDetails" hidden>
          <div id="activityErrors"></div>
          <div class="activity-results" id="activityResults">
            ${review ? `<a id="downloadLink" class="download-link">Download Reel from Selected Clips</a>
              <div class="download-row" id="autoDownloadRow">
                <a id="autoDownloadLink" class="download-link">Download Auto-Generated Reel</a>
                <button id="discardAutoBtn" class="discard-btn">Discard</button>
              </div>` : ''}
          </div>
        </div>
      </footer>
    </div>`;
  }

  function setupActivity() {
    const status = document.getElementById("statusMessage")!;
    const bar = document.getElementById("progressBar")!;
    const fill = document.getElementById("progressFill")!;
    const errors = document.getElementById("activityErrors")!;
    const summary = document.getElementById("activityErrorSummary")!;
    const details = document.getElementById("activityDetails")!;
    const toggle = document.getElementById("activityToggle")!;
    const cancel = document.getElementById('activityCancel') as HTMLButtonElement;
    let task: { operation: string; url: string; requesting: boolean; requested: boolean } | null = null;
    function expand(open: boolean) {
      if (!open && details.contains(document.activeElement)) toggle.focus();
      details.hidden = !open;
      toggle.setAttribute("aria-expanded", String(open));
      updateErrorSummary();
    }
    function updateErrorSummary() {
      summary.hidden = errors.children.length === 0;
      summary.textContent = `${errors.children.length} request error${errors.children.length === 1 ? '' : 's'}${details.hidden ? ' — open Details for recovery' : ''}`;
    }
    toggle.addEventListener("click", () => expand(details.hidden));
    function clearError(operation: string) {
      Array.from(errors.children).forEach(node => {
        if ((node as HTMLElement).dataset.operation === operation) {
          if (node.contains(document.activeElement)) toggle.focus();
          node.remove();
        }
      });
      updateErrorSummary();
    }
    const activity = {
      trackTask(operation: string, taskId: string) {
        task = { operation, url: projects.url(`/api/cancel/${encodeURIComponent(taskId)}`), requesting: false, requested: false };
        cancel.hidden = false;
        cancel.disabled = false;
        cancel.textContent = 'Cancel task';
        cancel.setAttribute('aria-label', `Cancel ${operation.toLowerCase()}`);
        clearError('Cancel task');
      },
      isCancelling(operation: string) { return task?.operation === operation && (task.requesting || task.requested); },
      endTask(operation: string) {
        if (task?.operation !== operation) return;
        task = null;
        if (document.activeElement === cancel) { status.tabIndex = -1; status.focus(); }
        cancel.hidden = true;
        cancel.disabled = false;
        clearError('Cancel task');
        clearError(`${operation} status`);
      },
      status(operation: string, message: string, progress?: number) {
        status.dataset.operation = operation;
        // Progress ticks update the bar, not a second live announcement.
        const pendingCancel = task?.operation === operation && (task.requesting || task.requested);
        const text = `${operation}: ${pendingCancel ? 'Cancellation requested. Waiting for the worker to stop…' : message}`;
        if (status.textContent !== text) status.textContent = text;
        bar.hidden = false;
        bar.classList.add("visible");
        const known = typeof progress === "number" && Number.isFinite(progress);
        bar.classList.toggle("indeterminate", !known);
        if (known) {
          const value = Math.max(0, Math.min(100, progress));
          fill.style.width = `${value}%`;
          bar.setAttribute("aria-valuenow", String(value));
        } else {
          fill.style.width = "";
          bar.removeAttribute("aria-valuenow");
        }
      },
      finish(operation: string, message: string) {
        if (status.dataset.operation !== operation) return;
        status.textContent = `${operation}: ${message}`;
        delete status.dataset.operation;
        bar.hidden = true;
        bar.classList.remove("visible");
      },
      error(operation: string, message: string, recovery: string) {
        clearError(operation);
        const row = document.createElement("div");
        row.className = "activity-error";
        row.dataset.operation = operation;
        const text = document.createElement("span");
        text.setAttribute("role", "alert");
        text.textContent = `${operation}: ${message} ${recovery}`;
        const dismiss = document.createElement("button");
        dismiss.textContent = "Dismiss";
        dismiss.setAttribute("aria-label", `Dismiss ${operation} error`);
        dismiss.inert = !!document.querySelector<HTMLElement>(".editor-shell")?.dataset.drawer;
        dismiss.addEventListener("click", () => clearError(operation));
        row.append(text, dismiss);
        errors.append(row);
        toggle.hidden = false;
        expand(true);
      },
      clearError,
      results() { toggle.hidden = false; expand(true); }
    };
    cancel.addEventListener('click', async () => {
      const current = task;
      if (!current || current.requesting || current.requested) return;
      current.requesting = true;
      cancel.disabled = true;
      cancel.textContent = 'Cancelling…';
      activity.status(current.operation, 'Requesting cancellation…');
      try {
        const response = await fetch(current.url, { method: 'POST' });
        const result = await response.json();
        if (task !== current) return;
        if (!response.ok || result.status !== 'cancelling') throw new Error(result.message || 'Could not request cancellation.');
        current.requested = true;
        activity.clearError('Cancel task');
      } catch (error) {
        if (task !== current) return;
        cancel.disabled = false;
        cancel.textContent = 'Retry cancellation';
        activity.error('Cancel task', error instanceof Error ? error.message : 'Could not request cancellation.', 'The task is still being monitored. Retry cancellation, or wait for it to finish.');
      } finally {
        current.requesting = false;
        if (task === current) activity.status(current.operation, current.requested ? 'Cancelling…' : 'Still processing…');
      }
    });
    return activity;
  }

  function setupShell() {
    disposeShell();
    const controller = new AbortController();
    disposeShell = () => controller.abort();
    const activity = setupActivity();
    setupProjectChrome();
    const root = document.querySelector<HTMLElement>(".editor-shell")!;
    const mediaNode = document.getElementById("mediaPanel");
    const contextNode = document.getElementById("contextPanel");
    if (!mediaNode || !contextNode) return { activity, openContext() {} };
    const media = mediaNode;
    const context = contextNode;

    const mediaTrigger = document.getElementById("mediaTrigger")!;
    const contextTrigger = document.getElementById("contextTrigger")!;
    const header = root.querySelector<HTMLElement>(".shell-header")!;
    const editor = document.getElementById("centralEditor")!;
    const results = document.getElementById("activityResults")!;
    const activityToggle = document.getElementById("activityToggle")!;
    const skip = root.querySelector<HTMLElement>(".skip-link")!;
    const backdrop = document.getElementById("drawerBackdrop")!;
    let openPane: HTMLElement | null = null;
    let opener: HTMLElement | null = null;
    let lastFocused = document.activeElement as HTMLElement;
    const isDrawer = (pane: HTMLElement) =>
      pane === media || window.innerWidth < 1600 || root.dataset.contextHidden === "true";
    const triggerFor = (pane: HTMLElement) => pane === media ? mediaTrigger : contextTrigger;
    const headingFor = (pane: HTMLElement) => pane.querySelector<HTMLElement>("h2")!;
    function focusTargets(pane: HTMLElement) {
      return Array.from(pane.querySelectorAll<HTMLElement>('button, input, textarea, a[href], [tabindex="0"]'))
        .filter(node => node.checkVisibility() && !(node as HTMLButtonElement).disabled);
    }
    function setModality(pane: HTMLElement | null) {
      root.dataset.drawer = pane?.id || "";
      editor.inert = !!pane;
      document.getElementById('sourceTimeline')!.inert = !!pane;
      header.inert = !!pane;
      skip.inert = !!pane;
      results.inert = !!pane;
      activityToggle.inert = !!pane;
      document.getElementById("activityCancel")!.inert = !!pane;
      document.querySelectorAll<HTMLElement>("#activityErrors button").forEach(button => button.inert = !!pane);
      media.inert = !!pane && pane !== media;
      context.inert = !!pane && pane !== context;
      backdrop.style.display = pane ? "block" : "";
    }
    function closeDrawer(restore = true) {
      if (!openPane) return;
      const pane = openPane;
      const target = opener;
      openPane = null;
      opener = null;
      pane.classList.remove("drawer-open");
      pane.removeAttribute("role");
      pane.removeAttribute("aria-modal");
      triggerFor(pane).setAttribute("aria-expanded", "false");
      setModality(null);
      if (restore) (target?.checkVisibility() ? target : headingFor(pane)).focus();
    }
    function openDrawer(pane: HTMLElement, trigger = triggerFor(pane), keepFocus = false) {
      if (!isDrawer(pane)) return;
      if (openPane === pane) return;
      closeDrawer(false);
      openPane = pane;
      opener = trigger;
      pane.classList.add("drawer-open");
      pane.setAttribute("role", "dialog");
      pane.setAttribute("aria-modal", "true");
      triggerFor(pane).setAttribute("aria-expanded", "true");
      setModality(pane);
      if (!keepFocus) headingFor(pane).focus();
    }
    mediaTrigger.addEventListener("click", () => openDrawer(media));
    contextTrigger.addEventListener("click", () => {
      if (window.innerWidth >= 1600) {
        root.dataset.contextHidden = "false";
        responsive();
        headingFor(context).focus();
      } else openDrawer(context);
    });
    [media, context].forEach(pane => pane.querySelector(".drawer-close")!.addEventListener("click", () => {
      if (pane === context && !isDrawer(context)) {
        root.dataset.contextHidden = "true";
        contextTrigger.setAttribute("aria-expanded", "false");
        contextTrigger.focus();
      } else closeDrawer();
    }));
    backdrop.addEventListener("click", () => closeDrawer());
    // A running action can disable the focused button and move focus to body.
    // Drawer Escape/Tab must still work until that drawer has closed.
    document.addEventListener("keydown", event => {
      if (!openPane) return;
      if (event.key === "Escape") { event.preventDefault(); closeDrawer(); }
      if (event.key === "Tab") {
        const targets = focusTargets(openPane);
        const index = targets.indexOf(document.activeElement as HTMLElement);
        if (!targets.length) { event.preventDefault(); headingFor(openPane).focus(); }
        else if (event.shiftKey && index <= 0) { event.preventDefault(); targets[targets.length - 1].focus(); }
        else if (!event.shiftKey && (index === targets.length - 1 || index < 0)) { event.preventDefault(); targets[0].focus(); }
      }
    }, { signal: controller.signal });
    root.addEventListener("focusin", event => {
      lastFocused = event.target as HTMLElement;
      if (openPane && !openPane.contains(event.target as Node)) headingFor(openPane).focus();
    });
    function updateChromeHeight() {
      const footerHeight = root.querySelector<HTMLElement>("footer")!.offsetHeight;
      root.style.setProperty("--shell-header-height", `${header.offsetHeight}px`);
      root.style.setProperty("--shell-footer-height", `${footerHeight}px`);
      root.style.setProperty("--shell-chrome-height", `${header.offsetHeight + footerHeight}px`);
    }
    function responsive() {
      // Chromium may blur a control as the breakpoint hides its old layout.
      const focused = document.activeElement === document.body && !lastFocused.checkVisibility()
        ? lastFocused : document.activeElement as HTMLElement;
      updateChromeHeight();
      if (openPane && !isDrawer(openPane)) {
        const pane = openPane;
        closeDrawer(false);
        if (focused === triggerFor(pane) || !focused.checkVisibility()) headingFor(pane).focus();
      }
      for (const pane of [media, context]) {
        if (pane.contains(focused) && isDrawer(pane) && openPane !== pane) {
          openDrawer(pane, triggerFor(pane), true);
          focused.focus({ preventScroll: true });
        }
        if (focused === triggerFor(pane) && !isDrawer(pane)) headingFor(pane).focus();
      }
      const docked = !isDrawer(context);
      contextTrigger.setAttribute("aria-expanded", String(docked || openPane === context));
      const closeButton = context.querySelector(".drawer-close")!;
      closeButton.textContent = docked ? "Hide" : "Close";
      closeButton.setAttribute("aria-label", `${docked ? 'Hide' : 'Close'} AI Highlights`);
    }
    window.addEventListener("resize", responsive, { signal: controller.signal });
    const chromeObserver = new ResizeObserver(updateChromeHeight);
    chromeObserver.observe(header);
    chromeObserver.observe(root.querySelector("footer")!);
    disposeShell = () => { controller.abort(); chromeObserver.disconnect(); };
    responsive();
    return { activity, openContext() {
      if (window.innerWidth >= 1600) {
        root.dataset.contextHidden = "false";
        responsive();
      } else openDrawer(context);
    } };
  }

  async function main(recordingName?: string) {
    const app = document.getElementById("app")!;
    if (!app.querySelector(".editor-shell")) {
      app.innerHTML = shellMarkup('<main class="upload-box"><h2>Loading recording…</h2></main>');
      app.querySelector(".workspace")!.classList.remove("upload-container");
      setupShell().activity.status("Recording", "Loading…");
    }
  
    if (projects.enabled && !projects.active) {
      showUploadInterface();
      return;
    }
    // Try to load segments
    let segments: Segment[];
    let source: SourceReference | null = null;
    let currentVideoId = "default";
  
    try {
      const response = await fetch(projects.url("/data/segments_manifest.json"), { cache: "no-store" });
      if (response.status === 404) {
        showUploadInterface();
        return;
      }
      if (!response.ok) throw new Error(`Could not load the recording manifest (HTTP ${response.status}).`);
      const manifest = await response.json();
      segments = readSegments(manifest);
      source = readSource(manifest, projects.prefix);
    } catch (err) {
      const message = err instanceof Error && !(err instanceof SyntaxError)
        ? err.message : "The recording manifest could not be read. It may contain invalid JSON.";
      showUploadInterface(`${message} Upload a recording to try again.`);
      return;
    }

    if (segments.length === 0 && !source) {
      showUploadInterface();
      return;
    }
    if (projects.active) projects.active.has_media = true;
    
    // Build main UI
    buildMainUI(segments, currentVideoId, recordingName, source);
  }
  
  function mountSubtitles(parent: HTMLElement, activity: ReturnType<typeof setupActivity>) {
    const panel = document.createElement("section");
    panel.className = "subtitles-panel";
    panel.setAttribute("aria-label", "Original recording subtitles");
    panel.innerHTML = `<strong>Original recording subtitles</strong>
      <p>Full original transcript on recording time, including unselected speech.</p>
      <button disabled data-format="srt">Download SRT</button>
      <button disabled data-format="vtt">Download VTT</button>
      <span role="status">Checking captions…</span>`;
    parent.append(panel);
    const status = panel.querySelector("span")!;
    const buttons = Array.from(panel.querySelectorAll("button"));
    let downloading = false;
    function setStatus(message: string) {
      if (status.textContent !== message) status.textContent = message;
    }
    async function refresh() {
      if (!panel.isConnected) return;
      try {
        const response = await fetch(projects.url("/api/subtitles"), { cache: "no-store" });
        const result = await response.json();
        if (!downloading) {
          const importing = (document.getElementById("uploadButton") as HTMLButtonElement | null)?.disabled;
          setStatus(importing ? "Subtitles unavailable while importing" : result.message || "Subtitles unavailable");
          buttons.forEach(button => button.disabled = !!importing || !response.ok || !result.available);
        }
      } catch {
        setStatus("Could not check subtitle availability");
        buttons.forEach(button => button.disabled = true);
      }
      if (panel.isConnected) window.setTimeout(refresh, 2500);
    }
    buttons.forEach(button => button.addEventListener("click", async () => {
      downloading = true;
      buttons.forEach(button => button.disabled = true);
      try {
        const format = button.dataset.format!;
        const response = await fetch(projects.url(`/api/subtitles/${format}`), { cache: "no-store" });
        if (!response.ok) {
          const result = await response.json();
          throw new Error(result.message || "Subtitle download failed");
        }
        const url = URL.createObjectURL(await response.blob());
        const link = document.createElement("a");
        link.href = url;
        link.download = `original-recording.${format}`;
        panel.append(link);
        link.click();
        link.remove();
        window.setTimeout(() => URL.revokeObjectURL(url), 60000);
        setStatus("Subtitle download started");
        activity.clearError("Subtitle download");
      } catch (error) {
        const message = error instanceof Error ? error.message : "Subtitle download failed";
        setStatus(message);
        activity.error("Subtitle download", message, "Retry SRT or VTT in Media when available.");
      } finally {
        downloading = false;
        // Recheck on the next availability poll before enabling downloads.
      }
    }));
    void refresh();
  }

  function mountRelink(parent: HTMLElement, activity: ReturnType<typeof setupActivity>) {
    if (!window.hiLiteDesktop || !projects.active?.has_media) return;
    const button = document.createElement('button');
    button.id = 'relinkSource';
    button.dataset.projectOperation = 'relink';
    button.textContent = 'Relink original file';
    const help = document.createElement('p');
    help.className = 'field-help';
    help.textContent = 'If your linked recording moved, choose the same original file. Your transcript and edits stay intact.';
    parent.append(button, help);
    button.addEventListener('click', async () => {
      if (projectOperations.size || !projects.active) return;
      projectBusy('relink', true);
      activity.status('Source', 'Choose the original recording…');
      try {
        await projects.flush();
        const result = await window.hiLiteDesktop!.relinkProject(projects.active.id);
        if (result.status === 'cancelled') { activity.finish('Source', 'Relinking cancelled'); return; }
        if (result.status !== 'ok') throw new Error(result.message || 'Could not relink the original file.');
        activity.finish('Source', 'Original recording relinked');
        await main();
      } catch (error) {
        activity.finish('Source', 'Relinking not completed');
        activity.error('Source', error instanceof Error ? error.message : 'Could not relink the original file.', 'Choose the same unmodified recording and try Relink original file again.');
      } finally { projectBusy('relink', false); }
    });
  }

  function showUploadInterface(errorMessage = "") {
    disposeShell();
    const app = document.getElementById("app")!;
    const video = app.querySelector("video");
    if (video) {
      video.pause();
      video.removeAttribute("src");
      video.load();
    }
    
    app.innerHTML = shellMarkup(`
        <main class="upload-box" aria-labelledby="importHeading">
          <h2 id="importHeading">Import a recording</h2>
          <p>Choose a video to prepare its transcript and review clips.</p>
          <input type="file" id="videoInput" class="upload-input" accept="video/*">
          <button id="uploadButton" class="upload-button">Choose Video File</button>
        </main>
    `);
    const { activity } = setupShell();
  
    mountSubtitles(app.querySelector(".upload-box")!, activity);
    const videoInput = document.getElementById("videoInput") as HTMLInputElement;
    const uploadButton = document.getElementById("uploadButton") as HTMLButtonElement;
    if (errorMessage) {
      activity.error("Recording", errorMessage, "Choose Video File to retry.");
    }
  
    if (projects.enabled && projects.active?.has_media) {
      uploadButton.textContent = 'New / Open Project';
      uploadButton.addEventListener('click', openProjectPicker);
      const retry = document.createElement('button');
      retry.textContent = 'Retry loading recording';
      retry.addEventListener('click', () => { void main(); });
      uploadButton.after(retry);
    } else uploadButton.addEventListener("click", () => videoInput.click());
    uploadButton.focus();
  
    async function pollImport(taskId: string, recordingName?: string) {
      activity.trackTask('Import', taskId);
      activity.status('Import', 'Processing video…');
      try {
        while (app.contains(uploadButton)) {
          await new Promise(resolve => setTimeout(resolve, 2000));
          if (!app.contains(uploadButton)) return;
          let status;
          try {
            const response = await fetch(projects.url(`/api/processing-status/${encodeURIComponent(taskId)}`));
            if (!response.ok) throw new Error('Could not check video processing status.');
            status = await response.json();
            if (!isRecord(status) || typeof status.status !== 'string') throw new Error('Invalid video processing status. Please try again.');
          } catch (error) {
            if (!activity.isCancelling('Import')) throw error;
            activity.error('Import status', 'Could not confirm that the worker has stopped.', 'Waiting for the backend connection before allowing another import.');
            continue;
          }
          activity.clearError('Import status');
          activity.status('Import', typeof status.message === 'string' ? status.message : 'Processing…', typeof status.progress === 'number' ? status.progress : undefined);
          if (status.status === 'complete') {
            activity.finish('Import', 'Complete! Loading segments…');
            projectBusy('import', false);
            await main(recordingName);
            return;
          }
          if (status.status === 'cancelled') {
            activity.clearError('Import');
            activity.finish('Import', 'Cancelled. Choose a video to try again.');
            return;
          }
          if (status.status === 'error') throw new Error(typeof status.message === 'string' ? status.message : 'Video processing failed.');
          if (status.status !== 'processing' && status.status !== 'cancelling') {
            if (activity.isCancelling('Import')) {
              activity.error('Import status', 'The worker has not confirmed cancellation.', 'Waiting for a terminal task status before allowing another import.');
              continue;
            }
            throw new Error('Invalid video processing status. Please try again.');
          }
        }
      } finally { activity.endTask('Import'); }
    }
    if (projects.enabled && window.hiLiteDesktop && !projects.active?.has_media) {
      const link = document.createElement('button');
      link.id = 'linkSource';
      link.dataset.projectOperation = 'import';
      link.textContent = 'Link original file without copying';
      uploadButton.after(link);
      link.addEventListener('click', async () => {
        if (projectOperations.size) return;
        projectBusy('import', true);
        uploadButton.disabled = true;
        activity.status('Import', 'Choose a recording on this computer…');
        try {
          if (!projects.active) await projects.create('Linked recording');
          const result = await window.hiLiteDesktop!.linkProject(projects.active!.id);
          if (result.status === 'cancelled') { activity.finish('Import', 'File selection cancelled'); return; }
          if (result.status !== 'ok' || !result.task_id) throw new Error(result.message || 'Could not link this recording.');
          await pollImport(result.task_id);
        } catch (error) {
          activity.finish('Import', 'Ready to retry');
          activity.error('Import', error instanceof Error ? error.message : 'Could not link the recording.', 'Retry linking the file, or use Choose Video File to copy it into this project.');
        } finally { uploadButton.disabled = false; projectBusy('import', false); }
      });
    }
    mountRelink(app.querySelector('.upload-box')!, activity);

    videoInput.addEventListener("change", async (e) => {
      const file = (e.target as HTMLInputElement).files?.[0];
      if (!file || uploadButton.disabled) return;
      // Allow choosing the same file again after an upload or polling error.
      videoInput.value = "";
  
      projectBusy("import", true);
      uploadButton.disabled = true;
      app.querySelectorAll<HTMLButtonElement>("button[data-format]").forEach(button => button.disabled = true);
      uploadButton.textContent = "Uploading...";
      activity.status("Import", "Uploading video…");
  
      const formData = new FormData();
      formData.append("video", file);
  
      try {
        if (projects.enabled && !projects.active) await projects.create(file.name.replace(/\.[^.]+$/, '').slice(0, 120) || 'Untitled project');
        const response = await fetch(projects.url("/api/upload"), {
          method: "POST",
          body: formData
        });
  
        const result = await response.json();
  
        if (response.ok && result.status === "ok" && typeof result.task_id === "string" && result.task_id) {
          await pollImport(result.task_id, file.name);
        } else {
          throw new Error(result.message);
        }
      } catch (err) {
        console.error(err);
        activity.finish("Import", "Ready to retry");
        activity.error("Import", err instanceof Error ? err.message : "Upload failed", "Choose the video file again to retry.");
        uploadButton.disabled = false;
        uploadButton.textContent = "Choose Video File";
      } finally {
        uploadButton.disabled = false;
        uploadButton.textContent = 'Choose Video File';
        projectBusy("import", false);
      }
    });
  }
  
  function buildMainUI(segments: Segment[], videoId: string, recordingName?: string, source: SourceReference | null = null) {
    const app = document.getElementById("app")!;
    app.innerHTML = shellMarkup(`
      <aside class="side-panel" id="mediaPanel" aria-labelledby="mediaHeading">
        <div class="panel-heading"><h2 id="mediaHeading" tabindex="-1">Media</h2>
          <button class="drawer-close" aria-label="Close Media">Close</button></div>
        <div class="side-body" id="mediaBody">
          <button id="uploadNewBtn">Upload New</button>
          <p class="source-info" id="sourceInfo"></p>
          <p class="field-help" id="sourceDurationInfo"></p>
        </div>
      </aside>
      <main class="central-editor" id="centralEditor">
        <section class="preview-panel" aria-labelledby="previewHeading">
          <div class="preview-heading"><h2 id="previewHeading">Preview</h2><span id="activeSegmentContext"></span></div>
          <div class="video-container"><video id="mainVideo" controls preload="metadata" aria-label="Original recording preview"></video>
            <p id="sourcePreviewStatus" class="source-preview-status" hidden></p></div>
          <div class="playback-dock">Playback time on the original recording</div>
          <div class="clip-context">
            <h2 id="clipContextHeading">Reviewing segment</h2>
            <p id="clipContextText"></p>
            <p class="field-help" id="clipContextTiming"></p>
            <p class="field-help">Review freely. Check clips in the transcript to include them in your reel.</p>
          </div>
        </section>
        <section class="segments-panel" aria-labelledby="transcriptHeading">
          <h2 class="transcript-heading" id="transcriptHeading" tabindex="-1">Transcript</h2>
          <div class="toolbar">
            <button id="selectAll">Select All</button>
            <button id="selectNone">Deselect All</button>
            <button id="selectKeywords" class="primary">Select Keywords</button>
            <label><input type="checkbox" id="showOnlyKeywords"> Keyword Only</label>
          </div>
          <div class="stats">
            <div class="stat-item">Total: <strong>${segments.length}</strong></div>
            <div class="stat-item">Selected: <strong id="selectedCount">0</strong></div>
            <div class="stat-item">Duration: <strong id="selectedDuration">0.0s</strong></div>
            <span id="filterSummary" role="status"></span>
          </div>
          ${projects.active ? '<section id="transcriptEditorTools" aria-label="Editing tools"></section>' : ''}
          <div class="segments-list" id="segmentsList"></div>
        </section>
      </main>
      <aside class="side-panel" id="contextPanel" aria-labelledby="contextHeading">
        <div class="panel-heading"><h2 id="contextHeading" tabindex="-1">AI Highlights</h2>
          <button class="drawer-close" aria-label="Close AI Highlights">Close</button></div>
        <div class="side-body">
          <div id="generationControls">
            <label class="criteria-label" for="aiInstructions">What moments do you want to find?</label>
            <p class="field-help" id="criteriaHelp">Required. Describe what to keep and what to leave out.</p>
            <textarea id="aiInstructions" rows="4" required aria-describedby="criteriaHelp"
              placeholder="e.g. Big plays, surprising reactions and funny exchanges. Skip pauses and repeated moments."></textarea>
            <div class="provider-row">
              <span id="llmStatus" role="status">Checking AI backend…</span>
              <button id="llmSettingsToggle" aria-expanded="false" aria-controls="llmSettingsPanel">AI Settings</button>
            </div>
            <p id="llmNotice" class="field-help"></p>
            <div id="generationThreshold" class="field-help"></div>
            <p class="generation-outcome">Creates a separate AI reel. Your checked clips stay unchanged.</p>
            <button id="autoGenerateBtn" disabled aria-describedby="generationHint">Auto-Generate Highlight Reel</button>
            <p id="generationHint" class="field-help">Describe your moments above to enable Auto-Generate.</p>
          </div>

          <button id="advancedToggle" aria-expanded="false" aria-controls="advancedSection">Scoring options</button>
          <div class="advanced-section" id="advancedSection">
            <div class="slider-control">
              <label for="minScoreSlider"><span>Minimum score</span><span class="score-badge" id="scoreValue">7</span></label>
              <input type="range" id="minScoreSlider" min="1" max="10" value="7" step="1">
            </div>
            <label class="rescore-control"><input type="checkbox" id="rescoreCheck"> Re-run AI scoring</label>
            <p class="field-help">Otherwise, scores from the same instructions are reused.</p>
            <div class="context-actions">
              <button id="previewBtn">Selection Statistics</button>
              <button id="resetSettingsBtn">Reset Threshold</button>
            </div>
            <div class="preview-info" id="previewInfo" role="status"></div>
          </div>

          <div id="llmSettingsPanel" class="provider-settings" style="display: none;">
            <p class="field-help">Scoring uses a local model server if one is running (Ollama at 127.0.0.1:11434 by default), otherwise the API below with your own key.</p>
            <label for="llmBaseUrl">API base URL</label>
            <input id="llmBaseUrl" type="text" placeholder="https://api.openai.com/v1">
            <label for="llmModel">Model</label>
            <input id="llmModel" type="text" placeholder="gpt-4o-mini">
            <label for="llmApiKey">API key</label>
            <p class="field-help">Stored on this machine only, never in browser storage.</p>
            <input id="llmApiKey" type="password" autocomplete="off">
            <label for="llmLocalUrl">Local server URL</label>
            <input id="llmLocalUrl" type="text" placeholder="http://127.0.0.1:11434">
            <label for="llmLocalModel">Local model</label>
            <input id="llmLocalModel" type="text" placeholder="qwen2.5:7b-instruct">
            <p class="field-help" id="llmSettingsHint" role="status">Test Connection uses saved settings. Save any changes first.</p>
            <div class="context-actions">
              <button id="llmSaveBtn" class="primary">Save</button>
              <button id="llmTestBtn" aria-describedby="llmSettingsHint">Test Connection</button>
            </div>
            <div id="llmTestResult" role="status"></div>
          </div>
        </div>
      </aside>
      <div class="drawer-backdrop" id="drawerBackdrop"></div>
    `, true);
    const context = projects.active?.name || recordingName || "Current recording";
    document.getElementById("recordingContext")!.textContent = context;
    document.getElementById("recordingContext")!.title = context;
    document.getElementById("sourceInfo")!.textContent = `${context} · ${segments.length} segments · ${segments.reduce((sum, segment) => sum + segment.duration, 0).toFixed(1)}s of clips`;
    const shell = setupShell();
    setupEventListeners(segments, videoId, shell, source);
    document.getElementById("transcriptHeading")!.focus();
  }

  function setupEventListeners(segments: Segment[], videoId: string, shell: ReturnType<typeof setupShell>, source: SourceReference | null) {
    const { activity } = shell;
    const segmentsList = document.getElementById("segmentsList")!;
    const mainVideo = document.getElementById("mainVideo") as HTMLVideoElement;
    const exportBtn = document.getElementById("exportBtn") as HTMLButtonElement;
    mountSubtitles(document.getElementById("mediaBody")!, activity);
    mountRelink(document.getElementById("mediaBody")!, activity);
    const downloadLink = document.getElementById("downloadLink") as HTMLAnchorElement;
    const autoGenerateBtn = document.getElementById("autoGenerateBtn") as HTMLButtonElement;
    const autoDownloadLink = document.getElementById("autoDownloadLink") as HTMLAnchorElement;
    const autoDownloadRow = document.getElementById("autoDownloadRow")!;
    const discardAutoBtn = document.getElementById("discardAutoBtn") as HTMLButtonElement;
    const advancedToggle = document.getElementById("advancedToggle") as HTMLButtonElement;
    const advancedSection = document.getElementById("advancedSection")!;
    const previewBtn = document.getElementById("previewBtn") as HTMLButtonElement;
    const previewInfo = document.getElementById("previewInfo")!;
    const minScoreSlider = document.getElementById("minScoreSlider") as HTMLInputElement;
    const instructionsInput = document.getElementById("aiInstructions") as HTMLTextAreaElement;
    const generationThreshold = document.getElementById("generationThreshold")!;
    const scoreValue = document.getElementById("scoreValue")!;
    const resetSettingsBtn = document.getElementById("resetSettingsBtn") as HTMLButtonElement;
    const selectedCountEl = document.getElementById("selectedCount")!;
    const selectedDurationEl = document.getElementById("selectedDuration")!;
    const uploadNewBtn = document.getElementById("uploadNewBtn") as HTMLButtonElement;
  
    const llmStatus = document.getElementById("llmStatus")!;
    const llmNotice = document.getElementById("llmNotice")!;
    const llmSettingsToggle = document.getElementById("llmSettingsToggle") as HTMLButtonElement;
    const llmSettingsPanel = document.getElementById("llmSettingsPanel")!;
    const llmBaseUrl = document.getElementById("llmBaseUrl") as HTMLInputElement;
    const llmModel = document.getElementById("llmModel") as HTMLInputElement;
    const llmApiKey = document.getElementById("llmApiKey") as HTMLInputElement;
    const llmLocalUrl = document.getElementById("llmLocalUrl") as HTMLInputElement;
    const llmLocalModel = document.getElementById("llmLocalModel") as HTMLInputElement;
    const llmSaveBtn = document.getElementById("llmSaveBtn") as HTMLButtonElement;
    const llmTestBtn = document.getElementById("llmTestBtn") as HTMLButtonElement;
    const llmTestResult = document.getElementById("llmTestResult")!;

    const savedState = projects.active?.state || {};
    const savedSelection = Array.isArray(savedState.selected_ids) ? savedState.selected_ids : [];
    const selectedIds = new Set<number>(segments.filter(segment => savedSelection.includes(segment.id)).map(segment => segment.id));
    if (typeof savedState.instructions === 'string') instructionsInput.value = savedState.instructions;
    if (typeof savedState.min_score === 'number' && savedState.min_score >= 1 && savedState.min_score <= 10) minScoreSlider.value = String(savedState.min_score);
    const keywordFilter = document.getElementById('showOnlyKeywords') as HTMLInputElement;
    keywordFilter.checked = savedState.keyword_only === true;
    function saveEditorState() {
      projects.update({ selected_ids: segments.filter(segment => selectedIds.has(segment.id)).map(segment => segment.id), instructions: instructionsInput.value,
        min_score: Number(minScoreSlider.value), keyword_only: keywordFilter.checked });
    }
    let currentSegmentId: number | null = null;
    let llmAvailable = false;
    let autoGenerating = false;
    let exporting = false;

    function setAdvanced(open: boolean) {
      if (!open && advancedSection.contains(document.activeElement)) advancedToggle.focus();
      advancedSection.classList.toggle("visible", open);
      advancedToggle.setAttribute("aria-expanded", String(open));
    }

    function updateProviderSummary() {
      const summary = document.getElementById("contextProviderSummary")!;
      summary.textContent = llmStatus.textContent;
      summary.title = llmStatus.textContent || "";
    }

    // Native inputs retain current values while scoring options are hidden.
    // Both request paths take the same snapshot; panel visibility is only UI state.
    function readScoringSelection() {
      return { min_score: Number(minScoreSlider.value), instructions: instructionsInput.value.trim() };
    }

    function updateScoringSummary() {
      const selection = readScoringSelection();
      scoreValue.textContent = String(selection.min_score);
      generationThreshold.textContent = `Score threshold: ${selection.min_score}+`;
      previewInfo.classList.remove("visible");
      instructionsInput.removeAttribute("aria-invalid");
      setAutoGenerateEnabled(llmAvailable);
    }

    function setAutoGenerateEnabled(enabled: boolean) {
      autoGenerateBtn.disabled = !enabled || autoGenerating || !instructionsInput.value.trim();
      document.getElementById("generationHint")!.textContent = !instructionsInput.value.trim()
        ? "Describe your moments above to enable Auto-Generate."
        : !enabled ? "Configure or test AI Settings to enable Auto-Generate." : "";
    }

    async function refreshLlmStatus() {
      try {
        const response = await fetch("/api/llm-settings");
        if (!response.ok) throw new Error("Could not check AI backend status.");
        const s = await response.json();
        activity.clearError("AI status");
        const nextSaved = {
          base_url: s.base_url || "", model: s.model || "", api_key: "",
          local_url: s.local_url || "", local_model: s.local_model || ""
        };
        // Refresh saved values only where the user has no draft edit, including
        // edits made while Save/Test or the initial status request is in flight.
        for (const key of providerKeys) {
          if (providerFields[key].value === savedProviderValues[key]) {
            providerFields[key].value = nextSaved[key];
          }
        }
        savedProviderValues = nextSaved;
        updateProviderForm();
        llmApiKey.placeholder = s.has_api_key ? "(saved - enter a new key to replace)" : "";

        llmAvailable = !!s.resolved_backend;
        setAutoGenerateEnabled(llmAvailable);

        if (s.resolved_backend === "local") {
          llmStatus.textContent = `AI backend: local model (${s.local_model})`;
          llmNotice.textContent = "Scoring runs entirely on this machine. Nothing is sent over the network.";
        } else if (s.resolved_backend === "api") {
          let host = s.base_url;
          try { host = new URL(s.base_url).host; } catch {}
          llmStatus.textContent = `AI backend: API (${s.model})`;
          llmNotice.textContent = `Auto-Generate sends your transcript text to ${host} using your API key. Your video and audio never leave your machine.`;
        } else if (s.resolved_backend === "g4f") {
          llmStatus.textContent = "AI backend: g4f (development only)";
          llmNotice.textContent = "g4f relays your transcript to unpredictable third-party endpoints. Development use only; never use it with private recordings.";
        } else {
          llmStatus.textContent = "AI backend: not configured";
          llmNotice.textContent = "Auto-Generate is disabled. Run a local model server (e.g. Ollama) or add an API key under AI Settings. Manual clip selection always works, fully offline.";
        }
      } catch {
        llmAvailable = false;
        setAutoGenerateEnabled(false);
        llmStatus.textContent = "AI backend: status unavailable";
        llmNotice.textContent = "Auto-Generate is disabled. Check the backend connection, then save or test your AI Settings to retry.";
        activity.error("AI status", "Could not check AI backend status.", "Check the backend connection, then Save or Test AI Settings to retry.");
      }
      updateProviderSummary();
    }

    llmSettingsToggle.addEventListener("click", () => {
      const isOpen = llmSettingsPanel.style.display !== "none";
      if (isOpen && llmSettingsPanel.contains(document.activeElement)) llmSettingsToggle.focus();
      llmSettingsPanel.style.display = isOpen ? "none" : "block";
      llmSettingsToggle.setAttribute("aria-expanded", String(!isOpen));
      if (!isOpen) llmBaseUrl.focus();
    });

    const providerFields = {
      base_url: llmBaseUrl, model: llmModel, api_key: llmApiKey,
      local_url: llmLocalUrl, local_model: llmLocalModel
    };
    const providerKeys = Object.keys(providerFields) as (keyof typeof providerFields)[];
    function readProviderDraft() {
      return Object.fromEntries(providerKeys.map(key => [key, providerFields[key].value])) as Record<keyof typeof providerFields, string>;
    }
    let savedProviderValues = readProviderDraft();
    let providerBusy = false;
    function providerHasChanges() {
      return providerKeys.some(key => providerFields[key].value !== savedProviderValues[key]);
    }
    function updateProviderForm() {
      const dirty = providerHasChanges();
      llmSaveBtn.disabled = providerBusy;
      llmTestBtn.disabled = providerBusy || dirty;
      document.getElementById("llmSettingsHint")!.textContent = dirty
        ? "Unsaved changes. Save before testing the connection."
        : "Test Connection uses saved settings. Save any changes first.";
    }
    for (const field of Object.values(providerFields)) {
      field.addEventListener("input", () => {
        llmTestResult.textContent = "";
        updateProviderForm();
      });
    }

    llmSaveBtn.addEventListener("click", async () => {
      if (providerBusy) return;
      const submittedDraft = readProviderDraft();
      const payload: Record<string, string> = {
        base_url: llmBaseUrl.value.trim(),
        model: llmModel.value.trim(),
        local_url: llmLocalUrl.value.trim(),
        local_model: llmLocalModel.value.trim(),
      };
      // A blank key field means "keep the saved key", not "clear it".
      if (llmApiKey.value.trim()) payload.api_key = llmApiKey.value.trim();

      providerBusy = true;
      updateProviderForm();
      llmSaveBtn.textContent = "Saving...";
      if (!autoGenerating && !exporting) activity.status("AI Settings", "Saving…");
      try {
        const response = await fetch("/api/llm-settings", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload)
        });
        const result = await response.json();
        if (response.ok && result.status === "ok") {
          if (llmApiKey.value === submittedDraft.api_key) llmApiKey.value = "";
          savedProviderValues = { ...submittedDraft, api_key: "" };
          llmTestResult.textContent = "Settings saved.";
          llmTestResult.style.color = "var(--iris-sage)";
          activity.clearError("AI Settings");
          await refreshLlmStatus();
        } else {
          llmTestResult.textContent = `Error: ${result.message || "could not save settings"}`;
          llmTestResult.style.color = "var(--studio-error)";
          activity.error("AI Settings", result.message || "Could not save settings.", "Open AI Settings and retry Save.");
        }
      } catch (err) {
        console.error(err);
        llmTestResult.textContent = "Error: could not save settings";
        llmTestResult.style.color = "var(--studio-error)";
        activity.error("AI Settings", "Could not save settings.", "Check the backend connection and retry Save.");
      } finally {
        providerBusy = false;
        updateProviderForm();
        llmSaveBtn.textContent = "Save";
        activity.finish("AI Settings", llmTestResult.textContent || "Ready");
      }
    });

    llmTestBtn.addEventListener("click", async () => {
      if (providerBusy || providerHasChanges()) {
        updateProviderForm();
        return;
      }
      providerBusy = true;
      updateProviderForm();
      llmTestBtn.textContent = "Testing...";
      llmTestResult.textContent = "Contacting the model...";
      llmTestResult.style.color = "var(--studio-text)";
      if (!autoGenerating && !exporting) activity.status("AI connection", "Testing…");
      try {
        const response = await fetch("/api/llm-test", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({})
        });
        const result = await response.json();
        if (response.ok && result.status === "ok") {
          llmTestResult.textContent = `Connected: ${result.backend} backend, model ${result.model}.`;
          llmTestResult.style.color = "var(--iris-sage)";
          activity.clearError("AI connection");
          await refreshLlmStatus();
        } else {
          llmTestResult.textContent = `Failed: ${result.message}`;
          llmTestResult.style.color = "var(--studio-error)";
          llmAvailable = false;
          setAutoGenerateEnabled(false);
          llmStatus.textContent = "AI scoring: connection failed";
          llmNotice.textContent = `${result.message} Check AI Settings and test the connection again.`;
          activity.error("AI connection", result.message || "Connection failed.", "Check AI Settings and test again.");
        }
      } catch (err) {
        console.error(err);
        llmTestResult.textContent = "Failed: could not reach the backend server.";
        llmTestResult.style.color = "var(--studio-error)";
        llmAvailable = false;
        setAutoGenerateEnabled(false);
        llmStatus.textContent = "AI scoring: connection failed";
        llmNotice.textContent = "Could not reach the backend server. Check the backend connection and test AI Settings again.";
        activity.error("AI connection", "Could not reach the backend server.", "Check AI Settings and test again.");
      } finally {
        providerBusy = false;
        updateProviderForm();
        llmTestBtn.textContent = "Test Connection";
        updateProviderSummary();
        activity.finish("AI connection", llmTestResult.textContent || "Ready");
      }
    });

    updateScoringSummary();
    setAutoGenerateEnabled(false);
    refreshLlmStatus();

    function updateClipContext(segment: Segment) {
      document.getElementById("activeSegmentContext")!.textContent = `Segment ${segment.id}`;
      document.getElementById("clipContextHeading")!.textContent = `Reviewing segment ${segment.id}`;
      document.getElementById("clipContextText")!.textContent = segment.text;
      document.getElementById("clipContextTiming")!.textContent =
        `${formatTime(segment.start)} – ${formatTime(segment.end)} on recording · ${segment.duration.toFixed(1)}s clip`;
    }
  
    function markReviewed(segment: Segment | undefined) {
      currentSegmentId = segment?.id ?? null;
      if (segment) updateClipContext(segment);
      else {
        document.getElementById('activeSegmentContext')!.textContent = 'Original recording';
        document.getElementById('clipContextHeading')!.textContent = 'Reviewing source footage';
        document.getElementById('clipContextText')!.textContent = segments.length ? 'No transcript segment at this position.' : 'This recording has no transcript segments.';
        document.getElementById('clipContextTiming')!.textContent = 'Seek anywhere in the original recording using the source timeline.';
      }
      segmentsList.querySelectorAll<HTMLElement>('.segment-row').forEach(row => {
        const active = Number(row.dataset.id) === currentSegmentId;
        row.classList.toggle('active', active);
        const review = row.querySelector('.segment-review')!;
        if (active) review.setAttribute('aria-current', 'true');
        else review.removeAttribute('aria-current');
      });
      timeline.select(selectedIds, currentSegmentId);
    }
    const timeline = setupSourceTimeline(mainVideo, source, segments, time => {
      // Keep an explicitly reviewed segment while inside an overlap; otherwise
      // choose the first covering interval. Gaps have no active transcript row.
      const covers = (segment: Segment) => time >= segment.start && time < segment.end;
      const segment = segments.find(s => s.id === currentSegmentId && covers(s)) || segments.find(covers);
      if ((segment?.id ?? null) !== currentSegmentId) markReviewed(segment);
    });
    const disposeCurrentShell = disposeShell;
    disposeShell = () => { timeline.dispose(); disposeCurrentShell(); };
    if (projects.active) {
      const disposeEditor = mountTranscriptEditor({
        container: document.getElementById('transcriptEditorTools')!,
        projectId: projects.active.id, sourceUrl: source?.url || null, segments,
        get state() { return projects.active?.state || {}; },
        onChange: patch => projects.update(patch),
        onBusy: busy => projectBusy('transcript-editor', busy),
        onOpen: () => mainVideo.pause(),
      });
      const disposeWorkspace = disposeShell;
      disposeShell = () => { disposeEditor(); disposeWorkspace(); };
    }
    markReviewed(undefined);
  
    if (projects.enabled) uploadNewBtn.textContent = 'New / Open Project';
    uploadNewBtn.addEventListener("click", () => {
      if (autoGenerating || exporting) return;
      if (projects.enabled) { openProjectPicker(); return; }
      if (confirm("Upload a new video? Current progress will be lost.")) {
        // Clear the current view and show upload interface
        showUploadInterface();
      }
    });
  
    function updateVideoForSegment(segmentId: number) {
      const segment = segments.find(s => s.id === segmentId);
      if (segment) {
        currentSegmentId = segmentId;
        markReviewed(segment);
        timeline.seek(segment.start);
        const row = segmentsList.querySelector<HTMLElement>(`.segment-row[data-id="${segment.id}"]`);
        row?.scrollIntoView({ block: "nearest" });
      }
    }
  
    function updateStats() {
      selectedCountEl.textContent = selectedIds.size.toString();
      document.getElementById("headerSelectedCount")!.textContent = `${selectedIds.size} selected`;
      const totalDur = segments
        .filter(s => selectedIds.has(s.id))
        .reduce((a, s) => a + s.duration, 0);
      selectedDurationEl.textContent = totalDur.toFixed(1) + "s";
      updateFilterSummary();
      
      exportBtn.disabled = selectedIds.size === 0 || exporting;
      timeline.select(selectedIds, currentSegmentId);

    }
  
    function updateFilterSummary() {
      const filtering = (document.getElementById("showOnlyKeywords") as HTMLInputElement).checked;
      const hiddenSelected = segments.filter(segment => !segment.is_keyword && selectedIds.has(segment.id)).length;
      document.getElementById("filterSummary")!.textContent = filtering
        ? `${segments.filter(segment => segment.is_keyword).length} shown · ${hiddenSelected} selected hidden` : "";
    }

    function renderSegments(filterKeywords = false) {
      const focused = document.activeElement as HTMLElement;
      const focusedRow = focused.closest<HTMLElement>(".segment-row");
      const focusClass = ["segment-review", "segment-checkbox", "play-btn"].find(name => focused.classList.contains(name));
      const scrollTop = segmentsList.scrollTop;
      segmentsList.innerHTML = "";
      segments.forEach(seg => {
        if (filterKeywords && !seg.is_keyword) return;
  
        const div = document.createElement("div");
        div.dataset.id = String(seg.id);
        div.className = "segment-row" + 
          (seg.is_keyword ? " keyword" : "") +
          (selectedIds.has(seg.id) ? " selected" : "") +
          (currentSegmentId === seg.id ? " active" : "");
        
        div.innerHTML = `
          <button class="segment-review" data-id="${seg.id}" ${currentSegmentId === seg.id ? 'aria-current="true"' : ''} aria-label="Review segment ${seg.id}, ${formatTime(seg.start)} to ${formatTime(seg.end)}">
            <span class="segment-id">${seg.id}</span>
            <span class="segment-time">${formatTime(seg.start)} – ${formatTime(seg.end)}</span>
          </button>
          <div class="segment-text"></div>
          <div class="segment-info">
            ${seg.is_keyword ? '<span class="segment-score">Keyword</span>' : ''}
            <span>Duration: ${seg.duration.toFixed(1)}s</span>
          </div>
          <div class="segment-actions">
            <label class="segment-selection"><input type="checkbox" class="segment-checkbox" data-id="${seg.id}" aria-label="Select segment ${seg.id}" ${selectedIds.has(seg.id) ? "checked" : ""}></label>
            <button class="icon-btn play-btn" data-id="${seg.id}" aria-label="Play segment ${seg.id}">Play</button>
          </div>
        `;
        div.querySelector(".segment-text")!.textContent = seg.text;
        
        const checkbox = div.querySelector(".segment-checkbox") as HTMLInputElement;
        checkbox.addEventListener("change", (e) => {
          e.stopPropagation();
          if (checkbox.checked) selectedIds.add(seg.id);
          else selectedIds.delete(seg.id);
          div.classList.toggle("selected", checkbox.checked);
          updateStats();
          saveEditorState();
        });
  
        const playBtn = div.querySelector(".play-btn") as HTMLButtonElement;
        playBtn.addEventListener("click", (e) => {
          e.stopPropagation();
          updateVideoForSegment(seg.id);
          if (mainVideo.src) void mainVideo.play().catch(() => {});
        });
  
        div.addEventListener("click", (e) => {
          if (!(e.target as HTMLElement).closest(".segment-selection, .play-btn")) {
            updateVideoForSegment(seg.id);
          }
        });
  
        segmentsList.appendChild(div);
      });
      segmentsList.scrollTop = scrollTop;
      if (focusedRow && focusClass) {
        const replacement = segmentsList.querySelector<HTMLElement>(`.${focusClass}[data-id="${focusedRow.dataset.id}"]`);
        (replacement || document.getElementById("showOnlyKeywords")!).focus({ preventScroll: true });
      }
      updateFilterSummary();
    }
  
    renderSegments(keywordFilter.checked);
    updateStats();
  
    document.getElementById("selectAll")!.addEventListener("click", () => {
      segments.forEach(s => selectedIds.add(s.id));
      renderSegments((document.getElementById("showOnlyKeywords") as HTMLInputElement).checked);
      updateStats();
      saveEditorState();
    });
  
    document.getElementById("selectNone")!.addEventListener("click", () => {
      selectedIds.clear();
      renderSegments((document.getElementById("showOnlyKeywords") as HTMLInputElement).checked);
      updateStats();
      saveEditorState();
    });
  
    document.getElementById("selectKeywords")!.addEventListener("click", () => {
      selectedIds.clear();
      segments.filter(s => s.is_keyword).forEach(s => selectedIds.add(s.id));
      renderSegments((document.getElementById("showOnlyKeywords") as HTMLInputElement).checked);
      updateStats();
      saveEditorState();
    });
  
    document.getElementById("showOnlyKeywords")!.addEventListener("change", (e) => {
      renderSegments((e.target as HTMLInputElement).checked);
      saveEditorState();
    });
  
    advancedToggle.addEventListener("click", () => {
      setAdvanced(!advancedSection.classList.contains("visible"));
    });
  
    minScoreSlider.addEventListener("input", () => { updateScoringSummary(); saveEditorState(); });
    instructionsInput.addEventListener("input", () => { updateScoringSummary(); saveEditorState(); });
  
    resetSettingsBtn.addEventListener("click", () => {
      minScoreSlider.value = "7";
      updateScoringSummary();
      saveEditorState();
    });
  
    previewBtn.addEventListener("click", async () => {
      if (previewBtn.disabled) return;
      const selection = readScoringSelection();
      projectBusy("statistics", true);
      previewBtn.disabled = true;
      previewBtn.style.opacity = "0.5";
      previewBtn.textContent = "Loading...";
      if (!autoGenerating && !exporting) activity.status("Selection Statistics", "Loading score-filter estimate…");
      
      try {
        const response = await fetch(projects.url("/api/preview-auto-export"), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(selection)
        });
        const result = await response.json();
        
        const current = readScoringSelection();
        if (current.min_score !== selection.min_score || current.instructions !== selection.instructions) return;

        if (response.ok && result.status === "ok") {
          activity.clearError("Selection Statistics");
          const duration = Math.round(result.estimated_duration_seconds);
          previewInfo.replaceChildren(
            document.createTextNode(`Selection Statistics: ${result.count} highlights (of ${result.total_available} available) · Score-filter estimate`),
            document.createElement("br"),
            document.createTextNode(`⏱️ Duration: ${duration}s (${Math.floor(duration/60)}:${(duration%60).toString().padStart(2,'0')})`),
            document.createElement("br"),
            document.createTextNode(`🎯 Threshold: ${selection.min_score}+`)
          );
          previewInfo.classList.add("visible");
        } else {
          previewInfo.classList.remove("visible");
          activity.error("Selection Statistics", result.message || "Could not load statistics.", "Generate scores first if needed, then retry Selection Statistics.");
        }
      } catch (err) {
        console.error(err);
        previewInfo.classList.remove("visible");
        activity.error("Selection Statistics", "Failed to load selection statistics.", "Retry Selection Statistics when the backend is ready.");
      } finally {
        projectBusy("statistics", false);
        previewBtn.disabled = false;
        previewBtn.style.opacity = "1";
        previewBtn.textContent = "Selection Statistics";
        activity.finish("Selection Statistics", "Ready");
      }
    });
  
    autoGenerateBtn.addEventListener("click", async () => {
      if (autoGenerating) return;
      if (!llmAvailable) {
        shell.openContext();
        activity.error("Auto-generation", llmNotice.textContent || "AI is unavailable.", "Check AI Settings before auto-generating.");
        return;
      }
      const selection = readScoringSelection();
      const rescore = (document.getElementById("rescoreCheck") as HTMLInputElement).checked;

      if (!selection.instructions) {
        shell.openContext();
        activity.error("Auto-generation", "Please describe what counts as a highlight before auto-generating.", "Describe your moments above and retry Auto-Generate.");
        instructionsInput.setAttribute("aria-invalid", "true");
        instructionsInput.focus();
        return;
      }

      projectBusy("auto", true);
      autoGenerating = true;
      uploadNewBtn.disabled = true;
      autoGenerateBtn.disabled = true;
      autoGenerateBtn.textContent = "Starting...";
      if (!exporting) activity.status("Auto-generation", "Starting…");

      try {
        const response = await fetch(projects.url("/api/auto-generate"), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            ...selection,
            sort_chronological: true,
            rescore: rescore
          })
        });
        const result = await response.json();

        if (!response.ok || result.status !== "ok" || !result.task_id) {
          activity.error("Auto-generation", result.message || "Failed to start auto-generation", "Check AI Settings or wait for processing, then retry Generate.");
          return;
        }

        if (autoDownloadRow.contains(document.activeElement)) document.getElementById("activityToggle")!.focus();
        autoDownloadRow.classList.remove("visible");
        activity.trackTask("Auto-generation", result.task_id);
        activity.status("Auto-generation", "Processing…");
        // Poll the task: AI scoring can take a minute or two
        const deadline = Date.now() + 15 * 60 * 1000;
        while (Date.now() < deadline || activity.isCancelling("Auto-generation")) {
          await new Promise((r) => setTimeout(r, 3000));
          let status;
          try {
            const statusResponse = await fetch(projects.url(`/api/processing-status/${encodeURIComponent(result.task_id)}`));
            if (!statusResponse.ok) throw new Error('Could not check auto-generation status.');
            status = await statusResponse.json();
          } catch (error) {
            if (!activity.isCancelling('Auto-generation')) throw error;
            activity.error('Auto-generation status', 'Could not confirm that the worker has stopped.', 'Waiting for the backend connection before allowing another generation.');
            continue;
          }
          activity.clearError('Auto-generation status');
          if (status.status === 'cancelled') {
            activity.clearError('Auto-generation');
            activity.finish('Auto-generation', 'Cancelled. Your selected clips are unchanged.');
            return;
          }
          if (status.status === "complete") {
            autoDownloadLink.href = status.output_file;
            autoDownloadLink.target = "_blank";
            autoDownloadRow.classList.add("visible");
            autoDownloadLink.textContent = `Download Auto-Generated Reel (${status.highlights_used} clips, ${status.file_size_mb} MB)`;

            const filterMsg = ` (score >= ${selection.min_score})`;
            activity.clearError("Auto-generation");
            activity.finish("Auto-generation", `Ready! ${status.highlights_used} of ${status.total_available} highlights${filterMsg}.`);
            activity.results();
            return;
          }
          if (status.status === "error") {
            activity.error("Auto-generation", status.message || "Generation failed.", "Check AI Settings and retry Generate.");
            return;
          }
          autoGenerateBtn.textContent = status.message || "Working...";
          activity.status("Auto-generation", status.message || "Working…", status.progress);
        }
        activity.error("Auto-generation", "Timed out waiting for the highlight reel.", "Check the app logs before retrying Generate.");
      } catch (err) {
        console.error(err);
        activity.error("Auto-generation", "Failed to auto-generate highlight reel.", "Check the backend connection and retry Generate.");
      } finally {
        activity.endTask("Auto-generation");
        projectBusy("auto", false);
        autoGenerating = false;
        uploadNewBtn.disabled = exporting;
        setAutoGenerateEnabled(llmAvailable);
        autoGenerateBtn.textContent = "Auto-Generate Highlight Reel";
        activity.finish("Auto-generation", "Ready to retry");
      }
    });
  
    discardAutoBtn.addEventListener("click", async () => {
      if (discardAutoBtn.disabled) return;
      if (!confirm("Discard this auto-generated reel? You can then try a new prompt or pick clips manually.")) return;

      const href = autoDownloadLink.getAttribute("href");
      projectBusy("discard", true);
      discardAutoBtn.disabled = true;
      try {
        if (href) {
          const response = await fetch(projects.url("/api/discard-reel"), {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ output_file: href })
          });
          const result = await response.json();
          if (!response.ok || result.status !== "ok") {
            activity.error("Discard", result.message || "Failed to discard reel", "Wait for processing, then retry Discard.");
            return;
          }
        }
        if (autoDownloadRow.contains(document.activeElement)) document.getElementById("activityToggle")!.focus();
        autoDownloadRow.classList.remove("visible");
        autoDownloadLink.removeAttribute("href");
        activity.clearError("Discard");
      } catch (err) {
        console.error("Failed to delete reel on server:", err);
        activity.error("Discard", "Failed to discard reel.", "Check the backend connection and retry Discard.");
      } finally {
        projectBusy("discard", false);
        discardAutoBtn.disabled = false;
      }
    });

    exportBtn.addEventListener("click", async () => {
      if (exporting || selectedIds.size === 0) return;
      projectBusy("export", true);
      exporting = true;
      uploadNewBtn.disabled = true;
      const payload = { selected_ids: Array.from(selectedIds) };
      exportBtn.disabled = true;
      exportBtn.textContent = "Generating...";
      if (!autoGenerating) activity.status("Manual export", "Generating highlight reel…");
      try {
        const response = await fetch(projects.url("/api/export"), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload)
        });
        const result = await response.json();
        if (result.status === "ok") {
          downloadLink.href = result.output_file;
          downloadLink.target = "_blank";
          downloadLink.classList.add("visible");
          downloadLink.textContent = "Download Reel from Selected Clips";
          activity.clearError("Manual export");
          activity.finish("Manual export", "Highlight reel ready");
          activity.results();
        } else {
          activity.error("Manual export", result.message || "Export failed.", "Wait for processing, then retry Create Reel from Selected Clips.");
        }
      } catch (err) {
        console.error(err);
        activity.error("Manual export", "Failed to send selected segments.", "Check the backend connection and retry Create Reel from Selected Clips.");
      } finally {
        projectBusy("export", false);
        exporting = false;
        uploadNewBtn.disabled = autoGenerating;
        updateStats();
        exportBtn.textContent = "Create Reel from Selected Clips";
        activity.finish("Manual export", "Ready to retry");
      }
    });
  }
  
  async function start() {
    try {
      await projects.discover();
      await main();
    } catch (error) {
      document.getElementById('app')!.innerHTML = shellMarkup('<main class="upload-box"><h2>Projects unavailable</h2><p id="projectLoadError"></p><button id="retryProjectLoad">Retry loading projects</button></main>');
      setupShell();
      document.getElementById('projectLoadError')!.textContent = error instanceof Error ? error.message : 'Could not connect to the project store.';
      document.getElementById('retryProjectLoad')!.addEventListener('click', () => { void start(); });
    }
  }
  void start();
