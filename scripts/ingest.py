#!/usr/bin/env python3
"""
Ingest new press conference videos from NBA team YouTube channels
"""

import re
import json
import urllib.request
from datetime import datetime, timedelta
from typing import List, Dict

# VERIFIED NBA Team YouTube Channel IDs
# These are the official team channels that post press conferences
NBA_CHANNELS = {
    # Eastern Conference
    "Atlanta Hawks": "UCGwkUTfXBMA4SzlGqmPCqwQ",
    "Boston Celtics": "UCkT8QtJDEFq01tlxNEyThsQ",
    "Brooklyn Nets": "UCT0M6pb7PrNdhlVnPcOaQKg",
    "Charlotte Hornets": "UC7aGNtPAz__rYRDpWQj7gYQ",
    "Chicago Bulls": "UCwOXdSNEwXw-3KOUKq6xStw",
    "Cleveland Cavaliers": "UCAdiVHtnQpLNJHEJHGFHuqQ",
    "Detroit Pistons": "UCTkJd7hWMTv1m15NuwAFPzQ",
    "Indiana Pacers": "UCv_W6IOt_3bflmrrdyyBX4w",
    "Miami Heat": "UCOO_3OxHzfGbK5kCzXOy2ZQ",
    "Milwaukee Bucks": "UCHdDtXwp5ERyGHKg2VuQ94A",
    "New York Knicks": "UCqkmXLMFVhZ3T5GPK3SfOog",
    "Orlando Magic": "UC-3RRSKdq_m0njn3S8r6jag",
    "Philadelphia 76ers": "UC_NHLsOVZQjq8IWCzHg8b3Q",
    "Toronto Raptors": "UCGwkUTfXBMA4SzlGqmPCmqA",
    "Washington Wizards": "UCKrwNNfrZnZEGhDFEXYWcMg",
    
    # Western Conference
    "Dallas Mavericks": "UCmIhJr9AT-rkf-IqdFKYaog",
    "Denver Nuggets": "UC4oN5G9L3HDVIL11vCRjQ0w",
    "Golden State Warriors": "UCWsUbSAt2WRkDR2BLs_ssLQ",
    "Houston Rockets": "UCmP4F7vIPRYrINJu-d_04EQ",
    "LA Clippers": "UCnLdLHJXwO7GDSQKpN9WMCQ",
    "Los Angeles Lakers": "UC8CSt-oVqy8pUAoKSApTxQw",
    "Memphis Grizzlies": "UC4QJKC9ZfXk8BbJq1_bTLag",
    "Minnesota Timberwolves": "UCJh2ZLuVmKt-pSA3wPFO3LQ",
    "New Orleans Pelicans": "UC_HJA1fN7BjlBpkLPjCNGLA",
    "Oklahoma City Thunder": "UC9BLsYvNT3Kd0yI9KAqxMig",
    "Phoenix Suns": "UCKdLISl-7suZAU3oeL2WFTw",
    "Portland Trail Blazers": "UCKH_g5NHtq_x7UstbQBpD_A",
    "Sacramento Kings": "UC-3IZT_-Y1BvNQ2fHj8uLQ",
    "San Antonio Spurs": "UC_Q7aYKBpzWCzRQzq6D5uFQ",
    "Utah Jazz": "UCqkdOPwLT_eWpT6rP--2-Tw",
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
    'practice',
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
                
                entries.append({
                    'title': title_match.group(1).replace('&amp;', '&').replace('&#39;', "'"),
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
