#!/usr/bin/env python3
"""
Score and identify best moments from press conference transcripts using LLM
"""

import os
import json
import re
from typing import List, Dict, Optional

# Try Anthropic first, fall back to Groq (free)
try:
    import anthropic
    ANTHROPIC_AVAILABLE = True
except ImportError:
    ANTHROPIC_AVAILABLE = False

try:
    from groq import Groq
    GROQ_AVAILABLE = True
except ImportError:
    GROQ_AVAILABLE = False


SCORING_PROMPT = """Analyze this NBA press conference transcript and identify the most interesting, newsworthy, or entertaining moments.

TEAM: {team}
PERSON: {person}
TITLE: {title}

TRANSCRIPT (with timestamps):
{transcript}

---

Identify 3-5 of the BEST moments from this press conference. Focus on:
- Breaking news (injuries, trade hints, lineup changes)
- Controversial or surprising statements
- Emotional moments (frustration, excitement, humor)
- Quotable soundbites that would go viral
- Insights about team dynamics or strategy
- Responses to tough questions

For EACH moment, provide:
1. start_time: Timestamp where the moment starts (format: "M:SS")
2. end_time: Timestamp where it ends (keep clips 15-45 seconds max)
3. headline: A compelling headline (8 words MAX, attention-grabbing)
4. why_it_matters: One sentence explaining the significance
5. score: Interest score from 1-10 (10 = must-include, viral potential)
6. category: One of [breaking_news, controversy, emotion, soundbite, insight, humor]

IMPORTANT:
- Timestamps must match the transcript exactly
- Keep clips between 15-45 seconds (not too short, not too long)
- Headlines should be punchy and shareable
- Be selective - only flag genuinely interesting moments

Return your response as a JSON array:
[
  {{
    "start_time": "2:34",
    "end_time": "3:15",
    "headline": "LeBron Hints at Retirement Plans",
    "why_it_matters": "First time he's directly addressed timeline for career end",
    "score": 9,
    "category": "breaking_news"
  }}
]

If there are NO interesting moments worth highlighting, return an empty array: []

Return ONLY the JSON array, no other text."""


def get_llm_client():
    """Get available LLM client (Anthropic preferred, Groq as fallback)"""
    
    if ANTHROPIC_AVAILABLE and os.environ.get('ANTHROPIC_API_KEY'):
        return 'anthropic', anthropic.Anthropic()
    
    if GROQ_AVAILABLE and os.environ.get('GROQ_API_KEY'):
        return 'groq', Groq()
    
    raise RuntimeError(
        "No LLM client available. Set either ANTHROPIC_API_KEY or GROQ_API_KEY. "
        "Groq is free: https://console.groq.com/keys"
    )


def score_transcript_anthropic(client, transcript_data: Dict) -> List[Dict]:
    """Score moments using Anthropic Claude"""
    
    prompt = SCORING_PROMPT.format(
        team=transcript_data.get('team', 'Unknown'),
        person=transcript_data.get('person', 'Unknown'),
        title=transcript_data.get('title', 'Press Conference'),
        transcript=transcript_data.get('formatted', '')[:15000]  # Limit length
    )
    
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}]
    )
    
    response_text = response.content[0].text
    return parse_moments_json(response_text)


def score_transcript_groq(client, transcript_data: Dict) -> List[Dict]:
    """Score moments using Groq (free Llama 3.1)"""
    
    prompt = SCORING_PROMPT.format(
        team=transcript_data.get('team', 'Unknown'),
        person=transcript_data.get('person', 'Unknown'),
        title=transcript_data.get('title', 'Press Conference'),
        transcript=transcript_data.get('formatted', '')[:12000]  # Groq has lower limits
    )
    
    response = client.chat.completions.create(
        model="llama-3.1-70b-versatile",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=2000,
        temperature=0.3
    )
    
    response_text = response.choices[0].message.content
    return parse_moments_json(response_text)


def parse_moments_json(response_text: str) -> List[Dict]:
    """Parse JSON moments from LLM response"""
    
    # Clean up response - remove markdown code blocks if present
    cleaned = response_text.strip()
    cleaned = re.sub(r'^```json\s*', '', cleaned)
    cleaned = re.sub(r'^```\s*', '', cleaned)
    cleaned = re.sub(r'\s*```$', '', cleaned)
    
    try:
        moments = json.loads(cleaned)
        if isinstance(moments, list):
            return moments
        return []
    except json.JSONDecodeError as e:
        print(f"    ⚠️  Failed to parse moments JSON: {e}")
        print(f"    Response was: {response_text[:200]}...")
        return []


