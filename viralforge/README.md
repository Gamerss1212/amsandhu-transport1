# ViralForge

Point it at a long YouTube video. Get back a folder of finished vertical clips —
reframed, captioned, cut for pace, loudness-normalised — each with the caption
and hashtags to paste when you post it.

```bash
viralforge run "https://www.youtube.com/watch?v=..." --clips 5 --niche "business podcast"
```

```
out/the-episode-title/
├── 01-the-cost-of-waiting.mp4     1080x1920, captions burned in, -14 LUFS
├── 01-the-cost-of-waiting.txt     caption + hashtags per platform, ready to paste
├── 01-the-cost-of-waiting.srt     subtitles, if you'd rather upload them
├── 01-the-cost-of-waiting.json    scores and metadata for this clip
├── 01-the-cost-of-waiting.jpg     cover frame
├── 02-...
├── README.md                      the clip list
└── manifest.json                  the whole run: settings, trend profile, scores
```

---

## What it actually does

Nine stages, one command:

1. **Download** — yt-dlp. Any yt-dlp-supported site, or a local file.
2. **Transcribe** — faster-whisper with word-level timestamps. Falls back to the
   video's own caption track, or you can supply a transcript with `--transcript`.
3. **Measure the audio** — a 20 ms RMS envelope over the whole source. This one
   measurement drives dead-air trimming, emphasis detection and the delivery score.
