# AI Clipper

A fully automated AI clipper. You press one button, **GET CLIPS**, and it:

1. **Analyzes what goes viral right now.** It pulls 1000+ fresh TikTok and Instagram Reels
   videos on every run and scores each one for virality: views, growth speed, reach beyond the
   creator's followers, engagement, and shares/saves. It then learns which hooks, lengths,
   words, hashtags and posting times separate viral videos from flops. The run stops if fewer
   than 1000 videos are available.
2. **Finds long-form YouTube videos and watches all of them.** It checks the channels you watch
   in real time (new uploads show up within minutes), YouTube's most-popular chart, and
   searches for long, high-view videos. It ranks them by growth, size and trend fit. For the
   top picks it downloads the full video, transcribes every word, and reads:
   - YouTube's **"most replayed" heatmap** (real viewer data),
   - **timestamps viewers quote in comments** ("23:14 had me dying"),
   - **loudness spikes** (laughter, shouting, heated moments).

   Claude then reads the entire transcript in overlapping windows and proposes clips.
3. **Runs a strict review.** Every candidate is snapped to full sentences, scored on all the
   signals above, and sent to a second, much harsher AI judge that also sees real frames from
   the clip. The judge rejects by default and only approves a clip when it's clearly strong.
   A clip is delivered only if it clears **both** the judge score and the combined score
   (82 and 70 out of 100 by default).
4. **Edits the approved clips** into 1080×1920 vertical videos at the level you choose:

| Level | What you get |
|---|---|
| **simple** | Clean 9:16 cut, readable captions, loudness normalized |
| **normal** | Crop that follows the speaker's face, bold pop-in captions, hook title, long pauses trimmed |
| **hard** | Jump cuts (pauses and "um"/"uh" removed), karaoke word-highlight captions, punch-in zooms on key words, colour grade |
| **professional** | Smooth speaker tracking, pattern-interrupt zooms, background music that ducks under speech, whoosh sfx on cuts, progress bar, 1.03× pacing |
| **extreme** | All of the above, plus a split screen with b-roll/gameplay, white flashes on peak moments, slow push-in, 1.07× pacing, highest encode quality |

Each clip comes with a thumbnail and a ready-to-paste caption with hashtags and a credit line
(`clip_XX.txt`). The full AI reasoning and scores are in `clip_XX.json`.

---

## Windows: just download and run

Download **AIClipper-Windows.zip** from this repository's Releases page (release "AI Clipper for Windows"),
unzip it, and double-click **AIClipper.exe**. No Python needed. On first launch it asks for your API keys
in Notepad and then opens the app in your browser. `START HERE.txt` in the zip has the details.

The exe is rebuilt automatically by GitHub Actions (`.github/workflows/build-windows.yml`) whenever the code changes.
To build it yourself on Windows: `pip install -r requirements.txt pyinstaller` then `pyinstaller AIClipper.spec`.

## Setup from source (about 10 minutes)

1. **Install Python 3.10+** from https://python.org. On Windows, tick "Add Python to PATH".
2. Open a terminal in this `ai-clipper` folder and run:
   ```bash
   pip install -r requirements.txt
   ```
   You don't need to install ffmpeg: a copy is bundled through `imageio-ffmpeg`.
3. **Add your keys.** Copy `.env.example` to `.env` and fill it in:
   | Key | What it does | Where to get it |
   |---|---|---|
   | `ANTHROPIC_API_KEY` | The AI that picks and judges clips (**required** for accurate results) | https://console.anthropic.com |
   | `APIFY_TOKEN` | Collects the TikTok and Instagram data for step 1 | https://apify.com (Settings → Integrations) |
   | `YOUTUBE_API_KEY` | Better discovery, stats and comment timestamps (optional but recommended) | Google Cloud Console → enable "YouTube Data API v3" → Credentials |
4. **Configure.** Copy `config.example.yaml` to `config.yaml`. The main settings:
   - `discovery.watch_channels`: YouTube channel IDs to watch in real time (they start with `UC`).
   - `discovery.search_queries`: the kinds of long videos to look for.
   - `trends.tiktok.hashtags` / `trends.instagram.hashtags`: the niche to study.
   - `editing.default_level`: your usual editing level.
