## Summary

<!-- What does this PR change, and why? -->

## How was this tested?

<!-- Dev-server run? Pipeline run on a real video? For packaging changes, a
     packaged-app check (verify-app-flow.py) is required — paste its output. -->

## Checklist

- [ ] Pipeline prints are ASCII-only (no emoji — Windows cp1252 pipes kill the child process)
- [ ] Status-parser strings untouched, **or** the matchers in `process_video_async`
      (`web_interface.py`) were updated to match
- [ ] Runtime deps added to **both** `requirements.txt` and `requirements-backend.txt`
      (if any were added)
- [ ] Packaging changes verified on the packaged app with `verify-app-flow.py`
