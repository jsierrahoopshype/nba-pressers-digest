#!/usr/bin/env python3
"""
Download and process video clips from YouTube using yt-dlp and FFmpeg
"""

import os
import subprocess
import json
from pathlib import Path
from typing import List, Dict, Optional
import shutil


def check_dependencies():
    """Check that yt-dlp and ffmpeg are installed"""
    
    yt_dlp = shutil.which('yt-dlp')
    ffmpeg = shutil.which('ffmpeg')
    
    if not yt_dlp:
        raise RuntimeError("yt-dlp not found. Install with: pip install yt-dlp")
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found. Install with: apt install ffmpeg")
    
    return True


def download_clip(video_id: str, start_seconds: float, end_seconds: float, 
                  output_path: str, max_height: int = 1080) -> bool:
    """
    Download a specific time range from a YouTube video
    
    Args:
        video_id: YouTube video ID
        start_seconds: Start time in seconds
        end_seconds: End time in seconds
        output_path: Where to save the clip
        max_height: Maximum video height (for quality/size balance)
    
    Returns:
        True if successful, False otherwise
    """
    
    url = f"https://www.youtube.com/watch?v={video_id}"
    
    # Format time for yt-dlp (supports decimal seconds)
    start_time = f"{int(start_seconds//3600):02d}:{int((start_seconds%3600)//60):02d}:{start_seconds%60:06.3f}"
    end_time = f"{int(end_seconds//3600):02d}:{int((end_seconds%3600)//60):02d}:{end_seconds%60:06.3f}"
    
    # yt-dlp command with time range
    cmd = [
        "yt-dlp",
        "-f", f"bestvideo[height<={max_height}]+bestaudio/best[height<={max_height}]",
        "--download-sections", f"*{start_time}-{end_time}",
        "--force-keyframes-at-cuts",
        "--merge-output-format", "mp4",
        "-o", output_path,
        "--no-playlist",
        "--quiet",
        "--progress",
        url
    ]
    
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        
        if result.returncode == 0 and os.path.exists(output_path):
            return True
        else:
            print(f"    ⚠️  yt-dlp error: {result.stderr[:200]}")
            return False
            
    except subprocess.TimeoutExpired:
        print(f"    ⚠️  Download timed out")
        return False
    except Exception as e:
        print(f"    ⚠️  Download error: {str(e)}")
        return False


def add_overlays(input_path: str, output_path: str, 
                 headline: str, team: str, 
                 start_time: str = "0:00",
                 show_headline_duration: float = 5.0) -> bool:
    """
    Add text overlays to a clip for fair use compliance
    
    Adds:
    - Team attribution in corner (always visible)
    - Headline lower third (first N seconds)
    """
    
    # Escape special characters for FFmpeg
    headline_escaped = headline.replace("'", "'\\''").replace(":", "\\:")
    team_escaped = team.replace("'", "'\\''")
    
    # FFmpeg filter for overlays
    # Attribution: top-left, always visible
    # Headline: bottom center, fades after show_headline_duration
    
    filter_complex = f"""
    drawtext=text='{team_escaped}':
        fontsize=24:
        fontcolor=white:
        borderw=2:
        bordercolor=black:
        x=20:y=20,
    drawtext=text='{headline_escaped}':
        fontsize=32:
        fontcolor=white:
        box=1:
        boxcolor=black@0.7:
        boxborderw=10:
        x=(w-text_w)/2:
        y=h-80:
        enable='lt(t,{show_headline_duration})'
    """.replace('\n', '').replace('    ', '')
    
    cmd = [
        "ffmpeg",
        "-i", input_path,
        "-vf", filter_complex,
        "-c:a", "copy",
        "-y",  # Overwrite output
        "-loglevel", "error",
        output_path
    ]
    
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        
        if result.returncode == 0 and os.path.exists(output_path):
            return True
        else:
            print(f"    ⚠️  FFmpeg overlay error: {result.stderr[:200]}")
            # If overlay fails, copy original
            shutil.copy(input_path, output_path)
            return True
            
    except Exception as e:
        print(f"    ⚠️  Overlay error: {str(e)}")
        shutil.copy(input_path, output_path)
        return True


