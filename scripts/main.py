#!/usr/bin/env python3
"""
NBA Press Conference Digest - Main Orchestrator
Runs the full pipeline: ingest → transcribe → score → clip → compile → review
"""

import os
import json
import argparse
from datetime import datetime
from pathlib import Path

from ingest import get_new_videos
from transcribe import process_transcripts
from score_moments import score_all_transcripts
from clip_videos import download_and_process_clips
from compile_digest import compile_final_video, generate_metadata
from qa_checks import run_qa_checks

OUTPUT_DIR = Path("output")
DATA_DIR = Path("data")


def run_pipeline(hours_back=24, max_clips=12, auto_publish=False):
    """Run the complete digest generation pipeline"""
    
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = OUTPUT_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    
    print(f"\n{'='*60}")
    print(f"NBA Pressers Digest Pipeline - Run {run_id}")
    print(f"{'='*60}\n")
    
    # Stage 1: Ingest new videos
    print("📥 Stage 1: Ingesting new videos...")
    videos = get_new_videos(hours_back=hours_back)
    print(f"   Found {len(videos)} press conference videos")
    
    if not videos:
        print("   ⚠️  No new videos found. Exiting.")
        return None
    
    # Save video list
    with open(run_dir / "videos.json", "w") as f:
        json.dump(videos, f, indent=2, default=str)
    
    # Stage 2: Fetch transcripts
    print("\n📝 Stage 2: Fetching transcripts...")
    transcripts = process_transcripts(videos)
    print(f"   Got transcripts for {len(transcripts)} videos")
    
    with open(run_dir / "transcripts.json", "w") as f:
        json.dump(transcripts, f, indent=2)
    
    # Stage 3: Score moments
    print("\n🎯 Stage 3: Scoring best moments...")
    scored_moments = score_all_transcripts(transcripts)
    
    # Flatten and sort by score
    all_moments = []
    for video_id, moments in scored_moments.items():
        for moment in moments:
            moment['video_id'] = video_id
            all_moments.append(moment)
    
    all_moments.sort(key=lambda x: x.get('score', 0), reverse=True)
    top_moments = all_moments[:max_clips]
    
    print(f"   Selected top {len(top_moments)} moments")
    
    with open(run_dir / "moments.json", "w") as f:
        json.dump(top_moments, f, indent=2)
    
    # Stage 4: Download and process clips
    print("\n🎬 Stage 4: Downloading and processing clips...")
    clips_dir = run_dir / "clips"
    clips_dir.mkdir(exist_ok=True)
    
    processed_clips = download_and_process_clips(top_moments, clips_dir)
    print(f"   Processed {len(processed_clips)} clips")
    
    # Stage 5: Compile final video
    print("\n🎥 Stage 5: Compiling final digest...")
    final_video_path = run_dir / "digest.mp4"
    compile_final_video(processed_clips, final_video_path)
    
    # Generate metadata
    metadata = generate_metadata(processed_clips, datetime.now())
    with open(run_dir / "metadata.json", "w") as f:
        json.dump(metadata, f, indent=2)
    
    # Stage 6: QA Checks
    print("\n✅ Stage 6: Running QA checks...")
    qa_results = run_qa_checks(processed_clips, final_video_path)
    
    with open(run_dir / "qa_results.json", "w") as f:
        json.dump(qa_results, f, indent=2)
    
    # Generate human review summary
    review_summary = {
        "run_id": run_id,
        "video_count": len(videos),
        "clips_selected": len(processed_clips),
        "total_duration_seconds": sum(c.get('duration', 0) for c in processed_clips),
        "qa_passed": qa_results.get('all_passed', False),
        "qa_warnings": qa_results.get('warnings', []),
        "clips": [
            {
                "team": c.get('team'),
                "headline": c.get('headline'),
                "duration": c.get('duration'),
                "score": c.get('score')
            }
            for c in processed_clips
        ],
        "youtube_title": metadata.get('title'),
        "youtube_description": metadata.get('description'),
        "twitter_text": metadata.get('twitter_text'),
        "final_video": str(final_video_path),
        "ready_to_publish": qa_results.get('all_passed', False)
    }
    
    with open(run_dir / "review_summary.json", "w") as f:
        json.dump(review_summary, f, indent=2)
    
    # Print summary
    print(f"\n{'='*60}")
    print("📋 REVIEW SUMMARY")
    print(f"{'='*60}")
    print(f"Total clips: {len(processed_clips)}")
    print(f"Total duration: {review_summary['total_duration_seconds']//60}:{review_summary['total_duration_seconds']%60:02d}")
    print(f"QA Status: {'✅ PASSED' if qa_results.get('all_passed') else '⚠️ NEEDS REVIEW'}")
    print(f"\nClips included:")
    for i, clip in enumerate(review_summary['clips'], 1):
        print(f"  {i}. [{clip['team']}] {clip['headline']} ({clip['duration']}s)")
    
    if qa_results.get('warnings'):
        print(f"\n⚠️ Warnings:")
        for warning in qa_results['warnings']:
            print(f"  - {warning}")
    
    print(f"\nOutput directory: {run_dir}")
    print(f"Final video: {final_video_path}")
    
    if auto_publish and qa_results.get('all_passed'):
        print("\n🚀 Auto-publishing enabled and QA passed...")
        # Import and run publish functions here
        # from publish import publish_youtube, publish_twitter
        # publish_youtube(final_video_path, metadata)
        # publish_twitter(final_video_path, metadata)
        print("   ⚠️  Auto-publish not implemented - review and publish manually")
    
    return review_summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate NBA Pressers Digest")
    parser.add_argument("--hours", type=int, default=24, help="Hours back to search for videos")
    parser.add_argument("--max-clips", type=int, default=12, help="Maximum clips to include")
    parser.add_argument("--auto-publish", action="store_true", help="Auto-publish if QA passes")
    
    args = parser.parse_args()
    
    run_pipeline(
        hours_back=args.hours,
        max_clips=args.max_clips,
        auto_publish=args.auto_publish
    )
