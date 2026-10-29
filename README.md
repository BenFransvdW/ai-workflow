# Shorts pipeline

An unattended pipeline that turns Reddit stories into narrated YouTube Shorts on a Raspberry Pi 5,
with **every video approved by you on Telegram before it is uploaded**.

```
fetch ─▶ script ─▶ tts ─▶ captions ─▶ background ─▶ compose ─▶ approval (Telegram) ─▶ upload
Reddit   Claude    voice  whisper     procedural/   ffmpeg     Approve/Reject/        YouTube
(PRAW)   rewrite          + ASS       Pexels/local  1080x1920  Regenerate             Data API v3
```

- Python 3.11+, ffmpeg, numpy and Pillow. No GPU, no CUDA, nothing desktop-only.
- Every stage is a module with a CLI entry point, so you can rerun any of them on its own.
- State lives in SQLite (`data/shorts.db`). Config is in `config.yaml`. Secrets only come from `.env`.

Sample output from the offline fixture is in [`samples/`](samples/): one Short per procedural style
(espeak voice, energy-aligned captions, procedural music).

---

## How the hard requirements are met

### 1. Avoiding "inauthentic / reused content" demonetisation

| Measure | Where |
|---|---|
| **LLM rewrite, not a read-out.** Claude paraphrases and tightens the post, swaps names for roles, strips identifying details, writes an original ≤12-word hook, and ends with its own take (verdict, lesson or question, per series). | `stages/script.py`, `stages/prompts.py` |
| **Verbatim guard.** Any run of 8 words (configurable) shared with the source post rejects the draft, and the model is told what it copied. | `script.validate` |
| **Variety guard.** The hook and closing commentary are compared with the last 60 on the channel (char-trigram Jaccard plus same-opening check). Repeats are sent back for a rewrite, so the commentary never becomes a template. | `script.too_similar`, `recent_lines` table |
| **Series formats.** Each series has its own voices, caption styles (font, colours, pop/bounce/fade/karaoke), background providers and styles, music moods, pacing, tone and ending style. Within a series, the least recently used option is picked each time. | `config.yaml → series`, `series.py` |
| **Original visuals and music.** Five procedural background styles with randomised palettes and physics, plus procedurally synthesised music beds that are seeded per video. | `stages/background/procedural/`, `stages/music.py` |
| **Credit.** The description says *"Story submitted by u/… on r/…, adapted and rewritten for narration"*, links the original post, notes the AI voice, and credits Pexels creators when their footage is used. | `script.credit_block`, `metadata.py` |
| **Quality filters.** Posts are skipped if they are NSFW, removed or deleted, from a deleted author, link posts, mod posts, "update" posts, outside the length window, below a score threshold, or blocklisted. Posts are also skipped if they mention minors in a sexual or abusive context, or contain emails, phone numbers, URLs, handles, `u/` names or street addresses. The LLM does a second safety review (real or identifiable people, minors, explicit content) and can reject the story. | `stages/filters.py`, `prompts.SYSTEM` |
| **Dedupe.** `stories.reddit_id` is UNIQUE, and a story is never picked twice. Rejected and expired stories are never reused. | `schema.sql` |
| **Daily cap.** `limits.max_uploads_per_day` (default 2) is a hard cap, with at most one upload per configured posting slot and no burst catch-up. | `schedule.py` |
| **AI disclosure.** Every upload sets `status.containsSyntheticMedia: true`. | `stages/upload.py` |

### 2. Copyright-safe backgrounds

Nothing is ever downloaded from YouTube, TikTok, Twitch or any other video platform. Three providers are available:

- **`procedural`** (primary): original animations rendered headlessly with numpy and Pillow, using analytic anti-aliasing. Frames are rendered at 540x960, piped to ffmpeg, and upscaled to 1080x1920 at compose time. The styles are:

  | Style | What it shows |
  |---|---|
  | `bouncing` | balls inside a slowly rotating polygon, with gravity, elastic collisions and glowing trails; balls can grow and "pop" |
  | `plinko` | a marble-run peg board, with marbles cascading into bins, stacking, then the board clearing |
  | `runner` | a parallax endless runner: 3–5 hill layers, day or night sky, clouds, and a stick runner jumping obstacles |
  | `plasma` | a domain-warped "shader" loop, either smooth or as topographic contours |
  | `flowfield` | thousands of particles following an evolving flow field, with additive glowing trails |

  Every clip uses a random seed for palette, speed, layout and counts. The seed is stored in `bg_clips.params_json`.
  Clips are pre-rendered into a **rotating pool** (`pool_size_per_style`) during idle time at the end of each run. After `max_uses_per_clip` uses a clip is deleted and replaced with a fresh render.
