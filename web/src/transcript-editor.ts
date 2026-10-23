// SPDX-License-Identifier: GPL-3.0-or-later
import './transcript-editor.css';

export interface EditRange { id: string; clip_id: number; start: number; end: number; group_id: string; }
export interface EditDocument { version: 1; source_duration: number; ranges: EditRange[]; }
interface SourceSegment { id: number; start: number; end: number; text: string; }
interface Caption { id: string | number; start: number; end: number; text: string; }
interface Word { word: string; start: number; end: number; }
interface TranscriptCue extends Caption { words: Word[]; }
interface Candidate {
  id: string; title: string; reason: string; text: string; start: number; end: number;
  duration: number; scoring_source: string; ranges: { start: number; end: number }[];
}
interface Suggestion { id: string; type: string; start: number; end: number; text: string; reason: string; }
interface Settings {
  aspect_ratio: string; crop: { x: number; y: number }; burn_captions: boolean;
  caption_style: { font_family: string; font_size: number; font_color: string; outline_color: string; outline_width: number; position: string };
}
interface Snapshot { document: EditDocument; captions: Caption[]; settings: Settings; candidates: Candidate[]; }
export interface TranscriptEditorOptions {
  container: HTMLElement; projectId: string; sourceUrl: string | null; segments: SourceSegment[];
  state: Record<string, unknown>; onChange: (patch: Record<string, unknown>) => void;
  onBusy?: (busy: boolean) => void;
  onOpen?: () => void;
}
const record = (value: unknown): value is Record<string, unknown> => typeof value === 'object' && value !== null && !Array.isArray(value);
const finite = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value);
const clone = <T>(value: T): T => JSON.parse(JSON.stringify(value)) as T;
const time = (value: number) => `${Math.floor(Math.max(0, value) / 60)}:${(Math.max(0, value) % 60).toFixed(2).padStart(5, '0')}`;
const total = (ranges: { start: number; end: number }[]) => ranges.reduce((sum, range) => sum + range.end - range.start, 0);

/** Validate snapshots before rendering untrusted saved/API state. */
export function readEditDocument(value: unknown): EditDocument {
  if (!record(value) || value.version !== 1 || !finite(value.source_duration) || value.source_duration < 0 || !Array.isArray(value.ranges)) throw new Error('The edit document could not be read.');
  const ids = new Set<string>();
  const duration = value.source_duration;
  const ranges = value.ranges.map(raw => {
    if (!record(raw) || typeof raw.id !== 'string' || !raw.id || ids.has(raw.id) ||
      !Number.isSafeInteger(raw.clip_id) || (raw.clip_id as number) < 0 || !finite(raw.start) || !finite(raw.end) ||
      raw.start < 0 || raw.end <= raw.start || raw.end > duration || typeof raw.group_id !== 'string' || !raw.group_id) throw new Error('The edit contains an invalid source range.');
    ids.add(raw.id);
    return { id: raw.id, clip_id: raw.clip_id as number, start: raw.start, end: raw.end, group_id: raw.group_id };
  });
  return { version: 1, source_duration: duration, ranges };
}

/** Resolve an edited timeline position without assuming chronological source order. */
export function editedPosition(ranges: { start: number; end: number }[], position: number): { index: number; sourceTime: number } | null {
  if (!ranges.length) return null;
  let remaining = Math.max(0, Number.isFinite(position) ? position : 0);
  for (let index = 0; index < ranges.length; index++) {
    const range = ranges[index];
    const length = range.end - range.start;
    if (remaining < length || index === ranges.length - 1) return { index, sourceTime: range.start + Math.min(remaining, length) };
    remaining -= length;
  }
  return null;
}

/** Intersect candidates with authoritative manifest boundaries, never equate transcript and clip IDs. */
export function candidateDocument(candidates: Pick<Candidate, 'id' | 'ranges'>[], segments: SourceSegment[], duration: number): EditDocument {
  const ranges: EditRange[] = [];
  const ordered = [...segments].sort((a, b) => a.start - b.start || a.end - b.end);
  candidates.forEach((candidate, candidateIndex) => candidate.ranges.forEach(interval => {
    let coveredUntil = interval.start;
    for (const segment of ordered) {
      const start = Math.max(interval.start, segment.start, coveredUntil);
      const end = Math.min(interval.end, segment.end);
      if (end <= start) continue;
      ranges.push({ id: `candidate-${candidateIndex}-${ranges.length}`, clip_id: segment.id,
        start, end, group_id: `candidate-${candidateIndex}-${candidate.id}`.slice(0, 256) });
      coveredUntil = end;
    }
  }));
  if (!ranges.length) throw new Error('These candidates do not overlap the source clips. Generate candidates again for this recording.');
  return readEditDocument({ version: 1, source_duration: duration, ranges });
}

