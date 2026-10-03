# NBA Press Conference Daily Digest

Automated pipeline to create daily video digests of the best moments from NBA team press conferences.

> **Note:** this original pipeline's GitHub Actions workflow (`daily-digest.yml`) has been removed; the
> scripts in `scripts/` are kept for reference and can still be run by hand. The pipeline that runs today
> is [`pressers_v2/`](pressers_v2/README.md) (workflow `pressers-v2.yml`).

## Features

- 📥 **Automatic ingestion** from all 30 NBA team YouTube channels
- 📝 **Free transcription** using YouTube's auto-generated captions
- 🎯 **AI-powered moment scoring** identifies the most newsworthy clips
- 🎬 **Automatic video compilation** with attribution overlays
- ✅ **QA checks** before publishing
- 🚀 **GitHub Actions** for zero-maintenance daily runs

## Cost

| Component | Tool | Monthly Cost |
|-----------|------|--------------|
| Compute | GitHub Actions | $0 (2,000 min free) |
| Transcription | YouTube captions | $0 |
| AI Scoring | Claude Sonnet | ~$9/mo |
| Video Processing | yt-dlp + FFmpeg | $0 |
| **Total** | | **~$9/month** |

### Zero-Cost Alternative

Use Groq instead of Anthropic for moment scoring (free tier: 14k tokens/min):
```bash
export GROQ_API_KEY=your_key_here  # Get free at console.groq.com
```

## Quick Start

### 1. Clone and setup

```bash
git clone https://github.com/YOUR_USERNAME/nba-pressers-automation.git
cd nba-pressers-automation
pip install -r requirements.txt
```

### 2. Install system dependencies

```bash
# Ubuntu/Debian
sudo apt-get install ffmpeg

# macOS
brew install ffmpeg
```

### 3. Set API key

```bash
# Option A: Anthropic (paid, better quality)
export ANTHROPIC_API_KEY=sk-ant-xxx

# Option B: Groq (free tier available)
export GROQ_API_KEY=gsk_xxx
```

### 4. Run locally

```bash
cd scripts
python main.py --hours 24 --max-clips 12
```

### 5. Review output

```
output/
├── digest.mp4              # Final compiled video
├── digest_metadata.json    # YouTube/Twitter metadata
├── qa_report.txt          # QA check results
└── clips/
    └── processed_clips.json # Individual clip metadata
```

## GitHub Actions Setup

### 1. Add secrets

Go to Settings → Secrets → Actions and add:

**Required (choose one):**
- `ANTHROPIC_API_KEY` - Your Anthropic API key
- `GROQ_API_KEY` - Your Groq API key (free)

**Optional (for auto-publishing):**
- `YOUTUBE_CLIENT_ID`
- `YOUTUBE_CLIENT_SECRET`
- `YOUTUBE_REFRESH_TOKEN`
- `TWITTER_API_KEY`
- `TWITTER_API_SECRET`
- `TWITTER_ACCESS_TOKEN`
- `TWITTER_ACCESS_SECRET`

### 2. Run it

The scheduled workflow for this pipeline has been removed. Run `python scripts/main.py` by hand
(see Quick Start), or use the current pipeline in `pressers_v2/`.

### 3. Review and publish

After each run:
1. Download artifacts from the workflow run
2. Review `qa_report.txt`
3. Watch `digest.mp4`
4. If approved, upload to YouTube/Twitter manually (or enable auto-publish)

## Pipeline Stages

```
┌─────────────────────────────────────────────────────────────────┐
│                                                                 │
│  1. INGEST          2. TRANSCRIBE       3. SCORE               │
│  ─────────────      ─────────────       ─────────              │
│  RSS feeds from     Fetch YouTube       LLM identifies         │
│  30 NBA team        auto-captions       best moments           │
│  channels           (free)              (15-45s clips)         │
│                                                                 │
│  4. CLIP            5. COMPILE          6. QA + PUBLISH        │
│  ─────────          ─────────           ─────────────          │
│  Download clips     Concatenate into    Quality checks,        │
│  via yt-dlp,        single video,       human review,          │
│  add overlays       generate metadata   upload                 │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

## Legal Considerations

This tool is designed for **legally defensible fair use**:

✅ **What this does:**
- Short clips (15-45 seconds, never full answers)
- Clear attribution (team name overlay on every clip)
- Transformative purpose (digest format with curation)
- Links to original sources in description

⚠️ **You should also:**
- Add your own commentary overlay/voiceover
- Include analysis or context graphics
- Keep clips brief and newsworthy
- Respond promptly to any takedown requests

See the full blueprint document for detailed legal analysis.

## Customization

### Change channel list

Edit `scripts/ingest.py` → `NBA_CHANNELS` dict.

### Adjust scoring criteria

Edit the `SCORING_PROMPT` in `scripts/score_moments.py`.

### Change clip duration limits

Edit `validate_moment()` in `scripts/score_moments.py`.

### Add intro/outro

Place `intro.mp4` and `outro.mp4` in `assets/` folder.

## Troubleshooting

### "No transcript available"

Some videos disable captions. The pipeline will skip these automatically.

### "yt-dlp error"

Update yt-dlp: `pip install -U yt-dlp`

### "Rate limit exceeded"

If using Groq free tier, reduce batch size or add delays.

### FFmpeg overlay fails

The pipeline will use the original clip without overlays if this fails.

## Project Structure

```
nba-pressers-automation/
├── .github/
│   └── workflows/
│       └── pressers-v2*.yml    # GitHub Actions workflows (the current pipeline, pressers_v2/)
├── scripts/
│   ├── main.py                 # Main orchestrator
│   ├── ingest.py               # RSS feed ingestion
│   ├── transcribe.py           # YouTube caption fetching
│   ├── score_moments.py        # LLM moment scoring
│   ├── clip_videos.py          # Video downloading/processing
│   ├── compile_digest.py       # Final video compilation
│   └── qa_checks.py            # Quality assurance
├── data/                       # Intermediate data (gitignored)
├── output/                     # Final outputs (gitignored)
├── assets/                     # Intro/outro videos (optional)
├── requirements.txt
└── README.md
```

## Contributing

PRs welcome! Especially for:
- Better moment scoring prompts
- Additional publishing platforms
- Thumbnail generation
- Voice-over integration

## License

MIT License - Use freely, attribution appreciated.

---

Built for sports media automation. Questions? Open an issue.
