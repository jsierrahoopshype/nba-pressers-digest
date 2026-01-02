#!/usr/bin/env python3
"""
Ingest new press conference videos from NBA team YouTube channels
"""

import re
import json
import urllib.request
from datetime import datetime, timedelta
from typing import List, Dict

# VERIFIED NBA Team YouTube Channel IDs (from HoopsHype spreadsheet)
NBA_CHANNELS = {
    "Atlanta Hawks": "UCpfcwELvR1wtcRJ0UxNXHYw",
    "Boston Celtics": "UCMfT9dr6xC_RIWoA9hI0meQ",
    "Brooklyn Nets": "UCL7XnFZqcLjRHiS4xX4_vgA",
    "Charlotte Hornets": "UCiKXa2hObEziEZFkFx08i9A",
    "Chicago Bulls": "UCvZi1jVVZ2yq0k5kkjzmuGw",
    "Cleveland Cavaliers": "UCOdS-I1sYkKWhtTjMUWP_TA",
    "Dallas Mavericks": "UCZywaCS_y9YOSSAC9z3dIeg",
    "Denver Nuggets": "UCl8hzdP5wVlhuzNG3WCJa1w",
    "Detroit Pistons": "UC-z5VCIFLG6tximfWzzlSSQ",
    "Golden State Warriors": "UCeYc_OjHs3QNxIjti2whKzg",
    "Houston Rockets": "UCVD7l69MVGFq_wzQvbk9HbQ",
    "Indiana Pacers": "UCUQDCnAwU-35cOo8WCzg6zA",
    "LA Clippers": "UCoK6pw3iIVF9WAWnQd3hj-g",
    "Los Angeles Lakers": "UC8CSt-oVqy8pUAoKSApTxQw",
    "Memphis Grizzlies": "UCCK5EpWKYrAmILfaZThCV-Q",
    "Miami Heat": "UC8bZbiKoPNRi3taABIaFeBw",
    "Milwaukee Bucks": "UCRZDEVva3Z8h_Q0VetTgDUA",
    "Minnesota Timberwolves": "UCXWDN5NKVFgnPt25CMh98Cg",
    "New Orleans Pelicans": "UCHvG7tf62PwI04ZRfoptRSw",
    "New York Knicks": "UC0hb8f0OXHEzDrJDUq-YVVw",
    "Oklahoma City Thunder": "UCpXdQhy6kb5CTD8hKlmOL3w",
    "Orlando Magic": "UCxHFH-yfbhUrsWY4prPx3oQ",
    "Philadelphia 76ers": "UC5qJUyng_ezl0TVjVJFqtfQ",
    "Phoenix Suns": "UCLxlWVVHz2a8SdCfxzVXzQw",
    "Portland Trail Blazers": "UCTenKHt0h3VjdMvRWP6Lbvw",
    "Sacramento Kings": "UCSgFigczGdNMilV1K23JgUQ",
    "San Antonio Spurs": "UCEZHE-0CoHqeL1LGFa2EmQw",
    "Toronto Raptors": "UCYBFE432C2AmNRDGEXE4uVg",
    "Utah Jazz": "UCv9iSdeI9IzWfV8yTDsMYWA",
    "Washington Wizards": "UCT5g1W7HHYiG8wOZEYgYXLw",
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
    'talks to media',
    'speaks to media',
    'talks',
    'speaks',
    'reacts',
    'discusses',
    'addresses',
    'shootaround',
]

# Keywords to exclude
EXCLUDE_KEYWORDS = [
    'highlights',
    'full game',
    'game recap',
    'top plays',
    'best plays',
    'buzzer beater',
    'all-access',
    'behind the scenes',
    'mix',
    'hype video',
    'promo',
    'trailer',
    'dunk',
    'block',
    'assist',
]


