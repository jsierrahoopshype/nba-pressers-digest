#!/usr/bin/env python3
"""
Quality assurance checks for the digest before publishing
"""

import os
import subprocess
import json
from pathlib import Path
from typing import List, Dict, Tuple


def check_video_quality(video_path: str) -> Tuple[bool, List[str]]:
    """
    Check video file for quality issues
    
    Returns:
        (passed, warnings)
    """
    
    warnings = []
    
    if not os.path.exists(video_path):
        return False, ["Video file does not exist"]
    
    # Get video info with ffprobe
    cmd = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration,size:stream=width,height,codec_name,r_frame_rate",
        "-of", "json",
        video_path
    ]
    
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        info = json.loads(result.stdout)
        
        # Check duration
        duration = float(info.get('format', {}).get('duration', 0))
        if duration < 60:
            warnings.append(f"Video very short: {duration:.0f}s (expected >60s)")
        if duration > 600:
            warnings.append(f"Video very long: {duration:.0f}s (expected <600s)")
        
        # Check file size
        size_mb = int(info.get('format', {}).get('size', 0)) / (1024 * 1024)
        if size_mb < 1:
            warnings.append(f"File size very small: {size_mb:.1f}MB")
        if size_mb > 500:
            warnings.append(f"File size very large: {size_mb:.1f}MB")
        
        # Check resolution
        for stream in info.get('streams', []):
            if stream.get('codec_name') in ['h264', 'hevc', 'vp9']:
                width = stream.get('width', 0)
                height = stream.get('height', 0)
                if height < 480:
                    warnings.append(f"Low resolution: {width}x{height}")
        
        return True, warnings
        
    except Exception as e:
        return False, [f"Error checking video: {str(e)}"]


def check_clip_accuracy(clips: List[Dict]) -> Tuple[bool, List[str]]:
    """
    Check that clip metadata is reasonable
    
    Returns:
        (passed, warnings)
    """
    
    warnings = []
    
    for i, clip in enumerate(clips, 1):
        # Check headline length
        headline = clip.get('headline', '')
        if len(headline) > 60:
            warnings.append(f"Clip {i}: Headline too long ({len(headline)} chars)")
        if len(headline) < 5:
            warnings.append(f"Clip {i}: Headline too short")
        
        # Check duration
        duration = clip.get('duration', 0)
        if duration < 10:
            warnings.append(f"Clip {i}: Very short ({duration}s)")
        if duration > 90:
            warnings.append(f"Clip {i}: Very long ({duration}s)")
        
        # Check score
        score = clip.get('score', 0)
        if score < 5:
            warnings.append(f"Clip {i}: Low score ({score}/10) - '{headline[:30]}'")
        
        # Check required fields
        required = ['video_id', 'team', 'headline', 'source_url']
        for field in required:
            if not clip.get(field):
                warnings.append(f"Clip {i}: Missing {field}")
    
    return len(warnings) == 0, warnings


def check_content_diversity(clips: List[Dict]) -> Tuple[bool, List[str]]:
    """
    Check for diversity in sources (not all from same team)
    
    Returns:
        (passed, warnings)
    """
    
    warnings = []
    
    # Count clips per team
    team_counts = {}
    for clip in clips:
        team = clip.get('team', 'Unknown')
        team_counts[team] = team_counts.get(team, 0) + 1
    
    # Check for over-representation
    total = len(clips)
    for team, count in team_counts.items():
        if total > 3 and count > total * 0.5:
            warnings.append(f"Over-represented: {team} ({count}/{total} clips)")
    
    # Check for minimum diversity
    if total > 5 and len(team_counts) < 3:
        warnings.append(f"Low diversity: Only {len(team_counts)} teams in {total} clips")
    
    return len(warnings) == 0, warnings


def check_timestamps(clips: List[Dict]) -> Tuple[bool, List[str]]:
    """
    Check timestamp consistency
    
    Returns:
        (passed, warnings)
    """
    
    warnings = []
    
    for i, clip in enumerate(clips, 1):
        start = clip.get('start_seconds', 0)
        end = clip.get('end_seconds', 0)
        
        if start >= end:
            warnings.append(f"Clip {i}: Invalid timestamps (start >= end)")
        
        if start < 0:
            warnings.append(f"Clip {i}: Negative start time")
    
    return len(warnings) == 0, warnings


def check_attribution(clips: List[Dict]) -> Tuple[bool, List[str]]:
    """
    Check that all clips have proper attribution for fair use
    
    Returns:
        (passed, warnings)
    """
    
    warnings = []
    
    for i, clip in enumerate(clips, 1):
        if not clip.get('team'):
            warnings.append(f"Clip {i}: Missing team attribution")
        if not clip.get('source_url'):
            warnings.append(f"Clip {i}: Missing source URL")
    
    return len(warnings) == 0, warnings


