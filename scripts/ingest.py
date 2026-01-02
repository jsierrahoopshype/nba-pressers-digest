#!/usr/bin/env python3
"""
Ingest new press conference videos from NBA team YouTube channels
"""

import feedparser
from datetime import datetime, timedelta
from typing import List, Dict
import json
import re

# NBA Team YouTube Channel IDs
# Format: "Team Name": "Channel ID"
# To find channel ID: Go to channel page → View page source → search "channelId"
NBA_CHANNELS = {
    # Eastern Conference - Atlantic
    "Boston Celtics": "UCOVTNeLQpTY8awYaKq5N0PA",
    "Brooklyn Nets": "UC5E8Dz6fRh5k9N6HGNQVP5w",
    "New York Knicks": "UCqkmXLMFVhZ3T5GPK3Sfzgg",
    "Philadelphia 76ers": "UC_NHLsOVZQjq8IwCzHg3b3Q",
    "Toronto Raptors": "UCLpuZNEVlf4kKjmHJbE5HUw",
    
    # Eastern Conference - Central
    "Chicago Bulls": "UCVEi5gK6BOSpRvTdqCjZ3RA",
    "Cleveland Cavaliers": "UCQV2nk7DPD51_W6S1ORMPqw",
    "Detroit Pistons": "UCmI3LrCqXnL2SLqf5AhHH4g",
    "Indiana Pacers": "UC7v7WhN-_HQP10bPr6VhFMQ",
    "Milwaukee Bucks": "UCqkmXLMFVhZ3T5GPK3Sfzgq",
    
    # Eastern Conference - Southeast
    "Atlanta Hawks": "UCOXCZGWYxj7gV3TxPLEKqLg",
    "Charlotte Hornets": "UCLY32xN2kPJwHH_4WTP2ywQ",
    "Miami Heat": "UC0Ev6VvjjQtRWaUhNXHMSmg",
    "Orlando Magic": "UCHdDZmNHOJvYqmHQFpWP2FA",
    "Washington Wizards": "UCKrwNNfrZnZEGhDFEXYWcMg",
    
    # Western Conference - Northwest
    "Denver Nuggets": "UC4oN5G9L3HDVIL11vCRjQ0w",
    "Minnesota Timberwolves": "UCJh2ZLuVmKt-pSA3wPFO3LQ",
    "Oklahoma City Thunder": "UC9BLsYvNT3Kd0yI9KAqxMig",
    "Portland Trail Blazers": "UCKH_g5NHtq_x7UstbQBpD_A",
    "Utah Jazz": "UCqkdOPwLT_eWpT6rP--2-Tw",
    
    # Western Conference - Pacific
    "Golden State Warriors": "UCQGKXeHNW-v1wJn-hHJhmcg",
    "LA Clippers": "UCnLdLHJXwO7GDSQKpN9WMCQ",
    "Los Angeles Lakers": "UCvjPfBKz0T1ZR_XJl8V_8Qw",
    "Phoenix Suns": "UCKdLISl-7suZAU3oeL2WFTw",
    "Sacramento Kings": "UC-3cIZT_-Y1BvNQ2fHj8uLQ",
    
    # Western Conference - Southwest
    "Dallas Mavericks": "UC9QyG4Cp8xsw_cVXGPITCAQ",
    "Houston Rockets": "UCmP4F7vIPRYrINJu-d_04EQ",
    "Memphis Grizzlies": "UC4QJKC9ZfXk8BbJq1_bTLag",
    "New Orleans Pelicans": "UC_HJA1fN7BjlBpkLPjCNGLA",
    "San Antonio Spurs": "UC_Q7aYKBpzWCzRQzq6D5uFQ",
}

# Keywords that indicate press conference content
PRESS_CONFERENCE_KEYWORDS = [
    'press conference',
    'postgame',
    'post-game', 
    'post game',
    'pregame',
    'pre-game',
    'pre game',
    'media availability',
    'interview',
    'presser',
    'talks',
    'speaks',
    'reacts',
    'discusses',
    'on the',
    'addresses',
    'comments on',
]

# Keywords to exclude (game highlights, etc.)
EXCLUDE_KEYWORDS = [
    'highlights',
    'full game',
    'game recap',
    'top plays',
    'best plays',
    'dunk',
    'buzzer beater',
    'all-access',
    'behind the scenes',
    'practice',
    'workout',
]