- **`pexels`**: free-licensed portrait footage from the official Pexels API, using each series' themes (for example "satisfying", "kinetic sand" or "drone ocean"). Clips are cached in `data/bg_cache/pexels/`, with author, source URL and licence stored in `bg_clips`. A clip segment is never reused within `reuse_days`, and several short clips are chained together when one isn't long enough.
- **`local_folder`** (optional): your own recordings. The pipeline picks a random segment, never the same one twice in a row, and scales and crops it to 9:16.

If a provider is unavailable (for example no Pexels key or no network), the pipeline falls back to procedural.

### 3. Human approval on Telegram

- The bot (`python -m shorts bot`, a separate long-lived service) uses **long polling**, so it needs no public URL or open ports. It only talks to `TELEGRAM_CHAT_ID`; updates from any other chat are ignored.
- **Delivery:** when a video is rendered, the bot sends each part, then a summary with the title, description, subreddit, post link, voice, style and expiry time, plus buttons:
  - **Approve:** queued for the next posting slot, respecting the daily cap.
  - **Reject:** the story is never used again. The bot asks for an optional reason by reply; `/skip` skips it.
  - **Regenerate:** this version is superseded, and a new script, voice, caption style, music and background are made for the same story. The bot starts a pipeline run immediately.
- **Title edits:** reply `title: New title` to the summary or a video message. Part suffixes are kept.
- **Expiry and backlog:** videos not approved within `approval.window_hours` (48) **expire and are never uploaded**. The pipeline keeps about `approval.backlog_target` (3) groups waiting so you can approve several at once.
- **Commands:**
  - `/status`: queue counts, uploads today against the cap, next upload slot, candidate stories.
  - `/pause` and `/resume`: stop or restart generation and uploads.
- **Large files:** if a render is over `compose.telegram_max_mb` (48 MB; bots can send 50 MB), a 720p preview is sent instead. The full-quality file is what gets uploaded.

---

## Architecture

```
src/shorts/
  cli.py            python -m shorts <command>; every stage can be rerun
  config.py         pydantic models for config.yaml; Secrets reads .env
  db.py schema.sql  SQLite (WAL, shared by bot and pipeline)
  state.py          video state machine + audit log (video_events)
  series.py         series / voice / style / music rotation
  schedule.py       posting slots and daily cap
  pipeline.py       new group → script → render parts; failure handling and retries
  run.py            orchestrator (systemd timer entry point, lockfile)
  stages/
    fetch.py filters.py         Reddit → stories
    script.py prompts.py        Claude rewrite (structured JSON output)
    tts/                        elevenlabs, openai, piper, espeak (+ silence trim, pacing)
    captions/                   faster-whisper word timing → script-aligned words → ASS
    background/                 procedural (5 styles + pool), pexels, local_folder
    music.py                    procedural beds or your own folder
    compose.py                  ffmpeg: ducking, 2-pass loudnorm, captions, 1080x1920 < 60 s
    upload.py                   YouTube Data API v3 (dry-run default)
  approval/
    service.py                  approval logic (testable without Telegram)
    bot.py                      python-telegram-bot handlers + polling job
```

### Video state machine

```
candidate → scripted → rendered → awaiting_approval → approved → uploaded
    │           │          │              ├──→ rejected        └──→ failed ──→ approved (retry)
    └───────────┴──────────┴──→ failed    ├──→ expired
                                          └──→ superseded (Regenerate)
```

Each Short is one `videos` row. Parts of one story share a `groups` row, and approval actions apply to the whole group. Part *n* is only uploaded after part *n-1*. Every transition is validated and logged in `video_events`.

**Long stories:** the script stage budgets words per part from `words_per_minute × pacing × max_part_seconds`. When a story doesn't fit, Claude splits it into up to 3 parts, ending each non-final part with an original cliffhanger line, and the titles get "(Part 1/2)". If the narration still runs over 59 s, it is sped up by at most `compose.max_speedup`. Beyond that, the story is re-scripted with a smaller budget.

---

## Setup on a Raspberry Pi 5

Tested here on x86 Linux (Python 3.13, ffmpeg 6.1). The steps below target Raspberry Pi OS 64-bit (Bookworm).

```bash
sudo apt update
```

```bash
sudo apt install -y git python3-venv python3-dev ffmpeg espeak-ng fonts-dejavu fonts-liberation
```

The `bold_pop` caption style uses **Inter Black**. Install `fonts-inter` if your release packages it, or drop any
OFL-licensed `.ttf`/`.otf` into `assets/fonts/`; libass falls back to a default font if a family is missing.

```bash
git clone <this repo> /home/pi/shorts
```

```bash
cd /home/pi/shorts
```

```bash
python3 -m venv .venv
```

```bash
.venv/bin/pip install -e '.[all,dev]'
```

```bash
cp .env.example .env
```

```bash
chmod 600 .env
```

