#!/usr/bin/env python3
"""
Fetch YouTube auto-captions for press conference videos
Updated for youtube-transcript-api >= 1.0.0
"""

import json
import os
from typing import List, Dict, Optional
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api.formatters import TextFormatter


def get_transcript(video_id: str) -> Optional[Dict]:
    """
    Fetch auto-generated transcript for a YouTube video
    
    Args:
        video_id: YouTube video ID
        
    Returns:
        Dict with full_text and segments, or None if unavailable
    """
    try:
        # New API: directly fetch transcript (auto-selects best available)
        transcript_list = YouTubeTranscriptApi.get_transcript(
            video_id,
            languages=['en', 'en-US', 'en-GB']  # Prefer English
        )
        
        # Format segments with timestamps
        segments = []
        for entry in transcript_list:
            segments.append({
                'start': entry['start'],
                'duration': entry.get('duration', 0),
                'text': entry['text']
            })
        
        # Create full text
        full_text = ' '.join([s['text'] for s in segments])
        
        return {
            'full_text': full_text,
            'segments': segments,
            'word_count': len(full_text.split())
        }
        
    except Exception as e:
        # Try without language preference as fallback
        try:
            transcript_list = YouTubeTranscriptApi.get_transcript(video_id)
            
            segments = []
            for entry in transcript_list:
                segments.append({
                    'start': entry['start'],
                    'duration': entry.get('duration', 0),
                    'text': entry['text']
                })
            
            full_text = ' '.join([s['text'] for s in segments])
            
            return {
                'full_text': full_text,
                'segments': segments,
                'word_count': len(full_text.split())
            }
        except Exception as e2:
            print(f"  ⚠️  Error fetching transcript for {video_id}: {str(e2)}")
            return None


def process_videos(videos: List[Dict]) -> List[Dict]:
    """
    Fetch transcripts for a list of videos
    
    Args:
        videos: List of video dicts with video_id field
        
    Returns:
        List of video dicts with transcript data added
    """
    results = []
    
    for video in videos:
        video_id = video.get('video_id')
        title = video.get('title', 'Unknown')
        
        print(f"  Fetching transcript: {title[:50]}...")
        
        transcript = get_transcript(video_id)
        
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
