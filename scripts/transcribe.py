#!/usr/bin/env python3
"""
Fetch YouTube auto-captions using yt-dlp (bypasses cloud IP blocks)
"""

import json
import os
import subprocess
import tempfile
import re
from typing import List, Dict, Optional
from pathlib import Path


def parse_vtt_to_segments(vtt_content: str) -> List[Dict]:
    """Parse VTT subtitle file to segments with timestamps"""
    segments = []
    
    # VTT format: timestamp lines followed by text
    # 00:00:05.000 --> 00:00:10.000
    # Text here
    
    lines = vtt_content.split('\n')
    i = 0
    
    while i < len(lines):
        line = lines[i].strip()
        
        # Look for timestamp line
        timestamp_match = re.match(r'(\d{2}:\d{2}:\d{2}\.\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}\.\d{3})', line)
        
        if timestamp_match:
            start_str = timestamp_match.group(1)
            end_str = timestamp_match.group(2)
            
            # Convert to seconds
            start = vtt_timestamp_to_seconds(start_str)
            end = vtt_timestamp_to_seconds(end_str)
            duration = end - start
            
            # Get text (next non-empty lines until blank or next timestamp)
            i += 1
            text_lines = []
            while i < len(lines):
                text_line = lines[i].strip()
                if not text_line or re.match(r'\d{2}:\d{2}:\d{2}\.\d{3}', text_line):
                    break
                # Remove VTT formatting tags
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
    
    # Merge duplicate/overlapping segments (common in auto-captions)
    merged = merge_segments(segments)
    return merged


def merge_segments(segments: List[Dict]) -> List[Dict]:
    """Merge overlapping or duplicate segments"""
    if not segments:
        return []
    
    merged = []
    seen_texts = set()
    
    for seg in segments:
        # Skip exact duplicates
        text_key = seg['text'].strip().lower()
        if text_key in seen_texts:
            continue
        seen_texts.add(text_key)
        
        # Add segment
        merged.append(seg)
    
    return merged


def vtt_timestamp_to_seconds(timestamp: str) -> float:
    """Convert VTT timestamp (HH:MM:SS.mmm) to seconds"""
    parts = timestamp.split(':')
    hours = int(parts[0])
    minutes = int(parts[1])
    seconds = float(parts[2])
    return hours * 3600 + minutes * 60 + seconds


def get_transcript_ytdlp(video_id: str) -> Optional[Dict]:
    """
    Fetch transcript using yt-dlp (more reliable from cloud IPs)
    """
    url = f"https://www.youtube.com/watch?v={video_id}"
    
    with tempfile.TemporaryDirectory() as tmpdir:
        output_template = os.path.join(tmpdir, "%(id)s")
        
        # Try to download auto-generated captions
        cmd = [
            "yt-dlp",
            "--skip-download",
            "--write-auto-sub",
            "--sub-lang", "en",
            "--sub-format", "vtt",
            "--output", output_template,
            "--no-warnings",
            "--quiet",
            url
        ]
        
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            
            # Look for the subtitle file
            vtt_files = list(Path(tmpdir).glob("*.vtt"))
            
            if not vtt_files:
                # Try without auto-sub (manually uploaded captions)
                cmd[3] = "--write-sub"
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
                vtt_files = list(Path(tmpdir).glob("*.vtt"))
            
            if not vtt_files:
                return None
            
            # Read and parse VTT file
            vtt_content = vtt_files[0].read_text(encoding='utf-8')
            segments = parse_vtt_to_segments(vtt_content)
            
            if not segments:
                return None
            
            # Create full text
            full_text = ' '.join([s['text'] for s in segments])
            
            return {
                'full_text': full_text,
                'segments': segments,
                'word_count': len(full_text.split())
            }
            
        except subprocess.TimeoutExpired:
            print(f"  ⚠️  Timeout fetching transcript for {video_id}")
            return None
        except Exception as e:
            print(f"  ⚠️  Error fetching transcript for {video_id}: {str(e)}")
            return None


def process_videos(videos: List[Dict]) -> List[Dict]:
    """
    Fetch transcripts for a list of videos
    """
    results = []
    
    for video in videos:
        video_id = video.get('video_id')
        title = video.get('title', 'Unknown')
        
        print(f"  Fetching transcript: {title[:50]}...")
        
        transcript = get_transcript_ytdlp(video_id)
        
        if transcript:
            video_with_transcript = {
                **video,
                'transcript': transcript
            }
            results.append(video_with_transcript)
            print(f"    ✓ Got {transcript['word_count']} words")
        else:
            print(f"    ✗ No transcript available")
    
    return results


def load_videos(filepath: str = "data/videos.json") -> List[Dict]:
    """Load videos from JSON file"""
    if not os.path.exists(filepath):
        return []
    with open(filepath, 'r') as f:
        return json.load(f)


def save_transcripts(transcripts: List[Dict], filepath: str = "data/transcripts.json"):
    """Save transcripts to JSON file"""
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, 'w') as f:
        json.dump(transcripts, f, indent=2)
    print(f"\nSaved {len(transcripts)} transcripts to {filepath}")


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Fetch transcripts for YouTube videos")
    parser.add_argument("--input", type=str, default="data/videos.json", help="Input videos JSON")
    parser.add_argument("--output", type=str, default="data/transcripts.json", help="Output transcripts JSON")
    
    args = parser.parse_args()
    
    videos = load_videos(args.input)
    print(f"Processing {len(videos)} videos...")
    
    transcripts = process_videos(videos)
    save_transcripts(transcripts, args.output)