5. **Optional assets** for the professional and extreme levels. These folders are skipped when empty:
   - `assets/music/`: royalty-free background tracks (.mp3/.wav)
   - `assets/sfx/`: whoosh/pop sounds
   - `assets/broll/`: satisfying clips or gameplay for the extreme split screen
   - `assets/fonts/`: .ttf fonts (set `editing.font` to the font's name)
6. Check everything:
   ```bash
   python -m clipper doctor
   ```

## Use it

```bash
python -m clipper
```
Open http://127.0.0.1:8000, pick an editing level, and press **GET CLIPS**. You'll see each
step's progress live, new uploads from your watched channels in the sidebar, and finished
clips with Play / Download / Copy caption buttons. Clips are saved in `output/`.

Other commands:
```bash
python -m clipper run --level extreme      # one full run from the terminal
python -m clipper watch                    # real-time upload watcher
python -m clipper watch --auto-clip        # ...that runs the whole pipeline on every new upload
```

## Tuning the strictness

In `config.yaml` → `analysis`:
- `judge_threshold` / `fused_threshold`: raise them for fewer, better clips; lower them for more clips.
- `whisper_model`: `small` is a good default. `medium` or `large-v3` transcribe more accurately
  (use a GPU, `whisper_device: cuda`).
- `weights`: how much each signal counts (AI, most-replayed, comments, loudness, trend fit).
- `judge_with_frames`: lets the judge see real frames, so it can reject slides, black screens,
  or the wrong speaker in frame.
- `llm.effort` / `llm.judge_effort`: how hard Claude thinks (`high` / `xhigh` by default; `max` is the most thorough).

## Troubleshooting

- **"Sign in to confirm you're not a bot"** from YouTube: set `analysis.cookies_from_browser: chrome`
  (or firefox/edge/brave/safari) so downloads use your logged-in browser session.
- **"Only N TikTok/Instagram videos available"**: set `APIFY_TOKEN` or raise
  `trends.fetch_per_platform`. You can also drop `.json` / `.csv` exports into
  `data/trend_imports/` (columns such as `id, caption, views, likes, comments, shares, duration,
  created_at, platform`). Collected data is kept, so your history builds up over time.
- **Slow transcription**: use `whisper_model: base`, or run on a machine with an NVIDIA GPU.
- **Nothing passed the strict review**: that's the filter working. The run moves on to the next
  video automatically. Lower the thresholds if you want more clips.

## Costs

- **Claude:** roughly one AI pass per 18 minutes of video, plus one judge pass per video.
  A 2-hour podcast is about 8 calls. Check current pricing at https://www.anthropic.com/pricing.
- **Apify:** billed per result by the TikTok and Instagram scraper actors (about 1200 results per run with the default settings).
- **YouTube Data API:** free daily quota. Each search costs 100 units; stats and comments cost 1 unit each.

## Before you post: rights and platform rules

- Reposting someone else's video needs their permission. Many big creators run official
  clipping programs, and those creators are the best ones to target. Each caption includes a
  credit line to the original video.
- Collecting TikTok/Instagram data runs through Apify under its terms, and downloading from
  YouTube is subject to YouTube's terms. Use this for your own or permitted content.
- Posting stays manual: you review each clip before it goes out.

## Tests

```bash
python -m pytest
```
The tests cover trend learning, clip selection with the strict judge, jump-cut timing,
captions, face-track planning, real ffmpeg renders, the web API, and a full end-to-end run.

## How it's built

```
clipper/
  trends/      step 1 - Apify/import collectors, virality scoring, lift tables, text model
  discovery/   step 2a - YouTube API, RSS watcher, yt-dlp fallback, ranking
  analysis/    step 2b - download, whisper transcript, audience signals, AI finder + strict judge
  editing/     step 3 - levels, jump cuts, face tracking (YuNet), ASS captions, ffmpeg render
  web/         the one-button web app
  pipeline.py  wires the steps together
```
