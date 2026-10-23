// SPDX-License-Identifier: GPL-3.0-or-later
// Project state is deliberately separate from global provider credentials.
export interface ProjectSummary {
  id: string;
  name: string;
  updated_at?: string;
  created_at?: string;
  has_media?: boolean;
}
export interface Project extends ProjectSummary {
  revision: number;
  state: Record<string, unknown>;
}
export interface Checkpoint { id: string; name: string; created_at: string; project_revision: number; }
function readCheckpoint(value: unknown): Checkpoint {
  if (!record(value) || typeof value.id !== 'string' || !/^[a-f0-9]{32}$/.test(value.id) ||
      typeof value.name !== 'string' || typeof value.created_at !== 'string' || !Number.isSafeInteger(value.project_revision)) throw new Error('The checkpoint could not be read.');
  return value as unknown as Checkpoint;
}
const activeKey = 'hi-lite.active-project';
const record = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value);
const isSummary = (value: unknown): value is ProjectSummary => record(value) &&
  typeof value.id === 'string' && /^[a-zA-Z0-9_-]+$/.test(value.id) && typeof value.name === 'string';
function readProject(value: unknown): Project {
  if (!isSummary(value) || !record(value) || !Number.isSafeInteger(value.revision) ||
      (value.revision as number) < 0 || !record(value.state)) throw new Error('The saved project could not be read.');
  return value as unknown as Project;
}
function rememberedProject(): string | null {
  try { return localStorage.getItem(activeKey); } catch { return null; }
}

export class Projects {
  enabled = false;
  active: Project | null = null;
  recent: ProjectSummary[] = [];
  status = '';
  error = '';
  conflict = false;
  onChange = () => {};
  private timer = 0;
  private changed = 0;
  private saved = 0;
  private saving: Promise<void> | null = null;
  get dirty() { return this.changed !== this.saved; }
  get prefix() { return this.active ? `/api/projects/${encodeURIComponent(this.active.id)}` : ''; }
  url(path: string) { return this.prefix + path; }

  async discover(): Promise<boolean> {
    const response = await fetch('/api/projects', { cache: 'no-store' });
    if (response.status === 404 || response.status === 405) return false;
    if (!response.ok) throw new Error(`Could not load projects (HTTP ${response.status}).`);
    // Old static dev servers return index.html for an unknown API route.
    let result: unknown;
    try { result = await response.json(); } catch { return false; }
    if (!record(result) || !Array.isArray(result.projects)) return false;
    this.enabled = true;
    this.recent = result.projects.filter(isSummary);
    const remembered = rememberedProject();
    const id = this.recent.find(project => project.id === remembered)?.id ||
      this.recent.find(project => project.id === result.active_project_id)?.id || this.recent[0]?.id;
    if (id) await this.open(id);
    return true;
  }

  async refresh() {
    const response = await fetch('/api/projects', { cache: 'no-store' });
    const result = await response.json();
    if (!response.ok || !record(result) || !Array.isArray(result.projects)) throw new Error('Could not refresh recent projects.');
    this.recent = result.projects.filter(isSummary);
    this.onChange();
  }

  async open(id: string, discardUnsaved = false) {
    if (!discardUnsaved) await this.flush();
    const response = await fetch(`/api/projects/${encodeURIComponent(id)}`, { cache: 'no-store' });
    if (!response.ok) throw new Error(`Could not open this project (HTTP ${response.status}).`);
    const project = readProject(await response.json());
    if (project.id !== id) throw new Error('The server returned a different project.');
    window.clearTimeout(this.timer);
    this.active = project;
    this.changed = this.saved = 0;
    this.conflict = false;
    this.error = '';
    this.status = 'All changes saved';
    try { localStorage.setItem(activeKey, id); } catch { /* Browser storage is optional. */ }
    this.onChange();
  }

