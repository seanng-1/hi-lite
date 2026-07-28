"""
Highlight detection and analysis logic.
"""
import json
import os
import pandas as pd

# spacy is optional (only used for advanced analysis) and heavyweight, so it
# must not be a hard import — the packaged backend ships without it.
try:
    import spacy
except ImportError:
    spacy = None

# Configuration
HIGHLIGHT_KEYWORDS = ["goal", "score", "amazing", "incredible", "winner", "fantastic", "touchdown", "homerun"]
MAX_HIGHLIGHT_DURATION = 30  # seconds


def export_segments_to_excel(transcription_data, excel_path):
    """
    Writes segments (start, end, text, score) and words (optional) to an Excel workbook.
    """
    if not transcription_data or "segments" not in transcription_data:
        print("No segments to export.")
        return

    segments = transcription_data["segments"]

    seg_rows = [
        {
            "start": s.get("start"),
            "end": s.get("end"),
            "text": s.get("text"),
            "score": s.get("score", 0),  # Default score if missing
            "id": s.get("id"),
        }
        for s in segments
    ]
    df_segments = pd.DataFrame(seg_rows)

    # Optional words sheet if present
    word_rows = []
    for s in segments:
        for w in s.get("words", []) or []:
            word_rows.append({
                "seg_id": s.get("id"),
                "start": w.get("start"),
                "end": w.get("end"),
                "word": w.get("word"),
                "score": w.get("score", 0),
            })
    df_words = pd.DataFrame(word_rows) if word_rows else None

    with pd.ExcelWriter(excel_path, engine="openpyxl") as writer:
        df_segments.to_excel(writer, index=False, sheet_name="segments")
        if df_words is not None and not df_words.empty:
            df_words.to_excel(writer, index=False, sheet_name="words")
    print(f"Exported segments to {excel_path}")


def find_highlights(transcription_data, keywords=None, max_duration=None):
    """
    Analyzes transcription data to find highlight-worthy sections based on keywords and timestamps.
    Returns a list of dictionaries, each with 'start_time', 'end_time', and 'text'.
    """
    if keywords is None:
        keywords = HIGHLIGHT_KEYWORDS
    if max_duration is None:
        max_duration = MAX_HIGHLIGHT_DURATION
        
    highlights = []
    if not transcription_data or "segments" not in transcription_data:
        print("No transcription data or segments found.")
        return highlights

    # Load spacy model for more advanced analysis if needed
    try:
        nlp = spacy.load("en_core_web_sm") if spacy else None
    except OSError:
        print("Warning: spacy model 'en_core_web_sm' not found. Using basic keyword matching only.")
        nlp = None

    for segment in transcription_data["segments"]:
        text = segment["text"].lower()
        start = segment["start"]
        end = segment["end"]

        # Basic keyword matching
        if any(keyword in text for keyword in keywords):
            highlight_end = min(end, start + max_duration)
            highlights.append({
                "start_time": start,
                "end_time": highlight_end,
                "text": segment["text"],
                "segment_id": segment.get("id"),
                "score": segment.get("score", 0)
            })
            print(f"Found potential highlight: {segment['text']} ({start:.2f}s - {highlight_end:.2f}s)")
    
    return highlights


def find_highlights_from_segments(segment_files, keywords=None):
    """
    Find potential highlights from extracted segments using keyword matching.
    """
    if keywords is None:
        keywords = HIGHLIGHT_KEYWORDS
        
    highlights = []
    
    for segment in segment_files:
        text = segment["text"].lower()
        
        # Basic keyword matching
        if any(keyword in text for keyword in keywords):
            highlights.append(segment)
            print(f"Keyword highlight found: {segment['text'][:60]}... ({segment['start']:.1f}s-{segment['end']:.1f}s)")
    
    return highlights


def analyze_segment_scores(transcription_data, score_threshold=0.5):
    """
    Analyze segments based on transcription confidence scores.
    Returns segments with scores above the threshold.
    """
    if not transcription_data or "segments" not in transcription_data:
        return []
    
    high_score_segments = []
    for segment in transcription_data["segments"]:
        score = segment.get("score", 0)
        if score >= score_threshold:
            high_score_segments.append(segment)
    
    return high_score_segments


def save_segment_manifest(segment_files, output_folder):
    """
    Save segment information as JSON manifest for the web interface.
    """
    manifest_path = os.path.join(output_folder, "segments_manifest.json")
    
    # Convert segment info to JSON-serializable format
    manifest_data = {
        "segments": segment_files,
        "total_segments": len(segment_files),
        "total_duration": sum(s["duration"] for s in segment_files),
        "keyword_highlights": find_highlights_from_segments(segment_files)
    }
    
    with open(manifest_path, "w") as f:
        json.dump(manifest_data, f, indent=2)
    
    print(f"Segment manifest saved to {manifest_path}")


class HighlightAnalyzer:
    """
    A class for more advanced highlight analysis.
    """
    
    def __init__(self, keywords=None, max_duration=None):
        self.keywords = keywords or HIGHLIGHT_KEYWORDS
        self.max_duration = max_duration or MAX_HIGHLIGHT_DURATION
        
        # Try to load spacy model
        try:
            self.nlp = spacy.load("en_core_web_sm") if spacy else None
        except OSError:
            print("Warning: spacy model 'en_core_web_sm' not found.")
            self.nlp = None
    
    def analyze_sentiment(self, text):
        """
        Basic sentiment analysis (placeholder for more sophisticated analysis).
        """
        # This is a very basic implementation
        # You could integrate with more sophisticated sentiment analysis libraries
        positive_words = ["amazing", "incredible", "fantastic", "great", "excellent", "wonderful"]
        exciting_words = ["goal", "score", "touchdown", "homerun", "winner"]
        
        text_lower = text.lower()
        sentiment_score = 0
        
        for word in positive_words:
            if word in text_lower:
                sentiment_score += 1
        
        for word in exciting_words:
            if word in text_lower:
                sentiment_score += 2
        
        return sentiment_score
    
    def find_advanced_highlights(self, transcription_data):
        """
        Find highlights using multiple criteria: keywords, sentiment, and confidence scores.
        """
        highlights = []
        
        if not transcription_data or "segments" not in transcription_data:
            return highlights
        
        for segment in transcription_data["segments"]:
            text = segment["text"]
            start = segment["start"]
            end = segment["end"]
            confidence = segment.get("score", 0)
            
            # Calculate different scores
            keyword_score = sum(1 for keyword in self.keywords if keyword in text.lower())
            sentiment_score = self.analyze_sentiment(text)
            
            # Combine scores (you can adjust weights)
            combined_score = (keyword_score * 2) + sentiment_score + (confidence * 0.5)
            
            # Threshold for highlighting (adjust as needed)
            if combined_score >= 2.0:
                highlight_end = min(end, start + self.max_duration)
                highlights.append({
                    "start_time": start,
                    "end_time": highlight_end,
                    "text": text,
                    "segment_id": segment.get("id"),
                    "confidence": confidence,
                    "keyword_score": keyword_score,
                    "sentiment_score": sentiment_score,
                    "combined_score": combined_score
                })
                
                print(f"Advanced highlight found (score: {combined_score:.2f}): {text[:60]}...")
        
        # Sort by combined score (highest first)
        highlights.sort(key=lambda x: x["combined_score"], reverse=True)
        return highlights