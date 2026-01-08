#!/usr/bin/env python3
"""
Fetch YouTube transcripts - tries captions first, falls back to Whisper
"""

import json
import os
import subprocess
import tempfile
import re
import sys
from typing import List, Dict, Optional
from pathlib import Path

# Fix Windows console encoding
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')


def safe_print(text):
    """Print text safely, replacing problematic characters"""
    try:
        print(text)
    except UnicodeEncodeError:
        print(text.encode('ascii', errors='replace').decode('ascii'))


def parse_vtt_to_segments(vtt_content: str) -> List[Dict]:
    """Parse VTT subtitle file to segments with timestamps"""
    segments = []
    lines = vtt_content.split('\n')
    i = 0
    
    while i < len(lines):
        line = lines[i].strip()
        timestamp_match = re.match(r'(\d{2}:\d{2}:\d{2}\.\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}\.\d{3})', line)
        
        if timestamp_match:
            start_str = timestamp_match.group(1)
            end_str = timestamp_match.group(2)
            start = vtt_timestamp_to_seconds(start_str)
            end = vtt_timestamp_to_seconds(end_str)
            duration = end - start
            
            i += 1
            text_lines = []
            while i < len(lines):
                text_line = lines[i].strip()
                if not text_line or re.match(r'\d{2}:\d{2}:\d{2}\.\d{3}', text_line):
                    break
                text_line = re.sub(r'<[^>]+>', '', text_line)
                text_lines.append(text_line)
                i += 1
            
            text = ' '.join(text_lines)
            if text:
                segments.append({
                    'start': start,
                    'duration': duration,
                    'text': text
                })
        else:
            i += 1
    
    return merge_segments(segments)


def merge_segments(segments: List[Dict]) -> List[Dict]:
    """Merge overlapping or duplicate segments"""
    if not segments:
        return []
    
    merged = []
    seen_texts = set()
    
    for seg in segments:
        text_key = seg['text'].strip().lower()
        if text_key in seen_texts:
            continue
        seen_texts.add(text_key)
        merged.append(seg)
    
    return merged


def vtt_timestamp_to_seconds(timestamp: str) -> float:
    """Convert VTT timestamp (HH:MM:SS.mmm) to seconds"""
    parts = timestamp.split(':')
    hours = int(parts[0])
    minutes = int(parts[1])
    seconds = float(parts[2])
    return hours * 3600 + minutes * 60 + seconds


def get_captions_ytdlp(video_id: str, tmpdir: str) -> Optional[Dict]:
    """Try to get existing captions using yt-dlp"""
    url = f"https://www.youtube.com/watch?v={video_id}"
    output_template = os.path.join(tmpdir, "%(id)s")
    
    # Try auto-generated captions first
    cmd = [
        "yt-dlp",
        "--skip-download",
        "--write-auto-sub",
        "--sub-lang", "en",
        "--sub-format", "vtt",
        "--output", output_template,
        url
    ]
    
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        vtt_files = list(Path(tmpdir).glob("*.vtt"))
        
        if not vtt_files:
            # Try manual captions
            cmd_manual = [
                "yt-dlp",
                "--skip-download",
                "--write-sub",
                "--sub-lang", "en",
                "--sub-format", "vtt",
                "--output", output_template,
                url
            ]
            result = subprocess.run(cmd_manual, capture_output=True, text=True, timeout=30)
            vtt_files = list(Path(tmpdir).glob("*.vtt"))
        
        if vtt_files:
            vtt_content = vtt_files[0].read_text(encoding='utf-8')
            segments = parse_vtt_to_segments(vtt_content)
            if segments:
                full_text = ' '.join([s['text'] for s in segments])
                return {
                    'full_text': full_text,
                    'segments': segments,
                    'word_count': len(full_text.split()),
                    'method': 'captions'
                }
            else:
                safe_print(f"    [!] VTT found but no segments parsed")
        else:
            # Log why no captions
            if result.stderr:
                err_short = result.stderr[:200].replace('\n', ' ')
                safe_print(f"    [!] No captions: {err_short}")
    except subprocess.TimeoutExpired:
        safe_print(f"    [!] Caption fetch timeout")
    except Exception as e:
        safe_print(f"    [!] Caption error: {str(e)[:100]}")
    
    return None