function readCaptions(value: unknown): Caption[] {
  if (!Array.isArray(value)) throw new Error('Caption data could not be read.');
  return value.map((raw, index) => {
    if (!record(raw) || !finite(raw.start) || !finite(raw.end) || raw.start < 0 || raw.end <= raw.start || typeof raw.text !== 'string') throw new Error('A caption has invalid timing or text.');
    return { id: typeof raw.id === 'string' || typeof raw.id === 'number' ? raw.id : `caption-${index}`, start: raw.start, end: raw.end, text: raw.text };
  });
}
function readCandidates(value: unknown): Candidate[] {
  if (!Array.isArray(value)) return [];
  return value.filter(record).flatMap((raw, index) => {
    if (!finite(raw.start) || !finite(raw.end) || raw.end <= raw.start || !Array.isArray(raw.ranges)) return [];
    const ranges = raw.ranges.filter(record).filter(range => finite(range.start) && finite(range.end) && range.start >= 0 && range.end > range.start).map(range => ({ start: range.start as number, end: range.end as number }));
    if (!ranges.length) return [];
    return [{ id: String(raw.id ?? `candidate-${index}`), title: String(raw.title ?? `Candidate ${index + 1}`), reason: String(raw.reason ?? ''), text: String(raw.text ?? ''), start: raw.start, end: raw.end, duration: total(ranges), scoring_source: String(raw.scoring_source ?? 'local_heuristic'), ranges }];
  });
}
function readSettings(value: unknown): Settings {
  const raw = record(value) ? value : {};
  const crop = record(raw.crop) ? raw.crop : {};
  const style = record(raw.caption_style) ? raw.caption_style : {};
  const number = (value: unknown, fallback: number, min: number, max: number) => finite(value) ? Math.min(max, Math.max(min, value)) : fallback;
  const color = (value: unknown, fallback: string) => typeof value === 'string' && /^#[a-f\d]{6}$/i.test(value) ? value : fallback;
  return { aspect_ratio: ['original', '9:16', '16:9', '1:1'].includes(String(raw.aspect_ratio)) ? String(raw.aspect_ratio) : 'original',
    crop: { x: number(crop.x, 0.5, 0, 1), y: number(crop.y, 0.5, 0, 1) }, burn_captions: raw.burn_captions === true,
    caption_style: { font_family: typeof style.font_family === 'string' && /^[A-Za-z0-9 _-]{1,64}$/.test(style.font_family) ? style.font_family : 'Arial',
      font_size: number(style.font_size, 42, 8, 120), font_color: color(style.font_color, '#ffffff'), outline_color: color(style.outline_color, '#000000'),
      outline_width: number(style.outline_width, 2, 0, 10), position: ['top', 'center', 'bottom'].includes(String(style.position)) ? String(style.position) : 'bottom' } };
}

