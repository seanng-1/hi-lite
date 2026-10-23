# Feature Plan implementation

Source request: [issue #1](https://github.com/seanng-1/hi-lite/issues/1), created 23 October 2026.
Target branch: `app-reliability`.
Starting commit: `2904e91006d4ade44a37852ad8abd7db404cf9a7` (`UI refresh`).
Implementation is kept on local branch `feature/roadmap-local`; nothing has been pushed, merged remotely, or deployed.

## Implemented scope

| Phase | Delivered functionality | Main coverage |
|---|---|---|
| M1: persistent projects | Named project folders; create/open/recent navigation; saved selections, edits, captions and settings; serialized debounced autosave; atomic revision-checked writes; reopening without transcription; non-destructive legacy adoption | Project store/API tests, state-machine tests, DOM interaction tests |
| M1.5: desktop originals (optional) | Native Electron chooser; no-copy original linking; SHA-256-verified relinking after moving a source; token-protected main-process request and sandboxed preload | Source ownership/API tests, Electron bridge/trust-boundary tests |
| M2: non-destructive media | Metadata-only imports; project-scoped original playback; common on-demand source-range exporter; request/job isolation; atomic exports; persisted job results and interrupted-import recovery | Real FFmpeg/API tests, identity/cache smoke tests, admission regressions |
| M3: transcript editor | Passage keep/remove, range split/trim/combine/reorder, undo/redo, empty-sequence recovery, source-sequence preview and saved edit exports | Immutable edit-model tests and DOM/API interactions |
| M4a: captions | Edited caption text; cut/reorder remapping; SRT/VTT downloads; styled burned captions and preview overlay | Subtitle/remap tests and real captioned video exports |
| M4b: creator exports | Original/9:16/16:9/1:1 formats; horizontal/vertical focal crop; format-aware caption sizing; individual edited groups or reels; reopening saved export links | Geometry tests, real aspect-ratio exports, DOM settings/export tests |
| M5: clip generation | Requested count, duration and category; full-transcript bounded AI batches; reviewable editable candidates; clearly labeled offline heuristic mode; exact identity and partial-coverage validation | Planner/scoring/cache tests and real subprocess smoke with a stub provider |
| M5.5: cancellation (optional) | Cancel import, scoring/candidate and editor rendering jobs; poll until terminal; subprocess-tree cleanup; no new operation before admission releases | Job/API/process tests, cancellation UI regressions |
| M6: cleanup | Reviewable pause-shortening and aligned filler-word suggestions; explicit acceptance; undo | Cleanup model tests and DOM acceptance/undo tests |

## Architecture and compatibility

- Existing recordings are adopted in place, not copied or moved. Existing source/transcript validity markers are preserved.
- Completed projects cannot be overwritten by importing another video. Failed or interrupted first imports can be retried only when no completed original was published.
- Project state saves use optimistic revisions. A conflicting window cannot silently overwrite a newer save.
- Browser media routes never accept arbitrary filesystem paths. Desktop linking is only available with the per-launch capability retained by Electron main.
- Exports preserve requested source-time cut order, gaps and repeats. Adjacent identical-framing ranges are coalesced to avoid rendering thousands of unnecessary intermediates.
- Rendering uses exact cut-duration metadata, temporary lossless audio, and a final continuous encode to prevent per-cut timing drift. Frame discretization remains bounded by output frame resolution.
- Existing clip-based workspaces and legacy API routes remain supported. `--render-segments` is an opt-in CLI compatibility mode.
- New libraries are test-only: jsdom is used for deterministic DOM interaction tests, not shipped in the frontend runtime.

## Verification and remaining validation

See the accompanying delivery report for exact final commands and counts.

The automated checks include Python unit/integration tests; real FFmpeg video/audio rendering, aspect/crop/caption exports and pixel-transition timing; API contracts; scoring subprocess/cache smoke tests with a stub provider; strict TypeScript compilation; production frontend build; project-state tests; DOM interactions; and Electron main/preload bridge tests.

These are **not** substitutes for the remaining manual/platform checks:

1. The real Chromium browser suite could not start in this execution environment because its socket creation was denied. A supported cloud browser was available but could not reach the separate workspace's localhost server. Actual browser layout, native video playback and focus behavior need confirmation on a supported machine.
2. The native file chooser GUI and Windows packaged installer/end-to-end flow were not run. Electron TypeScript and isolated bridge security tests passed.
3. Speech recognition was stubbed with deterministic transcripts during integration tests. No Whisper model download or live transcription was run.
4. No live AI provider was called. Network-provider tests use deterministic fixtures; the user's configured provider still needs its normal connection test.

## Operating limits and tradeoffs

- Browser source playback depends on the source's codecs. H.264/AAC MP4 offers the broadest compatibility; FFmpeg can export other supported files even when a browser cannot preview them.
- Keep linked original files available and unchanged. Relinking requires an unchanged copy, not replacement footage.
- Rendering requires temporary disk space for selected footage and lossless audio. Final encoding favors accurate cut/caption timing over stream-copy speed.
- Edit history is available while the editor remains mounted; saved edit results persist across app restarts. Undo history itself is not persisted.
- Project JSON is limited to 16 MiB. The renderer accepts up to 100,000 logical source ranges, 50,000 source captions, and bounded remapped-caption expansion. Excessive requests fail with an explicit validation error.
- AI and heuristic candidates may be fewer than the requested count when the recording lacks enough distinct passages. Suggestions do not alter the edit until explicitly accepted.

## Reopening the delivered repository

The Git bundle contains complete repository history, the unchanged local `app-reliability` base, and the implementation branch. Clone it without network access:

```sh
git clone -b feature/roadmap-local hi-lite-app-reliability-full-history.bundle hi-lite
cd hi-lite
git log --oneline origin/app-reliability..HEAD
```

The source ZIP contains the final tracked source tree and this report, without dependencies, runtime media, credentials or generated exports. It does not contain Git history; use the bundle for that.

Remote publication remains deliberately deferred.