4. **Load a trend profile** — what performs on your target platforms, in your
   niche. See [Trend data](#trend-data), which is the honest part of this README.
5. **Find candidates** — the transcript is split into sentences (punctuation
   where it exists, pauses where it doesn't), and every run of sentences inside
   your duration bounds becomes a candidate. A 40-minute podcast yields a few
   hundred.
6. **Score them** — a measured pass over all of them, then Claude reads a
   shortlist against the trend rubric and scores each on hook, payoff,
   standalone-ness, emotion, shareability, rewatch value and trend fit. It also
   returns a better in/out point and an on-screen hook line.
7. **Analyse framing** — scene cuts and face tracking, on the selected ranges only.
8. **Edit** — dead air removed, shots cut at scene changes and emphasis beats,
   9:16 reframe that follows the speaker, word-by-word captions, hook card,
   punch-ins, progress bar, light grade, loudness normalisation, optional ducked
   music bed.
9. **Write the copy** — caption, alternates, hashtags and a first comment, per
   platform, from the clip's own transcript and the trend profile.

Every stage caches next to the media, so a second run with different render
settings skips the download, the transcription and the audio analysis.

---

## Install

```bash
git clone <this repo> && cd viralforge
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[all]"
viralforge doctor
```

`doctor` tells you exactly what is missing and what each missing piece costs you.

**Required:** Python 3.10+, and **ffmpeg** built with libass and libx264 —
`brew install ffmpeg`, `sudo apt install ffmpeg`, or `winget install Gyan.FFmpeg`.

**Strongly recommended:**

| Extra | Install | Without it |
|---|---|---|
| `faster-whisper` | `pip install -e ".[whisper]"` | Falls back to the YouTube caption track — less accurate on names and numbers, and auto-captions only carry per-word timings in one format |
| `opencv` | `pip install -e ".[vision]"` | Reframing falls back to motion tracking: usable, but not speaker-aware |
| Claude API key | `export ANTHROPIC_API_KEY=...` or `ant auth login` | Clip selection runs on measured signal only, and captions come from templates. Everything still runs — see `--no-llm` |

On OpenCV 5 the bundled Haar cascades are gone; ViralForge fetches the 340 KB
YuNet face model once into `~/.cache/viralforge` instead. Set
`reframe.download_models: false` to forbid that, or point `VF_YUNET_MODEL` at
your own copy.

---

## Using it

```bash
# The common case
viralforge run "https://youtu.be/..." --clips 8 --niche fitness

# A local file, your own transcript, no API calls at all
viralforge run lecture.mp4 --transcript lecture.srt --no-llm

# See what it would pick, render nothing
viralforge run "https://youtu.be/..." --dry-run

# Tune the look
viralforge run URL --style clean --fps 60 --watermark "@yourhandle" --music bed.mp3

# Write a config file so you stop typing flags
viralforge init
```

`viralforge <url>` works too — the `run` is implied.

### Caption styles

| Style | Looks like |
|---|---|
| `impact` *(default)* | Big, bold, uppercase, yellow highlight stepping word by word, pop-in on each card |
| `clean` | White, sentence case, thin outline, one card at a time, no highlight |
| `bold_box` | Same rhythm as `impact` with a solid box behind the text |
| `karaoke` | Highlight only, no pop |

Anything fontconfig can find works: `captions.font: "Inter"`.

### Every setting

`viralforge init` writes a commented `viralforge.yaml`; `viralforge.example.yaml`
in this repo is the same file. Sections: `ingest`, `transcribe`, `candidates`,
`scoring`, `trends`, `reframe`, `captions`, `audio`, `render`, `copy`, `output`.
Any key can be overridden from the environment as `VF_<SECTION>_<KEY>`, e.g.
`VF_RENDER_FPS=60`.

---

## Trend data

You asked for the software to look at what goes viral on TikTok and Instagram
and use that to pick clips. Here is exactly what it does, including the part
that isn't magic.

**Neither TikTok nor Instagram offers a public API that answers "what is going
viral right now".** Anyone who claims otherwise is either scraping, reselling
someone else's scrape, or guessing. So ViralForge supports three ways to ground
its judgement, and it always labels which one produced the profile you're
looking at:

| `trends.provider` | Where the numbers come from | How good it is |
|---|---|---|
| `local` *(default)* | A shipped baseline: duration bands, hook patterns, pacing and caption guidance per niche | A sensible starting point. Not measured data, and it says so in every manifest |
| `file` | **Your own numbers** — a TikTok Creator Center export, Instagram Insights, or any CSV/JSON of posts | The best option. It's your audience, and it's real |
| `apify` | TikTok / Instagram scrapers run through your own Apify account | Real data from outside your account. Costs money, and scraping is on you to square with each platform's terms |

With `file` or `apify`, the profile is built in two halves. The numbers —
duration bands, caption length, hashtag pools — are arithmetic on the samples.
The judgement — why the winners won, what the hooks share, what to avoid — comes
from Claude reading the top and bottom deciles side by side. Views are
normalised against follower count where known, so it compares formats rather
than audience sizes.

```bash
# Learn from your own analytics export
viralforge trends analyze --samples my-tiktok-export.csv --niche fitness --out trends.json

# Then every run uses it
viralforge run URL --niche fitness      # with trends.profile_path: trends.json

# See the active profile
viralforge trends show --niche "business podcast"
```

Column names are matched loosely — `Play count`, `playCount`, `views` and
`video_views` all land in the same field, so you can usually feed an export in
unedited. Give it **40+ posts** if you can: below that the numeric half of the
profile has too little to work with and it keeps the baseline bands rather than
inventing some from four data points. It says which it used in every manifest.

---

## What it doesn't do

- **It doesn't post for you.** TikTok's Content Posting API and Instagram's
  Graph API both need an approved app tied to a business account and a review
  process; wiring that to a general-purpose tool would mean handing your
  credentials to something that can't guarantee it will still be approved next
  month. ViralForge stops one step short: the video is finished and the caption
  is in a text file next to it, ready to paste.
- **It can't promise a clip will go viral.** Nothing can. What it does is find
  the moments that are structurally capable of it — a complete thought, a hook
  in the first three seconds, a payoff, no context required — and cut them so
  nothing gets in the way.
- **It won't fix bad source audio.** Loudness is normalised and a compressor
  tames the peaks, but a clip from a bad recording is a normalised clip from a
  bad recording.
- **The hook line is only as good as the transcript.** With `--no-llm` it's the
  clip's first sentence, trimmed to fit.

---

## How the editing works

A few decisions worth knowing about, because they're the difference between a
render that takes seconds and one that takes minutes, or between a clip that
looks made and one that looks generated:

**The crop never changes size mid-shot.** ffmpeg reconfigures its entire filter
graph whenever a filter's output dimensions change. Animating a crop's width and
height measured ~100× slower than animating x/y on the same clip. So each shot
gets a constant crop that moves, and a zoom is a *new shot* with a tighter
constant crop — which is the punch-in cut that reads better on short-form anyway.

**Each shot's crop has its own filter instance name.** ffmpeg dispatches
`sendcmd` commands to every filter whose *class* name matches, so a clip cut
into six shots would have all six crops driven by all six tracking scripts at
once, and the subject wanders out of frame. Each crop is named `crop@sN` and
targeted individually.

**The camera's response is adaptive.** One smoothing constant can't be both calm
while someone gestures and quick when they cross the room, so the approach rate
scales with how far behind the framing is. Measured against a known subject
path: 113 px median error with fixed smoothing, 45 px with adaptive (on a
1920-wide source).

**Faces are found with CLAHE, not global equalisation.** `equalizeHist` on a
frame with a large flat dark backdrop stretches the background across the
histogram and compresses the face until the detector's features vanish. On one
dark-backdrop test frame: 0 detections with `equalizeHist`, 1 with CLAHE, 1 raw.

**The progress bar is an overlay, not a drawbox.** `drawbox` evaluates its width
once, so a `t`-driven expression renders as a permanently full bar. A full-width
bar slid in from the left with `overlay` (which does evaluate per frame)
actually fills.

**Captions are laid out, not just timed.** Card breaks respect sentence
punctuation first, pauses second, and the frame's width third, with two-line
cards balanced so you don't get five words over one. Text width is estimated
from per-character advance widths — a few percent off, which the margin absorbs,
and it avoids a freetype dependency.

**Silence trimming backs off if it would gut the clip.** The gate is anchored to
each recording's own noise floor rather than a fixed dB value, and if it would
remove more than a third of a clip it's assumed wrong for that audio and skipped.

---

## Development

```bash
pip install -e ".[all,dev]"
pytest                      # 80 tests, ~75s
pytest -m "not slow"        # skip the real render, ~1s
```

The slow test renders actual video from synthetic footage and asserts on the
result: 1080x1920 H.264, integrated loudness inside -16.5..-11.5 LUFS, bright
caption pixels in the caption band, a progress bar that is fuller at the end
than at the start, and every sidecar file present.

`tests/fixtures.py` builds the source: speech-shaped audio with real silences, a
schematic face that a real face detector detects, and a matching SRT. `face_center(t)`
returns the subject's true position, so reframing accuracy is measured, not eyeballed.

```
viralforge/
├── ingest/       yt-dlp download, local files, metadata
├── analyze/      transcribe · audio · visual · candidates · scoring · llm
├── trends/       profile model, providers (local/file/apify), profile derivation
├── edit/         planner · reframe · captions · render
├── publish/      copywriter · packaging
├── pipeline.py   the orchestrator
└── cli.py
```