export function mountTranscriptEditor(options: TranscriptEditorOptions): () => void {
  const { container, projectId } = options;
  const prefix = `/api/projects/${encodeURIComponent(projectId)}`;
  const controller = new AbortController();
  const { signal } = controller;
  let disposed = false, loaded = false, busy = false, activeTask = '', loading = false;
  let editDocument: EditDocument = { version: 1, source_duration: 0, ranges: [] };
  let captions: Caption[] = [], transcript: TranscriptCue[] = [], candidates = readCandidates(options.state.candidates);
  let suggestions: Suggestion[] = [], settings = readSettings(options.state.export_settings);
  const selected = new Set<string>(), selectedCandidates = new Set<string>();
  const undo: Snapshot[] = [], redo: Snapshot[] = [];
  let previewRanges: { start: number; end: number }[] = [], previewIndex = 0, animation = 0;
  let passageAnchor: { start: number; end: number } | null = null;
  const tracked: { node: HTMLButtonElement | HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement; enabled: () => boolean }[] = [];
  const objectUrls = new Set<string>();

  function el<K extends keyof HTMLElementTagNameMap>(tag: K, className = '', text = ''): HTMLElementTagNameMap[K] {
    const node = document.createElement(tag);
    node.className = className;
    if (text) node.textContent = text;
    return node;
  }
  function listen(node: EventTarget, event: string, callback: EventListener) { node.addEventListener(event, callback, { signal }); }
  function button(text: string, action: () => void | Promise<void>, enabled: () => boolean = () => true, className = '') {
    const node = el('button', className, text);
    node.type = 'button';
    listen(node, 'click', () => { if (!node.disabled) void action(); });
    tracked.push({ node, enabled });
    return node;
  }
  function field(label: string, node: HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement, help = '') {
    const wrapper = el('label', 'te-field');
    wrapper.append(el('span', '', label), node);
    if (help) wrapper.append(el('small', 'te-muted', help));
    return wrapper;
  }
  function input(value: string, type = 'number', min = '0', max?: string, step = '0.01') {
    const node = el('input'); node.type = type; node.value = value;
    if (type === 'number' || type === 'range') { node.min = min; node.step = step; if (max) node.max = max; }
    return node;
  }
  function select(values: [string, string][]) {
    const node = el('select');
    values.forEach(([value, label]) => { const option = el('option', '', label); option.value = value; node.append(option); });
    return node;
  }
  function guard<T extends HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement>(node: T, enabled = () => true) { tracked.push({ node, enabled }); return node; }
  function status(message: string, error = false) {
    if (disposed) return;
    statusNode.textContent = message; statusNode.classList.toggle('te-error', error);
    launchStatus.textContent = busy ? message : loaded ? `${editDocument.ranges.length} ranges · ${time(total(editDocument.ranges))}` : 'Non-destructive editing';
  }
  function refreshControls() {
    tracked.forEach(({ node, enabled }) => { node.disabled = !loaded || busy || !enabled(); });
    undoButton.disabled = !loaded || busy || !undo.length;
    redoButton.disabled = !loaded || busy || !redo.length;
    closeButton.disabled = false; launch.disabled = false;
    cancelButton.hidden = !activeTask; cancelButton.disabled = !activeTask;
    retryButton.hidden = loaded || loading;
    retryButton.disabled = loading;
    dialog.setAttribute('aria-busy', String(busy));
  }
  function setBusy(value: boolean) { busy = value; options.onBusy?.(value); refreshControls(); }
  async function response(path: string, body?: unknown): Promise<Response> {
    const result = await fetch(prefix + '/api' + path, { signal, cache: 'no-store', ...(body === undefined ? {} : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }) });
    if (!result.ok) {
      let message = `Request failed (HTTP ${result.status}).`;
      try { const data: unknown = await result.json(); if (record(data) && typeof data.message === 'string') message = data.message; } catch { /* Keep HTTP status for non-JSON errors. */ }
      throw Object.assign(new Error(message), { status: result.status });
    }
    return result;
  }
  async function json(path: string, body?: unknown): Promise<Record<string, unknown>> {
    const data: unknown = await (await response(path, body)).json();
    if (!record(data)) throw new Error('The server returned an unreadable response.');
    return data;
  }
  async function run(action: () => Promise<void>) {
    if (busy || disposed) return;
    flushCaptionDrafts(); setBusy(true);
    try { await action(); } catch (error) { if (!disposed) status(error instanceof Error ? error.message : 'Something went wrong. Your existing edit is unchanged.', true); }
    finally { if (!disposed) setBusy(false); }
  }
  const snapshot = (): Snapshot => clone({ document: editDocument, captions, settings, candidates });
  function save() { if (!disposed) options.onChange({ edit_document: clone(editDocument), edited_captions: clone(captions), export_settings: clone(settings), candidates: clone(candidates) }); }
  function remember() { undo.push(snapshot()); if (undo.length > 100) undo.shift(); redo.length = 0; }
  function restore(value: Snapshot) { editDocument = value.document; captions = value.captions; settings = value.settings; candidates = value.candidates; selected.clear(); pause(); save(); render(); }
  function commit(document: EditDocument, message: string) { remember(); editDocument = document; selected.clear(); pause(); save(); render(); status(message); }
  async function apply(action: Record<string, unknown>, message: string) {
    await run(async () => { const data = await json('/edit-preview', { document: editDocument, action }); if (disposed) return; commit(readEditDocument(data.document), message); });
  }
  function pause() { video.pause(); window.cancelAnimationFrame(animation); animation = 0; }
  function play(ranges: { start: number; end: number }[], position = 0) {
    if (!options.sourceUrl || !ranges.length) { status('Original source playback is unavailable.', true); return; }
    previewRanges = clone(ranges);
    const point = editedPosition(previewRanges, position)!;
    previewIndex = point.index; video.currentTime = point.sourceTime;
    void video.play().catch(error => status(`Preview could not start: ${error.message}`, true));
  }
  function syncPlayback() {
    window.cancelAnimationFrame(animation); animation = 0;
    const range = previewRanges[previewIndex];
    if (range && !video.paused && video.currentTime >= range.end - 0.015) {
      if (previewIndex + 1 < previewRanges.length) { previewIndex++; video.currentTime = previewRanges[previewIndex].start; }
      else { video.pause(); video.currentTime = range.end; }
    }
    const current = previewRanges[previewIndex];
    const elapsed = current ? total(previewRanges.slice(0, previewIndex)) + Math.max(0, Math.min(current.end - current.start, video.currentTime - current.start)) : 0;
    seek.max = String(Math.max(0.001, total(previewRanges))); seek.value = String(elapsed);
    clock.textContent = `Edit ${time(elapsed)} / ${time(total(previewRanges))} · Source ${time(video.currentTime)}`;
    captionPreview.textContent = captions.filter(cue => cue.start <= video.currentTime && cue.end > video.currentTime).map(cue => cue.text).filter(Boolean).join('\n');
    captionPreview.hidden = !settings.burn_captions || !captionPreview.textContent;
    if (!video.paused && !disposed) animation = window.requestAnimationFrame(syncPlayback);
  }

  const launcher = el('div', 'te-launcher');
  const launch = button('Open transcript editor', () => { options.onOpen?.(); dialog.showModal(); if (!loaded) void load(); }, () => true, 'te-open');
  const launchStatus = el('span', 'te-muted', 'Trim, captions, crop and exports');
  launcher.append(launch, launchStatus);
  const dialog = el('dialog', 'te-dialog'); dialog.setAttribute('aria-label', 'Transcript editor');
  const header = el('header', 'te-header');
  const heading = el('div'); heading.append(el('h2', '', 'Transcript editor'), el('p', 'te-muted', 'Edit the sequence, not the original recording. Changes save to this project.'));
  const headerActions = el('div', 'te-actions');
  const undoButton = button('Undo', () => { if (!undo.length) return; redo.push(snapshot()); restore(undo.pop()!); status('Last change undone.'); });
  const redoButton = button('Redo', () => { if (!redo.length) return; undo.push(snapshot()); restore(redo.pop()!); status('Change restored.'); });
  const closeButton = button('Close editor', () => dialog.close());
  headerActions.append(undoButton, redoButton, closeButton); header.append(heading, headerActions);
  const statusBar = el('div', 'te-status-bar');
  const statusNode = el('p', 'te-status', 'Open the editor to load your source transcript.'); statusNode.setAttribute('role', 'status'); statusNode.setAttribute('aria-live', 'polite');
  const retryButton = button('Retry loading', () => load()); retryButton.hidden = true;
  const cancelButton = button('Cancel task', async () => {
    if (!activeTask) return;
    cancelButton.disabled = true;
    try { await json(`/cancel/${encodeURIComponent(activeTask)}`, {}); status('Cancellation requested. Waiting for the task to stop…'); }
    catch (error) { status(error instanceof Error ? error.message : 'Cancellation failed.', true); cancelButton.disabled = false; }
  }); cancelButton.hidden = true;
  statusBar.append(statusNode, retryButton, cancelButton);
  const body = el('div', 'te-body');
  const preview = el('aside', 'te-preview'); preview.setAttribute('aria-label', 'Edited video preview and export settings');
  const videoFrame = el('div', 'te-video-frame');
  const video = el('video'); video.preload = 'metadata'; video.playsInline = true; video.setAttribute('aria-label', 'Edited source preview');
  const captionPreview = el('div', 'te-caption-preview'); captionPreview.hidden = true; videoFrame.append(video, captionPreview);
  const previewActions = el('div', 'te-actions');
  previewActions.append(button('Play edit', () => play(editDocument.ranges), () => !!options.sourceUrl && !!editDocument.ranges.length, 'primary'), button('Pause', pause));
  const seek = input('0', 'range', '0', '1', '0.01'); seek.setAttribute('aria-label', 'Seek edited sequence');
  const clock = el('output', 'te-muted', 'Edit 0:00.00 · Source 0:00.00');
  preview.append(videoFrame, previewActions, seek, clock, el('p', 'te-muted', 'Preview follows the cut order. Crop and caption positioning are approximate until rendered.'));
  listen(video, 'play', () => { window.cancelAnimationFrame(animation); syncPlayback(); });
  listen(video, 'pause', () => { window.cancelAnimationFrame(animation); animation = 0; });
  listen(video, 'ended', () => {
    if (previewIndex + 1 < previewRanges.length) { previewIndex++; video.currentTime = previewRanges[previewIndex].start; void video.play().catch(error => status(`Preview could not continue: ${error.message}`, true)); }
    else syncPlayback();
  });
  listen(video, 'seeked', () => { if (video.paused) syncPlayback(); });
  listen(video, 'error', () => status('The source video could not load. Editing remains available; check that the recording still exists.', true));
  listen(seek, 'input', () => { const point = editedPosition(previewRanges, Number(seek.value)); if (point) { previewIndex = point.index; video.currentTime = point.sourceTime; } });

  const settingsBox = el('details', 'te-settings'); settingsBox.open = true; settingsBox.append(el('summary', '', 'Framing and captions'));
  const settingsFields = el('div', 'te-form-grid');
  const aspect = guard(select([['original', 'Original'], ['9:16', 'Portrait 9:16'], ['16:9', 'Landscape 16:9'], ['1:1', 'Square 1:1']]));
  const cropX = guard(input('0.5', 'range', '0', '1', '0.01')), cropY = guard(input('0.5', 'range', '0', '1', '0.01'));
  const burn = guard(input('', 'checkbox'));
  const fontSize = guard(input('42', 'number', '8', '120', '1'));
  const fontColor = guard(input('#ffffff', 'color')), outlineColor = guard(input('#000000', 'color'));
  const outlineWidth = guard(input('2', 'number', '0', '10', '0.5'));
  const fontFamily = guard(select([['Arial', 'Arial'], ['Verdana', 'Verdana'], ['DejaVu Sans', 'DejaVu Sans'], ['Helvetica', 'Helvetica']]));
  const captionPosition = guard(select([['bottom', 'Bottom'], ['center', 'Center'], ['top', 'Top']]));
  settingsFields.append(field('Aspect ratio', aspect), field('Burn captions into video', burn), field('Crop horizontal focus', cropX, 'Left ← → Right'), field('Crop vertical focus', cropY, 'Top ← → Bottom'),
    field('Caption font', fontFamily), field('Font size', fontSize), field('Text color', fontColor), field('Outline color', outlineColor), field('Outline width', outlineWidth), field('Caption position', captionPosition));
  settingsBox.append(settingsFields); preview.append(settingsBox);
  function readControls(): Settings { return readSettings({ aspect_ratio: aspect.value, crop: { x: Number(cropX.value), y: Number(cropY.value) }, burn_captions: burn.checked,
    caption_style: { font_family: fontFamily.value, font_size: Number(fontSize.value), font_color: fontColor.value, outline_color: outlineColor.value, outline_width: Number(outlineWidth.value), position: captionPosition.value } }); }
  [aspect, cropX, cropY, burn, fontSize, fontColor, outlineColor, outlineWidth, fontFamily, captionPosition].forEach(node => listen(node, 'change', () => {
    if (!loaded || busy) return;
    const next = readControls(); if (JSON.stringify(next) === JSON.stringify(settings)) return;
    remember(); settings = next; save(); renderSettings(); refreshControls(); status('Export settings updated.');
  }));
  [cropX, cropY].forEach(node => listen(node, 'input', () => { video.style.objectPosition = `${Number(cropX.value) * 100}% ${Number(cropY.value) * 100}%`; }));
  const exportActions = el('div', 'te-export-actions');
  exportActions.append(button('Export edited reel', () => renderExport(false), () => !!editDocument.ranges.length, 'primary'), button('Export individual clips', () => renderExport(true), () => !!editDocument.ranges.length),
    button('Download edited SRT', () => subtitle('srt'), () => !!editDocument.ranges.length && !!captions.length), button('Download edited VTT', () => subtitle('vtt'), () => !!editDocument.ranges.length && !!captions.length));
  const downloads = el('div', 'te-downloads'); downloads.setAttribute('aria-label', 'Completed exports');
  preview.append(exportActions, downloads);

  const workspace = el('section', 'te-workspace');
  const tabs = el('div', 'te-tabs'); tabs.setAttribute('role', 'tablist'); tabs.setAttribute('aria-label', 'Editor tools');
  const panels = new Map<string, HTMLElement>(); const tabButtons = new Map<string, HTMLButtonElement>();
  const tabId = `te-${Math.random().toString(36).slice(2)}`;
  function activate(name: string) { panels.forEach((panel, key) => panel.hidden = key !== name); tabButtons.forEach((tab, key) => { tab.setAttribute('aria-selected', String(key === name)); tab.tabIndex = key === name ? 0 : -1; }); }
  ['Sequence', 'Transcript', 'Captions', 'Candidates', 'Cleanup'].forEach((name, index) => {
    const tab = el('button', '', name); tab.type = 'button'; tab.setAttribute('role', 'tab'); tab.id = `${tabId}-tab-${index}`;
    const panel = el('section', 'te-tool-panel'); panel.id = `${tabId}-panel-${index}`; panel.setAttribute('role', 'tabpanel'); panel.setAttribute('aria-labelledby', tab.id);
    tab.setAttribute('aria-controls', panel.id); listen(tab, 'click', () => activate(name));
    listen(tab, 'keydown', (event) => { const key = (event as KeyboardEvent).key; const names = [...panels.keys()]; let next = index;
      if (key === 'ArrowRight') next = (index + 1) % names.length; else if (key === 'ArrowLeft') next = (index + names.length - 1) % names.length; else if (key === 'Home') next = 0; else if (key === 'End') next = names.length - 1; else return;
      event.preventDefault(); activate(names[next]); tabButtons.get(names[next])!.focus(); });
    tabButtons.set(name, tab); panels.set(name, panel); tabs.append(tab);
  });
  workspace.append(tabs, ...panels.values()); activate('Sequence');
  body.append(preview, workspace);
  dialog.append(header, statusBar, body); container.append(launcher, dialog);
  listen(dialog, 'close', () => { pause(); if (loaded && !busy) flushCaptionDrafts(); });
  listen(dialog, 'keydown', event => {
    const key = event as KeyboardEvent;
    if (!(key.ctrlKey || key.metaKey) || key.key.toLowerCase() !== 'z' || (key.target instanceof HTMLInputElement || key.target instanceof HTMLTextAreaElement)) return;
    key.preventDefault(); (key.shiftKey ? redoButton : undoButton).click();
  });

  const sequencePanel = panels.get('Sequence')!;
  const sequenceSummary = el('p', 'te-muted');
  const sequenceActions = el('div', 'te-actions');
  sequenceActions.append(button('Select all', () => { editDocument.ranges.forEach(range => selected.add(range.id)); renderRanges(); refreshControls(); }),
    button('Clear selection', () => { selected.clear(); renderRanges(); refreshControls(); }),
    button('Remove selected', () => apply({ type: 'remove', range_ids: [...selected] }, 'Selected ranges removed. Undo is available.'), () => !!selected.size),
    button('Keep only selected', () => apply({ type: 'keep', range_ids: [...selected] }, 'Only the selected ranges remain.'), () => !!selected.size),
    button('Combine selected', () => apply({ type: 'combine', range_ids: editDocument.ranges.filter(range => selected.has(range.id)).map(range => range.id) }, 'Consecutive ranges combined into one export clip.'), () => selected.size > 1),
    button('Restore all source clips', () => {
      commit(readEditDocument({ version: 1, source_duration: editDocument.source_duration, ranges: options.segments.map((segment, index) => ({ id: `source-range-${index}`, clip_id: segment.id,
        start: segment.start, end: segment.end, group_id: `source-group-${index}` })) }), 'All source clips restored. Caption corrections and export settings are unchanged. Undo is available.');
    }, () => !!options.segments.length));
  const rangeList = el('div', 'te-list'); rangeList.setAttribute('aria-label', 'Ordered source ranges');
  sequencePanel.append(sequenceSummary, sequenceActions, el('p', 'te-muted', 'Select consecutive ranges to combine them. Combining preserves cuts and groups them into one individual export.'), rangeList);
  const transcriptPanel = panels.get('Transcript')!;
  const passageControls = el('div', 'te-passage-controls');
  const passageStart = guard(input('0')), passageEnd = guard(input('0'));
  const passageValid = () => Number(passageStart.value) >= 0 && Number(passageEnd.value) > Number(passageStart.value) && Number(passageEnd.value) <= editDocument.source_duration;
  const passageAction = (type: string) => apply({ type, start: Number(passageStart.value), end: Number(passageEnd.value) }, type === 'remove' ? 'Passage removed everywhere it occurs in the edit.' : 'The edit now keeps only this passage.');
  passageControls.append(field('Source start (seconds)', passageStart), field('Source end (seconds)', passageEnd), button('Preview passage', () => play([{ start: Number(passageStart.value), end: Number(passageEnd.value) }]), passageValid),
    button('Remove passage', () => passageAction('remove'), passageValid), button('Keep only passage', () => passageAction('keep'), passageValid));
  [passageStart, passageEnd].forEach(node => listen(node, 'input', refreshControls));
  const transcriptList = el('div', 'te-list');
  transcriptPanel.append(el('p', 'te-muted', 'Click a sentence or aligned word to select a source passage. Shift-click extends it. Removing cuts the video; source text is never overwritten. Previously removed footage is restored with Undo, not Keep.'), passageControls, transcriptList);
  const captionsPanel = panels.get('Captions')!;
  const captionList = el('div', 'te-list');
  captionsPanel.append(el('p', 'te-muted', 'Correct caption text without changing the source transcript. Empty text hides a cue. Captions are automatically cut and retimed to the final sequence on export.'), captionList);

  const candidatesPanel = panels.get('Candidates')!;
  const candidateForm = el('div', 'te-form-grid');
  const candidateCount = guard(input('3', 'number', '1', '20', '1')), candidateDuration = guard(input('30', 'number', '1', '600', '1'));
  const candidateType = guard(select(['highlights', 'tutorial', 'educational', 'funny', 'story', 'quote', 'hook', 'summary'].map(value => [value, value[0].toUpperCase() + value.slice(1)] as [string, string])));
  const useAi = guard(input('', 'checkbox')); const instructions = guard(el('textarea')); instructions.rows = 2; instructions.maxLength = 10000;
  candidateForm.append(field('Desired clips', candidateCount), field('Target seconds per clip', candidateDuration), field('Clip type', candidateType), field('Use configured AI provider', useAi), field('Optional AI instructions', instructions));
  const candidateActions = el('div', 'te-actions');
  candidateActions.append(button('Find candidates', generateCandidates), button('Use selected candidates', () => {
    try { const chosen = candidates.filter(candidate => selectedCandidates.has(candidate.id)); commit(candidateDocument(chosen, options.segments, editDocument.source_duration), 'Selected candidates replaced the edit. Review, refine, or Undo.'); activate('Sequence'); }
    catch (error) { status(error instanceof Error ? error.message : 'Could not use candidates.', true); }
  }, () => !!selectedCandidates.size));
  const candidateList = el('div', 'te-list');
  candidatesPanel.append(el('p', 'te-muted', 'Generate suggestions first, then choose what belongs in your edit. Using selected candidates replaces the sequence and can be undone. AI is optional; local suggestions do not contact a provider.'), candidateForm, candidateActions, candidateList);
  const cleanupPanel = panels.get('Cleanup')!;
  const cleanupForm = el('div', 'te-form-grid');
  const minPause = guard(input('1', 'number', '0.1', undefined, '0.1')), keepPause = guard(input('0.3', 'number', '0', undefined, '0.1'));
  const fillers = guard(input('um, uh, erm, hmm, ah', 'text'));
  cleanupForm.append(field('Find pauses of at least (seconds)', minPause), field('Retain pause (seconds)', keepPause), field('Filler words, comma-separated', fillers));
  const cleanupList = el('div', 'te-list');
  cleanupPanel.append(el('p', 'te-muted', 'Suggestions do not change your edit until accepted. Filler cuts require aligned word timestamps. Preview the context before accepting; Undo restores the last change.'), cleanupForm,
    button('Review cleanup suggestions', () => run(async () => {
      const result = await json('/cleanup', { min_pause: Number(minPause.value), keep_pause: Number(keepPause.value), fillers: fillers.value.split(',').map(value => value.trim()).filter(Boolean) });
      if (disposed) return;
      suggestions = Array.isArray(result.suggestions) ? result.suggestions.filter(record).filter(raw => finite(raw.start) && finite(raw.end) && raw.end > raw.start).map(raw => ({ id: String(raw.id), type: String(raw.type), start: raw.start as number, end: raw.end as number, text: String(raw.text ?? ''), reason: String(raw.reason ?? '') })) : [];
      renderCleanup(); status(suggestions.length ? `${suggestions.length} cleanup suggestions ready for review.` : 'No cleanup suggestions found. Word-level filler cleanup requires word timestamps.');
    })), cleanupList);

  function dropDetachedControls() { for (let index = tracked.length - 1; index >= 0; index--) if (!container.contains(tracked[index].node)) tracked.splice(index, 1); }
  function renderRanges() {
    rangeList.replaceChildren();
    sequenceSummary.textContent = `${editDocument.ranges.length} source ranges · ${time(total(editDocument.ranges))} edited duration · ${selected.size} selected`;
    if (!editDocument.ranges.length) rangeList.append(el('p', 'te-empty', 'The sequence is empty. Undo a removal, restore source clips, or choose new candidates.'));
    editDocument.ranges.forEach((range, index) => {
      const card = el('article', 'te-range'); card.dataset.rangeId = range.id;
      const check = input('', 'checkbox'); check.checked = selected.has(range.id); check.setAttribute('aria-label', `Select range ${index + 1}`); guard(check);
      listen(check, 'change', () => { check.checked ? selected.add(range.id) : selected.delete(range.id); sequenceSummary.textContent = `${editDocument.ranges.length} source ranges · ${time(total(editDocument.ranges))} edited duration · ${selected.size} selected`; refreshControls(); });
      const title = el('div', 'te-range-title');
      const grouped = index > 0 && editDocument.ranges[index - 1].group_id === range.group_id;
      title.append(check, el('strong', '', `${index + 1}. Clip ${range.clip_id}${grouped ? ' · Combined' : ''}`), el('span', 'te-muted', `${time(range.start)}–${time(range.end)}`));
      const source = options.segments.find(segment => segment.id === range.clip_id);
      const text = el('p', 'te-range-text', source?.text || 'Source passage');
      const actions = el('div', 'te-actions');
      actions.append(button('Preview', () => play([range]), () => !!options.sourceUrl), button('Move up', () => move(index, -1), () => index > 0), button('Move down', () => move(index, 1), () => index < editDocument.ranges.length - 1));
      const trim = el('details', 'te-trim'); trim.append(el('summary', '', 'Trim or split range'));
      const controls = el('div', 'te-form-grid'); const start = guard(input(String(range.start))), end = guard(input(String(range.end))), split = guard(input(String(Number(((range.start + range.end) / 2).toFixed(3)))));
      controls.append(field('Start (source seconds)', start), field('End (source seconds)', end), button('Apply trim', () => apply({ type: 'trim', range_id: range.id, start: Number(start.value), end: Number(end.value) }, 'Range trimmed. Undo restores the original boundaries.')),
        field('Split at (source seconds)', split), button('Use preview position', () => { split.value = String(Number(video.currentTime.toFixed(3))); }), button('Split range', () => apply({ type: 'split', range_id: range.id, at: Number(split.value) }, 'Range split into two editable clips.')));
      trim.append(controls); card.append(title, text, actions, trim); rangeList.append(card);
    });
    dropDetachedControls();
  }
  function move(index: number, delta: number) { const ids = editDocument.ranges.map(range => range.id); [ids[index], ids[index + delta]] = [ids[index + delta], ids[index]]; return apply({ type: 'reorder', range_ids: ids }, 'Sequence order updated.'); }
  function choosePassage(cue: { start: number; end: number }, extend: boolean) {
    if (!extend || !passageAnchor) passageAnchor = cue;
    passageStart.value = String(extend && passageAnchor ? Math.min(passageAnchor.start, cue.start) : cue.start);
    passageEnd.value = String(extend && passageAnchor ? Math.max(passageAnchor.end, cue.end) : cue.end);
    transcriptList.querySelectorAll<HTMLElement>('.te-word').forEach(node => node.classList.toggle('te-word-selected', Number(node.dataset.start) < Number(passageEnd.value) && Number(node.dataset.end) > Number(passageStart.value)));
    refreshControls();
  }
  function renderTranscript() {
    transcriptList.replaceChildren();
    if (!transcript.length) transcriptList.append(el('p', 'te-empty', 'No source transcript is available. Use the sequence controls to edit source ranges.'));
    transcript.forEach(cue => {
      const retained = editDocument.ranges.some(range => range.start < cue.end && range.end > cue.start);
      const card = el('article', `te-transcript-cue${retained ? '' : ' te-removed'}`);
      const sentence = button(`${time(cue.start)}–${time(cue.end)}${retained ? '' : ' · Removed'}`, () => {}, () => true, 'te-cue-time');
      listen(sentence, 'click', event => choosePassage(cue, (event as MouseEvent).shiftKey));
      const words = el('p', 'te-words');
      if (cue.words.length) cue.words.forEach(word => {
        const node = button(word.word, () => {}, () => true, 'te-word'); node.setAttribute('aria-label', `${word.word}, ${time(word.start)} to ${time(word.end)}`);
        node.dataset.start = String(word.start); node.dataset.end = String(word.end);
        node.classList.toggle('te-word-selected', word.start < Number(passageEnd.value) && word.end > Number(passageStart.value));
        listen(node, 'click', event => choosePassage(word, (event as MouseEvent).shiftKey)); words.append(node, document.createTextNode(' '));
      });
      else { const node = button(cue.text, () => {}, () => true, 'te-sentence'); listen(node, 'click', event => choosePassage(cue, (event as MouseEvent).shiftKey)); words.append(node); }
      card.append(sentence, words); transcriptList.append(card);
    });
    dropDetachedControls();
  }
  function flushCaptionDrafts() {
    if (!loaded || busy) return;
    const fields = [...captionList.querySelectorAll('textarea')];
    if (!fields.some((node, index) => captions[index] && node.value !== captions[index].text)) return;
    remember(); captions = captions.map((cue, index) => fields[index] ? { ...cue, text: fields[index].value } : cue); save(); refreshControls();
  }
  function renderCaptions() {
    captionList.replaceChildren();
    if (!captions.length) captionList.append(el('p', 'te-empty', 'No captions are available for this recording.'));
    captions.forEach((cue, index) => {
      const card = el('article', 'te-caption'); const textarea = guard(el('textarea')); textarea.value = cue.text; textarea.rows = 2; textarea.maxLength = 20000;
      const retained = editDocument.ranges.some(range => range.start < cue.end && range.end > cue.start);
      card.append(field(`${time(cue.start)}–${time(cue.end)}${retained ? '' : ' · Not in current edit'}`, textarea));
      listen(textarea, 'change', () => { if (busy || captions[index].text === textarea.value) return; remember(); captions[index] = { ...captions[index], text: textarea.value }; save(); refreshControls(); status('Caption correction saved to the project.'); syncPlayback(); });
      captionList.append(card);
    });
    dropDetachedControls();
  }
  function renderCandidates() {
    candidateList.replaceChildren();
    if (!candidates.length) candidateList.append(el('p', 'te-empty', 'No candidates yet. Choose a count, target length and clip type above.'));
    candidates.forEach(candidate => {
      const card = el('article', 'te-candidate'); const check = guard(input('', 'checkbox')); check.checked = selectedCandidates.has(candidate.id);
      check.setAttribute('aria-label', `Choose ${candidate.title}`);
      listen(check, 'change', () => { check.checked ? selectedCandidates.add(candidate.id) : selectedCandidates.delete(candidate.id); refreshControls(); });
      const title = el('label', 'te-range-title'); title.append(check, el('strong', '', candidate.title));
      card.append(title, el('p', 'te-muted', `${time(candidate.start)}–${time(candidate.end)} · ${time(candidate.duration)} selected · ${candidate.scoring_source === 'ai_scores' ? 'AI-assisted suggestion' : 'Local suggestion'}`),
        el('p', '', candidate.text), el('p', 'te-muted', candidate.reason), button('Preview candidate', () => play(candidate.ranges), () => !!options.sourceUrl));
      candidateList.append(card);
    });
    dropDetachedControls(); refreshControls();
  }
  function renderCleanup() {
    cleanupList.replaceChildren();
    suggestions.forEach(suggestion => {
      const retained = editDocument.ranges.some(range => range.start < suggestion.end && range.end > suggestion.start);
      const card = el('article', 'te-cleanup');
      card.append(el('strong', '', `${suggestion.type === 'silence' ? 'Shorten pause' : `Possible filler: ${suggestion.text}`} · ${time(suggestion.end - suggestion.start)}`),
        el('p', 'te-muted', `${time(suggestion.start)}–${time(suggestion.end)} · ${suggestion.reason}`));
      const actions = el('div', 'te-actions');
      actions.append(button('Preview context', () => play([{ start: Math.max(0, suggestion.start - 1), end: Math.min(editDocument.source_duration, suggestion.end + 1) }]), () => !!options.sourceUrl),
        button(retained ? 'Accept removal' : 'Already absent from edit', () => apply({ type: 'remove', start: suggestion.start, end: suggestion.end }, 'Cleanup applied. Use Undo to restore the passage.'), () => retained));
      card.append(actions); cleanupList.append(card);
    });
    dropDetachedControls(); refreshControls();
  }
  function renderSettings() {
    aspect.value = settings.aspect_ratio; cropX.value = String(settings.crop.x); cropY.value = String(settings.crop.y); burn.checked = settings.burn_captions;
    fontSize.value = String(settings.caption_style.font_size); fontColor.value = settings.caption_style.font_color; outlineColor.value = settings.caption_style.outline_color;
    if (![...fontFamily.options].some(option => option.value === settings.caption_style.font_family)) { const option = el('option', '', settings.caption_style.font_family); option.value = settings.caption_style.font_family; fontFamily.append(option); }
    outlineWidth.value = String(settings.caption_style.outline_width); fontFamily.value = settings.caption_style.font_family; captionPosition.value = settings.caption_style.position;
    videoFrame.style.aspectRatio = settings.aspect_ratio === 'original' ? (video.videoWidth && video.videoHeight ? `${video.videoWidth} / ${video.videoHeight}` : '16 / 9') : settings.aspect_ratio.replace(':', ' / ');
    const ratio = settings.aspect_ratio === 'original' ? (video.videoWidth / video.videoHeight || 16 / 9) : settings.aspect_ratio.split(':').map(Number).reduce((left, right) => left / right);
    videoFrame.style.width = `min(100%, calc(42vh * ${ratio}))`; videoFrame.style.alignSelf = 'center';
    video.style.objectFit = settings.aspect_ratio === 'original' ? 'contain' : 'cover'; video.style.objectPosition = `${settings.crop.x * 100}% ${settings.crop.y * 100}%`;
    captionPreview.style.fontFamily = settings.caption_style.font_family; captionPreview.style.fontSize = `${settings.caption_style.font_size / 1080 * 100}cqw`;
    captionPreview.style.color = settings.caption_style.font_color; captionPreview.style.webkitTextStroke = `${settings.caption_style.outline_width / 1080 * 100}cqw ${settings.caption_style.outline_color}`;
    captionPreview.dataset.position = settings.caption_style.position; syncPlayback();
  }
  function render() { renderRanges(); renderTranscript(); renderCaptions(); renderCandidates(); renderCleanup(); renderSettings(); refreshControls(); launchStatus.textContent = `${editDocument.ranges.length} ranges · ${time(total(editDocument.ranges))}`; }

  async function load() {
    if (loading || loaded || disposed) return;
    loading = true; setBusy(true); status('Loading the source transcript and saved edit…');
    try {
      const data = await json('/editor'); if (disposed) return;
      editDocument = readEditDocument(options.state.edit_document ?? data.document);
      captions = readCaptions(options.state.edited_captions ?? data.captions ?? []);
      const rawTranscript = Array.isArray(data.transcript) ? data.transcript : record(data.transcript) && Array.isArray(data.transcript.segments) ? data.transcript.segments : [];
      transcript = rawTranscript.filter(record).map((raw, index) => {
        const cue = readCaptions([{ ...raw, id: raw.id ?? index, start: raw.start ?? raw.start_time, end: raw.end ?? raw.end_time }])[0];
        const words = Array.isArray(raw.words) ? raw.words.filter(record).filter(word => typeof word.word === 'string' && finite(word.start) && finite(word.end) && word.end > word.start && word.start >= cue.start && word.end <= cue.end).map(word => ({ word: word.word as string, start: word.start as number, end: word.end as number })) : [];
        return { ...cue, words };
      });
      loaded = true; previewRanges = clone(editDocument.ranges); if (options.sourceUrl) video.src = options.sourceUrl;
      if (Array.isArray(data.exports)) data.exports.forEach((output, index) => { if (record(output)) outputLink(output.output_file, `Previous export ${index + 1}${finite(output.size) ? ` · ${(output.size / 1024 / 1024).toFixed(1)} MB` : ''}`); });
      render(); status('Ready. Source media and the original transcript stay unchanged.');
    } catch (error) { if (!disposed) status(error instanceof Error ? error.message : 'Could not load the editor. Retry when the server is ready.', true); }
    finally { loading = false; if (!disposed) setBusy(false); }
  }
  listen(video, 'loadedmetadata', renderSettings);
  async function wait(milliseconds: number) { await new Promise<void>((resolve, reject) => { const timer = window.setTimeout(() => { signal.removeEventListener('abort', abort); resolve(); }, milliseconds); const abort = () => { window.clearTimeout(timer); reject(new DOMException('Aborted', 'AbortError')); }; signal.addEventListener('abort', abort, { once: true }); }); }
  async function task(path: string, payload: unknown): Promise<Record<string, unknown>> {
    const started = await json(path, payload);
    if (typeof started.task_id !== 'string') return started;
    activeTask = started.task_id; refreshControls();
    let failures = 0;
    try {
      while (!disposed) {
        await wait(failures ? Math.min(10000, failures * 2000) : 1000);
        let data: Record<string, unknown>;
        try { data = await json(`/processing-status/${encodeURIComponent(activeTask)}`); failures = 0; }
        catch (error) { if (disposed || record(error) && (error.status === 404 || error.status === 403)) throw error; failures++; status('Task status temporarily unavailable. Reconnecting… The task may still be running.', true); continue; }
        if (['complete', 'completed', 'done'].includes(String(data.status))) return record(data.result) ? { ...data, ...data.result } : data;
        if (['cancelled', 'canceled'].includes(String(data.status))) throw new Error('Task cancelled. Your edit is unchanged.');
        if (data.status === 'error' || data.status === 'failed') throw new Error(String(data.message ?? 'The task failed. Your edit is unchanged.'));
        status(`${typeof data.message === 'string' ? data.message : 'Working…'}${finite(data.progress) ? ` (${Math.round(data.progress)}%)` : ''}`);
      }
      throw new DOMException('Aborted', 'AbortError');
    } finally { activeTask = ''; if (!disposed) refreshControls(); }
  }
  async function generateCandidates() {
    await run(async () => {
      const count = Number(candidateCount.value), duration = Number(candidateDuration.value);
      if (!Number.isInteger(count) || count < 1 || count > 20 || !finite(duration) || duration <= 0 || duration > 600) throw new Error('Choose 1–20 clips and a target duration of 1–600 seconds.');
      if (useAi.checked && !instructions.value.trim()) throw new Error('Describe what makes a good clip in AI instructions before using your provider.');
      status(useAi.checked ? 'Finding candidates with your configured AI provider…' : 'Finding local candidates across the source…');
      const result = await task('/candidates', { desired_count: count, target_duration: duration, clip_type: candidateType.value, instructions: instructions.value.trim(), use_ai: useAi.checked });
      if (disposed) return;
      remember(); candidates = readCandidates(result.candidates); selectedCandidates.clear(); save(); renderCandidates();
      status(`${candidates.length} candidates ready. Review before replacing the edit${candidates.length < count ? '; the source may not contain enough distinct passages for the requested count' : ''}.`);
    });
  }
  function outputLink(path: unknown, label: string) {
    if (typeof path !== 'string') return false;
    let url: URL; try { url = new URL(path, window.location.origin); } catch { return false; }
    if (url.origin !== window.location.origin || !(url.pathname.startsWith(`${prefix}/final/`) || url.pathname.startsWith('/final/'))) return false;
    const link = el('a', 'download-link', label); link.href = url.href; link.download = ''; downloads.append(link); return true;
  }
  async function renderExport(individual: boolean) {
    await run(async () => {
      status(individual ? 'Rendering individual edited clips…' : 'Rendering the edited reel…');
      const result = await task('/render', { document: editDocument, captions, settings, individual }); if (disposed) return;
      let count = 0;
      if (Array.isArray(result.outputs)) result.outputs.forEach((output, index) => { if (outputLink(record(output) ? output.output_file ?? output.url : output, individual ? `Download clip ${index + 1}` : 'Download edited reel')) count++; });
      if (!count && outputLink(result.output_file, individual ? 'Download edited clip' : 'Download edited reel')) count++;
      if (!count) throw new Error('Rendering finished, but no valid download link was returned. Your edit is still saved.');
      status(`${count} export${count === 1 ? '' : 's'} ready to download. Files reflect the edit and settings used when rendering started.`);
    });
  }
  async function subtitle(format: 'srt' | 'vtt') {
    await run(async () => {
      const result = await response('/edited-subtitles', { document: editDocument, captions, format });
      const content = await result.text(); if (disposed) return;
      if (!content.trim()) throw new Error('No caption text overlaps the edited sequence.');
      const url = URL.createObjectURL(new Blob([content], { type: format === 'vtt' ? 'text/vtt;charset=utf-8' : 'application/x-subrip;charset=utf-8' })); objectUrls.add(url);
      const link = el('a', 'download-link', `Download edited ${format.toUpperCase()}`); link.href = url; link.download = `edited-captions.${format}`; downloads.append(link); link.click();
      status(`Edited ${format.toUpperCase()} downloaded, with captions retimed to the sequence.`);
    });
  }
  refreshControls();
  return () => {
    disposed = true; controller.abort(); pause(); video.removeAttribute('src'); video.load();
    objectUrls.forEach(url => URL.revokeObjectURL(url)); if (dialog.open) dialog.close(); launcher.remove(); dialog.remove(); options.onBusy?.(false);
  };
}