def get_video_duration(video_path: str) -> float:
    """Get duration of a video file in seconds"""
    
    cmd = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        video_path
    ]
    
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        return float(result.stdout.strip())
    except:
        return 0


def download_and_process_clips(moments: List[Dict], output_dir: Path) -> List[Dict]:
    """
    Download and process all clips for the digest
    
    Args:
        moments: List of moment dicts with video_id, start_seconds, end_seconds, etc.
        output_dir: Directory to save processed clips
    
    Returns:
        List of processed clip metadata
    """
    
    check_dependencies()
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    
    processed_clips = []
    
    for i, moment in enumerate(moments, 1):
        video_id = moment['video_id']
        start = moment['start_seconds']
        end = moment['end_seconds']
        
        print(f"  [{i}/{len(moments)}] {moment['headline'][:40]}...")
        
        # File paths
        raw_clip = output_dir / f"raw_{i:02d}_{video_id}.mp4"
        final_clip = output_dir / f"clip_{i:02d}_{video_id}.mp4"
        
        # Download clip
        print(f"    Downloading {moment['duration']:.0f}s from {moment['start_time']}...")
        success = download_clip(video_id, start, end, str(raw_clip))
        
        if not success:
            print(f"    ✗ Failed to download")
            continue
        
        # Add overlays
        print(f"    Adding overlays...")
        success = add_overlays(
            str(raw_clip), 
            str(final_clip),
            headline=moment['headline'],
            team=moment['team'],
            start_time=moment['start_time']
        )
        
        if not success:
            print(f"    ✗ Failed to add overlays")
            continue
        
        # Get actual duration
        actual_duration = get_video_duration(str(final_clip))
        
        # Clean up raw clip
        if raw_clip.exists() and final_clip.exists():
            raw_clip.unlink()
        
        # Build processed clip metadata
        processed = {
            **moment,
            'clip_path': str(final_clip),
            'clip_filename': final_clip.name,
            'actual_duration': actual_duration,
            'clip_index': i
        }
        processed_clips.append(processed)
        
        print(f"    ✓ Saved {final_clip.name} ({actual_duration:.1f}s)")
    
    return processed_clips


def create_transition(duration: float = 0.5) -> str:
    """Create a short black transition clip"""
    
    transition_path = "/tmp/transition.mp4"
    
    cmd = [
        "ffmpeg",
        "-f", "lavfi",
        "-i", f"color=c=black:s=1920x1080:d={duration}",
        "-f", "lavfi", 
        "-i", f"anullsrc=r=48000:cl=stereo:d={duration}",
        "-c:v", "libx264",
        "-c:a", "aac",
        "-y",
        "-loglevel", "error",
        transition_path
    ]
    
    subprocess.run(cmd, capture_output=True)
    return transition_path


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Download and process video clips")
    parser.add_argument("--input", type=str, default="data/moments.json", help="Input moments JSON")
    parser.add_argument("--output-dir", type=str, default="output/clips", help="Output directory")
    parser.add_argument("--max-clips", type=int, default=12, help="Maximum clips to process")
    
    args = parser.parse_args()
    
    if not os.path.exists(args.input):
        print(f"Input file not found: {args.input}")
        exit(1)
    
    with open(args.input) as f:
        data = json.load(f)
    
    # Get top moments
    moments = data.get('top_moments', [])[:args.max_clips]
    
    if not moments:
        print("No moments to process")
        exit(1)
    
    print(f"Processing {len(moments)} clips...")
    processed = download_and_process_clips(moments, Path(args.output_dir))
    
    print(f"\n{'='*50}")
    print(f"Processed {len(processed)}/{len(moments)} clips successfully")
    
    # Save processed metadata
    output_file = os.path.join(args.output_dir, "processed_clips.json")
    with open(output_file, 'w') as f:
        json.dump(processed, f, indent=2)
    
    print(f"Saved metadata to {output_file}")
