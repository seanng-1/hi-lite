"""
Local audio transcription using faster-whisper (no Docker / Whishper required).

Produces the same result schema as the Whishper API so the rest of the
pipeline (video_processor, highlight_analyzer, web UI) works unchanged:

    {
        "language": "en",
        "duration": 123.4,
        "text": "...",
        "segments": [
            {
                "id": 0, "start": 1.2, "end": 4.5, "text": "...", "score": 0.9,
                "words": [{"word": " The", "start": 1.2, "end": 1.4, "score": 0.98}, ...]
            },
            ...
        ]
    }

Models are downloaded once from Hugging Face into HI_LITE_MODELS_DIR
(defaults to <HI_LITE_DATA_DIR>/whisper_models) and reused afterwards,
so the packaged app works offline after the first transcription.
"""
import os


def get_models_dir():
    models_dir = os.getenv('HI_LITE_MODELS_DIR')
    if not models_dir:
        data_dir = os.getenv('HI_LITE_DATA_DIR', os.path.abspath('.'))
        models_dir = os.path.join(data_dir, 'whisper_models')
    os.makedirs(models_dir, exist_ok=True)
    return models_dir


def transcribe_audio(audio_path, model_size="small", device="cpu", language="auto", timeout_s=None):
    """
    Transcribe an audio file locally with word-level timestamps.
    Signature mirrors transcription_client.transcribe_audio; timeout_s is unused.
    Returns None on failure (matching the Whishper client's behaviour).
    """
    try:
        from faster_whisper import WhisperModel
    except ImportError as e:
        print(f"ERROR: faster-whisper is not installed: {e}")
        return None

    if not os.path.exists(audio_path):
        print(f"ERROR: Audio file not found: {audio_path}")
        return None

    models_dir = get_models_dir()
    print(f"Loading Whisper model '{model_size}' (device={device}, models dir: {models_dir})")

    try:
        model = WhisperModel(
            model_size,
            device=device,
            compute_type="int8" if device == "cpu" else "float16",
            download_root=models_dir,
        )
    except Exception as e:
        print(f"ERROR: Could not load Whisper model '{model_size}': {e}")
        return None

    kwargs = {"word_timestamps": True, "vad_filter": True}
    if language and language != "auto":
        kwargs["language"] = language

    print(f"Transcribing {audio_path} ...")
    try:
        segments_iter, info = model.transcribe(audio_path, **kwargs)

        segments = []
        for i, seg in enumerate(segments_iter):
            words = [
                {
                    "word": w.word,
                    "start": round(w.start, 3),
                    "end": round(w.end, 3),
                    "score": round(w.probability, 3),
                }
                for w in (seg.words or [])
            ]
            segments.append({
                "id": i,
                "start": round(seg.start, 3),
                "end": round(seg.end, 3),
                "text": seg.text,
                "score": round(getattr(seg, "avg_logprob", 0) or 0, 3),
                "words": words,
            })
            print(f"  [{seg.start:7.2f} - {seg.end:7.2f}] {seg.text.strip()}")

        return {
            "language": info.language,
            "duration": round(info.duration, 3),
            "text": "".join(s["text"] for s in segments),
            "segments": segments,
        }
    except Exception as e:
        print(f"ERROR: Transcription failed: {e}")
        import traceback
        traceback.print_exc()
        return None
