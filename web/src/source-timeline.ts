// SPDX-License-Identifier: GPL-3.0-or-later
export interface SourceReference { id: string; url: string; }
interface Interval { id: number; start: number; end: number; }

export function readSource(data: unknown): SourceReference | null {
  if (typeof data !== 'object' || !data) return null;
  const source = (data as { source?: SourceReference }).source;
  return source && /^[a-f0-9]{32}$/.test(source.id) && source.url === `/api/source/${source.id}` ? source : null;
}

export function formatSourceTime(seconds: number): string {
  const value = Math.max(0, Number.isFinite(seconds) ? seconds : 0);
  const hours = Math.floor(value / 3600);
  const minutes = Math.floor(value / 60) % 60;
  return `${hours ? `${hours}:` : ''}${hours ? String(minutes).padStart(2, '0') : minutes}:${String(Math.floor(value % 60)).padStart(2, '0')}`;
}

export const timelineMarkup = `<section class="source-timeline" id="sourceTimeline" aria-label="Original recording timeline">
  <div class="timeline-heading"><strong>Source timeline</strong><output id="sourceClock">— / —</output>
    <div class="timeline-legend"><span class="legend-segments">Transcript</span><span class="legend-selected">Selected</span><span class="legend-buffer">Buffered</span></div>
  </div>
  <div id="timelineStatus" class="field-help" role="status">Loading original source…</div>
  <div id="sourceScrubber" class="source-scrubber" role="slider" tabindex="0" aria-label="Seek original recording" aria-valuemin="0" aria-valuemax="0" aria-valuenow="0" aria-disabled="true" aria-describedby="timelineStatus">
    <div id="sourceRuler" class="source-ruler" aria-hidden="true"></div>
    <div id="timelineSegments" class="timeline-segments" aria-hidden="true"></div>
    <div id="timelineBuffer" class="timeline-buffer" aria-hidden="true"></div>
    <div id="sourcePlayhead" class="source-playhead" aria-hidden="true"></div>
  </div>
</section>`;

