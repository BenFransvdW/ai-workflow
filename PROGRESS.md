# Progress

## Done (see git log)
- Scaffold: config, SQLite schema, state machine, scheduling, lock, CLI
- Stages: fetch + filters, Claude script rewrite, TTS (ElevenLabs/OpenAI/Piper/espeak), captions, background (procedural/Pexels/local), music, compose, upload (dry-run default)
- Telegram approval bot, orchestrator `run`, systemd units, samples
- `shorts doctor`: offline preflight (ffmpeg, secrets, Piper models, YouTube token, dry-run state) — weekly/2026-11-03

## Last session (2026-11-03)
- Created this file (it did not exist).
- Added `src/shorts/doctor.py` + `doctor` CLI command + `tests/test_doctor.py` (9 tests). Full suite: 242 passed.
- Dev-env note: use `python -m pip` (bare `pip` targets a different Python here); the distro `cryptography`/`Pillow` needed reinstalling with `--ignore-installed`.
- Nothing half-done.

## Next 3 priorities
1. Run on a real Raspberry Pi 5: verify performance estimates, Piper/whisper installs, and that `doctor` output is accurate there.
2. Add a `/doctor`-style health summary to the Telegram bot (e.g. warn on repeated failed runs or an expired YouTube token).
3. Retention/cleanup: prune old `data/videos/*` working dirs and dry-run output after upload/expiry to protect SD-card space.