Download a Piper voice (free, local, good quality on a Pi 5 CPU) for the `*_piper` voices in `config.yaml`:

```bash
.venv/bin/python -m piper.download_voices en_US-ryan-high en_US-amy-medium --data-dir models/piper
```

The faster-whisper `small` model (about 460 MB) downloads on first use into `models/whisper/`. To fetch it ahead of time:

```bash
.venv/bin/python -c "from faster_whisper import WhisperModel; WhisperModel('small', device='cpu', compute_type='int8', download_root='models/whisper')"
```

If whisper can't load, captions fall back to an energy-based aligner that is in sync sentence by sentence but less precise per word. Set `captions.whisper_model: base` for faster runs.

Check that everything works offline (no keys needed):

```bash
.venv/bin/python -m pytest -q
```

```bash
.venv/bin/python -m shorts samples
```

The second command writes to `samples/`.

Then fill the background pool once (optional; it also happens gradually at the end of each run):

```bash
.venv/bin/python -m shorts prerender
```

### Performance

Measured in this development container (4 x86 cores):

- **Procedural backgrounds:** 12–35 ms per 540x960 frame, depending on style. A 62 s pool clip takes about 25–60 s.
- **Compose:** about 30 s for a 53 s Short (libx264 `veryfast`).
- **Procedural music:** about 3 s per minute of audio.

A Pi 5 should be roughly 2–4× slower. That is fine for 1–2 Shorts a day, since a run renders at most `max_renders_per_run` groups and pool refills happen in idle time. These Pi numbers are estimates; the code has not been run on a Pi yet. The Pi 5 has no hardware H.264 encoder, so x264 runs in software. Use a fan or heatsink.

---

## Credentials

All secrets go in `.env` (gitignored). See `.env.example`.

### Reddit

1. Log in, open <https://www.reddit.com/prefs/apps> and choose **create another app…**.
2. Pick type **script**, give it a name, and set the redirect URI to `http://localhost:8080` (unused).
3. Put the client ID (under the app name) and the secret in `REDDIT_CLIENT_ID` and `REDDIT_CLIENT_SECRET`.
4. Set `REDDIT_USER_AGENT` to something descriptive, such as `shorts-pipeline/0.1 by u/yourname`.

The pipeline only reads public listings through the official API.

### Anthropic

Create a key at <https://console.anthropic.com/> and set `ANTHROPIC_API_KEY`.

- **Model and effort:** set in `config.yaml → llm.model` (default `claude-opus-5-5`) and `llm.effort` (default `medium`). For lower cost, try `claude-sonnet-5-5`.
- **Output format:** the script stage uses structured JSON output (`output_config.format`).
- **Server-side fallback:** `llm.server_fallbacks: true` enables it, so a request declined by a safety classifier is retried on a fallback model. A story that is still refused is rejected.

### YouTube Data API (OAuth)

1. In <https://console.cloud.google.com/>, create a project and enable **YouTube Data API v3**.
2. Set up the **OAuth consent screen**: user type External, add the scope `.../auth/youtube.upload`, and add your Google account as a **test user**.
3. Under **Credentials**, choose **Create credentials → OAuth client ID → Desktop app**, download the JSON, and copy it to the Pi. Set `YOUTUBE_CLIENT_SECRETS` to that path and `YOUTUBE_TOKEN_FILE` to where the token should be stored.
4. Run the one-time consent flow. The Pi is headless, so forward the callback port from your laptop:

   ```bash
   ssh -L 8765:localhost:8765 pi@raspberrypi.local
   ```

   Then on the Pi:

   ```bash
   cd ~/shorts && .venv/bin/python -m shorts auth-youtube
   ```

   Open the printed URL in your laptop's browser and approve. The token is saved and refreshed automatically after that.
5. Uploads stay in **dry-run** until you set `upload.dry_run: false` in `config.yaml` or pass `--live`. Dry runs write the exact request body to `data/dry_run/`.

> **Note:** Google restricts videos uploaded through `videos.insert` from **unverified** API projects to
> **private** visibility. To publish publicly you need the project to pass Google's API compliance audit
> (YouTube API Services → "Audit and quota extension" form). Until then, set `upload.privacy_status: private`
> and publish manually in YouTube Studio, or keep dry-run on. The default quota (10,000 units/day, 1,600 per upload) is plenty for 2/day.

### Pexels (optional)