def fetch_url(url: str, timeout: int = 15) -> str:
    """Fetch URL content with proper headers"""
    try:
        req = urllib.request.Request(
            url, 
            headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Accept': 'application/xml, text/xml, */*',
            }
        )
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.read().decode('utf-8')
    except Exception as e:
        return None


def parse_rss_entries(xml_content: str) -> List[Dict]:
    """Parse YouTube RSS XML to extract video entries"""
    entries = []
    
    # Find all <entry> blocks
    entry_pattern = r'<entry>(.*?)</entry>'
    entries_raw = re.findall(entry_pattern, xml_content, re.DOTALL)
    
    for entry_xml in entries_raw:
        try:
            title_match = re.search(r'<title>(.*?)</title>', entry_xml)
            video_id_match = re.search(r'<yt:videoId>(.*?)</yt:videoId>', entry_xml)
            published_match = re.search(r'<published>(.*?)</published>', entry_xml)
            link_match = re.search(r'<link rel="alternate" href="([^"]+)"', entry_xml)
            
            if title_match and video_id_match and published_match:
                pub_str = published_match.group(1)
                # Parse ISO format date
                pub_date = datetime.fromisoformat(pub_str.replace('Z', '+00:00'))
                
                # Clean up HTML entities in title
                title = title_match.group(1)
                title = title.replace('&amp;', '&')
                title = title.replace('&#39;', "'")
                title = title.replace('&quot;', '"')
                
                entries.append({
                    'title': title,
                    'video_id': video_id_match.group(1),
                    'published': pub_date,
                    'url': link_match.group(1) if link_match else f"https://www.youtube.com/watch?v={video_id_match.group(1)}"
                })
        except Exception:
            continue
    
    return entries


def is_press_conference(title: str) -> bool:
    """Check if video title indicates a press conference"""
    title_lower = title.lower()
    
    # Exclude first
    for keyword in EXCLUDE_KEYWORDS:
        if keyword in title_lower:
            return False
    
    # Check for press conference keywords
    for keyword in PRESS_CONFERENCE_KEYWORDS:
        if keyword in title_lower:
            return True
    
    return False


def extract_person_name(title: str) -> str:
    """Try to extract the speaker's name from title"""
    clean_title = re.sub(
        r'(press conference|postgame|pregame|interview|talks|speaks|on|discusses|addresses|reacts|media availability|shootaround).*', 
        '', title, flags=re.IGNORECASE
    )
    clean_title = clean_title.strip()
    words = clean_title.split()
    if len(words) >= 2:
        return ' '.join(words[:3])
    return clean_title


def get_new_videos(hours_back: int = 24, channels: Dict[str, str] = None) -> List[Dict]:
    """
    Fetch new press conference videos from NBA team channels
    """
    if channels is None:
        channels = NBA_CHANNELS
    
    # Calculate cutoff time
    cutoff = datetime.utcnow() - timedelta(hours=hours_back)
    new_videos = []
    
    print(f"Scanning {len(channels)} channels for videos from last {hours_back} hours...")
    
    for team, channel_id in channels.items():
        try:
            # Fetch RSS feed
            feed_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
            xml_content = fetch_url(feed_url)
            
            if not xml_content:
                print(f"  ⚠️  Could not fetch feed for {team}")
                continue
            
            # Check if we got valid XML
            if '<feed' not in xml_content:
                print(f"  ⚠️  Invalid feed for {team}")
                continue
            
            # Parse entries
            entries = parse_rss_entries(xml_content)
            team_videos = 0
            
            for entry in entries:
                # Make timezone-naive for comparison
                pub_date = entry['published'].replace(tzinfo=None)
                
                if pub_date < cutoff:
                    continue
                
                if not is_press_conference(entry['title']):
                    continue
                
                video_data = {
                    'team': team,
                    'title': entry['title'],
                    'video_id': entry['video_id'],
                    'url': entry['url'],
                    'published': entry['published'].isoformat(),
                    'channel_id': channel_id,
                    'person': extract_person_name(entry['title']),
                }
                
                new_videos.append(video_data)
                team_videos += 1
            
            if team_videos > 0:
                print(f"  ✓ {team}: {team_videos} videos")
                
        except Exception as e:
            print(f"  ⚠️  Error with {team}: {str(e)}")
    
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
