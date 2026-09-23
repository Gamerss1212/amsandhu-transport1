# AI Clipper

A fully automated AI clipper. You press one button, **GET CLIPS**, and it:

1. **Analyzes what goes viral right now.** It pulls 350+ fresh short-form videos (YouTube Shorts
   for free; TikTok and Instagram Reels too if you add the optional Apify token) and scores each one
   for virality: views, growth speed, reach beyond the creator's followers, engagement, and
   shares/saves. It then learns which hooks, lengths, words, hashtags and posting times separate
   viral videos from flops. The run stops if fewer than 350 videos are available.
2. **Finds long-form YouTube videos and watches all of them.** It checks the channels you watch
   in real time (new uploads show up within minutes), YouTube's most-popular chart, and
   searches for long, high-view videos. It ranks them by growth, size and trend fit. For the
   top picks it downloads the full video, transcribes every word, and reads:
   - YouTube's **"most replayed" heatmap** (real viewer data),
   - **timestamps viewers quote in comments** ("23:14 had me dying"),
   - **loudness spikes** (laughter, shouting, heated moments).

   The built-in judge then tries every sentence of the transcript as a clip start, at several
   lengths, and scores each one on hook, standalone start, clean ending/payoff, intensity,
   pace and length.
3. **Runs a strict review.** Every candidate is snapped to full sentences and scored on all the
   signals above. Clips that start or end mid-thought, or have dead air, are rejected outright,
   and only clips that clear both the content and combined thresholds are delivered.
   With the optional Claude key, Claude also reads the transcript and a second, harsher Claude
   judge looks at real frames before approving.
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
unzip it, and double-click **AIClipper.exe**. No Python and no keys needed: it opens the app in your
browser. Choose **Find videos for me** or **Use my videos** (paste links/files), pick how many clips you
want and an editing style, and press the big button.
`START HERE.txt` in the zip has the step-by-step guide.

The exe is rebuilt automatically by GitHub Actions (`.github/workflows/build-windows.yml`) whenever the code changes.
To build it yourself on Windows: `pip install -r requirements.txt pyinstaller` then `pyinstaller AIClipper.spec`.

## Setup from source (about 10 minutes)

1. **Install Python 3.10+** from https://python.org. On Windows, tick "Add Python to PATH".
2. Open a terminal in this `ai-clipper` folder and run:
   ```bash
   pip install -r requirements.txt
   ```
   You don't need to install ffmpeg: a copy is bundled through `imageio-ffmpeg`.
3. **Keys: none needed.** Everything works for free. Optional extras, in `.env` (copy `.env.example`):
   | Key | Extra it adds | Where to get it |
   |---|---|---|
   | `ANTHROPIC_API_KEY` | Claude as an additional AI judge | https://console.anthropic.com |
   | `APIFY_TOKEN` | Real TikTok and Instagram data in step 1 | https://apify.com (Settings → Integrations) |
   | `YOUTUBE_API_KEY` | Faster search and more stats | Google Cloud Console → enable "YouTube Data API v3" → Credentials |
4. **Configure.** Copy `config.example.yaml` to `config.yaml`. The main settings:
   - `discovery.watch_channels`: YouTube channel IDs to watch in real time (they start with `UC`).
   - `discovery.search_queries`: the kinds of long videos to look for.
   - `trends.tiktok.hashtags` / `trends.instagram.hashtags`: the niche to study.
   - `editing.default_level`: your usual editing level.
5. **Your own assets (optional)** for the professional and extreme levels. When these folders are empty,
   built-in royalty-free music, a whoosh and animated b-roll are used instead:
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
python -m clipper clip "https://youtu.be/..."  # clip one video you choose (link or file)
python -m clipper watch                    # real-time upload watcher
python -m clipper watch --auto-clip        # ...that runs the whole pipeline on every new upload
```

## Tuning the strictness

In `config.yaml` → `analysis`:
- `local_content_threshold` / `local_fused_threshold`: strictness of the free built-in judge.
  Raise them for fewer, better clips; lower them for more.
- `judge_threshold` / `fused_threshold`: the same for the optional Claude judge.
- `whisper_model`: `small` is a good default. `medium` or `large-v3` transcribe more accurately
  (use a GPU, `whisper_device: cuda`).
- `weights`: how much each signal counts (AI, most-replayed, comments, loudness, trend fit).
- `judge_with_frames`: lets the judge see real frames, so it can reject slides, black screens,
  or the wrong speaker in frame.
- `llm.effort` / `llm.judge_effort`: how hard Claude thinks (`high` / `xhigh` by default; `max` is the most thorough).

## Troubleshooting

- **"Sign in to confirm you're not a bot"** from YouTube: sign in to YouTube in your browser. The app
  automatically retries with the YouTube login from Firefox, Edge, Chrome, Brave, Opera or Vivaldi.
  Chrome/Edge lock their cookie file while open, so close them fully or use Firefox. You can also set
  `analysis.cookies_from_browser: firefox` (or point `cookies_file` at an exported cookies.txt).
- **"Only N short videos available"**: check your internet connection, add more channels under
  `trends.free.youtube_channels`, or just run again (collected videos add up). You can also drop `.json` / `.csv` exports into
  `data/trend_imports/` (columns such as `id, caption, views, likes, comments, shares, duration,
  created_at, platform`). Collected data is kept, so your history builds up over time.
- **Slow transcription**: use `whisper_model: base`, or run on a machine with an NVIDIA GPU.
- **Nothing passed the strict review**: that's the filter working. The run moves on to the next
  video automatically. Lower the thresholds if you want more clips.

## Costs

**Free by default.** Only the optional extras cost anything:
- **Claude (optional):** roughly one AI pass per 18 minutes of video, plus one judge pass per video.
  Check current pricing at https://www.anthropic.com/pricing.
- **Apify (optional):** billed per result by the TikTok and Instagram scraper actors.
- **YouTube Data API (optional):** free daily quota.

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
  trends/      step 1 - free YouTube Shorts collector, optional Apify, virality scoring, text model
  discovery/   step 2a - YouTube API, RSS watcher, yt-dlp fallback, ranking
  analysis/    step 2b - download, whisper transcript, audience signals, built-in judge (+ optional Claude)
  editing/     step 3 - levels, jump cuts, face tracking (YuNet), ASS captions, ffmpeg render
  web/         the one-button web app
  pipeline.py  wires the steps together
```
