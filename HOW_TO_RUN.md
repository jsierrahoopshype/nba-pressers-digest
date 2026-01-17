# How to Run NBA Pressers Digest

This tool scans NBA team YouTube channels, grabs press conference transcripts, and uses AI to find the 12 best quotes.

---

## Step 1: Install Python

If you don't have Python installed:
- **Mac**: Open Terminal and type: `brew install python`
- **Windows**: Download from https://www.python.org/downloads/ and run the installer (check "Add to PATH")

---

## Step 2: Download This Project

Open Terminal (Mac) or Command Prompt (Windows) and type:

```
git clone https://github.com/jsierrahoopshype/nba-pressers-digest.git
cd nba-pressers-digest
```

---

## Step 3: Install Required Libraries

```
pip install -r requirements.txt
```

---

## Step 4: Get an API Key (pick one)

**Option A: Anthropic (paid, best quality)**
1. Go to https://console.anthropic.com/settings/keys
2. Click "Create Key"
3. Copy the key

**Option B: Groq (FREE)**
1. Go to https://console.groq.com/keys
2. Sign up for free
3. Click "Create API Key"
4. Copy the key

---

## Step 5: Run the Pipeline

**Mac/Linux:**
```
export ANTHROPIC_API_KEY="paste-your-key-here"
cd scripts
python main.py --hours 48
```

**Windows:**
```
set ANTHROPIC_API_KEY=paste-your-key-here
cd scripts
python main.py --hours 48
```

(Replace `ANTHROPIC_API_KEY` with `GROQ_API_KEY` if using Groq)

---

## What Happens

1. Scans all 30 NBA team YouTube channels
2. Finds press conference videos from the last 48 hours
3. Downloads transcripts
4. AI picks the 12 best quotes/moments
5. Results saved to `output/` folder

---

## Output Files

After running, check the `output/` folder for:
- `videos.json` - List of videos found
- `transcripts.json` - Full transcripts
- `moments.json` - The best quotes picked by AI
- `review_summary.json` - Summary of top moments

---

## Troubleshooting

**"No videos found"** - Try increasing hours: `python main.py --hours 72`

**"No API key"** - Make sure you set the API key (Step 5)

**"Module not found"** - Run `pip install -r requirements.txt` again