  async create(name: string) {
    await this.flush();
    const response = await fetch('/api/projects', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name })
    });
    const result = await response.json();
    if (!response.ok || !isSummary(result)) throw new Error('Could not create the project. Your current project is unchanged.');
    // Read the authoritative revision even when creation returns only a summary.
    await this.open(result.id);
    this.recent = [result, ...this.recent.filter(project => project.id !== result.id)];
    this.onChange();
  }

  private async checkpointRequest(path: string, options: RequestInit = {}) {
    if (!this.active) throw new Error('Open a project first.');
    const active = this.active;
    const response = await fetch(this.prefix + '/checkpoints' + path, { cache: 'no-store', ...options });
    const result = await response.json();
    if (!response.ok) {
      const message = record(result) && typeof result.message === 'string' ? result.message : `Checkpoint request failed (HTTP ${response.status}).`;
      if (response.status === 409 && record(result) && result.conflict === true && this.active === active) {
        this.conflict = true; this.error = message; this.status = 'Project changed in another window'; this.onChange();
      }
      throw new Error(message);
    }
    return result;
  }

  async checkpoints(): Promise<Checkpoint[]> {
    const result = await this.checkpointRequest('');
    if (!record(result) || !Array.isArray(result.checkpoints)) throw new Error('Checkpoints could not be loaded.');
    return result.checkpoints.map(readCheckpoint);
  }

  async checkpoint(name: string): Promise<Checkpoint> {
    await this.flush();
    return readCheckpoint(await this.checkpointRequest('', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ revision: this.active?.revision, name })
    }));
  }

  async restoreCheckpoint(id: string) {
    await this.flush();
    const active = this.active, version = this.changed;
    const result = await this.checkpointRequest(`/${encodeURIComponent(id)}/restore`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ revision: active?.revision })
    });
    const restored = readProject(record(result) ? result.project : null);
    if (restored.id !== active?.id) throw new Error('The server returned a different project.');
    if (this.active !== active) throw new Error('The checkpoint was restored in the previous project. Your currently open project is unchanged.');
    if (this.changed !== version) {
      this.conflict = true;
      this.error = 'The checkpoint was restored on disk, but newer changes in this window were kept. Reload the saved version to use the restored checkpoint.';
      this.status = 'Changes not saved';
      this.onChange();
      throw new Error(this.error);
    }
    this.active = restored;
    this.changed = this.saved = 0;
    this.error = ''; this.conflict = false; this.status = 'Checkpoint restored';
    this.onChange();
  }

  async deleteCheckpoint(id: string) {
    await this.checkpointRequest(`/${encodeURIComponent(id)}`, { method: 'DELETE' });
  }

  update(patch: Record<string, unknown>) {
    if (!this.active) return;
    const state = { ...this.active.state, ...patch };
    if (JSON.stringify(state) === JSON.stringify(this.active.state)) return;
    this.active.state = state;
    this.changed++;
    this.status = this.error ? 'Changes not saved' : 'Unsaved changes';
    this.onChange();
    window.clearTimeout(this.timer);
    this.timer = window.setTimeout(() => { void this.flush().catch(() => {}); }, 500);
  }

  async flush(): Promise<void> {
    window.clearTimeout(this.timer);
    if (this.saving) {
      await this.saving;
      return this.flush();
    }
    if (!this.active || !this.dirty) return;
    if (this.conflict) throw new Error(this.error);
    const project = this.active;
    const version = this.changed;
    const state = JSON.parse(JSON.stringify(project.state)) as Record<string, unknown>;
    this.status = 'Saving…';
    this.onChange();
    const request = (async () => {
      try {
        const response = await fetch(`/api/projects/${encodeURIComponent(project.id)}`, {
          method: 'PUT', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ revision: project.revision, state })
        });
        if (response.status === 409) {
          this.conflict = true;
          throw new Error('This project changed in another window. Your changes are still here. Reload the saved version to continue, or keep this window open to preserve your work.');
        }
        if (!response.ok) throw new Error(`Could not save changes (HTTP ${response.status}). Retry before leaving this project.`);
        const result = readProject(await response.json());
        if (result.id !== project.id || result.revision <= project.revision) throw new Error('The server did not confirm the saved revision. Retry before leaving this project.');
        project.revision = result.revision;
        project.updated_at = result.updated_at;
        this.saved = version;
        this.error = '';
        this.status = this.dirty ? 'Unsaved changes' : 'All changes saved';
      } catch (error) {
        this.error = error instanceof Error ? error.message : 'Could not save changes. Retry before leaving this project.';
        this.status = 'Changes not saved';
        throw error;
      } finally { this.onChange(); }
    })();
    this.saving = request;
    try { await request; } finally { if (this.saving === request) this.saving = null; }
    if (this.dirty) await this.flush();
  }
}
