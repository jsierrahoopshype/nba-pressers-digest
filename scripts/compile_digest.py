#!/usr/bin/env python3
"""
Compile individual clips into final digest video and generate metadata
"""

import os
import subprocess
import json
from pathlib import Path
from datetime import datetime
from typing import List, Dict


def compile_final_video(clips: List[Dict], output_path: Path, 
                        add_intro: bool = False, add_outro: bool = False) -> bool:
    """
    Concatenate all clips into a single digest video
    
    Args:
        clips: List of clip dicts with 'clip_path'
        output_path: Where to save final video
        add_intro: Whether to add intro slate (requires intro.mp4)
        add_outro: Whether to add outro slate (requires outro.mp4)
    
    Returns:
        True if successful
    """
    
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    # Create concat file list
    concat_list = output_path.parent / "concat_list.txt"
    
    with open(concat_list, 'w') as f:
        # Add intro if exists
        if add_intro and os.path.exists("assets/intro.mp4"):
            f.write(f"file 'assets/intro.mp4'\n")
        
        # Add all clips
        for clip in clips:
            clip_path = clip.get('clip_path', '')
            if clip_path and os.path.exists(clip_path):
                # FFmpeg concat needs absolute paths
                abs_path = os.path.abspath(clip_path)
                f.write(f"file '{abs_path}'\n")
        
        # Add outro if exists
        if add_outro and os.path.exists("assets/outro.mp4"):
            f.write(f"file 'assets/outro.mp4'\n")
    
    # FFmpeg concat command
    cmd = [
        "ffmpeg",
        "-f", "concat",
        "-safe", "0",
        "-i", str(concat_list),
        "-c", "copy",  # Copy without re-encoding (fast)
        "-y",
        "-loglevel", "error",
        str(output_path)
    ]
    
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        
        if result.returncode == 0 and output_path.exists():
            # Clean up concat list
            concat_list.unlink()
            return True
        else:
            print(f"FFmpeg concat error: {result.stderr}")
            return False
            
    except Exception as e:
        print(f"Compilation error: {str(e)}")
        return False


def generate_metadata(clips: List[Dict], date: datetime) -> Dict:
    """
    Generate YouTube/Twitter metadata for the digest
    
    Args:
        clips: List of processed clip dicts
        date: Date of the digest
    
    Returns:
        Dict with title, description, twitter_text, timestamps, etc.
    """
    
    date_str = date.strftime("%b %d")
    date_full = date.strftime("%B %d, %Y")
    
    # Find highest-scoring clip for title hook
    if clips:
        top_clip = max(clips, key=lambda x: x.get('score', 0))
        title_hook = top_clip.get('headline', 'Daily Digest')
    else:
        title_hook = "Daily Digest"
    
    # Generate title
    title = f"{title_hook} + More | NBA Pressers {date_str}"
    
    # Generate timestamps for description
    timestamps = []
    current_time = 0
    for clip in clips:
        minutes = int(current_time // 60)
        seconds = int(current_time % 60)
        timestamp = f"{minutes}:{seconds:02d}"
        
        team = clip.get('team', 'NBA')
        headline = clip.get('headline', 'Highlight')
        timestamps.append(f"{timestamp} - {headline} ({team})")
        
        current_time += clip.get('actual_duration', clip.get('duration', 30))
    
    # Generate description
    description_parts = [
        f"🏀 NBA Press Conference Digest - {date_full}",
        "",
        "Today's top moments:",
        ""
    ]
    description_parts.extend(timestamps)
    description_parts.extend([
        "",
        "---",
        "Sources (clips used under fair use for news commentary):"
    ])
    
    # Add source links
    seen_urls = set()
    for clip in clips:
        url = clip.get('source_url', '')
        team = clip.get('team', '')
        if url and url not in seen_urls:
            description_parts.append(f"• {team}: {url}")
            seen_urls.add(url)
    
    description_parts.extend([
        "",
        "For licensing inquiries: [your email]",
        "",
        "#NBA #Basketball #PressConference #NBAnews"
    ])
    
    description = "\n".join(description_parts)
    
    # Generate Twitter text
    twitter_highlights = []
    for clip in clips[:4]:  # Top 4 for Twitter
        emoji = "👀" if clip.get('score', 0) >= 8 else "•"
        twitter_highlights.append(f"{emoji} {clip.get('headline', '')}")
    
    twitter_text = f"🏀 NBA Pressers Digest ({date_str}):\n\n" + "\n".join(twitter_highlights)
    twitter_text += "\n\nFull breakdown ⬇️"
    
    # Calculate total duration
    total_duration = sum(c.get('actual_duration', c.get('duration', 30)) for c in clips)
    
    return {
        'title': title,
        'description': description,
        'twitter_text': twitter_text,
        'timestamps': timestamps,
        'tags': ['NBA', 'basketball', 'press conference', 'highlights', 'sports news'],
        'total_duration_seconds': total_duration,
        'total_duration_formatted': f"{int(total_duration//60)}:{int(total_duration%60):02d}",
        'clip_count': len(clips),
        'date': date.isoformat(),
        'date_display': date_full
    }


def generate_thumbnail_text(clips: List[Dict]) -> Dict:
    """
    Generate text suggestions for thumbnail
    
    Returns suggestions for a split-image thumbnail with text overlays
    """
    
    if not clips:
        return {'primary': 'NBA Pressers', 'secondary': 'Daily Digest'}
    
    # Get top 2 clips for thumbnail
    sorted_clips = sorted(clips, key=lambda x: x.get('score', 0), reverse=True)
    top_two = sorted_clips[:2]
    
    # Extract key words for thumbnail
    suggestions = {
        'primary_headline': top_two[0].get('headline', '') if top_two else '',
        'secondary_headline': top_two[1].get('headline', '') if len(top_two) > 1 else '',
        'primary_team': top_two[0].get('team', '') if top_two else '',
        'secondary_team': top_two[1].get('team', '') if len(top_two) > 1 else '',
        'primary_person': top_two[0].get('person', '') if top_two else '',
        'secondary_person': top_two[1].get('person', '') if len(top_two) > 1 else '',
    }
    
    # Thumbnail text suggestions
    suggestions['thumbnail_ideas'] = [
        f"{suggestions['primary_headline'][:20]}..." if len(suggestions['primary_headline']) > 20 else suggestions['primary_headline'],
        f"+ {len(clips) - 1} MORE" if len(clips) > 1 else "",
    ]
    
    return suggestions


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Compile digest video")
    parser.add_argument("--input", type=str, default="output/clips/processed_clips.json", help="Input clips JSON")
    parser.add_argument("--output", type=str, default="output/digest.mp4", help="Output video path")
    
    args = parser.parse_args()
    
    if not os.path.exists(args.input):
        print(f"Input file not found: {args.input}")
        exit(1)
    
    with open(args.input) as f:
        clips = json.load(f)
    
    print(f"Compiling {len(clips)} clips...")
    success = compile_final_video(clips, Path(args.output))
    
    if success:
        print(f"✓ Created {args.output}")
        
        # Generate metadata
        metadata = generate_metadata(clips, datetime.now())
        
        metadata_path = args.output.replace('.mp4', '_metadata.json')
        with open(metadata_path, 'w') as f:
            json.dump(metadata, f, indent=2)
        
        print(f"✓ Created {metadata_path}")
        print(f"\nTitle: {metadata['title']}")
        print(f"Duration: {metadata['total_duration_formatted']}")
    else:
        print("✗ Compilation failed")
        exit(1)
