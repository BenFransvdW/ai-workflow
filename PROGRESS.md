# Progress

## Done (see git log)
- Scaffold: config, SQLite schema, state machine, scheduling, lock, CLI
- Stages: fetch + filters, Claude script rewrite, TTS (ElevenLabs/OpenAI/Piper/espeak), captions, background (procedural/Pexels/local), music, compose, upload (dry-run default)
- Telegram approval bot, orchestrator `run`, systemd units, samples
- `shorts doctor`: offline preflight (ffmpeg, secrets, Piper models, YouTube token, dry-run state) — weekly/2026-11-03
- Telegram `/health` command + proactive failure alerts — weekly/2026-11-10 (PR #3, open)
- Stale-run / low-disk / rejected-token health checks, debounced condition alerts, `shorts health` CLI — weekly/2026-11-17 (stacked on PR #3)

## Last session (2026-11-17)
- PR #3 (weekly/2026-11-10, /health + failure alerts) is still unmerged, so `PROGRESS.md` on the default
  branch didn't mention it. This session first re-implemented /health by mistake, noticed PR #3, threw
  that away, and rebuilt on top of PR #3 instead. **This branch is stacked on `weekly/2026-11-10`; merge PR #3 first.**
- `src/shorts/health.py` additions:
  - `mark_run` records `last_run_at` in `settings` after each completed (or paused) `run_once` pass.
    `/health` WARNs when no pass has completed for `health.stale_run_hours` (6h; the timer fires every 30 min).
    It skips this check when paused or on a fresh install.
  - Disk-space check: WARN below `health.min_free_gb` (2 GB) free on the data disk.
  - Live mode only: FAIL when a recent upload failure says `invalid_grant`, i.e. Google revoked or expired the refresh token.
  - `condition_alert`: the bot pushes those three conditions, since none of them creates a failure event
    for `new_failure_alert` to report. It's debounced: it sends when a new problem appears, repeats every
    `alert_every_hours` (12), and sends one all-clear when everything is fixed.
- The hard-coded 24h / 3-failure constants moved to a new `health:` section in `config.yaml`.
- New `python -m shorts health` CLI command (exit 1 on FAIL).
- `tests/conftest.py` sets `health.min_free_gb: 0` so the host's real disk can't make tests flaky.
- 11 new tests in `tests/test_health.py`. Full suite: 265 passed.
- Dev-env note: in a fresh container run `python -m pip install -e '.[all,dev]'`, then
  `python -m pip install --ignore-installed cryptography Pillow cffi`; without the second step, telegram imports panic.
- Nothing half-done.

## Next 3 priorities
1. Retention/cleanup: prune old `data/videos/*` working dirs and dry-run output after upload, expiry or
   rejection, to protect SD-card space. The new low-disk alert flags the problem but nothing frees space yet.
2. Daily digest: an optional once-a-day Telegram message (uploads, rejections, failures) built from `health_text` + `status_text`.
3. Run on a real Raspberry Pi 5: check the performance estimates, the Piper/whisper installs, and that `doctor`/`/health` are accurate there.
   This needs the hardware.
