#!/usr/bin/env python3
"""
Fetch transcripts from YouTube videos using auto-generated captions
"""

from youtube_transcript_api import YouTubeTranscriptApi, TranscriptsDisabled, NoTranscriptFound
from typing import List, Dict, Optional
import json


def get_transcript(video_id: str, languages: List[str] = ['en']) -> Optional[List[Dict]]:
    """
    Fetch YouTube transcript for a video
    
    Args:
        video_id: YouTube video ID (11 characters)
        languages: Preferred languages in order of preference
    
    Returns:
        List of transcript segments with 'text', 'start', 'duration'
        or None if transcript unavailable
    """
    try:
        # Try to get transcript in preferred languages
        transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)
        
        # Try manual transcripts first (higher quality)
        try:
            transcript = transcript_list.find_manually_created_transcript(languages)
            return transcript.fetch()
        except:
            pass
        
        # Fall back to auto-generated
        try:
            transcript = transcript_list.find_generated_transcript(languages)
            return transcript.fetch()
        except:
            pass
        
        # Last resort: get any available transcript
        for transcript in transcript_list:
            try:
                return transcript.fetch()
            except:
                continue
        
        return None
        
    except TranscriptsDisabled:
        print(f"  ⚠️  Transcripts disabled for {video_id}")
        return None
    except NoTranscriptFound:
        print(f"  ⚠️  No transcript found for {video_id}")
        return None
    except Exception as e:
        print(f"  ⚠️  Error fetching transcript for {video_id}: {str(e)}")
        return None


def format_transcript_for_llm(transcript: List[Dict]) -> str:
    """
    Format transcript segments into readable text with timestamps
    
    Args:
        transcript: List of {text, start, duration} dicts
    
    Returns:
        Formatted string with timestamps
    """
    lines = []
    for segment in transcript:
        start = segment['start']
        minutes = int(start // 60)
        seconds = int(start % 60)
        timestamp = f"[{minutes}:{seconds:02d}]"
        lines.append(f"{timestamp} {segment['text']}")
    
    return '\n'.join(lines)


def get_transcript_text_only(transcript: List[Dict]) -> str:
    """Get just the text without timestamps"""
    return ' '.join(segment['text'] for segment in transcript)


def get_segment_at_time(transcript: List[Dict], start_seconds: float, end_seconds: float) -> str:
    """Extract transcript text between two timestamps"""
    segments = []
    for segment in transcript:
        seg_start = segment['start']
        seg_end = seg_start + segment['duration']
        
        # Check if segment overlaps with requested range
        if seg_end >= start_seconds and seg_start <= end_seconds:
            segments.append(segment['text'])
    
    return ' '.join(segments)


def process_transcripts(videos: List[Dict]) -> Dict[str, Dict]:
    """
    Fetch transcripts for a list of videos
    
    Args:
        videos: List of video dicts with 'video_id', 'team', 'title'
    
    Returns:
        Dict mapping video_id to transcript data
    """
    transcripts = {}
    
    for video in videos:
        video_id = video['video_id']
        print(f"  Fetching transcript: {video['title'][:50]}...")
        
        transcript = get_transcript(video_id)
        
        if transcript:
            transcripts[video_id] = {
                'video_id': video_id,
                'team': video.get('team', ''),
                'title': video.get('title', ''),
                'url': video.get('url', ''),
                'person': video.get('person', ''),
                'raw_transcript': transcript,
                'formatted': format_transcript_for_llm(transcript),
                'text_only': get_transcript_text_only(transcript),
                'duration_seconds': transcript[-1]['start'] + transcript[-1]['duration'] if transcript else 0
            }
            print(f"    ✓ Got {len(transcript)} segments ({transcripts[video_id]['duration_seconds']:.0f}s)")
        else:
            print(f"    ✗ No transcript available")
    
    return transcripts


def timestamp_to_seconds(timestamp: str) -> float:
    """Convert MM:SS or M:SS timestamp to seconds"""
    parts = timestamp.split(':')
    if len(parts) == 2:
        return int(parts[0]) * 60 + int(parts[1])
    elif len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    return 0


def seconds_to_timestamp(seconds: float) -> str:
    """Convert seconds to MM:SS format"""
    minutes = int(seconds // 60)
    secs = int(seconds % 60)
    return f"{minutes}:{secs:02d}"


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Fetch YouTube transcripts")
    parser.add_argument("--video-id", type=str, help="Single video ID to test")
    parser.add_argument("--input", type=str, default="data/videos.json", help="Input videos JSON")
    parser.add_argument("--output", type=str, default="data/transcripts.json", help="Output file")
    
    args = parser.parse_args()
    
    if args.video_id:
        # Test single video
        transcript = get_transcript(args.video_id)
        if transcript:
            print(f"Got {len(transcript)} segments")
            print("\nFirst 10 segments:")
            for seg in transcript[:10]:
                print(f"[{seg['start']:.1f}] {seg['text']}")
        else:
            print("No transcript available")
    else:
        # Process videos from file
        import os
        
        if not os.path.exists(args.input):
            print(f"Input file not found: {args.input}")
            exit(1)
        
        with open(args.input) as f:
            videos = json.load(f)
        
        transcripts = process_transcripts(videos)
        
        os.makedirs(os.path.dirname(args.output), exist_ok=True)
        with open(args.output, 'w') as f:
            json.dump(transcripts, f, indent=2)
        
        print(f"\nSaved {len(transcripts)} transcripts to {args.output}")