def run_qa_checks(clips: List[Dict], video_path: str = None) -> Dict:
    """
    Run all QA checks
    
    Args:
        clips: List of clip metadata dicts
        video_path: Path to final compiled video (optional)
    
    Returns:
        Dict with check results
    """
    
    results = {
        'all_passed': True,
        'warnings': [],
        'errors': [],
        'checks': {}
    }
    
    # Video quality check
    if video_path and os.path.exists(video_path):
        passed, warnings = check_video_quality(video_path)
        results['checks']['video_quality'] = {'passed': passed, 'warnings': warnings}
        if not passed:
            results['all_passed'] = False
            results['errors'].extend(warnings)
        else:
            results['warnings'].extend(warnings)
    
    # Clip accuracy
    passed, warnings = check_clip_accuracy(clips)
    results['checks']['clip_accuracy'] = {'passed': passed, 'warnings': warnings}
    results['warnings'].extend(warnings)
    
    # Content diversity
    passed, warnings = check_content_diversity(clips)
    results['checks']['content_diversity'] = {'passed': passed, 'warnings': warnings}
    results['warnings'].extend(warnings)
    
    # Timestamps
    passed, warnings = check_timestamps(clips)
    results['checks']['timestamps'] = {'passed': passed, 'warnings': warnings}
    if not passed:
        results['all_passed'] = False
        results['errors'].extend(warnings)
    
    # Attribution
    passed, warnings = check_attribution(clips)
    results['checks']['attribution'] = {'passed': passed, 'warnings': warnings}
    if not passed:
        results['all_passed'] = False
        results['errors'].extend(warnings)
    
    # Summary
    results['total_clips'] = len(clips)
    results['total_warnings'] = len(results['warnings'])
    results['total_errors'] = len(results['errors'])
    
    return results


def generate_review_report(qa_results: Dict, clips: List[Dict], 
                          metadata: Dict = None) -> str:
    """
    Generate human-readable review report
    """
    
    lines = [
        "=" * 60,
        "NBA PRESSERS DIGEST - QA REVIEW REPORT",
        "=" * 60,
        "",
        f"Overall Status: {'✅ PASSED' if qa_results['all_passed'] else '⚠️ NEEDS REVIEW'}",
        f"Total Clips: {qa_results['total_clips']}",
        f"Warnings: {qa_results['total_warnings']}",
        f"Errors: {qa_results['total_errors']}",
        "",
    ]
    
    if metadata:
        lines.extend([
            "PROPOSED TITLE:",
            metadata.get('title', 'N/A'),
            "",
            f"Total Duration: {metadata.get('total_duration_formatted', 'N/A')}",
            "",
        ])
    
    lines.extend([
        "CLIPS INCLUDED:",
        "-" * 40,
    ])
    
    for i, clip in enumerate(clips, 1):
        lines.append(f"{i}. [{clip.get('team', '?')}] {clip.get('headline', '?')}")
        lines.append(f"   Score: {clip.get('score', '?')}/10 | Duration: {clip.get('duration', '?')}s")
        lines.append(f"   Source: {clip.get('source_url', 'N/A')}")
        lines.append("")
    
    if qa_results['errors']:
        lines.extend([
            "❌ ERRORS (must fix):",
            "-" * 40,
        ])
        for error in qa_results['errors']:
            lines.append(f"  • {error}")
        lines.append("")
    
    if qa_results['warnings']:
        lines.extend([
            "⚠️ WARNINGS (review):",
            "-" * 40,
        ])
        for warning in qa_results['warnings']:
            lines.append(f"  • {warning}")
        lines.append("")
    
    lines.extend([
        "=" * 60,
        "Review complete. Approve to publish or edit clips as needed.",
        "=" * 60,
    ])
    
    return "\n".join(lines)


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Run QA checks on digest")
    parser.add_argument("--clips", type=str, default="output/clips/processed_clips.json")
    parser.add_argument("--video", type=str, default="output/digest.mp4")
    parser.add_argument("--output", type=str, default="output/qa_report.txt")
    
    args = parser.parse_args()
    
    # Load clips
    if os.path.exists(args.clips):
        with open(args.clips) as f:
            clips = json.load(f)
    else:
        clips = []
    
    # Run checks
    results = run_qa_checks(clips, args.video if os.path.exists(args.video) else None)
    
    # Generate report
    report = generate_review_report(results, clips)
    print(report)
    
    # Save report
    with open(args.output, 'w') as f:
        f.write(report)
    
    print(f"\nSaved report to {args.output}")
    
    # Exit with error code if checks failed
    exit(0 if results['all_passed'] else 1)
