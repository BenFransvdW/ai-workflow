"""Upload stage: YouTube Data API v3 (OAuth). Only `approved` videos are ever uploaded.

Dry-run (the default) writes the exact request body to data/dry_run/ instead of calling the API.
Every upload declares `status.containsSyntheticMedia = true` because the narration is AI-generated.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from ..context import Ctx
from ..db import is_paused
from ..metadata import final_metadata
from ..schedule import can_upload_now
from ..state import State, transition

log = logging.getLogger("shorts.upload")
SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


class NotUploadable(RuntimeError):
    pass


def request_body(ctx: Ctx, video_id: int) -> dict:
    meta = final_metadata(ctx.conn, video_id)
    up = ctx.config.upload
    tags, total = [], 0
    for t in meta["tags"]:  # YouTube caps the combined tag length at ~500 characters
        if total + len(t) + 1 > 450:
            break
        tags.append(t)
        total += len(t) + 1
    return {
        "snippet": {
            "title": meta["title"][:100],
            "description": meta["description"],
            "tags": tags,
            "categoryId": up.category_id,
            "defaultLanguage": up.default_language,
            "defaultAudioLanguage": up.default_language,
        },
        "status": {
            "privacyStatus": up.privacy_status,
            "selfDeclaredMadeForKids": False,
            "containsSyntheticMedia": True,  # altered/synthetic content disclosure (AI voice)
        },
    }


def youtube_service(ctx: Ctx):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    token = Path(ctx.secrets.require("YOUTUBE_TOKEN_FILE"))
    if not token.exists():
        raise RuntimeError(f"{token} missing: run `shorts auth-youtube` first (see README)")
    creds = Credentials.from_authorized_user_file(str(token), SCOPES)
    if not creds.valid and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token.write_text(creds.to_json())
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def authorize(ctx: Ctx) -> None:
    """One-time OAuth consent. Run on a machine with a browser, or use SSH port forwarding (README)."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(ctx.secrets.require("YOUTUBE_CLIENT_SECRETS"), SCOPES)
    creds = flow.run_local_server(host="localhost", port=8765, open_browser=False,
                                  authorization_prompt_message="Open this URL in your browser:\n{url}\n")
    token = Path(ctx.secrets.require("YOUTUBE_TOKEN_FILE"))
    token.parent.mkdir(parents=True, exist_ok=True)
    token.write_text(creds.to_json())
    token.chmod(0o600)
    print(f"saved {token}")


def _check_uploadable(ctx: Ctx, video_id: int):
    v = ctx.conn.execute("SELECT * FROM videos WHERE id = ?", (video_id,)).fetchone()
    if v is None:
        raise NotUploadable(f"video {video_id} not found")
    if v["state"] != State.APPROVED.value:
        raise NotUploadable(f"video {video_id} is {v['state']}, not approved")
    if v["part_index"] > 1:
        prev = ctx.conn.execute("SELECT state FROM videos WHERE group_id = ? AND part_index = ?",
                                (v["group_id"], v["part_index"] - 1)).fetchone()
        if prev is None or prev["state"] != State.UPLOADED.value:
            raise NotUploadable(f"video {video_id}: part {v['part_index'] - 1} must be uploaded first")
    if not v["video_path"] or not Path(v["video_path"]).exists():
        raise NotUploadable(f"video {video_id}: rendered file missing")
    return v


def _insert(service, body: dict, path: str) -> str:
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload

    media = MediaFileUpload(path, mimetype="video/mp4", chunksize=8 * 1024 * 1024, resumable=True)
    req = service.videos().insert(part="snippet,status", body=body, media_body=media)
    response, retries = None, 0
    while response is None:
        try:
            _, response = req.next_chunk()
        except HttpError as e:
            if e.resp.status in (500, 502, 503, 504) and retries < 5:
                retries += 1
                time.sleep(2 ** retries)
                continue
            raise
    return response["id"]


def upload_video(ctx: Ctx, video_id: int, *, dry_run: bool = True, service=None, now: datetime | None = None) -> str:
    v = _check_uploadable(ctx, video_id)
    body = request_body(ctx, video_id)
    if dry_run:
        out = ctx.work_dir("dry_run") / f"video_{video_id}.json"
        out.write_text(json.dumps({"file": v["video_path"], "part": "snippet,status", "body": body}, indent=2))
        yt_id = f"dryrun-{video_id}"
        log.info("DRY RUN: would upload video %s %r (request saved to %s)", video_id, body["snippet"]["title"], out)
    else:
        try:
            yt_id = _insert(service or youtube_service(ctx), body, v["video_path"])
        except Exception as e:
            transition(ctx.conn, video_id, State.FAILED, f"upload: {e}", error=str(e)[-2000:],
                       attempts=v["attempts"] + 1)
            raise
        log.info("uploaded video %s → https://youtube.com/shorts/%s", video_id, yt_id)
    transition(ctx.conn, video_id, State.UPLOADED, "dry run" if dry_run else "uploaded", youtube_id=yt_id,
               uploaded_at=(now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(timespec="seconds"),
               dry_run=int(dry_run))
    group = ctx.conn.execute("SELECT story_id FROM groups WHERE id = ?", (v["group_id"],)).fetchone()
    ctx.conn.execute("UPDATE stories SET status = 'used' WHERE id = ?", (group["story_id"],))
    return yt_id


def next_in_queue(ctx: Ctx) -> int | None:
    """Oldest approved video whose earlier parts are already uploaded."""
    for v in ctx.conn.execute("SELECT * FROM videos WHERE state = 'approved' ORDER BY approved_at, group_id, part_index"):
        try:
            _check_uploadable(ctx, v["id"])
            return v["id"]
        except NotUploadable as e:
            log.debug("skip %s: %s", v["id"], e)
    return None


def retry_failed_uploads(ctx: Ctx) -> None:
    rows = ctx.conn.execute(
        "SELECT v.id, v.attempts FROM videos v WHERE v.state = 'failed' AND (SELECT from_state FROM video_events e"
        " WHERE e.video_id = v.id ORDER BY e.id DESC LIMIT 1) = 'approved'").fetchall()
    for r in rows:
        if r["attempts"] < ctx.config.limits.max_attempts:
            transition(ctx.conn, r["id"], State.APPROVED, "retry upload")


def process_upload_queue(ctx: Ctx, *, dry_run: bool = True, now: datetime | None = None, service=None) -> str | None:
    """Upload at most one approved video if a posting slot is open and the daily cap allows it."""
    now = now or datetime.now(timezone.utc)
    if is_paused(ctx.conn):
        log.info("uploads paused")
        return None
    retry_failed_uploads(ctx)
    ok, why = can_upload_now(ctx.conn, ctx.config, now)
    if not ok:
        log.info("no upload now: %s", why)
        return None
    vid = next_in_queue(ctx)
    if vid is None:
        log.info("upload slot open but nothing approved")
        return None
    try:
        return upload_video(ctx, vid, dry_run=dry_run, service=service, now=now)
    except Exception as e:
        log.error("upload of video %s failed: %s", vid, e)
        return None
