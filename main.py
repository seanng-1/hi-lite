"""
Main script for video highlight processing.
Updated to use modular structure.
"""
import os
import sys
import json
from transcript_store import invalidate_transcript, publish_transcript
from datetime import datetime

# Windows consoles/pipes default to cp1252, which cannot encode transcript
# text and would kill the process with UnicodeEncodeError. Force UTF-8.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
from video_processor import extract_audio, extract_all_segments, build_segment_metadata
from transcription_client import transcribe_audio
from highlight_analyzer import export_segments_to_excel, save_segment_manifest, find_highlights_from_segments


def main(video_input_path, output_folder="highlights", final_output_name="highlight_reel.mp4", *, render_segments=False):
    """
    Main processing function for video highlights.
    """
    if not os.path.exists(output_folder):
        os.makedirs(output_folder)

    invalidate_transcript(output_folder)

    audio_output_path = os.path.join(output_folder, "extracted_audio.wav")

    print("=== Video Highlight Processing ===")
    print(f"Input video: {video_input_path}")
    print(f"Output folder: {output_folder}")
    print()

    # 1. Extract audio
    print("Step 1: Extracting audio...")
    extract_audio(video_input_path, audio_output_path)
    print()

    # 2. Transcribe audio (faster-whisper; do not reword this print -- the
    # server's status parser string-matches it)
    print("Step 2: Transcribing audio...")
    transcription = transcribe_audio(
        audio_path=audio_output_path,
        model_size=os.getenv("WHISPER_MODEL", "small"),  # tiny, base, small, medium, large-v3
        device="cpu",
        language="auto"
    )
    
    if not transcription:
        print("ERROR: Transcription failed. Exiting.")
        return

    print("Transcription completed successfully!")
    print()

    # 3. Save transcription data
    print("Step 3: Saving transcription data...")
    json_path = os.path.join(output_folder, "transcription.json")
    xlsx_path = os.path.join(output_folder, "transcription.xlsx")
    
    publish_transcript(output_folder, transcription)
    print(f"Transcription saved to {json_path}")

    export_segments_to_excel(transcription, xlsx_path)
    print()

    # Keep this phase string for existing server status parsers. The default is
    # metadata-only: original-source preview/export do not need eager clips.
    print("Step 4: Extracting video segments...")
    if render_segments:
        segment_files = extract_all_segments(video_input_path, transcription, output_folder)
    else:
        print("Preparing source-range metadata (no segment rendering)...")
        segment_files = build_segment_metadata(video_input_path, transcription, output_folder)
    
    if not segment_files:
        print("ERROR: No segments extracted. Exiting.")
        return

    print(f"Extracted {len(segment_files)} segments")
    print()

    # 5. Save segment manifest
    print("Step 5: Creating segment manifest...")
    save_segment_manifest(segment_files, output_folder)
    print()

    # Summary
    #TODO: Edit the last few steps here. This main function now handles all the initial processing
    # the user then needs to make selections via the UI to send new HTTP requests to finish creating highlights/download.
    print("=== Processing Complete! ===")
    print(f"Extracted {len(segment_files)} total segments")
    print()
    print("Next Steps:")
    print("1. Open the web UI (dev: localhost:5173)")
    print("2. Select the segments you want in your highlight reel")
    print("3. Export the stitched reel from the UI")
    print()
    print(f"All files saved in: {output_folder}/")
    if render_segments:
        print(f"Video segments in: {output_folder}/segments/")
    else:
        print("Clips will be rendered from the original recording when exported.")


def process_selection_file(selection_file_path, video_input_path=None, output_folder="highlights"):
    """
    Create a highlight reel from a selection file created by the web interface.
    """
    from video_processor import create_highlight_reel_from_selection
    
    # Load segment manifest
    manifest_path = os.path.join(output_folder, "segments_manifest.json")
    if not os.path.exists(manifest_path):
        print(f"Segment manifest not found: {manifest_path}")
        print("Please run the main processing first.")
        return
    
    with open(manifest_path, "r") as f:
        manifest = json.load(f)
    
    segment_files = manifest["segments"]
    
    # Use video from manifest or provided path
    if not video_input_path:
        # Try to find the original video file
        possible_videos = ["test.mp4", "input.mp4", "video.mp4"]
        for video_file in possible_videos:
            if os.path.exists(video_file):
                video_input_path = video_file
                break
        
        if not video_input_path:
            print("Could not find original video file.")
            print("Please specify the video path: python main.py --create-reel <selection_file> --video <video_path>")
            return
    
    print(f"Creating highlight reel from selection: {selection_file_path}")
    output_path = create_highlight_reel_from_selection(
        video_input_path, selection_file_path, segment_files, output_folder
    )
    
    if output_path:
        print(f"Highlight reel created: {output_path}")
    else:
        print("Failed to create highlight reel")


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Video Highlight Processing Tool")
    parser.add_argument("video", nargs="?", help="Input video file")
    parser.add_argument("--output", "-o", default="highlights", help="Output folder")
    parser.add_argument("--create-reel", help="Create highlight reel from selection file")
    parser.add_argument("--video-path", help="Video path for creating reel (if different)")
    parser.add_argument("--render-segments", action="store_true", help="Legacy mode: eagerly render every segment")
    
    args = parser.parse_args()
    
    if args.create_reel:
        # Create highlight reel from selection
        process_selection_file(
            args.create_reel, 
            args.video_path or args.video, 
            args.output
        )
    else:
        # Main processing
        if not args.video:
            parser.error("a video file is required (usage: python main.py video.mp4)")
        if not os.path.exists(args.video):
            print(f"Video file not found: {args.video}")
            print("Please provide a valid video file path.")
            exit(1)
        
        main(args.video, args.output, render_segments=args.render_segments)