export function setupSourceTimeline(video: HTMLVideoElement, source: SourceReference | null,
    intervals: Interval[], onTime: (time: number) => void) {
  const track = document.getElementById('sourceScrubber')!;
  const ruler = document.getElementById('sourceRuler')!;
  const segmentLayer = document.getElementById('timelineSegments')!;
  const bufferLayer = document.getElementById('timelineBuffer')!;
  const playhead = document.getElementById('sourcePlayhead')!;
  const clock = document.getElementById('sourceClock')!;
  const status = document.getElementById('timelineStatus')!;
  const previewStatus = document.getElementById('sourcePreviewStatus')!;
  const sourceInfo = document.getElementById('sourceDurationInfo')!;
  const controller = new AbortController();
  const signal = controller.signal;
  let duration = 0;
  let pendingSeek: number | null = null;
  let drag: number | null = null;
  let resumeAfterDrag = false;
  let unavailable = !source;
  const markers = intervals.map(segment => {
    const node = document.createElement('span');
    node.className = 'timeline-segment';
    node.dataset.id = String(segment.id);
    node.title = `Segment ${segment.id}: ${formatSourceTime(segment.start)}–${formatSourceTime(segment.end)}`;
    segmentLayer.append(node);
    return { segment, node };
  });

  function fail(message: string) {
    unavailable = true;
    duration = 0;
    video.pause();
    if (drag !== null && track.hasPointerCapture(drag)) track.releasePointerCapture(drag);
    drag = null;
    track.setAttribute('aria-disabled', 'true');
    status.textContent = message;
    sourceInfo.textContent = message;
    previewStatus.textContent = message;
    previewStatus.hidden = false;
    clock.textContent = '— / —';
    ruler.replaceChildren();
    bufferLayer.replaceChildren();
    markers.forEach(({ node }) => node.hidden = true);
  }

  function drawRuler() {
    ruler.replaceChildren();
    const count = Math.max(2, Math.min(10, Math.floor(track.clientWidth / 100)));
    for (let index = 0; index <= count; index++) {
      const tick = document.createElement('span');
      tick.style.left = `${index / count * 100}%`;
      tick.textContent = formatSourceTime(duration * index / count);
      ruler.append(tick);
    }
  }

  function sync() {
    if (!duration || unavailable) return;
    const time = Math.max(0, Math.min(duration, video.currentTime));
    playhead.style.left = `${time / duration * 100}%`;
    clock.textContent = `${formatSourceTime(time)} / ${formatSourceTime(duration)}`;
    track.setAttribute('aria-valuenow', String(time));
    track.setAttribute('aria-valuetext', `${formatSourceTime(time)} of ${formatSourceTime(duration)}`);
    onTime(time);
  }

  function buffers() {
    bufferLayer.replaceChildren();
    if (!duration || unavailable) return;
    for (let index = 0; index < video.buffered.length; index++) {
      const start = Math.max(0, video.buffered.start(index));
      const end = Math.min(duration, video.buffered.end(index));
      if (end <= start) continue;
      const range = document.createElement('span');
      range.style.left = `${start / duration * 100}%`;
      range.style.width = `${(end - start) / duration * 100}%`;
      bufferLayer.append(range);
    }
  }

  function seek(time: number) {
    if (unavailable) return;
    if (!duration) { pendingSeek = time; return; }
    video.currentTime = Math.max(0, Math.min(duration, time));
    sync();
  }

  function metadata() {
    if (!source || video.error) return;
    if (!Number.isFinite(video.duration) || video.duration <= 0) {
      fail('This source has no seekable duration. Reimport a finite video recording for source preview.');
      return;
    }
    unavailable = false;
    duration = video.duration;
    track.setAttribute('aria-disabled', 'false');
    track.setAttribute('aria-valuemax', String(duration));
    status.textContent = 'Entire original recording · Drag to scrub or use arrow keys to seek.';
    sourceInfo.textContent = `Original recording · ${formatSourceTime(duration)} total duration`;
    previewStatus.hidden = true;
    drawRuler();
    markers.forEach(({ segment, node }) => {
      const start = Math.min(duration, segment.start), end = Math.min(duration, segment.end);
      node.hidden = end <= start;
      node.style.left = `${start / duration * 100}%`;
      node.style.width = `${(end - start) / duration * 100}%`;
    });
    if (pendingSeek !== null) { const time = pendingSeek; pendingSeek = null; seek(time); }
    sync();
    buffers();
  }

  const pointerSeek = (event: PointerEvent) => {
    const bounds = track.getBoundingClientRect();
    seek((event.clientX - bounds.left) / bounds.width * duration);
  };
  track.addEventListener('pointerdown', event => {
    if (event.button !== 0 || unavailable || !duration) return;
    event.preventDefault();
    track.focus();
    drag = event.pointerId;
    resumeAfterDrag = !video.paused;
    video.pause();
    track.setPointerCapture(drag);
    pointerSeek(event);
  });
  track.addEventListener('pointermove', event => { if (drag === event.pointerId) pointerSeek(event); });
  function finishDrag(event: PointerEvent) {
    if (drag !== event.pointerId) return;
    if (event.type === 'pointerup') pointerSeek(event);
    drag = null;
    if (track.hasPointerCapture(event.pointerId)) track.releasePointerCapture(event.pointerId);
    if (resumeAfterDrag && event.type === 'pointerup') void video.play().catch(() => {});
  }
  track.addEventListener('pointerup', finishDrag);
  track.addEventListener('pointercancel', finishDrag);
  track.addEventListener('lostpointercapture', () => { drag = null; });
  track.addEventListener('keydown', event => {
    if (unavailable || !duration) return;
    const step = event.shiftKey ? 10 : 1;
    const times: Record<string, number> = { ArrowLeft: video.currentTime - step, ArrowRight: video.currentTime + step,
      ArrowDown: video.currentTime - step, ArrowUp: video.currentTime + step,
      Home: 0, End: duration, PageDown: video.currentTime - 10, PageUp: video.currentTime + 10 };
    if (event.key in times) { event.preventDefault(); seek(times[event.key]); }
  });
  video.addEventListener('loadedmetadata', metadata, { signal });
  video.addEventListener('durationchange', metadata, { signal });
  ['timeupdate', 'seeking', 'seeked', 'ended'].forEach(event => video.addEventListener(event, sync, { signal }));
  ['progress', 'loadeddata', 'seeked'].forEach(event => video.addEventListener(event, buffers, { signal }));
  video.addEventListener('error', () => fail('Source preview unavailable: this file may use an unsupported browser codec or the source is no longer available. Reimport a browser-playable recording. Transcript selection and reel export remain available.'), { signal });
  const observer = new ResizeObserver(() => { if (duration) drawRuler(); });
  observer.observe(track);
  if (source) video.src = source.url;
  else fail('Original source unavailable. Reimport this recording to enable source preview.');

  return {
    seek,
    select(ids: Set<number>, active: number | null) {
      markers.forEach(({ segment, node }) => {
        node.classList.toggle('selected', ids.has(segment.id));
        node.classList.toggle('active', active === segment.id);
      });
    },
    dispose() { controller.abort(); observer.disconnect(); }
  };
}
