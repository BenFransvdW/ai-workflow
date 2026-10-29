"""Final title/description/tags for a video (used by the approval bot and the uploader)."""

from __future__ import annotations

import sqlite3

from .db import jload


def final_metadata(conn: sqlite3.Connection, video_id: int) -> dict:
    v = conn.execute("SELECT * FROM videos WHERE id = ?", (video_id,)).fetchone()
    desc = (v["description"] or "").strip()
    credits = (jload(v["bg_json"], {}) or {}).get("credits", [])
    if credits:
        desc += "\n" + "\n".join(credits)
    if v["part_count"] > 1:
        desc += f"\nPart {v['part_index']} of {v['part_count']}."
    desc += "\n\n#shorts #redditstories #storytime"
    return {"title": v["title"], "description": desc[:4900], "tags": jload(v["tags_json"], [])}
