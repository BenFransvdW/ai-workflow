# Progress

## Done (see git log)
- Scaffold: config, SQLite schema, state machine, scheduling, lock, CLI
- Stages: fetch + filters, Claude script rewrite, TTS (ElevenLabs/OpenAI/Piper/espeak), captions, background (procedural/Pexels/local), music, compose, upload (dry-run default)
- Telegram approval bot, orchestrator `run`, systemd units, samples
- `shorts doctor`: offline preflight (ffmpeg, secrets, Piper models, YouTube token, dry-run state) — weekly/2026-11-03
- Telegram `/health` command + proactive failure alerts — weekly/2026-11-10

## Last session (2026-11-10)
- Added `src/shorts/health.py`:
  - `run_health` / `health_text`: problems first. FAIL when videos have used up `limits.max_attempts`. WARN on 3+ failures in 24 h. Inspects the YouTube token JSON offline: an expired token with no refresh token is a WARN in dry-run and a FAIL when live. All `doctor` checks are included.
  - `new_failure_alert`: reports failures recorded since the last poll. It tracks the last seen `video_events.id` in `settings`. The first call only sets a baseline, so failures from before the bot started aren't replayed.
- Bot: added the `/health` command, and `tick` now sends failure alerts. Updated the HELP text and README.
- Added `tests/test_health.py` (12 tests) and updated the handler count in `test_approval.py`. Full suite: 254 passed.
- Nothing half-done. Limitation: a *revoked* refresh token can't be detected offline. It only shows up as an upload failure, which the new alert does report.
- Dev-env note: `python -m pip install -e '.[dev]'` in this container is slow (several minutes) and its output was swallowed when piped through `tail`. Use `.[all,dev]`; the bot tests need `telegram`. Reinstall Pillow/cryptography with `--ignore-installed` as before.

## Next 3 priorities
1. Retention/cleanup: prune old `data/videos/*` working dirs and dry-run output after upload, expiry or rejection, to protect SD-card space. Add a disk-free check to `/health`.
2. Run on a real Raspberry Pi 5: verify performance estimates, Piper/whisper installs, and that `doctor` and `/health` output is accurate there.
3. Daily digest: an optional once-a-day Telegram message (uploads, rejections, failures) using `health_text` and `status_text`.
