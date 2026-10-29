"""Command line entry point: `python -m shorts <command>`. Every stage can be rerun on its own."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone

from .context import Ctx
from .logging_setup import setup_logging

log = logging.getLogger("shorts.cli")


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="shorts", description=__doc__)
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--env", default=None, help=".env file (default: next to config.yaml)")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("fetch", help="fetch candidate posts from Reddit")
    sp = sub.add_parser("new-group", help="pick a series + story and create a candidate group")
    sp.add_argument("--series")
    sp = sub.add_parser("script", help="LLM rewrite for a group")
    sp.add_argument("--group", type=int, required=True)
    for name in ("tts", "captions", "background", "compose"):
        sp = sub.add_parser(name, help=f"run the {name} stage for one video")
        sp.add_argument("--video", type=int, required=True)
    sp = sub.add_parser("render", help="tts → captions → background → compose for every part of a group")
    sp.add_argument("--group", type=int, required=True)
    sp = sub.add_parser("upload", help="upload approved videos whose slot is due (dry-run by default)")
    sp.add_argument("--video", type=int, help="upload this approved video now, ignoring slots")
    sp.add_argument("--live", action="store_true", help="really upload (overrides upload.dry_run)")
    sp = sub.add_parser("run", help="one orchestrator pass (systemd timer entry point)")
    sp.add_argument("--live", action="store_true", help="really upload (overrides upload.dry_run)")
    sp.add_argument("--no-generate", action="store_true")
    sub.add_parser("bot", help="run the Telegram approval bot (long-lived)")
    sp = sub.add_parser("prerender", help="fill the procedural background pool")
    sp.add_argument("--style", action="append")
    sp.add_argument("--count", type=int, help="clips per style (default: pool_size_per_style)")
    sp = sub.add_parser("samples", help="render one sample video per procedural style into samples/")
    sp.add_argument("--seconds", type=float, default=None, help="trim fixture narration to this length")
    sp.add_argument("--style", action="append")
    sub.add_parser("auth-youtube", help="one-time OAuth flow; writes YOUTUBE_TOKEN_FILE")
    sub.add_parser("status", help="print queue counts and next upload time")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    ctx = Ctx.from_files(args.config, args.env)
    setup_logging(ctx.data_dir / "logs", "bot" if args.cmd == "bot" else "pipeline",
                  logging.DEBUG if args.verbose else logging.INFO)
    cmd = args.cmd

    if cmd == "fetch":
        from .stages.fetch import fetch
        print(f"{fetch(ctx)} new candidate stories")
    elif cmd == "new-group":
        from .pipeline import new_group
        print(new_group(ctx, series_name=args.series))
    elif cmd == "script":
        from .stages.script import script_group
        script_group(ctx, args.group)
    elif cmd == "tts":
        from .stages.tts import tts_video
        tts_video(ctx, args.video)
    elif cmd == "captions":
        from .stages.captions import captions_video
        captions_video(ctx, args.video)
    elif cmd == "background":
        from .stages.background import background_video
        background_video(ctx, args.video)
    elif cmd == "compose":
        from .stages.compose import compose_video
        compose_video(ctx, args.video)
    elif cmd == "render":
        from .pipeline import render_group
        render_group(ctx, args.group)
    elif cmd == "upload":
        from .stages.upload import process_upload_queue, upload_video
        dry = not args.live and ctx.config.upload.dry_run
        if args.video:
            upload_video(ctx, args.video, dry_run=dry)
        else:
            process_upload_queue(ctx, dry_run=dry)
    elif cmd == "run":
        from .run import run_once
        return run_once(ctx, live=args.live, generate=not args.no_generate)
    elif cmd == "bot":
        from .approval.bot import run_bot
        run_bot(ctx)
    elif cmd == "prerender":
        from .stages.background.pool import fill_pool
        fill_pool(ctx, styles=args.style, count=args.count)
    elif cmd == "samples":
        from .samples import render_samples
        for path in render_samples(ctx, styles=args.style, seconds=args.seconds):
            print(path)
    elif cmd == "auth-youtube":
        from .stages.upload import authorize
        authorize(ctx)
    elif cmd == "status":
        from .approval.service import ApprovalService
        print(json.dumps(ApprovalService(ctx).status(datetime.now(timezone.utc)), indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