def get_youtube_rss_url(channel_id: str) -> str:
    """Generate RSS feed URL for a YouTube channel"""
    return f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"


def is_press_conference(title: str) -> bool:
    """Check if video title indicates a press conference"""
    title_lower = title.lower()
    
    # Check for exclusion keywords first
    for keyword in EXCLUDE_KEYWORDS:
        if keyword in title_lower:
            return False
    
    # Check for press conference keywords
    for keyword in PRESS_CONFERENCE_KEYWORDS:
        if keyword in title_lower:
            return True
    
    return False


def extract_player_coach_name(title: str) -> str:
    """Try to extract the main person's name from title"""
    # Common patterns:
    # "LeBron James Postgame Press Conference"
    # "Coach Spoelstra on Win vs Celtics"
    # "Anthony Davis Talks Injury Update"
    
    # Remove common suffixes
    clean_title = re.sub(r'(press conference|postgame|pregame|interview|talks|speaks|on|discusses|addresses|reacts).*', '', title, flags=re.IGNORECASE)
    clean_title = clean_title.strip()
    
    # Take first 2-3 words as potential name
    words = clean_title.split()
    if len(words) >= 2:
        return ' '.join(words[:3])
    return clean_title


def get_new_videos(hours_back: int = 24, channels: Dict[str, str] = None) -> List[Dict]:
    """
    Fetch new press conference videos from all NBA team channels
    
    Args:
        hours_back: Number of hours to look back for new videos
        channels: Optional dict of {team_name: channel_id}. Uses NBA_CHANNELS if not provided.
    
    Returns:
        List of video dicts with team, title, video_id, url, published date
    """
    if channels is None:
        channels = NBA_CHANNELS
    
    cutoff = datetime.utcnow() - timedelta(hours=hours_back)
    new_videos = []
    
    print(f"Scanning {len(channels)} channels for videos from last {hours_back} hours...")
    
    for team, channel_id in channels.items():
        try:
            feed_url = get_youtube_rss_url(channel_id)
            feed = feedparser.parse(feed_url)
            
            if feed.bozo:  # Parse error
                print(f"  ⚠️  Error parsing feed for {team}")
                continue
            
            team_videos = 0
            for entry in feed.entries:
                # Parse published date
                try:
                    published = datetime(*entry.published_parsed[:6])
                except (AttributeError, TypeError):
                    continue
                
                # Check if recent enough
                if published < cutoff:
                    continue
                
                # Check if it's a press conference
                if not is_press_conference(entry.title):
                    continue
                
                # Extract video ID
                video_id = entry.get('yt_videoid', '')
                if not video_id:
                    # Try to extract from link
                    match = re.search(r'v=([a-zA-Z0-9_-]{11})', entry.link)
                    if match:
                        video_id = match.group(1)
                
                if not video_id:
                    continue
                
                video_data = {
                    'team': team,
                    'title': entry.title,
                    'video_id': video_id,
                    'url': entry.link,
                    'published': published.isoformat(),
                    'channel_id': channel_id,
                    'person': extract_player_coach_name(entry.title),
                }
                
                new_videos.append(video_data)
                team_videos += 1
            
            if team_videos > 0:
                print(f"  ✓ {team}: {team_videos} videos")
                
        except Exception as e:
            print(f"  ⚠️  Error fetching {team}: {str(e)}")
    
    # Sort by published date (newest first)
    new_videos.sort(key=lambda x: x['published'], reverse=True)
    
    return new_videos


def save_videos_to_file(videos: List[Dict], filepath: str = "data/videos.json"):
    """Save video list to JSON file"""
    import os
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    
    with open(filepath, 'w') as f:
        json.dump(videos, f, indent=2)
    print(f"Saved {len(videos)} videos to {filepath}")


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Ingest NBA press conference videos")
    parser.add_argument("--hours", type=int, default=24, help="Hours back to search")
    parser.add_argument("--output", type=str, default="data/videos.json", help="Output file path")
    
    args = parser.parse_args()
    
    videos = get_new_videos(hours_back=args.hours)
    
    print(f"\n{'='*50}")
    print(f"Found {len(videos)} press conference videos")
    print(f"{'='*50}")
    
    for i, video in enumerate(videos[:10], 1):
        print(f"{i}. [{video['team']}] {video['title']}")
    
    if len(videos) > 10:
        print(f"... and {len(videos) - 10} more")
    
    if args.output:
        save_videos_to_file(videos, args.output)