Request a free key at <https://www.pexels.com/api/> and set `PEXELS_API_KEY`. Footage is used under the
[Pexels License](https://www.pexels.com/license/). Creators are credited in the description anyway.

### ElevenLabs and OpenAI TTS (optional)

- **ElevenLabs:** set `ELEVENLABS_API_KEY`. Voice IDs go in `config.yaml → tts.voices`.
- **OpenAI:** set `OPENAI_API_KEY`. The `gpt-4o-mini-tts` model is used with per-voice `instructions`.
- **Free options:** Piper and espeak need no keys. To stay free, remove the paid voices from each series' `voices` list.

### Telegram bot and chat ID

1. In Telegram, message **@BotFather**, send `/newbot` and follow the prompts. Put the token in `TELEGRAM_BOT_TOKEN`.
2. Open a chat with your new bot and send it any message (for example `/start`).
3. Find your chat ID:

   ```bash
   curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates" | python3 -m json.tool | grep -A3 '"chat"'
   ```

   Use the numeric `"id"` (for a private chat it's your user ID) as `TELEGRAM_CHAT_ID`. You can also ask **@userinfobot**.
4. Only that chat can control the bot. Messages and button presses from anyone else are ignored.

---

## Running

Run any stage by hand:

```bash
.venv/bin/python -m shorts fetch
```

```bash
.venv/bin/python -m shorts new-group --series verdict_hour
```

```bash
.venv/bin/python -m shorts script --group 1
```

```bash
.venv/bin/python -m shorts render --group 1
```

`render` runs tts → captions → background → compose for every part of the group. The individual stages, each taking a video ID:

```bash
.venv/bin/python -m shorts tts --video 1
```

```bash
.venv/bin/python -m shorts captions --video 1
```

```bash
.venv/bin/python -m shorts background --video 1
```

```bash
.venv/bin/python -m shorts compose --video 1
```

Check the queue:

```bash
.venv/bin/python -m shorts status
```

Upload approved videos when a slot is open (a dry run unless you pass `--live`):

```bash
.venv/bin/python -m shorts upload
```

Run one full orchestrator pass, which is what the timer does:

```bash
.venv/bin/python -m shorts run
```

Start the approval bot in the foreground:

```bash
.venv/bin/python -m shorts bot
```

Each `run` pass does the following:

1. Takes `data/run.lock`, or exits if another run holds it.
2. Expires stale videos.
3. Stops here if paused.
4. Uploads at most one approved video if a posting slot is open and the daily cap allows it.
5. Retries failed renders (up to `limits.max_attempts`) and renders regenerated groups.
6. Tops up the backlog, fetching more stories when candidates run low.
7. Refreshes the background pool.

### systemd

Copy the units and enable them:

```bash
sudo cp deploy/systemd/shorts-*.service deploy/systemd/shorts-pipeline.timer /etc/systemd/system/
```

```bash
sudo systemctl daemon-reload
```

```bash
sudo systemctl enable --now shorts-bot.service
```

```bash
sudo systemctl enable --now shorts-pipeline.timer
```

Watch the logs:

```bash
journalctl -u shorts-bot -f
```

```bash
journalctl -u shorts-pipeline -f
```

- `shorts-bot.service` is a long-lived service that restarts automatically.
- `shorts-pipeline.service` is a oneshot (`Nice=10`, idle I/O) triggered every 30 minutes by `shorts-pipeline.timer`. Running every 30 minutes means posting slots are hit promptly; most runs do nothing.
- The units assume user `pi` and `/home/pi/shorts`. Edit them if yours differ.
- Logs go to journald and to `data/logs/`.

---

## Testing

Run the suite with:

```bash
.venv/bin/python -m pytest -q
```

All 233 tests run offline.

- **Fully exercised end to end with real media:** captions, procedural backgrounds, music and compose (ffprobe checks 1080x1920, h264/aac, under 60 s, about -14 LUFS).
- **Exercised with mocked APIs:**
  - Reddit, through a fake PRAW client.
  - Anthropic, through a fake client returning structured JSON, covering the retry, safety-reject and refusal paths.
  - ElevenLabs and OpenAI, through faked HTTP. Piper, through a mocked subprocess.
  - Pexels, through fixture JSON and generated clips.
  - YouTube, through a fake service object, plus dry run.
  - Telegram, through fake bot and update objects.
- **State machine:** every allowed and disallowed transition.
- **Full run:** `tests/test_run.py::test_end_to_end_run_with_mocks` goes fetch → script → tts → captions → procedural background → compose → approval → dry-run upload.

**Not verified yet (no network in the development environment):** real calls to Reddit, Anthropic,
ElevenLabs, OpenAI, Pexels, YouTube and Telegram, and faster-whisper and Piper with downloaded models.
First-run checklist on the Pi:

1. Run `shorts fetch` and check the candidates with `shorts status`.
2. Run `shorts new-group`, then `shorts script --group N`, and read the script in the DB.
3. Run `shorts render --group N` and watch the MP4.
4. Start the bot, send `/status`, and approve the video.
5. Run `shorts upload --live` with `privacy_status: private` and check that the upload appears in YouTube Studio with the altered-content label.