def timestamp_to_seconds(timestamp: str) -> float:
    """Convert MM:SS or H:MM:SS to seconds"""
    parts = timestamp.split(':')
    if len(parts) == 2:
        return int(parts[0]) * 60 + float(parts[1])
    elif len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    return 0


def validate_moment(moment: Dict, transcript_duration: float) -> bool:
    """Validate that a moment has valid timestamps and data"""
    
    required_fields = ['start_time', 'end_time', 'headline', 'score']
    for field in required_fields:
        if field not in moment:
            return False
    
    start = timestamp_to_seconds(moment['start_time'])
    end = timestamp_to_seconds(moment['end_time'])
    
    # Check valid time range
    if start >= end:
        return False
    if start < 0 or end > transcript_duration + 10:  # Allow small buffer
        return False
    
    # Check clip duration (15-60 seconds)
    duration = end - start
    if duration < 10 or duration > 90:
        return False
    
    return True


def score_all_transcripts(transcripts: Dict[str, Dict]) -> Dict[str, List[Dict]]:
    """
    Score moments for all transcripts
    
    Args:
        transcripts: Dict mapping video_id to transcript data
    
    Returns:
        Dict mapping video_id to list of scored moments
    """
    
    provider, client = get_llm_client()
    print(f"Using {provider.upper()} for moment scoring")
    
    all_moments = {}
    
    for video_id, transcript_data in transcripts.items():
        print(f"  Scoring: {transcript_data.get('title', video_id)[:50]}...")
        
        try:
            if provider == 'anthropic':
                moments = score_transcript_anthropic(client, transcript_data)
            else:
                moments = score_transcript_groq(client, transcript_data)
            
            # Validate and enrich moments
            duration = transcript_data.get('duration_seconds', 0)
            valid_moments = []
            
            for moment in moments:
                if validate_moment(moment, duration):
                    # Add metadata
                    moment['video_id'] = video_id
                    moment['team'] = transcript_data.get('team', '')
                    moment['source_title'] = transcript_data.get('title', '')
                    moment['source_url'] = transcript_data.get('url', '')
                    moment['person'] = transcript_data.get('person', '')
                    moment['start_seconds'] = timestamp_to_seconds(moment['start_time'])
                    moment['end_seconds'] = timestamp_to_seconds(moment['end_time'])
                    moment['duration'] = moment['end_seconds'] - moment['start_seconds']
                    valid_moments.append(moment)
            
            all_moments[video_id] = valid_moments
            print(f"    ✓ Found {len(valid_moments)} valid moments")
            
        except Exception as e:
            print(f"    ⚠️  Error scoring {video_id}: {str(e)}")
            all_moments[video_id] = []
    
    return all_moments


def get_top_moments(all_moments: Dict[str, List[Dict]], max_count: int = 12) -> List[Dict]:
    """Get top N moments across all videos, sorted by score"""
    
    # Flatten all moments
    flat_moments = []
    for video_id, moments in all_moments.items():
        flat_moments.extend(moments)
    
    # Sort by score (descending)
    flat_moments.sort(key=lambda x: x.get('score', 0), reverse=True)
    
    # Return top N
    return flat_moments[:max_count]


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Score press conference moments")
    parser.add_argument("--input", type=str, default="data/transcripts.json", help="Input transcripts JSON")
    parser.add_argument("--output", type=str, default="data/moments.json", help="Output file")
    parser.add_argument("--top", type=int, default=12, help="Number of top moments to select")
    
    args = parser.parse_args()
    
    if not os.path.exists(args.input):
        print(f"Input file not found: {args.input}")
        exit(1)
    
    with open(args.input) as f:
        transcripts = json.load(f)
    
    print(f"Scoring {len(transcripts)} transcripts...")
    all_moments = score_all_transcripts(transcripts)
    
    # Get top moments
    top_moments = get_top_moments(all_moments, max_count=args.top)
    
    print(f"\n{'='*50}")
    print(f"TOP {len(top_moments)} MOMENTS:")
    print(f"{'='*50}")
    
    for i, moment in enumerate(top_moments, 1):
        print(f"{i}. [{moment['team']}] {moment['headline']} (score: {moment['score']})")
        print(f"   {moment['why_it_matters']}")
        print(f"   Time: {moment['start_time']} - {moment['end_time']} ({moment['duration']:.0f}s)")
        print()
    
    # Save all moments
    output_data = {
        'all_moments': all_moments,
        'top_moments': top_moments
    }
    
    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, 'w') as f:
        json.dump(output_data, f, indent=2)
    
    print(f"Saved moments to {args.output}")