def transcribe_with_whisper(video_id: str, tmpdir: str) -> Optional[Dict]:
    """Download audio and transcribe with Whisper"""
    url = f"https://www.youtube.com/watch?v={video_id}"
    audio_path = os.path.join(tmpdir, f"{video_id}.mp3")
    
    # Download audio only
    cmd = [
        "yt-dlp",
        "-x",  # Extract audio
        "--audio-format", "mp3",
        "--audio-quality", "5",  # Medium quality (smaller file)
        "-o", audio_path,
        "--no-playlist",
        url
    ]
    
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        
        # yt-dlp might add extension
        if not os.path.exists(audio_path):
            audio_files = list(Path(tmpdir).glob(f"{video_id}.*"))
            if audio_files:
                audio_path = str(audio_files[0])
        
        if not os.path.exists(audio_path):
            if result.stderr:
                err_short = result.stderr[:300].replace('\n', ' ')
                safe_print(f"    [!] Failed to download audio: {err_short}")
            else:
                safe_print(f"    [!] Failed to download audio (no error message)")
            return None
        
        # Check file size (skip if too large - over 100MB)
        file_size = os.path.getsize(audio_path)
        if file_size > 100 * 1024 * 1024:
            safe_print(f"    [!] Audio too large ({file_size // 1024 // 1024}MB), skipping")
            return None
        
        # Transcribe with Whisper
        safe_print(f"    [MIC] Transcribing with Whisper...")
        
        import whisper
        model = whisper.load_model("base")  # Use 'base' for speed, 'small' for accuracy
        result = model.transcribe(audio_path, language="en")
        
        if not result or not result.get('segments'):
            return None
        
        segments = []
        for seg in result['segments']:
            segments.append({
                'start': seg['start'],
                'duration': seg['end'] - seg['start'],
                'text': seg['text'].strip()
            })
        
        full_text = result.get('text', '').strip()
        
        return {
            'full_text': full_text,
            'segments': segments,
            'word_count': len(full_text.split()),
            'method': 'whisper'
        }
        
    except ImportError:
        safe_print(f"    [!] Whisper not installed, skipping audio transcription")
        return None
    except subprocess.TimeoutExpired:
        safe_print(f"    [!] Download timeout")
        return None
    except Exception as e:
        safe_print(f"    [!] Whisper error: {str(e)[:100]}")
        return None


def get_transcript(video_id: str) -> Optional[Dict]:
    """Get transcript - try captions first, fall back to Whisper"""
    
    with tempfile.TemporaryDirectory() as tmpdir:
        # First try existing captions (fast)
        result = get_captions_ytdlp(video_id, tmpdir)
        if result:
            return result
        
        # Fall back to Whisper transcription
        result = transcribe_with_whisper(video_id, tmpdir)
        if result:
            return result
    
    return None


def process_videos(videos: List[Dict], max_whisper: int = 10) -> List[Dict]:
    """
    Fetch transcripts for a list of videos
    max_whisper: Maximum number of videos to transcribe with Whisper (to limit runtime)
    """
    results = []
    whisper_count = 0
    
    for video in videos:
        video_id = video.get('video_id')
        title = video.get('title', 'Unknown')
        
        # Safely truncate title for display
        display_title = title[:50]
        safe_print(f"  Fetching transcript: {display_title}...")
        
        with tempfile.TemporaryDirectory() as tmpdir:
            # First try captions
            transcript = get_captions_ytdlp(video_id, tmpdir)
            
            if transcript:
                video_with_transcript = {**video, 'transcript': transcript}
                results.append(video_with_transcript)
                safe_print(f"    [OK] Got {transcript['word_count']} words (captions)")
                continue
            
            # Fall back to Whisper if under limit
            if whisper_count < max_whisper:
                transcript = transcribe_with_whisper(video_id, tmpdir)
                if transcript:
                    whisper_count += 1
                    video_with_transcript = {**video, 'transcript': transcript}
                    results.append(video_with_transcript)
                    safe_print(f"    [OK] Got {transcript['word_count']} words (whisper) [{whisper_count}/{max_whisper}]")
                    continue
            
            safe_print(f"    [X] No transcript available")
    
    return results


def load_videos(filepath: str = "data/videos.json") -> List[Dict]:
    """Load videos from JSON file"""
    if not os.path.exists(filepath):
        return []
    with open(filepath, 'r', encoding='utf-8') as f:
        return json.load(f)


def save_transcripts(transcripts: List[Dict], filepath: str = "data/transcripts.json"):
    """Save transcripts to JSON file"""
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(transcripts, f, indent=2, ensure_ascii=False)
    safe_print(f"\nSaved {len(transcripts)} transcripts to {filepath}")


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Fetch transcripts for YouTube videos")
    parser.add_argument("--input", type=str, default="data/videos.json", help="Input videos JSON")
    parser.add_argument("--output", type=str, default="data/transcripts.json", help="Output transcripts JSON")
    parser.add_argument("--max-whisper", type=int, default=10, help="Max videos to transcribe with Whisper")
    
    args = parser.parse_args()
    
    videos = load_videos(args.input)
    safe_print(f"Processing {len(videos)} videos...")
    
    transcripts = process_videos(videos, max_whisper=args.max_whisper)
    save_transcripts(transcripts, args.output)
