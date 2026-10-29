-- SQLite schema for the shorts pipeline. Applied idempotently by shorts.db.connect().

CREATE TABLE IF NOT EXISTS stories (
    id            INTEGER PRIMARY KEY,
    reddit_id     TEXT NOT NULL UNIQUE,          -- dedupe key: a post is only ever processed once
    subreddit     TEXT NOT NULL,
    author        TEXT NOT NULL,
    title         TEXT NOT NULL,
    body          TEXT NOT NULL,
    url           TEXT NOT NULL,
    score         INTEGER NOT NULL DEFAULT 0,
    word_count    INTEGER NOT NULL DEFAULT 0,
    created_utc   REAL,
    fetched_at    TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'candidate'
                  CHECK (status IN ('candidate', 'filtered', 'in_use', 'used', 'rejected')),
    filter_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_stories_status ON stories(status);

-- One generation of a story for one series. Multi-part stories have one video row per part.
CREATE TABLE IF NOT EXISTS groups (
    id                    INTEGER PRIMARY KEY,
    story_id              INTEGER NOT NULL REFERENCES stories(id),
    series                TEXT NOT NULL,
    voice                 TEXT,
    caption_style         TEXT,
    music_bed             TEXT,
    regen_of              INTEGER REFERENCES groups(id),
    regenerate_requested  INTEGER NOT NULL DEFAULT 0,
    reject_reason         TEXT,
    tg_message_id         INTEGER,             -- summary message carrying the buttons
    tg_video_message_ids  TEXT,                -- JSON list
    sent_at               TEXT,
    created_at            TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS videos (
    id            INTEGER PRIMARY KEY,
    group_id      INTEGER NOT NULL REFERENCES groups(id),
    part_index    INTEGER NOT NULL DEFAULT 1,
    part_count    INTEGER NOT NULL DEFAULT 1,
    state         TEXT NOT NULL DEFAULT 'candidate',
    script_json   TEXT,          -- {"hook", "narration", "cliffhanger", "commentary", "text"}
    title         TEXT,
    description   TEXT,
    tags_json     TEXT,
    audio_path    TEXT,
    words_json    TEXT,          -- word-level timestamps
    ass_path      TEXT,
    bg_clip_id    INTEGER REFERENCES bg_clips(id),
    bg_start      REAL,
    music_seed    INTEGER,
    video_path    TEXT,
    preview_path  TEXT,
    duration      REAL,
    approved_at   TEXT,
    uploaded_at   TEXT,
    youtube_id    TEXT,
    dry_run       INTEGER NOT NULL DEFAULT 0,
    attempts      INTEGER NOT NULL DEFAULT 0,
    error         TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    UNIQUE (group_id, part_index)
);
CREATE INDEX IF NOT EXISTS idx_videos_state ON videos(state);

CREATE TABLE IF NOT EXISTS video_events (
    id          INTEGER PRIMARY KEY,
    video_id    INTEGER NOT NULL REFERENCES videos(id),
    from_state  TEXT,
    to_state    TEXT NOT NULL,
    reason      TEXT,
    at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bg_clips (
    id           INTEGER PRIMARY KEY,
    provider     TEXT NOT NULL,          -- procedural | pexels | local_folder
    style        TEXT,                   -- procedural style or pexels theme
    params_json  TEXT,
    path         TEXT NOT NULL UNIQUE,
    duration     REAL NOT NULL,
    width        INTEGER,
    height       INTEGER,
    source_url   TEXT,
    author       TEXT,
    author_url   TEXT,
    license      TEXT,
    external_id  TEXT,
    created_at   TEXT NOT NULL,
    UNIQUE (provider, external_id)
);

CREATE TABLE IF NOT EXISTS bg_usage (
    id        INTEGER PRIMARY KEY,
    clip_id   INTEGER NOT NULL REFERENCES bg_clips(id),
    start     REAL NOT NULL,
    end       REAL NOT NULL,
    video_id  INTEGER,
    used_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bg_usage_clip ON bg_usage(clip_id, used_at);

CREATE TABLE IF NOT EXISTS recent_lines (
    id    INTEGER PRIMARY KEY,
    kind  TEXT NOT NULL CHECK (kind IN ('hook', 'commentary', 'title')),
    text  TEXT NOT NULL,
    at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key    TEXT PRIMARY KEY,
    value  TEXT
);
