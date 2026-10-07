"""
Video processing functions for extracting audio and creating video segments.
"""
import os
import subprocess
import moviepy.config as mpc
from moviepy import VideoFileClip, concatenate_videoclips
import json
import tempfile
import shutil

# ffmpeg binaries (Electron ships them and points these env vars at them)
FFMPEG = os.getenv("FFMPEG_PATH", "ffmpeg")
FFPROBE = os.getenv("FFPROBE_PATH", "ffprobe")


def extract_audio(video_path, audio_output_path):
    """
    Extracts audio from a video file and saves it as a 16 kHz mono WAV
    (the format Whisper expects). Uses ffmpeg directly, falling back to
    MoviePy if ffmpeg is unavailable.
    """
    try:
        cmd = [
            FFMPEG, '-y', '-i', video_path,
            '-vn', '-acodec', 'pcm_s16le', '-ar', '16000', '-ac', '1',
            audio_output_path
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg failed: {result.stderr[-300:]}")
    except (FileNotFoundError, RuntimeError) as e:
        print(f"Direct ffmpeg extraction failed ({e}), falling back to MoviePy...")
        with VideoFileClip(video_path) as video:
            video.audio.write_audiofile(audio_output_path, codec='pcm_s16le')

    file_size = os.path.getsize(audio_output_path)
    print(f"Audio extracted to {audio_output_path} (Size: {file_size / (1024*1024):.2f} MB)")


def _speech_bounds(segment):
    """Return (start, end) of actual speech in a segment, preferring word-level timestamps."""
    if segment.get('words') and len(segment['words']) > 0:
        return segment['words'][0]['start'], segment['words'][-1]['end']
    return segment['start'], segment['end']


def get_smart_buffer(segment, prev_segment, next_segment, video_duration):
    """
    Applies buffer ONLY to the first and last word timestamps within a segment.
    This extends the video clip to capture build-up before first word and wind-down after last word.

    Adjacent clips never overlap: the silence gap between two segments is split at
    its midpoint, so this clip's post-buffer and the next clip's pre-buffer each get
    at most half the gap. Both sides compute the same midpoint, so the boundary is
    consistent across neighbouring clips.
    """
    pre_buffer = float(os.getenv('HI_LITE_PRE_BUFFER', 0.5))
    post_buffer = float(os.getenv('HI_LITE_POST_BUFFER', 0.75))

    speech_start, speech_end = _speech_bounds(segment)
    start_time = speech_start - pre_buffer
    end_time = speech_end + post_buffer

    if prev_segment:
        _, prev_speech_end = _speech_bounds(prev_segment)
        gap = speech_start - prev_speech_end
        # Never start before the midpoint of the gap; if segments touch or the
        # transcript overlaps, fall back to our own speech start (no pre-buffer).
        boundary = prev_speech_end + gap / 2 if gap > 0 else speech_start
        start_time = max(start_time, boundary)

    if next_segment:
        next_speech_start, _ = _speech_bounds(next_segment)
        gap = next_speech_start - speech_end
        boundary = speech_end + gap / 2 if gap > 0 else speech_end
        end_time = min(end_time, boundary)

    # Bounds checking
    start_time = max(0, start_time)
    end_time = min(end_time, video_duration) if video_duration else end_time

    return start_time, end_time


def extract_all_segments_alternative(video_path, transcription_data, output_folder):
    """
    Alternative segment extraction using direct FFmpeg calls to avoid MoviePy issues.
    This method is more reliable for batch processing.
    """
    import subprocess
    import json
    
    if not transcription_data or "segments" not in transcription_data:
        print("No transcription segments found.")
        return []

    segments_folder = os.path.join(output_folder, "segments")
    os.makedirs(segments_folder, exist_ok=True)

    segment_files = []
    total_segments = len(transcription_data["segments"])
    
    # Get video duration using ffprobe
    try:
        result = subprocess.run([
            FFPROBE, '-v', 'quiet', '-print_format', 'json', '-show_format', video_path
        ], capture_output=True, text=True, check=True)
        video_info = json.loads(result.stdout)
        video_duration = float(video_info['format']['duration'])
        print(f"Video duration: {video_duration:.2f}s")
    except Exception as e:
        print(f"Could not get video duration, using MoviePy fallback: {e}")
        with VideoFileClip(video_path) as video:
            video_duration = video.duration

    for i, segment in enumerate(transcription_data["segments"]):
        print(f"Processing segment {i+1}/{total_segments}")
        
        # Get neighboring segments for context
        prev_segment = transcription_data["segments"][i-1] if i > 0 else None
        next_segment = transcription_data["segments"][i+1] if i < len(transcription_data["segments"])-1 else None
        
        # Use enhanced timing with word-level + smart buffering
        start_time, end_time = get_smart_buffer(
            segment, 
            prev_segment, 
            next_segment, 
            video_duration
        )
        
        # Validate timing
        if start_time >= video_duration:
            print(f"  Skipping segment {i}: start time {start_time:.2f}s beyond video duration")
            continue
            
        if end_time > video_duration:
            end_time = video_duration
            print(f"  Adjusted end time for segment {i} to {end_time:.2f}s")
            
        if end_time <= start_time:
            print(f"  Skipping segment {i}: invalid time range {start_time:.2f}s - {end_time:.2f}s")
            continue

        text = segment.get("text", "").strip()
        if not text:
            print(f"  Skipping segment {i}: no text content")
            continue

        segment_filename = f"segment_{i:03d}.mp4"
        segment_path = os.path.join(segments_folder, segment_filename)
        duration = end_time - start_time

        print(f"  Extracting segment {i}: {start_time:.2f}s - {end_time:.2f}s ({duration:.2f}s duration)")

        try:
            # Use ffmpeg directly for more reliable extraction
            cmd = [
                FFMPEG, '-y',  # -y to overwrite existing files
                '-i', video_path,
                '-ss', str(start_time),
                '-t', str(duration),
                '-c:v', 'libx264',
                '-c:a', 'aac',
                '-preset', 'fast',
                '-crf', '23',
                '-movflags', '+faststart',
                '-avoid_negative_ts', 'make_zero',
                segment_path
            ]
            
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            
            if result.returncode == 0 and os.path.exists(segment_path) and os.path.getsize(segment_path) > 0:
                segment_files.append({
                    "id": i,
                    "transcript_id": segment.get("id"),
                    "filename": segment_filename,
                    "path": segment_path,
                    "start": start_time,
                    "end": end_time,
                    "duration": duration,
                    "text": text,
                    "score": segment.get("score", 0)
                })
                print(f"  + Successfully created segment {i}")
            else:
                print(f"  x Failed to create segment {i}")
                if result.stderr:
                    print(f"    FFmpeg error: {result.stderr[-200:]}")  # Last 200 chars of error
                    
        except subprocess.TimeoutExpired:
            print(f"  x Timeout extracting segment {i}")
        except Exception as e:
            print(f"  x Error extracting segment {i}: {str(e)}")

    print(f"Successfully extracted {len(segment_files)}/{total_segments} segments using FFmpeg method")
    return segment_files


def extract_all_segments(video_path, transcription_data, output_folder):
    """
    Extract all transcription segments as individual video files.
    Returns a list of segment info dictionaries.
    
    This function now tries the FFmpeg method first, falling back to MoviePy if needed.
    """
    # First try the more reliable FFmpeg approach
    try:
        print("Attempting extraction using direct FFmpeg method...")
        return extract_all_segments_alternative(video_path, transcription_data, output_folder)
    except FileNotFoundError:
        print("FFmpeg not found, falling back to MoviePy method...")
    except Exception as e:
        print(f"FFmpeg method failed ({e}), falling back to MoviePy method...")
    
    # Fallback to original MoviePy method with improvements
    # Create temp directory with better handling
    temp_dir = tempfile.mkdtemp(prefix="video_segments_")
    segment_files = []

    try:
        if not transcription_data or "segments" not in transcription_data:
            print("No transcription segments found.")
            return []

        segments_folder = os.path.join(output_folder, "segments")
        os.makedirs(segments_folder, exist_ok=True)

        segment_files = []
        
        # Load video once and keep it open for all segments
        print(f"Loading video: {video_path}")
        with VideoFileClip(video_path) as video:
            print(f"Video duration: {video.duration:.2f}s")
            total_segments = len(transcription_data["segments"])

            for i, segment in enumerate(transcription_data["segments"]):
                print(f"Processing segment {i+1}/{total_segments}")
                
                # Get neighboring segments for context
                prev_segment = transcription_data["segments"][i-1] if i > 0 else None
                next_segment = transcription_data["segments"][i+1] if i < len(transcription_data["segments"])-1 else None
                
                # Use enhanced timing with word-level + smart buffering
                start_time, end_time = get_smart_buffer(
                    segment, 
                    prev_segment, 
                    next_segment, 
                    video.duration
                )

                print(f"[Segment {i}] Original: {segment['start']:.2f}-{segment['end']:.2f} | Buffered: {start_time:.2f}-{end_time:.2f}")

                
                # Validate timing
                if start_time >= video.duration:
                    print(f"  Skipping segment {i}: start time {start_time:.2f}s beyond video duration")
                    continue
                    
                if end_time > video.duration:
                    end_time = video.duration
                    print(f"  Adjusted end time for segment {i} to {end_time:.2f}s")
                    
                if end_time <= start_time:
                    print(f"  Skipping segment {i}: invalid time range {start_time:.2f}s - {end_time:.2f}s")
                    continue

                text = segment.get("text", "").strip()
                if not text:
                    print(f"  Skipping segment {i}: no text content")
                    continue

                # Create segment filename
                segment_filename = f"segment_{i:03d}.mp4"
                segment_path = os.path.join(segments_folder, segment_filename)
                
                # Create unique temp file for this segment
                temp_audio_file = os.path.join(temp_dir, f"segment_{i:03d}_temp_audio.m4a")

                try:
                    print(f"  Extracting segment {i}: {start_time:.2f}s - {end_time:.2f}s ({end_time-start_time:.2f}s duration)")
                    
                    # Extract segment with proper context management
                    segment_clip = video.subclipped(start_time, end_time)
                    
                    # Multiple retry attempts with different strategies
                    success = False
                    retry_count = 0
                    max_retries = 3
                    
                    while not success and retry_count < max_retries:
                        try:
                            if retry_count == 0:
                                # First attempt: Standard approach
                                segment_clip.write_videofile(
                                    segment_path,
                                    codec="libx264",
                                    audio_codec="aac",
                                    temp_audiofile=temp_audio_file,
                                    remove_temp=False,
                                    logger=None,
                                    preset="medium",
                                    ffmpeg_params=["-movflags", "+faststart"]
                                )
                            elif retry_count == 1:
                                # Second attempt: More conservative settings
                                segment_clip.write_videofile(
                                    segment_path,
                                    codec="libx264",
                                    audio_codec="aac",
                                    temp_audiofile=temp_audio_file,
                                    remove_temp=False,
                                    logger=None,
                                    preset="ultrafast",  # Faster encoding
                                    threads=1  # Single thread to avoid conflicts
                                )
                            else:
                                # Third attempt: Minimal settings, no temp audio file
                                segment_clip.write_videofile(
                                    segment_path,
                                    codec="libx264",
                                    audio_codec="aac",
                                    remove_temp=True,
                                    logger=None,
                                    preset="ultrafast",
                                    threads=1
                                )
                            
                            success = True
                            
                        except Exception as retry_e:
                            retry_count += 1
                            print(f"    Retry {retry_count} failed: {str(retry_e)}")
                            
                            # Clean up any partial file before retry
                            if os.path.exists(segment_path):
                                try:
                                    os.remove(segment_path)
                                except:
                                    pass
                            
                            if retry_count < max_retries:
                                print(f"    Attempting retry {retry_count + 1}/{max_retries}")
                                import time
                                time.sleep(1)  # Brief pause before retry
                    
                    # Close the segment clip
                    segment_clip.close()

                    # Verify the output file was created and has content
                    if success and os.path.exists(segment_path) and os.path.getsize(segment_path) > 0:
                        segment_files.append({
                            "id": i,
                            "transcript_id": segment.get("id"),
                            "filename": segment_filename,
                            "path": segment_path,
                            "start": start_time,
                            "end": end_time,
                            "duration": end_time - start_time,
                            "text": text,
                            "score": segment.get("score", 0)
                        })
                        if retry_count > 0:
                            print(f"  + Successfully created segment {i} (after {retry_count} retries)")
                        else:
                            print(f"  + Successfully created segment {i}")
                    else:
                        print(f"  x Failed to create segment {i}: all retry attempts failed")

                except Exception as e:
                    print(f"  x Error extracting segment {i}: {str(e)}")
                    # Clean up partial files
                    if os.path.exists(segment_path):
                        try:
                            os.remove(segment_path)
                        except:
                            pass
                    continue
                
                finally:
                    # Clean up temp audio file for this segment
                    if os.path.exists(temp_audio_file):
                        try:
                            os.remove(temp_audio_file)
                        except:
                            pass

        print(f"Successfully extracted {len(segment_files)}/{total_segments} segments to {segments_folder}")
        return segment_files

    finally:
        print(f"Returning {len(segment_files)} buffered segments to save_segment_manifest()")
        # Clean up temp directory
        try:
            shutil.rmtree(temp_dir)
        except Exception as e:
            print(f"Warning: Could not clean up temp directory {temp_dir}: {e}")


def trim_video(video_path, highlight, output_path):
    """
    Trim a video to create a highlight clip.
    """
    try:
        with VideoFileClip(video_path) as video:
            trimmed_clip = video.subclipped(highlight["start_time"], highlight["end_time"])
            trimmed_clip.write_videofile(output_path, codec="libx264", audio_codec="aac")
            trimmed_clip.close()
        print(f"Trimmed video saved to {output_path}")
    except Exception as e:
        print(f"Error trimming video: {e}")
        raise


def stitch_videos(clip_paths, output_path):
    """
    Concatenate multiple video clips into a single file.
    """
    if not clip_paths:
        print("No clips to stitch.")
        return

    clips = []
    try:
        # Load all clips
        for clip_path in clip_paths:
            if not os.path.exists(clip_path):
                print(f"Warning: Clip not found: {clip_path}")
                continue
            clips.append(VideoFileClip(clip_path))
        
        if not clips:
            print("No valid clips found to stitch.")
            return
            
        final_clip = concatenate_videoclips(clips)
        final_clip.write_videofile(output_path, codec="libx264", audio_codec="aac")
        final_clip.close()
        
        print(f"Final highlight reel saved to {output_path}")
        
    except Exception as e:
        print(f"Error stitching videos: {e}")
        raise
    finally:
        # Clean up all clips
        for clip in clips:
            try:
                clip.close()
            except:
                pass


def create_highlight_reel_from_selection(video_input_path, selection_file_path, segment_files, output_folder):
    """
    Create a highlight reel from a selection file created by the web interface.
    """
    try:
        # Load selection
        with open(selection_file_path, "r") as f:
            selection = json.load(f)
        
        selected_ids = set(selection["selected_segment_ids"])
        selected_segments = [s for s in segment_files if s["id"] in selected_ids]
        
        if not selected_segments:
            print("No segments found for selection.")
            return
        
        # Sort by original order (by segment ID)
        selected_segments.sort(key=lambda x: x["id"])
        
        # Get paths to selected segment files and verify they exist
        selected_paths = []
        for s in selected_segments:
            if os.path.exists(s["path"]) and os.path.getsize(s["path"]) > 0:
                selected_paths.append(s["path"])
            else:
                print(f"Warning: Segment file missing or empty: {s['path']}")
        
        if not selected_paths:
            print("No valid segment files found for selection.")
            return
        
        # Create output path
        timestamp = selection.get("timestamp", "").replace(":", "-").replace(".", "-")[:19]
        output_filename = f"highlight_reel_{timestamp}.mp4"
        output_path = os.path.join(output_folder, output_filename)
        
        print(f"Creating highlight reel with {len(selected_paths)} segments...")
        print(f"Total duration: {selection.get('total_duration', 0):.1f}s")
        
        # Stitch the videos
        stitch_videos(selected_paths, output_path)
        
        return output_path
        
    except Exception as e:
        print(f"Error creating highlight reel: {e}")
        raise


# Additional utility functions for debugging and validation

def validate_segment_files(segment_files):
    """
    Validate that segment files exist and are playable.
    Returns list of valid segments and list of invalid segments.
    """
    valid_segments = []
    invalid_segments = []
    
    for segment in segment_files:
        file_path = segment["path"]
        
        if not os.path.exists(file_path):
            invalid_segments.append((segment, "File does not exist"))
            continue
            
        if os.path.getsize(file_path) == 0:
            invalid_segments.append((segment, "File is empty"))
            continue
            
        # Try to open with moviepy to verify it's a valid video
        try:
            with VideoFileClip(file_path) as test_clip:
                if test_clip.duration > 0:
                    valid_segments.append(segment)
                else:
                    invalid_segments.append((segment, "Zero duration"))
        except Exception as e:
            invalid_segments.append((segment, f"Cannot open as video: {str(e)}"))
    
    print(f"Validation complete: {len(valid_segments)} valid, {len(invalid_segments)} invalid segments")
    
    if invalid_segments:
        print("Invalid segments:")
        for segment, reason in invalid_segments:
            print(f"  Segment {segment['id']}: {reason}")
    
    return valid_segments, invalid_segments


def get_video_info(video_path):
    """
    Get basic information about a video file.
    """
    try:
        with VideoFileClip(video_path) as video:
            info = {
                "duration": video.duration,
                "fps": video.fps,
                "size": video.size,
                "has_audio": video.audio is not None
            }
            return info
    except Exception as e:
        print(f"Error getting video info: {e}")
        return None