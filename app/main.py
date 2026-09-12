#!/usr/bin/env python3
"""AImanVisualCopilot (AVC) v0.9.0.

A read-only MCP observation layer for Windows. AVC ingests semantic frames from a
Windows observer and exposes compact, continuous UI context to AI clients.
"""
from __future__ import annotations

import base64
import difflib
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import sqlite3
import time
import uuid
import zipfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from PIL import Image, ImageChops, ImageEnhance

from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, ImageContent, TextContent, ToolAnnotations
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

import migrations
import state_engine

VERSION = "0.9.0"
SERVICE = "AImanVisualCopilot"
MCP_TOKEN = os.environ.get("AVC_MCP_TOKEN", "")
BOOTSTRAP_TOKEN = os.environ.get("AVC_BOOTSTRAP_TOKEN", "")
DASHBOARD_TOKEN_HASH = os.environ.get("AVC_DASHBOARD_TOKEN_HASH", "")
DATA_DIR = Path(os.environ.get("AVC_DATA_DIR", "/var/lib/aiman-visual-copilot"))
PUBLIC_URL = os.environ.get("AVC_PUBLIC_URL", "http://127.0.0.1:8391").rstrip("/")
MCP_HOST = os.environ.get("AVC_MCP_HOST") or (urlparse(PUBLIC_URL).hostname if PUBLIC_URL else "localhost")
RETENTION_HOURS = max(1, int(os.environ.get("AVC_RETENTION_HOURS", "24")))
SCREENSHOT_RETENTION_HOURS = max(1, int(os.environ.get("AVC_SCREENSHOT_RETENTION_HOURS", "6")))
KEYFRAME_SCREENSHOT_RETENTION_HOURS = max(SCREENSHOT_RETENTION_HOURS, int(os.environ.get("AVC_KEYFRAME_SCREENSHOT_RETENTION_HOURS", "24")))
DEV_EVENT_RETENTION_HOURS = max(24, int(os.environ.get("AVC_DEV_EVENT_RETENTION_HOURS", "168")))
EVENT_RETENTION_HOURS = max(24, int(os.environ.get("AVC_EVENT_RETENTION_HOURS", "168")))
BROWSER_RETENTION_HOURS = max(1, int(os.environ.get("AVC_BROWSER_RETENTION_HOURS", "24")))
MEMORY_RETENTION_HOURS = max(168, int(os.environ.get("AVC_MEMORY_RETENTION_HOURS", "720")))
BROWSER_EXTENSION_DIR = Path(os.environ.get("AVC_BROWSER_EXTENSION_DIR", "/opt/aiman-visual-copilot/browser-extension"))
SCREENSHOT_MAX_BYTES = max(64, int(os.environ.get("AVC_SCREENSHOT_MAX_MB", "500"))) * 1024 * 1024
DB_MAX_BYTES = max(64, int(os.environ.get("AVC_DB_MAX_MB", "200"))) * 1024 * 1024
TOTAL_MAX_BYTES = max(256, int(os.environ.get("AVC_TOTAL_MAX_MB", "1024"))) * 1024 * 1024
FREE_SEMANTIC_ONLY_BYTES = max(1, int(os.environ.get("AVC_FREE_SEMANTIC_ONLY_GB", "20"))) * 1024**3
FREE_MINIMAL_BYTES = max(1, int(os.environ.get("AVC_FREE_MINIMAL_GB", "10"))) * 1024**3
MINIMAL_FRAME_INTERVAL_SECONDS = max(10, int(os.environ.get("AVC_MINIMAL_FRAME_INTERVAL_SECONDS", "60")))
SCREENSHOT_TARGET_BYTES = int(SCREENSHOT_MAX_BYTES * 0.90)
DB_SOFT_BYTES = int(DB_MAX_BYTES * 0.85)
DB_TARGET_BYTES = int(DB_MAX_BYTES * 0.72)
TOTAL_TARGET_BYTES = int(TOTAL_MAX_BYTES * 0.90)
DB_WAL_RESERVE_BYTES = min(16 * 1024 * 1024, max(4 * 1024 * 1024, DB_MAX_BYTES // 10))
DB_MAIN_MAX_BYTES = max(32 * 1024 * 1024, DB_MAX_BYTES - DB_WAL_RESERVE_BYTES)
DB_FILE = DATA_DIR / "avc.db"
SCREENSHOT_DIR = DATA_DIR / "screenshots"
MAX_SCREENSHOT_BYTES = 8 * 1024 * 1024
MAX_VISIBLE_TEXT = 64_000
MAX_JSON_FIELD = 256_000

DATA_DIR.mkdir(parents=True, exist_ok=True)
SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)


def now_ts() -> float:
    return time.time()


def now_iso(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts or now_ts(), timezone.utc).isoformat()


def sha256_text(v: str) -> str:
    return hashlib.sha256(v.encode("utf-8")).hexdigest()


def token_hash(v: str) -> str:
    return sha256_text(v)


def clip_text(value: Any, limit: int) -> str:
    s = "" if value is None else str(value)
    return s if len(s) <= limit else s[:limit] + "\n…[truncated]"


def safe_json(value: Any, limit: int = MAX_JSON_FIELD) -> str:
    raw = json.dumps(value if value is not None else {}, ensure_ascii=False, separators=(",", ":"))
    if len(raw) <= limit:
        return raw
    return json.dumps({"truncated": True, "preview": raw[:limit]}, ensure_ascii=False)


def load_json(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


@contextmanager
def db():
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    # Apply the physical SQLite guard on every AVC connection. max_page_count is
    # connection-scoped on the SQLite build used by this host, so startup-only is
    # not sufficient for a hard database ceiling.
    page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
    max_pages = max(8192, DB_MAIN_MAX_BYTES // max(1024, page_size))
    conn.execute(f"PRAGMA max_page_count={max_pages}")
    conn.execute("PRAGMA wal_autocheckpoint=1000")
    conn.execute(f"PRAGMA journal_size_limit={8 * 1024 * 1024}")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with db() as conn:
        migrations.preflight_legacy_schema(conn)
        conn.executescript("""
        PRAGMA journal_mode=WAL;
        PRAGMA synchronous=NORMAL;
        CREATE TABLE IF NOT EXISTS sources (
          id TEXT PRIMARY KEY,
          label TEXT NOT NULL COLLATE NOCASE,
          device_id TEXT NOT NULL UNIQUE,
          hostname TEXT NOT NULL DEFAULT '',
          platform TEXT NOT NULL DEFAULT 'windows',
          agent_version TEXT NOT NULL DEFAULT '',
          token_hash TEXT,
          created_at REAL NOT NULL,
          last_seen REAL NOT NULL,
          monitoring_enabled INTEGER NOT NULL DEFAULT 1,
          control_updated_at REAL NOT NULL DEFAULT 0,
          last_control_poll REAL NOT NULL DEFAULT 0,
          browser_token_hash TEXT,
          last_browser_seen REAL NOT NULL DEFAULT 0,
          metadata_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_sources_last_seen ON sources(last_seen DESC);

        CREATE TABLE IF NOT EXISTS enrollments (
          token_hash TEXT PRIMARY KEY,
          label TEXT NOT NULL,
          created_at REAL NOT NULL,
          expires_at REAL NOT NULL,
          used_at REAL
        );
        CREATE INDEX IF NOT EXISTS idx_enrollments_expires ON enrollments(expires_at);

        CREATE TABLE IF NOT EXISTS frames (
          id TEXT PRIMARY KEY,
          source_id TEXT NOT NULL,
          seq INTEGER NOT NULL DEFAULT 0,
          ts REAL NOT NULL,
          received_at REAL NOT NULL,
          app TEXT NOT NULL DEFAULT '',
          window_title TEXT NOT NULL DEFAULT '',
          url TEXT NOT NULL DEFAULT '',
          active_tab TEXT NOT NULL DEFAULT '',
          focus_json TEXT NOT NULL DEFAULT '{}',
          cursor_json TEXT NOT NULL DEFAULT '{}',
          visual_hash TEXT NOT NULL DEFAULT '',
          surface TEXT NOT NULL DEFAULT 'desktop',
          summary TEXT NOT NULL DEFAULT '',
          visible_text TEXT NOT NULL DEFAULT '',
          uia_json TEXT NOT NULL DEFAULT '{}',
          dom_json TEXT NOT NULL DEFAULT '{}',
          events_json TEXT NOT NULL DEFAULT '[]',
          changes_json TEXT NOT NULL DEFAULT '[]',
          screenshot_path TEXT,
          screenshot_mime TEXT,
          screenshot_sha256 TEXT,
          screenshot_bytes INTEGER NOT NULL DEFAULT 0,
          width INTEGER,
          height INTEGER,
          semantic_hash TEXT NOT NULL DEFAULT '',
          metadata_json TEXT NOT NULL DEFAULT '{}',
          FOREIGN KEY(source_id) REFERENCES sources(id)
        );
        CREATE INDEX IF NOT EXISTS idx_frames_source_ts ON frames(source_id, ts DESC);
        CREATE INDEX IF NOT EXISTS idx_frames_ts ON frames(ts DESC);

        CREATE TABLE IF NOT EXISTS vision_cache (
          frame_id TEXT PRIMARY KEY,
          source_id TEXT NOT NULL DEFAULT '',
          frame_ts REAL NOT NULL DEFAULT 0,
          app TEXT NOT NULL DEFAULT '',
          window_title TEXT NOT NULL DEFAULT '',
          url TEXT NOT NULL DEFAULT '',
          description TEXT NOT NULL DEFAULT '',
          page_type TEXT NOT NULL DEFAULT '',
          objects_json TEXT NOT NULL DEFAULT '[]',
          controls_json TEXT NOT NULL DEFAULT '[]',
          entities_json TEXT NOT NULL DEFAULT '[]',
          model TEXT NOT NULL DEFAULT '',
          generated_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_vision_cache_generated_at ON vision_cache(generated_at DESC);

        CREATE TABLE IF NOT EXISTS timeline_events (
          id TEXT PRIMARY KEY,
          source_id TEXT NOT NULL,
          frame_id TEXT NOT NULL DEFAULT '',
          ts REAL NOT NULL,
          event_type TEXT NOT NULL,
          app TEXT NOT NULL DEFAULT '',
          window_title TEXT NOT NULL DEFAULT '',
          url TEXT NOT NULL DEFAULT '',
          active_tab TEXT NOT NULL DEFAULT '',
          detail_json TEXT NOT NULL DEFAULT '{}',
          created_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_timeline_events_source_ts ON timeline_events(source_id, ts DESC);
        CREATE INDEX IF NOT EXISTS idx_timeline_events_type_ts ON timeline_events(event_type, ts DESC);

        CREATE TABLE IF NOT EXISTS browser_snapshots (
          id TEXT PRIMARY KEY,
          source_id TEXT NOT NULL,
          ts REAL NOT NULL,
          received_at REAL NOT NULL,
          tab_id INTEGER,
          window_id INTEGER,
          active INTEGER NOT NULL DEFAULT 1,
          url TEXT NOT NULL DEFAULT '',
          title TEXT NOT NULL DEFAULT '',
          visible_text TEXT NOT NULL DEFAULT '',
          dom_json TEXT NOT NULL DEFAULT '{}',
          focus_json TEXT NOT NULL DEFAULT '{}',
          viewport_json TEXT NOT NULL DEFAULT '{}',
          semantic_hash TEXT NOT NULL DEFAULT '',
          extension_version TEXT NOT NULL DEFAULT '',
          metadata_json TEXT NOT NULL DEFAULT '{}',
          FOREIGN KEY(source_id) REFERENCES sources(id)
        );
        CREATE INDEX IF NOT EXISTS idx_browser_snapshots_source_ts ON browser_snapshots(source_id, ts DESC);

        CREATE TABLE IF NOT EXISTS dev_events (
          id TEXT PRIMARY KEY,
          source_id TEXT NOT NULL,
          ts REAL NOT NULL,
          received_at REAL NOT NULL,
          tab_id INTEGER,
          window_id INTEGER,
          url TEXT NOT NULL DEFAULT '',
          event_type TEXT NOT NULL DEFAULT '',
          severity TEXT NOT NULL DEFAULT 'info',
          summary TEXT NOT NULL DEFAULT '',
          detail_json TEXT NOT NULL DEFAULT '{}',
          is_dev_page INTEGER NOT NULL DEFAULT 0,
          extension_version TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_dev_events_source_ts ON dev_events(source_id, ts DESC);
        CREATE INDEX IF NOT EXISTS idx_dev_events_type_ts ON dev_events(event_type, ts DESC);

        CREATE TABLE IF NOT EXISTS vision_candidates (
          frame_id TEXT PRIMARY KEY,
          source_id TEXT NOT NULL,
          frame_ts REAL NOT NULL,
          priority REAL NOT NULL DEFAULT 0.5,
          reason_json TEXT NOT NULL DEFAULT '[]',
          status TEXT NOT NULL DEFAULT 'pending',
          created_at REAL NOT NULL,
          updated_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_vision_candidates_priority ON vision_candidates(status, priority DESC, frame_ts DESC);

        CREATE TABLE IF NOT EXISTS semantic_events (
          id TEXT PRIMARY KEY,
          source_id TEXT NOT NULL,
          frame_id TEXT NOT NULL DEFAULT '',
          ts REAL NOT NULL,
          event_type TEXT NOT NULL,
          summary TEXT NOT NULL DEFAULT '',
          app TEXT NOT NULL DEFAULT '',
          window_title TEXT NOT NULL DEFAULT '',
          url TEXT NOT NULL DEFAULT '',
          confidence REAL NOT NULL DEFAULT 0.5,
          anomaly INTEGER NOT NULL DEFAULT 0,
          severity TEXT NOT NULL DEFAULT '',
          detail_json TEXT NOT NULL DEFAULT '{}',
          created_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_semantic_events_source_ts ON semantic_events(source_id, ts DESC);
        CREATE INDEX IF NOT EXISTS idx_semantic_events_type_ts ON semantic_events(event_type, ts DESC);

        CREATE TABLE IF NOT EXISTS causal_links (
          id TEXT PRIMARY KEY, source_id TEXT NOT NULL, cause_frame_id TEXT NOT NULL DEFAULT '', effect_frame_id TEXT NOT NULL DEFAULT '',
          cause_ts REAL NOT NULL, effect_ts REAL NOT NULL, cause_type TEXT NOT NULL DEFAULT '', cause_label TEXT NOT NULL DEFAULT '',
          effect_type TEXT NOT NULL DEFAULT '', effect_summary TEXT NOT NULL DEFAULT '', latency_ms INTEGER NOT NULL DEFAULT 0,
          confidence REAL NOT NULL DEFAULT 0.5, detail_json TEXT NOT NULL DEFAULT '{}', created_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_causal_links_source_ts ON causal_links(source_id, effect_ts DESC);

        CREATE TABLE IF NOT EXISTS memory_episodes (
          id TEXT PRIMARY KEY,
          source_id TEXT NOT NULL,
          start_ts REAL NOT NULL,
          end_ts REAL NOT NULL,
          title TEXT NOT NULL DEFAULT '',
          summary TEXT NOT NULL DEFAULT '',
          start_frame_id TEXT NOT NULL DEFAULT '',
          end_frame_id TEXT NOT NULL DEFAULT '',
          event_count INTEGER NOT NULL DEFAULT 0,
          confidence REAL NOT NULL DEFAULT 0.5,
          anomaly_count INTEGER NOT NULL DEFAULT 0,
          app_json TEXT NOT NULL DEFAULT '[]',
          event_types_json TEXT NOT NULL DEFAULT '[]',
          updated_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_memory_episodes_source_ts ON memory_episodes(source_id, start_ts DESC);

        CREATE TABLE IF NOT EXISTS memory_sessions (
          id TEXT PRIMARY KEY,
          source_id TEXT NOT NULL,
          start_ts REAL NOT NULL,
          end_ts REAL NOT NULL,
          title TEXT NOT NULL DEFAULT '',
          summary TEXT NOT NULL DEFAULT '',
          episode_ids_json TEXT NOT NULL DEFAULT '[]',
          episode_count INTEGER NOT NULL DEFAULT 0,
          anomaly_count INTEGER NOT NULL DEFAULT 0,
          updated_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_memory_sessions_source_ts ON memory_sessions(source_id, start_ts DESC);
        """)
        source_cols = {r[1] for r in conn.execute("PRAGMA table_info(sources)").fetchall()}
        if "monitoring_enabled" not in source_cols:
            conn.execute("ALTER TABLE sources ADD COLUMN monitoring_enabled INTEGER NOT NULL DEFAULT 1")
        if "control_updated_at" not in source_cols:
            conn.execute("ALTER TABLE sources ADD COLUMN control_updated_at REAL NOT NULL DEFAULT 0")
        if "last_control_poll" not in source_cols:
            conn.execute("ALTER TABLE sources ADD COLUMN last_control_poll REAL NOT NULL DEFAULT 0")
        if "browser_token_hash" not in source_cols:
            conn.execute("ALTER TABLE sources ADD COLUMN browser_token_hash TEXT")
        if "last_browser_seen" not in source_cols:
            conn.execute("ALTER TABLE sources ADD COLUMN last_browser_seen REAL NOT NULL DEFAULT 0")
        cols = {r[1] for r in conn.execute("PRAGMA table_info(frames)").fetchall()}
        if "screenshot_bytes" not in cols:
            conn.execute("ALTER TABLE frames ADD COLUMN screenshot_bytes INTEGER NOT NULL DEFAULT 0")
        if "active_tab" not in cols:
            conn.execute("ALTER TABLE frames ADD COLUMN active_tab TEXT NOT NULL DEFAULT ''")
        if "focus_json" not in cols:
            conn.execute("ALTER TABLE frames ADD COLUMN focus_json TEXT NOT NULL DEFAULT '{}'")
        if "cursor_json" not in cols:
            conn.execute("ALTER TABLE frames ADD COLUMN cursor_json TEXT NOT NULL DEFAULT '{}'")
        if "visual_hash" not in cols:
            conn.execute("ALTER TABLE frames ADD COLUMN visual_hash TEXT NOT NULL DEFAULT ''")
        if "is_keyframe" not in cols:
            conn.execute("ALTER TABLE frames ADD COLUMN is_keyframe INTEGER NOT NULL DEFAULT 0")
        if "importance" not in cols:
            conn.execute("ALTER TABLE frames ADD COLUMN importance REAL NOT NULL DEFAULT 0")
        if "keyframe_reasons_json" not in cols:
            conn.execute("ALTER TABLE frames ADD COLUMN keyframe_reasons_json TEXT NOT NULL DEFAULT '[]'")
        if "visual_change_json" not in cols:
            conn.execute("ALTER TABLE frames ADD COLUMN visual_change_json TEXT NOT NULL DEFAULT '{}'")
        if "dev_observer_enabled" not in source_cols:
            conn.execute("ALTER TABLE sources ADD COLUMN dev_observer_enabled INTEGER NOT NULL DEFAULT 1")
        vcols = {r[1] for r in conn.execute("PRAGMA table_info(vision_cache)").fetchall()}
        if "source_id" not in vcols:
            conn.execute("ALTER TABLE vision_cache ADD COLUMN source_id TEXT NOT NULL DEFAULT ''")
        if "frame_ts" not in vcols:
            conn.execute("ALTER TABLE vision_cache ADD COLUMN frame_ts REAL NOT NULL DEFAULT 0")
        if "app" not in vcols:
            conn.execute("ALTER TABLE vision_cache ADD COLUMN app TEXT NOT NULL DEFAULT ''")
        if "window_title" not in vcols:
            conn.execute("ALTER TABLE vision_cache ADD COLUMN window_title TEXT NOT NULL DEFAULT ''")
        if "url" not in vcols:
            conn.execute("ALTER TABLE vision_cache ADD COLUMN url TEXT NOT NULL DEFAULT ''")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_vision_cache_source_ts ON vision_cache(source_id, frame_ts DESC)")
        # Backfill sizes for screenshots created before the storage-guard migration.
        for row in conn.execute("SELECT id,screenshot_path FROM frames WHERE screenshot_path IS NOT NULL AND screenshot_bytes=0").fetchall():
            try:
                size = Path(row[1]).stat().st_size
            except Exception:
                size = 0
            conn.execute("UPDATE frames SET screenshot_bytes=? WHERE id=?", (size, row[0]))
        page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
        max_pages = max(8192, DB_MAIN_MAX_BYTES // max(1024, page_size))
        conn.execute(f"PRAGMA max_page_count={max_pages}")
        conn.execute("PRAGMA wal_autocheckpoint=1000")
        conn.execute(f"PRAGMA journal_size_limit={8 * 1024 * 1024}")
        migrations.apply_migrations(conn)


init_db()


def initialize_incremental_state() -> None:
    """Seed the v0.9 current state without rewriting legacy semantic history."""
    with db() as conn:
        source_ids = [r[0] for r in conn.execute("SELECT id FROM sources")]
        for source_id in source_ids:
            state_engine.initialize_latest(conn, source_id)


initialize_incremental_state()


def _path_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except Exception:
        return 0


def database_storage_bytes() -> int:
    return sum(_path_size(Path(str(DB_FILE) + suffix)) for suffix in ("", "-wal", "-shm"))


def storage_status() -> dict[str, Any]:
    with db() as conn:
        row = conn.execute("SELECT COALESCE(SUM(screenshot_bytes),0), COUNT(*) FROM frames").fetchone()
        screenshot_bytes = int(row[0] or 0)
        frame_count = int(row[1] or 0)
        browser_snapshot_count = int(conn.execute("SELECT COUNT(*) FROM browser_snapshots").fetchone()[0])
        event_count = int(conn.execute("SELECT COUNT(*) FROM timeline_events").fetchone()[0])
        vision_cache_count = int(conn.execute("SELECT COUNT(*) FROM vision_cache").fetchone()[0])
        semantic_event_count = int(conn.execute("SELECT COUNT(*) FROM semantic_events").fetchone()[0])
        episode_count = int(conn.execute("SELECT COUNT(*) FROM memory_episodes").fetchone()[0])
        session_count = int(conn.execute("SELECT COUNT(*) FROM memory_sessions").fetchone()[0])
        causal_link_count = int(conn.execute("SELECT COUNT(*) FROM causal_links").fetchone()[0])
        dev_event_count = int(conn.execute("SELECT COUNT(*) FROM dev_events").fetchone()[0])
        vision_candidate_count = int(conn.execute("SELECT COUNT(*) FROM vision_candidates WHERE status='pending'").fetchone()[0])
        keyframe_count = int(conn.execute("SELECT COUNT(*) FROM frames WHERE is_keyframe=1").fetchone()[0])
        keyframe_screenshot_count = int(conn.execute("SELECT COUNT(*) FROM frames WHERE is_keyframe=1 AND screenshot_path IS NOT NULL").fetchone()[0])
    database_bytes = database_storage_bytes()
    # The AVC data directory contains the database and screenshot store. Count any
    # other small files too so the 1 GiB directory guard remains a true hard ceiling.
    known = screenshot_bytes + database_bytes
    other_bytes = 0
    try:
        for child in DATA_DIR.iterdir():
            if child == SCREENSHOT_DIR or child.name in {DB_FILE.name, DB_FILE.name + "-wal", DB_FILE.name + "-shm"}:
                continue
            if child.is_file():
                other_bytes += _path_size(child)
    except Exception:
        pass
    total_bytes = known + other_bytes
    free_bytes = shutil.disk_usage(DATA_DIR).free
    if free_bytes < FREE_MINIMAL_BYTES:
        mode = "minimal"
        reason = "free_disk_below_10gb"
    elif free_bytes < FREE_SEMANTIC_ONLY_BYTES:
        mode = "semantic_only"
        reason = "free_disk_below_20gb"
    else:
        mode = "normal"
        reason = "normal"
    pressure: list[str] = []
    if screenshot_bytes >= SCREENSHOT_MAX_BYTES:
        pressure.append("screenshot_cap")
    if database_bytes >= DB_MAX_BYTES:
        pressure.append("database_cap")
    if total_bytes >= TOTAL_MAX_BYTES:
        pressure.append("total_cap")
    return {
        "mode": mode,
        "reason": reason,
        "pressure": pressure,
        "screenshot_bytes": screenshot_bytes,
        "database_bytes": database_bytes,
        "other_bytes": other_bytes,
        "total_bytes": total_bytes,
        "free_disk_bytes": free_bytes,
        "frame_count": frame_count,
        "browser_snapshot_count": browser_snapshot_count,
        "event_count": event_count,
        "vision_cache_count": vision_cache_count,
        "semantic_event_count": semantic_event_count,
        "episode_count": episode_count,
        "session_count": session_count,
        "causal_link_count": causal_link_count,
        "dev_event_count": dev_event_count,
        "vision_candidate_count": vision_candidate_count,
        "keyframe_count": keyframe_count,
        "keyframe_screenshot_count": keyframe_screenshot_count,
        "limits": {
            "screenshot_max_bytes": SCREENSHOT_MAX_BYTES,
            "ordinary_screenshot_retention_hours": SCREENSHOT_RETENTION_HOURS,
            "keyframe_screenshot_retention_hours": KEYFRAME_SCREENSHOT_RETENTION_HOURS,
            "database_max_bytes": DB_MAX_BYTES,
            "total_max_bytes": TOTAL_MAX_BYTES,
            "semantic_only_below_free_bytes": FREE_SEMANTIC_ONLY_BYTES,
            "minimal_below_free_bytes": FREE_MINIMAL_BYTES,
        },
        "screenshot_allowed": mode == "normal" and screenshot_bytes < SCREENSHOT_MAX_BYTES and total_bytes < TOTAL_MAX_BYTES,
    }


def _unlink_paths(paths: list[str]) -> None:
    for raw in paths:
        try:
            Path(raw).unlink(missing_ok=True)
        except Exception:
            pass


def prune_screenshots_to(target_bytes: int) -> int:
    target_bytes = max(0, int(target_bytes))
    removed: list[str] = []
    freed = 0
    with db() as conn:
        current = int(conn.execute("SELECT COALESCE(SUM(screenshot_bytes),0) FROM frames").fetchone()[0] or 0)
        if current <= target_bytes:
            return 0
        rows = conn.execute(
            "SELECT id,screenshot_path,screenshot_bytes,is_keyframe,importance FROM frames WHERE screenshot_path IS NOT NULL "
            "ORDER BY is_keyframe ASC, importance ASC, ts ASC"
        ).fetchall()
        for row in rows:
            if current <= target_bytes:
                break
            size = int(row[2] or 0)
            if row[1]:
                removed.append(row[1])
            conn.execute(
                "UPDATE frames SET screenshot_path=NULL,screenshot_mime=NULL,screenshot_sha256=NULL,screenshot_bytes=0 WHERE id=?",
                (row[0],),
            )
            current -= size
            freed += size
    _unlink_paths(removed)
    return freed


def _checkpoint_and_vacuum() -> None:
    try:
        conn = sqlite3.connect(DB_FILE, timeout=60)
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("VACUUM")
        conn.close()
    except Exception:
        try:
            conn.close()
        except Exception:
            pass


def prune_database_to_target(target_bytes: int) -> int:
    before = database_storage_bytes()
    if before <= target_bytes:
        return 0

    # Priority 1: old full Semantic Frames. Keep a small recent tail.
    for _ in range(4):
        if database_storage_bytes() <= target_bytes: break
        removed: list[str] = []
        with db() as conn:
            count = int(conn.execute("SELECT COUNT(*) FROM frames").fetchone()[0])
            if count <= 20: break
            batch = min(max(100, count // 5), 5000, max(1, count - 20))
            rows = conn.execute("SELECT id,screenshot_path FROM frames ORDER BY ts ASC LIMIT ?", (batch,)).fetchall()
            removed = [r[1] for r in rows if r[1]]
            conn.execute("DELETE FROM frames WHERE id IN (SELECT id FROM frames ORDER BY ts ASC LIMIT ?)", (batch,))
        _unlink_paths(removed)
        _checkpoint_and_vacuum()

    # Priority 2: Browser Bridge snapshots. They are reproducible from future browsing
    # and can be discarded before the long-lived event/vision memory layers.
    for _ in range(4):
        if database_storage_bytes() <= target_bytes: break
        with db() as conn:
            count = int(conn.execute("SELECT COUNT(*) FROM browser_snapshots").fetchone()[0])
            if count <= 20: break
            batch = min(max(100, count // 4), 5000, max(1, count - 20))
            conn.execute("DELETE FROM browser_snapshots WHERE id IN (SELECT id FROM browser_snapshots ORDER BY ts ASC LIMIT ?)", (batch,))
        _checkpoint_and_vacuum()

    # Priority 3: Browser Dev Observer diagnostics. Preserve a recent diagnostic tail.
    for _ in range(3):
        if database_storage_bytes() <= target_bytes: break
        with db() as conn:
            count = int(conn.execute("SELECT COUNT(*) FROM dev_events").fetchone()[0])
            if count <= 100: break
            batch = min(max(100, count // 4), 5000, max(1, count - 100))
            conn.execute("DELETE FROM dev_events WHERE id IN (SELECT id FROM dev_events ORDER BY ts ASC LIMIT ?)", (batch,))
        _checkpoint_and_vacuum()

    # Priority 4: completed/orphan Vision Candidate queue entries. Pending entries with
    # retained screenshots are kept because they are cheap pointers to valuable visual evidence.
    for _ in range(2):
        if database_storage_bytes() <= target_bytes: break
        with db() as conn:
            conn.execute("DELETE FROM vision_candidates WHERE status!='pending' OR frame_id NOT IN (SELECT id FROM frames)")
        _checkpoint_and_vacuum()

    # Priority 5: long-lived event timeline, preserving at least a recent working set.
    for _ in range(3):
        if database_storage_bytes() <= target_bytes: break
        with db() as conn:
            count = int(conn.execute("SELECT COUNT(*) FROM timeline_events").fetchone()[0])
            if count <= 100: break
            batch = min(max(200, count // 4), 10000, max(1, count - 100))
            conn.execute("DELETE FROM timeline_events WHERE id IN (SELECT id FROM timeline_events ORDER BY ts ASC LIMIT ?)", (batch,))
        _checkpoint_and_vacuum()

    # Priority 6 / last resort: cached visual summaries. These are intentionally the
    # most durable layer, but the 200 MB hard database ceiling still wins.
    for _ in range(2):
        if database_storage_bytes() <= target_bytes: break
        with db() as conn:
            count = int(conn.execute("SELECT COUNT(*) FROM vision_cache").fetchone()[0])
            if count <= 100: break
            batch = min(max(100, count // 5), 5000, max(1, count - 100))
            conn.execute("DELETE FROM vision_cache WHERE frame_id IN (SELECT frame_id FROM vision_cache ORDER BY generated_at ASC LIMIT ?)", (batch,))
        _checkpoint_and_vacuum()

    return max(0, before - database_storage_bytes())


def enforce_storage_limits() -> dict[str, Any]:
    status = storage_status()
    # Hard screenshot cap: evict oldest screenshots with headroom rather than hovering
    # at exactly 500 MB.
    if status["screenshot_bytes"] >= int(SCREENSHOT_MAX_BYTES * 0.95):
        prune_screenshots_to(SCREENSHOT_TARGET_BYTES)
        status = storage_status()
    # Total directory guard: screenshots are disposable before semantic history.
    if status["total_bytes"] >= int(TOTAL_MAX_BYTES * 0.95) and status["screenshot_bytes"]:
        desired_shots = max(0, min(SCREENSHOT_TARGET_BYTES, TOTAL_TARGET_BYTES - status["database_bytes"] - status["other_bytes"]))
        prune_screenshots_to(desired_shots)
        status = storage_status()
    # Database guard: start pruning well before SQLite's absolute max_page_count.
    if status["database_bytes"] > DB_SOFT_BYTES:
        prune_database_to_target(DB_TARGET_BYTES)
        status = storage_status()
    return status


def cleanup_if_needed() -> None:
    # Retention cleanup remains cheap/probabilistic; hard capacity enforcement is
    # performed on every ingest independently.
    if secrets.randbelow(50) != 0:
        return
    frame_cutoff = now_ts() - RETENTION_HOURS * 3600
    ordinary_shot_cutoff = now_ts() - SCREENSHOT_RETENTION_HOURS * 3600
    keyframe_shot_cutoff = now_ts() - KEYFRAME_SCREENSHOT_RETENTION_HOURS * 3600
    paths: list[str] = []
    with db() as conn:
        rows = conn.execute(
            "SELECT screenshot_path FROM frames WHERE screenshot_path IS NOT NULL AND "
            "((is_keyframe=0 AND ts < ?) OR (is_keyframe=1 AND ts < ?))",
            (ordinary_shot_cutoff, keyframe_shot_cutoff),
        ).fetchall()
        paths = [r[0] for r in rows if r[0]]
        conn.execute(
            "UPDATE frames SET screenshot_path=NULL,screenshot_mime=NULL,screenshot_sha256=NULL,screenshot_bytes=0 "
            "WHERE screenshot_path IS NOT NULL AND ((is_keyframe=0 AND ts < ?) OR (is_keyframe=1 AND ts < ?))",
            (ordinary_shot_cutoff, keyframe_shot_cutoff),
        )
        conn.execute("DELETE FROM frames WHERE ts < ?", (frame_cutoff,))
        event_cutoff = now_ts() - EVENT_RETENTION_HOURS * 3600
        conn.execute("DELETE FROM timeline_events WHERE ts < ?", (event_cutoff,))
        browser_cutoff = now_ts() - BROWSER_RETENTION_HOURS * 3600
        conn.execute("DELETE FROM browser_snapshots WHERE ts < ?", (browser_cutoff,))
        dev_cutoff = now_ts() - DEV_EVENT_RETENTION_HOURS * 3600
        conn.execute("DELETE FROM dev_events WHERE ts < ?", (dev_cutoff,))
        memory_cutoff = now_ts() - MEMORY_RETENTION_HOURS * 3600
        conn.execute("DELETE FROM semantic_events WHERE ts < ?", (memory_cutoff,))
        conn.execute("DELETE FROM memory_episodes WHERE end_ts < ?", (memory_cutoff,))
        conn.execute("DELETE FROM memory_sessions WHERE end_ts < ?", (memory_cutoff,))
        conn.execute("DELETE FROM causal_links WHERE effect_ts < ?", (memory_cutoff,))
        conn.execute("DELETE FROM vision_candidates WHERE (frame_ts < ? AND status!='pending') OR frame_id NOT IN (SELECT id FROM frames)", (memory_cutoff,))
    _unlink_paths(paths)


def source_dict(row: sqlite3.Row) -> dict[str, Any]:
    t = now_ts()
    age = max(0.0, t - float(row["last_seen"]))
    enabled = bool(row["monitoring_enabled"]) if "monitoring_enabled" in row.keys() else True
    last_poll = float(row["last_control_poll"] or 0) if "last_control_poll" in row.keys() else 0.0
    control_age = max(0.0, t - last_poll) if last_poll > 0 else None
    agent_reachable = control_age is not None and control_age < 20
    last_browser_seen = float(row["last_browser_seen"] or 0) if "last_browser_seen" in row.keys() else 0.0
    browser_age = max(0.0, t - last_browser_seen) if last_browser_seen > 0 else None
    browser_reachable = browser_age is not None and browser_age < 20
    online = enabled and age < 20
    if not enabled:
        state = "monitoring_off" if agent_reachable else "monitoring_off_agent_unreachable"
    elif online:
        state = "monitoring"
    elif agent_reachable:
        state = "starting"
    else:
        state = "offline"
    return {
        "id": row["id"], "label": row["label"], "device_id": row["device_id"],
        "hostname": row["hostname"], "platform": row["platform"], "agent_version": row["agent_version"],
        "last_seen": now_iso(float(row["last_seen"])), "age_seconds": round(age, 2), "online": online,
        "monitoring_enabled": enabled, "dev_observer_enabled": bool(row["dev_observer_enabled"]) if "dev_observer_enabled" in row.keys() else True,
        "state": state, "agent_reachable": agent_reachable,
        "last_control_poll": now_iso(last_poll) if last_poll else None,
        "control_age_seconds": round(control_age, 2) if control_age is not None else None,
        "browser_reachable": browser_reachable,
        "last_browser_seen": now_iso(last_browser_seen) if last_browser_seen else None,
        "browser_age_seconds": round(browser_age, 2) if browser_age is not None else None,
        "control_updated_at": now_iso(float(row["control_updated_at"])) if "control_updated_at" in row.keys() and float(row["control_updated_at"] or 0) else None,
        "metadata": load_json(row["metadata_json"], {}),
    }


def resolve_source(source: str | None = None) -> sqlite3.Row:
    with db() as conn:
        if source:
            row = conn.execute(
                "SELECT * FROM sources WHERE id=? OR label=? OR device_id=? ORDER BY last_seen DESC LIMIT 1",
                (source, source, source),
            ).fetchone()
        else:
            row = conn.execute("SELECT * FROM sources ORDER BY last_seen DESC LIMIT 1").fetchone()
    if not row:
        raise ValueError("no AVC observation source is registered" if not source else f"AVC source not found: {source}")
    return row


def frame_dict(row: sqlite3.Row, text_limit: int = 12_000, include_structures: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {
        "frame_id": row["id"], "source_id": row["source_id"], "seq": row["seq"],
        "timestamp": now_iso(float(row["ts"])), "age_seconds": round(max(0, now_ts() - float(row["ts"])), 2),
        "app": row["app"], "window_title": row["window_title"], "url": row["url"],
        "active_tab": row["active_tab"] if "active_tab" in row.keys() else "",
        "focus": load_json(row["focus_json"], {}) if "focus_json" in row.keys() else {},
        "cursor": load_json(row["cursor_json"], {}) if "cursor_json" in row.keys() else {},
        "visual_hash": row["visual_hash"] if "visual_hash" in row.keys() else "",
        "surface": row["surface"],
        "summary": row["summary"], "visible_text": clip_text(row["visible_text"], max(0, min(text_limit, 32_000))),
        "events": load_json(row["events_json"], []), "changes": load_json(row["changes_json"], []),
        "screenshot_available": bool(row["screenshot_path"] and Path(row["screenshot_path"]).exists()),
        "screenshot_sha256": row["screenshot_sha256"], "width": row["width"], "height": row["height"],
        "is_keyframe": bool(row["is_keyframe"]) if "is_keyframe" in row.keys() else False,
        "importance": float(row["importance"] or 0) if "importance" in row.keys() else 0.0,
        "keyframe_reasons": load_json(row["keyframe_reasons_json"], []) if "keyframe_reasons_json" in row.keys() else [],
        "visual_change": load_json(row["visual_change_json"], {}) if "visual_change_json" in row.keys() else {},
        "metadata": load_json(row["metadata_json"], {}),
    }
    cache = vision_cache_for(row["id"])
    if cache:
        out["vision_cache"] = cache
    if include_structures:
        out["uia"] = load_json(row["uia_json"], {})
        out["dom"] = load_json(row["dom_json"], {})
    return out


def vision_cache_for(frame_id: str) -> dict[str, Any] | None:
    with db() as conn:
        row = conn.execute("SELECT * FROM vision_cache WHERE frame_id=?", (frame_id,)).fetchone()
    if not row:
        return None
    return {
        "frame_id": row["frame_id"],
        "source_id": row["source_id"] if "source_id" in row.keys() else "",
        "frame_timestamp": now_iso(float(row["frame_ts"])) if "frame_ts" in row.keys() and float(row["frame_ts"] or 0) else None,
        "app": row["app"] if "app" in row.keys() else "",
        "window_title": row["window_title"] if "window_title" in row.keys() else "",
        "url": row["url"] if "url" in row.keys() else "",
        "description": row["description"],
        "page_type": row["page_type"],
        "objects": load_json(row["objects_json"], []),
        "controls": load_json(row["controls_json"], []),
        "entities": load_json(row["entities_json"], []),
        "model": row["model"],
        "generated_at": now_iso(float(row["generated_at"])),
    }


def frame_row(frame_id: str) -> sqlite3.Row | None:
    with db() as conn:
        return conn.execute("SELECT * FROM frames WHERE id=? LIMIT 1", (frame_id,)).fetchone()


def frame_has_image(row: sqlite3.Row) -> bool:
    return bool(row["screenshot_path"] and Path(row["screenshot_path"]).exists())


def _change_types(row: sqlite3.Row) -> set[str]:
    out: set[str] = set()
    for item in load_json(row["changes_json"], []):
        if isinstance(item, dict) and item.get("type"):
            out.add(str(item["type"]))
    for item in load_json(row["events_json"], []):
        if isinstance(item, dict) and item.get("type"):
            out.add(str(item["type"]))
        elif isinstance(item, str):
            out.add(item)
    return out


def _frame_key(row: sqlite3.Row) -> tuple[str, str, str, str]:
    active_tab = row["active_tab"] if "active_tab" in row.keys() else ""
    return (row["app"] or "", row["window_title"] or "", row["url"] or "", active_tab or "")


def select_keyframes(rows: list[sqlite3.Row], max_frames: int = 6) -> list[sqlite3.Row]:
    """Choose visual keyframes from a chronological frame sequence.

    Preserve transition entries/exits and stable segment endpoints, then fill temporal
    gaps. Exact duplicate screenshots are de-duplicated unless they are endpoints.
    """
    if not rows:
        return []
    max_frames = max(1, min(int(max_frames), 12))
    if len(rows) <= max_frames:
        return rows

    weights = {
        "app_changed": 100, "window_changed": 90, "url_changed": 95,
        "active_tab_changed": 90, "dialog_opened": 90, "dialog_closed": 85,
        "focus_changed": 55, "significant_visual_change": 75,
        "visible_text_changed": 30, "browser_dom_changed": 65, "scroll_stopped": 25, "scroll_started": 15,
    }
    scores: dict[int, float] = {0: 70.0, len(rows)-1: 120.0}
    reasons: dict[int, set[str]] = {0: {"range_start"}, len(rows)-1: {"range_end"}}

    last_key = _frame_key(rows[0])
    for i, row in enumerate(rows):
        types = _change_types(row)
        if types:
            score = sum(weights.get(t, 18) for t in types)
            scores[i] = max(scores.get(i, 0), score)
            reasons.setdefault(i, set()).update(types)
        key = _frame_key(row)
        if i > 0 and key != last_key:
            # Last frame of the previous segment = stable exit; current = new state.
            scores[i-1] = max(scores.get(i-1, 0), 68)
            reasons.setdefault(i-1, set()).add("stable_segment_end")
            scores[i] = max(scores.get(i, 0), 88)
            reasons.setdefault(i, set()).add("state_entry")
        last_key = key
        if frame_has_image(row):
            scores[i] = scores.get(i, 0) + 5

    # Segment endpoints even when observer did not emit a typed transition event.
    start = 0
    for i in range(1, len(rows)+1):
        if i == len(rows) or _frame_key(rows[i]) != _frame_key(rows[start]):
            end = i - 1
            scores[end] = max(scores.get(end, 0), 60)
            reasons.setdefault(end, set()).add("stable_segment_end")
            start = i

    selected: set[int] = {0, len(rows)-1}
    ranked = sorted((i for i in scores if i not in selected), key=lambda i: (scores[i], float(rows[i]["ts"])), reverse=True)
    for i in ranked:
        if len(selected) >= max_frames:
            break
        # Avoid exact screenshot duplicates unless this is a strong transition.
        sha = rows[i]["screenshot_sha256"] or ""
        duplicate = bool(sha and any((rows[j]["screenshot_sha256"] or "") == sha for j in selected))
        if duplicate and scores.get(i, 0) < 80:
            continue
        selected.add(i)

    # Fill remaining slots from the largest temporal gaps for continuity.
    while len(selected) < max_frames:
        ordered = sorted(selected)
        best = None
        best_gap = -1.0
        for a, b in zip(ordered, ordered[1:]):
            if b - a <= 1:
                continue
            midpoint_ts = (float(rows[a]["ts"]) + float(rows[b]["ts"])) / 2
            choices = [k for k in range(a+1, b) if k not in selected]
            if not choices:
                continue
            k = min(choices, key=lambda x: abs(float(rows[x]["ts"]) - midpoint_ts))
            gap = float(rows[b]["ts"]) - float(rows[a]["ts"])
            if gap > best_gap:
                best, best_gap = k, gap
        if best is None:
            break
        selected.add(best)
        reasons.setdefault(best, set()).add("timeline_gap_fill")

    result = [rows[i] for i in sorted(selected)]
    # Attach transient selection reasons through metadata only in manifests; rows stay immutable.
    _KEYFRAME_REASONS.clear()
    for i in selected:
        _KEYFRAME_REASONS[rows[i]["id"]] = sorted(reasons.get(i, {"selected"}))
    return result


_KEYFRAME_REASONS: dict[str, list[str]] = {}


def keyframe_manifest(row: sqlite3.Row, image_index: int | None = None) -> dict[str, Any]:
    cache = vision_cache_for(row["id"])
    return {
        "frame_id": row["id"],
        "timestamp": now_iso(float(row["ts"])),
        "app": row["app"], "window_title": row["window_title"], "url": row["url"],
        "active_tab": row["active_tab"] if "active_tab" in row.keys() else "",
        "focus": load_json(row["focus_json"], {}) if "focus_json" in row.keys() else {},
        "cursor": load_json(row["cursor_json"], {}) if "cursor_json" in row.keys() else {},
        "visual_hash": row["visual_hash"] if "visual_hash" in row.keys() else "",
        "surface": row["surface"], "summary": row["summary"],
        "changes": load_json(row["changes_json"], []), "events": load_json(row["events_json"], []),
        "selection_reasons": _KEYFRAME_REASONS.get(row["id"], []),
        "persistent_keyframe": bool(row["is_keyframe"]) if "is_keyframe" in row.keys() else False,
        "importance": float(row["importance"] or 0) if "importance" in row.keys() else 0.0,
        "keyframe_reasons": load_json(row["keyframe_reasons_json"], []) if "keyframe_reasons_json" in row.keys() else [],
        "visual_change": load_json(row["visual_change_json"], {}) if "visual_change_json" in row.keys() else {},
        "screenshot_available": frame_has_image(row),
        "image_index": image_index,
        "vision_cache": cache,
    }


def _rows_for_range(source_id: str, start_ts: float, end_ts: float, raw_limit: int = 1200) -> list[sqlite3.Row]:
    if end_ts < start_ts:
        start_ts, end_ts = end_ts, start_ts
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM frames WHERE source_id=? AND ts>=? AND ts<=? ORDER BY ts ASC LIMIT ?",
            (source_id, start_ts, end_ts, max(1, min(int(raw_limit), 5000))),
        ).fetchall()
    return list(rows)


def _parse_time_value(value: str | float | int) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    raw = str(value).strip()
    try:
        return float(raw)
    except Exception:
        pass
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _visual_result(source_row: sqlite3.Row, rows: list[sqlite3.Row], max_frames: int, label: str) -> CallToolResult:
    selected = select_keyframes(rows, max_frames)
    content: list[Any] = []
    manifests: list[dict[str, Any]] = []
    image_no = 0
    for row in selected:
        idx = None
        if frame_has_image(row):
            image_no += 1
            idx = image_no
        manifests.append(keyframe_manifest(row, idx))
    payload = {
        "ok": True, "source": source_dict(source_row), "selection": label,
        "raw_frame_count": len(rows), "selected_frame_count": len(selected),
        "image_count": image_no, "frames": manifests,
        "note": "Images are returned in chronological order. image_index maps each manifest entry to the following MCP image content.",
    }
    content.append(TextContent(type="text", text=json.dumps(payload, ensure_ascii=False, indent=2)))
    for manifest, row in zip(manifests, selected):
        if not frame_has_image(row):
            continue
        content.append(TextContent(type="text", text=f"IMAGE {manifest['image_index']} — {row['id']} — {now_iso(float(row['ts']))} — {row['app']} — {row['window_title']}"))
        content.append(ImageContent(
            type="image",
            data=base64.b64encode(Path(row["screenshot_path"]).read_bytes()).decode("ascii"),
            mime_type=row["screenshot_mime"] or "image/jpeg",
        ))
    return CallToolResult(content=content, is_error=False)


def latest_frame(source_id: str) -> sqlite3.Row | None:
    with db() as conn:
        return conn.execute("SELECT * FROM frames WHERE source_id=? ORDER BY ts DESC LIMIT 1", (source_id,)).fetchone()


def timeline_data(source_id: str, seconds: int = 60, limit: int = 40) -> dict[str, Any]:
    """Return a bounded overview sampled across the entire requested window."""
    seconds = max(1, min(int(seconds), 86400))
    limit = max(2, min(int(limit), 500))
    end_ts = now_ts(); cutoff = end_ts - seconds
    with db() as conn:
        total = int(conn.execute(
            "SELECT COUNT(*) FROM frames WHERE source_id=? AND ts>=? AND ts<=?",
            (source_id, cutoff, end_ts),
        ).fetchone()[0])
        if total <= limit:
            rows = conn.execute(
                "SELECT * FROM frames WHERE source_id=? AND ts>=? AND ts<=? ORDER BY ts ASC",
                (source_id, cutoff, end_ts),
            ).fetchall()
        else:
            # Choose exactly <=limit ordinal positions, including first and last.
            ordinals = sorted({1 + round(i * (total - 1) / (limit - 1)) for i in range(limit)})
            marks = ','.join('?' for _ in ordinals)
            rows = conn.execute(
                f"""WITH ordered AS (
                     SELECT *, ROW_NUMBER() OVER (ORDER BY ts ASC) AS rn
                     FROM frames WHERE source_id=? AND ts>=? AND ts<=?
                   )
                   SELECT * FROM ordered WHERE rn IN ({marks}) ORDER BY ts ASC""",
                (source_id, cutoff, end_ts, *ordinals),
            ).fetchall()
    items=[]
    for r in rows:
        items.append({
            "frame_id": r["id"], "timestamp": now_iso(float(r["ts"])), "app": r["app"],
            "window_title": r["window_title"], "url": r["url"], "surface": r["surface"],
            "summary": r["summary"], "changes": load_json(r["changes_json"], []), "events": load_json(r["events_json"], []),
        })
    return {
        "requested_seconds": seconds, "raw_frame_count": total, "returned_frame_count": len(items),
        "truncated": total > len(items),
        "requested_start": now_iso(cutoff), "requested_end": now_iso(end_ts),
        "coverage_start": items[0]["timestamp"] if items else None,
        "coverage_end": items[-1]["timestamp"] if items else None,
        "frames": items,
        "usage_note": "A truncated timeline is a sampled overview across the full window. Use avc_timepoint or avc_frame_replay for an exact historical moment.",
    }


def browser_snapshot_dict(row: sqlite3.Row | None, text_limit: int = 24000, include_dom: bool = True) -> dict[str, Any] | None:
    if not row:
        return None
    out = {
        "snapshot_id": row["id"], "source_id": row["source_id"],
        "timestamp": now_iso(float(row["ts"])), "age_seconds": round(max(0, now_ts()-float(row["ts"])),2),
        "tab_id": row["tab_id"], "window_id": row["window_id"], "active": bool(row["active"]),
        "url": row["url"], "title": row["title"],
        "visible_text": clip_text(row["visible_text"], max(0,min(int(text_limit),32000))),
        "focus": load_json(row["focus_json"], {}), "viewport": load_json(row["viewport_json"], {}),
        "semantic_hash": row["semantic_hash"], "extension_version": row["extension_version"],
        "metadata": load_json(row["metadata_json"], {}),
    }
    if include_dom:
        out["dom"] = load_json(row["dom_json"], {})
    return out



def _browser_title_core(value: str) -> str:
    x=(value or "").strip().lower()
    for suffix in (" - google chrome"," - microsoft edge"," — mozilla firefox"," - brave"," - opera"):
        if x.endswith(suffix):x=x[:-len(suffix)]
    return re.sub(r"\s+"," ",x).strip()


def browser_title_matches(window_title: str, page_title: str) -> bool:
    a=_browser_title_core(window_title); b=_browser_title_core(page_title)
    if not a or not b:return False
    if a==b:return True
    if min(len(a),len(b))>=6 and (a in b or b in a):return True
    return difflib.SequenceMatcher(None,a,b).ratio() >= 0.78


def latest_browser_snapshot(source_id: str, around_ts: float | None = None, max_delta: float = 8.0) -> sqlite3.Row | None:
    with db() as conn:
        if around_ts is None:
            return conn.execute("SELECT * FROM browser_snapshots WHERE source_id=? ORDER BY ts DESC LIMIT 1", (source_id,)).fetchone()
        return conn.execute(
            "SELECT * FROM browser_snapshots WHERE source_id=? AND ts BETWEEN ? AND ? ORDER BY ABS(ts-?) ASC LIMIT 1",
            (source_id, around_ts-max_delta, around_ts+max_delta, around_ts),
        ).fetchone()


def auth_browser(request: Request) -> sqlite3.Row | None:
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        return None
    digest = token_hash(auth.split(" ",1)[1].strip())
    with db() as conn:
        return conn.execute("SELECT * FROM sources WHERE browser_token_hash=? LIMIT 1", (digest,)).fetchone()


def browser_history_data(source_id: str, seconds: int = 120, limit: int = 40) -> list[dict[str, Any]]:
    seconds=max(1,min(int(seconds),3600)); limit=max(1,min(int(limit),100))
    cutoff=now_ts()-seconds
    with db() as conn:
        rows=conn.execute("SELECT * FROM browser_snapshots WHERE source_id=? AND ts>=? ORDER BY ts DESC LIMIT ?",(source_id,cutoff,limit)).fetchall()
    return [browser_snapshot_dict(r, text_limit=2000, include_dom=False) for r in reversed(rows)]




def _json_strings(value: Any, limit: int = 500) -> set[str]:
    out: set[str] = set()
    def walk(v: Any) -> None:
        if len(out) >= limit:
            return
        if isinstance(v, str):
            x = v.strip()
            if x and len(x) <= 500:
                out.add(x)
        elif isinstance(v, dict):
            for k, x in v.items():
                if isinstance(k, str) and k not in {"x","y","width","height","enabled"}:
                    walk(k)
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)
    walk(value)
    return out


def _frame_strings(row: sqlite3.Row) -> set[str]:
    out = set()
    for key in ("window_title","url","active_tab","visible_text","summary"):
        try:
            val = row[key]
            if val:
                for line in str(val).splitlines():
                    line=line.strip()
                    if line and len(line)<=500:
                        out.add(line)
        except Exception:
            pass
    out |= _json_strings(load_json(row["uia_json"], {}), 350)
    out |= _json_strings(load_json(row["dom_json"], {}), 350)
    return out


def _focus_label(row: sqlite3.Row) -> str:
    try:
        f=load_json(row["focus_json"], {})
        role=_norm_role(str(f.get("role") or f.get("tag") or ""))
        if role in {"document","region","window","pane","group","text","unknown"}: return ""
        name=str(f.get("name") or f.get("value") or f.get("automation_id") or "").strip()
        if not name or len(name)>120: return ""
        return clip_text(name,120)
    except Exception:
        return ""


def _anomaly_from_frame(row: sqlite3.Row) -> tuple[bool,str,str]:
    title=str(row["window_title"] or "")
    title_low=title.lower()
    app=str(row["app"] or "").lower()
    changes=load_json(row["changes_json"], [])
    events=load_json(row["events_json"], [])
    # 0.9 only promotes structured failure evidence. Ordinary page/chat/document text
    # is never treated as an error merely because it contains words such as "失败".
    explicit={
        "app_crash":"critical", "fatal_error":"critical", "error_dialog":"high",
        "not_responding":"high", "connection_failed":"high", "timeout":"high",
        "http_error":"medium", "network_error":"high", "resource_failed":"medium",
        "permission_denied":"medium",
    }
    for item in list(changes or []) + list(events or []):
        if not isinstance(item,dict):
            continue
        typ=str(item.get("type") or "").lower()
        if typ in explicit:
            return True,explicit[typ],typ
        severity=str(item.get("severity") or "").lower()
        if item.get("anomaly") and severity in {"medium","high","critical"}:
            return True,severity,typ or "structured_anomaly"
    # Windows itself marks a hung application in the window caption. This is a
    # system signal, not an interpretation of document content.
    if "not responding" in title_low or "未响应" in title:
        return True,"high","not_responding"
    # A real alert/dialog landmark plus failure semantics is acceptable evidence.
    dom=load_json(row["dom_json"], {})
    landmarks=dom.get("landmarks",[]) if isinstance(dom,dict) else []
    for landmark in landmarks if isinstance(landmarks,list) else []:
        if not isinstance(landmark,dict) or str(landmark.get("role") or "").lower() not in {"alert","alertdialog","dialog"}:
            continue
        label=(str(landmark.get("name") or "")+" "+title).lower()
        for term,sev in (("fatal","critical"),("crash","critical"),("exception","critical"),("error","high"),("failed","high"),("错误","high"),("失败","high")):
            if term in label:
                return True,sev,"dialog_"+term
    # Low-information/blank-like visual state: screenshot exists but almost no semantic text.
    meta=load_json(row["metadata_json"], {})
    has_bridge=bool(meta.get("browser_bridge"))
    dom_has_content=bool((dom.get("controls") if isinstance(dom,dict) else None) or (dom.get("headings") if isinstance(dom,dict) else None))
    if row["screenshot_path"] and has_bridge and len(str(row["visible_text"] or "").strip()) < 8 and not dom_has_content and app in {"chrome","msedge","firefox","brave","opera"}:
        return True,"low","browser_low_information_frame"
    return False,"",""


def _human_event(row: sqlite3.Row, previous: sqlite3.Row | None = None) -> list[dict[str,Any]]:
    changes=load_json(row["changes_json"], [])
    types={str(x.get("type") or "") for x in changes if isinstance(x,dict)}
    by_type={str(x.get("type") or ""):x for x in changes if isinstance(x,dict)}
    focus=_focus_label(row)
    results=[]
    def add(t,summary,confidence=0.8,detail=None):
        results.append({"type":t,"summary":summary,"confidence":confidence,"detail":detail or {}})
    if "app_changed" in types:
        c=by_type["app_changed"]; add("app_switch",f"用户从 {c.get('from') or '其他应用'} 切换到 {c.get('to') or row['app']}",0.96,c)
    if "window_changed" in types:
        c=by_type["window_changed"]; target=c.get("to") or row["window_title"]
        add("window_change",f"用户打开或切换到窗口「{clip_text(target,180)}」",0.94,c)
    if "url_changed" in types:
        c=by_type["url_changed"]; target=c.get("to") or row["url"]
        add("navigation",f"浏览器导航到 {clip_text(target,220)}",0.97,c)
    if "active_tab_changed" in types:
        c=by_type["active_tab_changed"]; target=c.get("to") or row["active_tab"]
        add("tab_switch",f"用户切换到浏览器标签「{clip_text(target,180)}」",0.94,c)
    if "focus_changed" in types and focus:
        # Focus-only churn is useful at raw Frame level but too noisy for semantic memory.
        pass
    if "dialog_opened" in types:
        add("dialog_opened","界面出现新的对话框或弹窗",0.9,by_type.get("dialog_opened",{}))
    if "dialog_closed" in types:
        add("dialog_closed","对话框或弹窗被关闭",0.9,by_type.get("dialog_closed",{}))
    if not results and "significant_visual_change" in types:
        dist=int((by_type.get("significant_visual_change") or {}).get("distance") or 0)
        if dist >= 14:
            add("visual_update","界面发生明显视觉变化",0.66,by_type.get("significant_visual_change",{}))
    # browser_dom_changed / visible_text_changed remain searchable at Frame level, but
    # are intentionally not promoted to semantic memory on their own.
    anomaly,severity,reason=_anomaly_from_frame(row)
    if anomaly:
        add("anomaly",f"检测到可能的异常：{reason}",0.82,{"severity":severity,"reason":reason,"anomaly":True})
    return results


def _episode_title(events: list[dict[str,Any]]) -> str:
    text=" ".join((e.get("window_title") or "")+" "+(e.get("url") or "")+" "+(e.get("summary") or "") for e in events).lower()
    if "chrome://extensions" in text or "extensions" in text and "chrome" in text:
        return "浏览器扩展管理"
    if "localhost" in text or "127.0.0.1" in text:
        return "本地网页调试"
    if "aimanvisualcopilot" in text or "avc" in text and "monitor" in text:
        return "AVC 调试与观察"
    if "powershell" in text or "terminal" in text or "cmd.exe" in text:
        return "终端操作"
    if any(x in text for x in ("error","failed","offline","exception","错误","失败")):
        return "异常排查"
    titles=[e.get("window_title") for e in events if e.get("window_title")]
    if titles:
        t=max(titles,key=len)
        for suffix in (" - Google Chrome"," - Microsoft Edge"," — Mozilla Firefox"):
            t=t.replace(suffix,"")
        return clip_text(t,120)
    apps=[e.get("app") for e in events if e.get("app")]
    return (apps[0] if apps else "桌面活动")+" 操作"


def rebuild_causal_links(source_id: str, hours: int = 6) -> int:
    hours=max(1,min(int(hours),72)); cutoff=now_ts()-hours*3600; now=now_ts()
    # 0.9 causality requires an explicit observed input. Focus proximity alone is no
    # longer accepted because focus can change without a click or key press.
    with db() as conn:
        inputs=conn.execute("SELECT * FROM interaction_events WHERE source_id=? AND ts>=? ORDER BY ts ASC",(source_id,cutoff)).fetchall()
        effects=conn.execute("""SELECT * FROM timeline_events WHERE source_id=? AND ts>=? AND event_type IN
          ('url_changed','browser_url_changed','active_tab_changed','window_changed','dialog_opened','dialog_closed','browser_dom_changed','significant_visual_change')
          ORDER BY ts ASC""",(source_id,cutoff)).fetchall()
        dev_rows=conn.execute("SELECT * FROM dev_events WHERE source_id=? AND ts>=? AND severity IN ('medium','high','critical') ORDER BY ts ASC",(source_id,cutoff)).fetchall()
    links=[]
    def nearest_input(effect_ts: float, max_seconds: float = 5.0):
        candidates=[i for i in inputs if 0 <= effect_ts-float(i["ts"]) <= max_seconds]
        return max(candidates,key=lambda x:float(x["ts"])) if candidates else None
    for effect in effects:
        cause=nearest_input(float(effect["ts"]),5.0)
        if not cause: continue
        latency=max(0,int((float(effect["ts"])-float(cause["ts"]))*1000))
        kind=str(cause["kind"] or "input"); etype=str(effect["event_type"] or "change")
        label=str(cause["target_name"] or cause["key_name"] or cause["target_role"] or kind)
        detail=load_json(effect["detail_json"],{})
        direct_nav=etype in {"url_changed","browser_url_changed","active_tab_changed"}
        confidence=0.97 if direct_nav and kind in {"click","submit"} else 0.95 if direct_nav and cause["key_name"]=="Enter" else 0.86 if kind in {"click","submit","key"} else 0.76
        summary=(f"界面导航到 {clip_text(detail.get('to') or effect['url'],220)}" if direct_nav else
                 f"输入事件后发生 {etype}")
        cid="cause_"+sha256_text(f"{source_id}:{cause['id']}:{effect['id']}:{etype}")[:24]
        links.append((cid,source_id,cause["frame_id"],effect["frame_id"],float(cause["ts"]),float(effect["ts"]),
                      "observed_"+kind,label,etype,summary,latency,confidence,
                      safe_json({"interaction_id":cause["id"],"selector":cause["selector"],"key":cause["key_name"],"effect_change":detail},16000),now))
    for d in dev_rows:
        cause=nearest_input(float(d["ts"]),5.0)
        if not cause: continue
        latency=max(0,int((float(d["ts"])-float(cause["ts"]))*1000)); kind=str(cause["kind"] or "input")
        label=str(cause["target_name"] or cause["key_name"] or cause["target_role"] or kind)
        effect="dev_"+str(d["event_type"] or "event")
        cid="cause_"+sha256_text(f"dev:{source_id}:{cause['id']}:{d['id']}")[:24]
        links.append((cid,source_id,cause["frame_id"],"",float(cause["ts"]),float(d["ts"]),"observed_"+kind,label,
                      effect,clip_text(d["summary"],500),latency,0.93,
                      safe_json({"interaction_id":cause["id"],"dev_event_id":d["id"],"severity":d["severity"]},16000),now))

    with db() as conn:
        conn.execute("DELETE FROM causal_links WHERE source_id=? AND effect_ts>=?",(source_id,cutoff))
        if links:
            conn.executemany("INSERT OR REPLACE INTO causal_links(id,source_id,cause_frame_id,effect_frame_id,cause_ts,effect_ts,cause_type,cause_label,effect_type,effect_summary,latency_ms,confidence,detail_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",links)
    return len(links)


def rebuild_semantic_memory(source_id: str, hours: int = 6) -> dict[str,Any]:
    hours=max(1,min(int(hours),72)); cutoff=now_ts()-hours*3600; now=now_ts()
    with db() as conn:
        frames=conn.execute("SELECT * FROM frames WHERE source_id=? AND ts>=? ORDER BY ts ASC",(source_id,cutoff)).fetchall()
    semantic=[]; prev=None; last_sig=None
    for row in frames:
        raw_events=_human_event(row,prev)
        anomalies=[e for e in raw_events if e["type"]=="anomaly" or e.get("detail",{}).get("anomaly")]
        normal=[e for e in raw_events if e not in anomalies]
        frame_events=[]
        if normal:
            # One human-facing event per Frame. Preserve atomic components in detail for drill-down.
            summaries=[]
            for e in normal:
                if e["summary"] not in summaries: summaries.append(e["summary"])
            primary=normal[0]["type"] if len(normal)==1 else "compound_transition"
            frame_events.append({"type":primary,"summary":"；".join(summaries[:5]),"confidence":max(e["confidence"] for e in normal),"detail":{"components":[{"type":e["type"],"summary":e["summary"],"detail":e.get("detail",{})} for e in normal]}})
        frame_events += anomalies[:1]
        for idx,e in enumerate(frame_events):
            sig=(e["type"],e["summary"],row["window_title"])
            if last_sig and sig==last_sig[0] and float(row["ts"])-last_sig[1] < 3.0:
                continue
            anomaly=1 if e["type"]=="anomaly" or e.get("detail",{}).get("anomaly") else 0
            severity=e.get("detail",{}).get("severity","")
            semantic.append({"id":"sev_"+sha256_text(f"{row['id']}:{idx}:{e['type']}")[:24],"source_id":source_id,"frame_id":row["id"],"ts":float(row["ts"]),"event_type":e["type"],"summary":e["summary"],"app":row["app"],"window_title":row["window_title"],"url":row["url"],"confidence":float(e["confidence"]),"anomaly":anomaly,"severity":severity,"detail":e.get("detail",{})})
            last_sig=(sig,float(row["ts"]))
        prev=row

    # 0.8 Dev Observer events join semantic memory instead of living in a separate log silo.
    with db() as conn:
        dev_rows=conn.execute("SELECT * FROM dev_events WHERE source_id=? AND ts>=? ORDER BY ts ASC",(source_id,cutoff)).fetchall()
    frame_times=[float(r["ts"]) for r in frames]
    for d in dev_rows:
        sev=(d["severity"] or "info").lower(); typ=str(d["event_type"] or "dev_event")
        anomaly = 1 if sev in {"medium","high","critical"} or typ in {"js_error","unhandled_rejection","network_error"} else 0
        conf = {"critical":0.99,"high":0.98,"medium":0.94,"low":0.82,"info":0.72}.get(sev,0.72)
        nearest_id=""
        if frames:
            # Small recent working sets make a linear nearest lookup cheap and deterministic.
            nearest=min(frames,key=lambda r:abs(float(r["ts"])-float(d["ts"])))
            if abs(float(nearest["ts"])-float(d["ts"])) <= 8.0:
                nearest_id=nearest["id"]
        summary=clip_text(d["summary"] or typ,1000)
        semantic.append({"id":"sev_"+sha256_text(f"dev:{d['id']}")[:24],"source_id":source_id,"frame_id":nearest_id,"ts":float(d["ts"]),"event_type":"dev_"+typ,"summary":summary,"app":"browser-dev","window_title":"","url":d["url"],"confidence":conf,"anomaly":anomaly,"severity":sev if anomaly else "","detail":{"dev_event_id":d["id"],"is_dev_page":bool(d["is_dev_page"]),"detail":load_json(d["detail_json"],{})}})
    # Behavioral anomaly: navigation/reload loops on a developer page.
    navs=[d for d in dev_rows if str(d["event_type"] or "")=="navigation"]
    for i in range(3,len(navs)):
        group=navs[i-3:i+1]
        if float(group[-1]["ts"])-float(group[0]["ts"]) > 60:
            continue
        urls=[str(x["url"] or "") for x in group]
        loop_kind=""
        if len(set(urls))==1:
            loop_kind="repeated_reload"
        elif urls[0]==urls[2] and urls[1]==urls[3] and urls[0]!=urls[1]:
            loop_kind="navigation_loop"
        if loop_kind:
            d=group[-1]; eid="sev_"+sha256_text(f"behavior:{source_id}:{d['id']}:{loop_kind}")[:24]
            summary="检测到开发页面重复刷新循环" if loop_kind=="repeated_reload" else "检测到开发页面在两个地址之间反复跳转"
            semantic.append({"id":eid,"source_id":source_id,"frame_id":"","ts":float(d["ts"]),"event_type":"anomaly_behavior_loop","summary":summary,"app":"browser-dev","window_title":"","url":d["url"],"confidence":0.91,"anomaly":1,"severity":"medium","detail":{"reason":loop_kind,"urls":urls}})

    semantic.sort(key=lambda x:x["ts"])

    with db() as conn:
        conn.execute("DELETE FROM semantic_events WHERE source_id=? AND ts>=?",(source_id,cutoff))
        for e in semantic:
            conn.execute("INSERT OR REPLACE INTO semantic_events(id,source_id,frame_id,ts,event_type,summary,app,window_title,url,confidence,anomaly,severity,detail_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (e["id"],source_id,e["frame_id"],e["ts"],e["event_type"],e["summary"],e["app"],e["window_title"],e["url"],e["confidence"],e["anomaly"],e["severity"],safe_json(e["detail"],32000),now))
    # Episodes: group semantic events by idle gap. The compressed narrative is deterministic and cheap.
    episodes=[]; group=[]
    def flush_group(g):
        if not g: return
        start=g[0]["ts"]; end=g[-1]["ts"]; title=_episode_title(g)
        summaries=[]
        for x in g:
            if x["summary"] not in summaries: summaries.append(x["summary"])
        summary=" → ".join(summaries[:7])
        if len(summaries)>7: summary += f" → …（另有 {len(summaries)-7} 个事件）"
        apps=sorted({x["app"] for x in g if x["app"]}); etypes=sorted({x["event_type"] for x in g})
        eid="epi_"+sha256_text(f"{source_id}:{g[0]['frame_id']}:{g[-1]['frame_id']}")[:24]
        episodes.append({"id":eid,"source_id":source_id,"start_ts":start,"end_ts":end,"title":title,"summary":summary,"start_frame_id":g[0]["frame_id"],"end_frame_id":g[-1]["frame_id"],"event_count":len(g),"confidence":round(sum(x["confidence"] for x in g)/max(1,len(g)),3),"anomaly_count":sum(x["anomaly"] for x in g),"apps":apps,"event_types":etypes})
    for e in semantic:
        if group and (e["ts"]-group[-1]["ts"] > 75 or e["ts"]-group[0]["ts"] > 600):
            flush_group(group); group=[]
        group.append(e)
    flush_group(group)
    with db() as conn:
        conn.execute("DELETE FROM memory_episodes WHERE source_id=? AND start_ts>=?",(source_id,cutoff))
        for e in episodes:
            conn.execute("INSERT OR REPLACE INTO memory_episodes(id,source_id,start_ts,end_ts,title,summary,start_frame_id,end_frame_id,event_count,confidence,anomaly_count,app_json,event_types_json,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (e["id"],source_id,e["start_ts"],e["end_ts"],e["title"],e["summary"],e["start_frame_id"],e["end_frame_id"],e["event_count"],e["confidence"],e["anomaly_count"],safe_json(e["apps"]),safe_json(e["event_types"]),now))
    # Sessions: group episodes separated by more than 20 minutes.
    sessions=[]; eg=[]
    def flush_session(g):
        if not g:return
        st=g[0]["start_ts"]; en=g[-1]["end_ts"]
        titles=[]
        for x in g:
            if x["title"] not in titles: titles.append(x["title"])
        joined=" / ".join(titles[:3])
        lower=joined.lower()
        title="AVC / AIman 调试会话" if ("avc" in lower or "aiman" in lower) else (titles[0] if len(titles)==1 else "工作会话："+joined)
        summary="；".join(x["summary"] for x in g[:5])
        sid="ses_"+sha256_text(f"{source_id}:{g[0]['id']}:{g[-1]['id']}")[:24]
        sessions.append({"id":sid,"start_ts":st,"end_ts":en,"title":clip_text(title,140),"summary":clip_text(summary,3000),"episode_ids":[x["id"] for x in g],"episode_count":len(g),"anomaly_count":sum(x["anomaly_count"] for x in g)})
    for e in episodes:
        if eg and e["start_ts"]-eg[-1]["end_ts"] > 1200:
            flush_session(eg); eg=[]
        eg.append(e)
    flush_session(eg)
    with db() as conn:
        conn.execute("DELETE FROM memory_sessions WHERE source_id=? AND start_ts>=?",(source_id,cutoff))
        for x in sessions:
            conn.execute("INSERT OR REPLACE INTO memory_sessions(id,source_id,start_ts,end_ts,title,summary,episode_ids_json,episode_count,anomaly_count,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                         (x["id"],source_id,x["start_ts"],x["end_ts"],x["title"],x["summary"],safe_json(x["episode_ids"]),x["episode_count"],x["anomaly_count"],now))
    causal_count=rebuild_causal_links(source_id,hours)
    return {"semantic_event_count":len(semantic),"episode_count":len(episodes),"session_count":len(sessions),"causal_link_count":causal_count,"hours":hours}


def _episode_dict(r: sqlite3.Row) -> dict[str,Any]:
    return {"episode_id":r["id"],"start":now_iso(float(r["start_ts"])),"end":now_iso(float(r["end_ts"])),"duration_seconds":round(float(r["end_ts"])-float(r["start_ts"]),2),"title":r["title"],"summary":r["summary"],"start_frame_id":r["start_frame_id"],"end_frame_id":r["end_frame_id"],"event_count":r["event_count"],"confidence":r["confidence"],"anomaly_count":r["anomaly_count"],"apps":load_json(r["app_json"],[]),"event_types":load_json(r["event_types_json"],[])}


def _session_dict(r: sqlite3.Row) -> dict[str,Any]:
    return {"session_id":r["id"],"start":now_iso(float(r["start_ts"])),"end":now_iso(float(r["end_ts"])),"duration_seconds":round(float(r["end_ts"])-float(r["start_ts"]),2),"title":r["title"],"summary":r["summary"],"episode_ids":load_json(r["episode_ids_json"],[]),"episode_count":r["episode_count"],"anomaly_count":r["anomaly_count"]}


def _screenshot_diff(a: sqlite3.Row,b: sqlite3.Row) -> tuple[dict[str,Any], bytes|None]:
    if not frame_has_image(a) or not frame_has_image(b):
        return {"available":False,"reason":"one_or_both_screenshots_not_retained"},None
    try:
        with Image.open(a["screenshot_path"]) as src_a, Image.open(b["screenshot_path"]) as src_b:
            ia=src_a.convert("RGB"); ib=src_b.convert("RGB")
        resized=False
        if ib.size != ia.size:
            ib=ib.resize(ia.size); resized=True
        diff=ImageChops.difference(ia,ib); bbox=diff.getbbox()
        gray=diff.convert("L"); hist=gray.histogram(); total=max(1,ia.size[0]*ia.size[1]); changed=sum(hist[12:]); frac=changed/total
        result={"available":True,"size":{"width":ia.size[0],"height":ia.size[1]},"normalized_size":resized,"changed_fraction":round(frac,5),"changed_pixels_approx":changed,"bbox":None}
        if bbox:
            x1,y1,x2,y2=bbox; result["bbox"]={"x":x1,"y":y1,"width":x2-x1,"height":y2-y1}
            # Crop with a little context, amplify the raw difference for AI inspection.
            pad=20; box=(max(0,x1-pad),max(0,y1-pad),min(ia.size[0],x2+pad),min(ia.size[1],y2+pad))
            crop=ImageEnhance.Brightness(diff.crop(box)).enhance(3.0)
            import io as _io
            bio=_io.BytesIO(); crop.save(bio,format="JPEG",quality=80)
            output=bio.getvalue(); bio.close(); crop.close(); gray.close(); diff.close(); ia.close(); ib.close()
            return result,output
        gray.close(); diff.close(); ia.close(); ib.close()
        return result,None
    except Exception as e:
        return {"available":False,"reason":"diff_error","error":str(e)},None



def _norm_role(role: str, tag: str = "") -> str:
    r=(role or tag or "").lower().replace("controltype.","")
    mapping={"a":"link","hyperlink":"link","input":"textbox","edit":"textbox","textarea":"textbox","tabitem":"tab","menuitem":"menuitem","checkbox":"checkbox","radio":"radio","button":"button","document":"document","pane":"region","listitem":"listitem","text":"text","select":"combobox","combobox":"combobox"}
    return mapping.get(r,r or "unknown")


def _norm_name(value: str) -> str:
    x=re.sub(r"\s+"," ",(value or "").strip().lower())
    x=re.sub(r"[^\w\u4e00-\u9fff]+"," ",x)
    return re.sub(r"\s+"," ",x).strip()[:240]


def _bounds(obj: dict[str,Any]) -> dict[str,int] | None:
    try:
        w=int(float(obj.get("width") or 0)); h=int(float(obj.get("height") or 0))
        if w<=0 or h<=0:return None
        return {"x":int(float(obj.get("x") or 0)),"y":int(float(obj.get("y") or 0)),"width":w,"height":h}
    except Exception:return None


def _center(b: dict[str,int] | None) -> tuple[float,float] | None:
    if not b:return None
    return (b["x"]+b["width"]/2,b["y"]+b["height"]/2)


def _role_compatible(a: str,b: str) -> bool:
    if a==b:return True
    pairs={frozenset(("link","button")),frozenset(("textbox","combobox")),frozenset(("text","link")),frozenset(("region","document"))}
    return frozenset((a,b)) in pairs


def build_scene_graph(row: sqlite3.Row, max_objects: int = 200) -> dict[str,Any]:
    max_objects=max(20,min(int(max_objects),500))
    uia=load_json(row["uia_json"],{}); dom=load_json(row["dom_json"],{})
    uels=uia.get("elements",[]) if isinstance(uia,dict) else []
    dcontrols=dom.get("controls",[]) if isinstance(dom,dict) else []
    headings=dom.get("headings",[]) if isinstance(dom,dict) else []
    landmarks=dom.get("landmarks",[]) if isinstance(dom,dict) else []
    meta=load_json(row["metadata_json"],{})
    bridge=meta.get("browser_bridge",{}) if isinstance(meta,dict) else {}
    viewport={}; browser_focus={}
    snapshot_id=str(bridge.get("snapshot_id") or "")
    if snapshot_id:
        with db() as conn:
            br=conn.execute("SELECT * FROM browser_snapshots WHERE id=? LIMIT 1",(snapshot_id,)).fetchone()
        if br:
            viewport=load_json(br["viewport_json"],{})
            browser_focus=load_json(br["focus_json"],{})

    # Find the browser content root in UIA. It provides an approximate transform from
    # DOM viewport coordinates to screen coordinates without asking GPT to reconcile systems.
    doc_candidates=[]
    for e in uels if isinstance(uels,list) else []:
        if not isinstance(e,dict):continue
        role=_norm_role(str(e.get("role") or ""))
        b=_bounds(e)
        if role in {"document","region"} and b and b["width"]>300 and b["height"]>200:
            score=b["width"]*b["height"]
            nm=_norm_name(str(e.get("name") or ""))
            if nm and nm in _norm_name(str(row["active_tab"] or row["window_title"])):score*=1.5
            doc_candidates.append((score,b,e))
    doc_bounds=max(doc_candidates,key=lambda x:x[0])[1] if doc_candidates else None

    # Calibrate CSS viewport coordinates to Windows physical screen coordinates.
    # Chromium reports DOM bounds in CSS pixels while UIA uses physical pixels.
    dpr=max(0.5,min(float(viewport.get("device_pixel_ratio") or 1.0),4.0))
    css_w=max(1.0,float(viewport.get("width") or 1)); css_h=max(1.0,float(viewport.get("height") or 1))
    scale=dpr
    content_origin=None
    if doc_bounds:
        width_scale=float(doc_bounds["width"])/css_w
        if 0.5 <= width_scale <= 4.0 and abs(width_scale-dpr) <= 0.35:
            scale=width_scale
        content_w=css_w*scale; content_h=css_h*scale
        # Horizontal residual is normally the browser border. Vertical residual is
        # the title/tab/address toolbar when UIA exposes the whole browser document.
        content_origin={
            "x":float(doc_bounds["x"])+max(0.0,(float(doc_bounds["width"])-content_w)/2.0),
            "y":float(doc_bounds["y"])+max(0.0,float(doc_bounds["height"])-content_h),
        }
    elif viewport.get("screen_x") is not None:
        outer_w=float(viewport.get("outer_width") or css_w); outer_h=float(viewport.get("outer_height") or css_h)
        content_origin={
            "x":float(viewport.get("screen_x") or 0)+(outer_w-css_w)/2.0,
            "y":float(viewport.get("screen_y") or 0)+max(0.0,outer_h-css_h),
        }

    uobjs=[]
    for i,e in enumerate(uels if isinstance(uels,list) else []):
        if not isinstance(e,dict):continue
        role=_norm_role(str(e.get("role") or "")); name=clip_text(e.get("name") or e.get("value") or "",300); b=_bounds(e)
        if not name and role not in {"button","textbox","checkbox","radio","tab","menuitem","link"}:continue
        uobjs.append({"idx":i,"role":role,"name":name,"norm":_norm_name(name),"bounds":b,"raw":e})

    dobjs=[]
    for i,e in enumerate(dcontrols if isinstance(dcontrols,list) else []):
        if not isinstance(e,dict):continue
        role=_norm_role(str(e.get("role") or ""),str(e.get("tag") or "")); name=clip_text(e.get("name") or e.get("value") or "",300); vb=_bounds(e)
        sb=None
        if vb and content_origin:
            sb={"x":int(round(content_origin["x"]+vb["x"]*scale)),"y":int(round(content_origin["y"]+vb["y"]*scale)),"width":int(round(vb["width"]*scale)),"height":int(round(vb["height"]*scale))}
        document_bounds=None
        if vb:
            document_bounds={"x":vb["x"]+int(viewport.get("x") or 0),"y":vb["y"]+int(viewport.get("y") or 0),"width":vb["width"],"height":vb["height"]}
        dobjs.append({"idx":i,"role":role,"name":name,"norm":_norm_name(name),"viewport_bounds":vb,"document_bounds":document_bounds,"screen_bounds_estimate":sb,"raw":e})

    by_name={}
    for u in uobjs:
        if u["norm"]:by_name.setdefault(u["norm"],[]).append(u)
    used_u=set(); objects=[]
    focus_name=_norm_name(str((browser_focus or load_json(row["focus_json"],{})).get("name") or ""))
    for d in dobjs:
        candidates=[u for u in by_name.get(d["norm"],[]) if u["idx"] not in used_u and _role_compatible(d["role"],u["role"])] if d["norm"] else []
        if not candidates and d["norm"]:
            candidates=[u for u in uobjs if u["idx"] not in used_u and _role_compatible(d["role"],u["role"]) and u["norm"] and
                        (u["norm"] in d["norm"] or d["norm"] in u["norm"]) and min(len(u["norm"]),len(d["norm"]))>=3]
        match=None
        if candidates:
            dc=_center(d["screen_bounds_estimate"])
            if dc:
                proposed=min(candidates,key=lambda u: (( _center(u["bounds"])[0]-dc[0])**2+( _center(u["bounds"])[1]-dc[1])**2) if _center(u["bounds"]) else 1e18)
                pc=_center(proposed["bounds"])
                if pc and ((pc[0]-dc[0])**2+(pc[1]-dc[1])**2)**0.5 <= max(180,4*max(d["screen_bounds_estimate"]["width"],d["screen_bounds_estimate"]["height"])):
                    match=proposed
            else:match=candidates[0]
        if match:used_u.add(match["idx"])
        sources=["dom"]+(["uia"] if match else [])
        confidence=0.985 if match else 0.94
        raw=d["raw"]
        object_id="obj_"+sha256_text(f"{row['id']}|{d['role']}|{d['norm']}|{d['idx']}")[:18]
        selector=str(raw.get("selector") or ""); dom_id=str(raw.get("id") or ""); href=str(raw.get("href") or "")
        stable_dom_id = dom_id if dom_id and not dom_id.startswith("radix-") else ""
        stable_selector = selector if ":nth-of-type" not in selector and "radix-" not in selector else ""
        stable_key = (href+"|"+d["role"]+"|"+d["norm"]) if href else (("#"+stable_dom_id) if stable_dom_id else (stable_selector or (d["role"]+"|"+d["norm"])))
        stable_id="scene_"+sha256_text(f"{row['source_id']}|dom|{stable_key}")[:20]
        state_hash=sha256_text(json.dumps({"name":d["name"],"value":clip_text(raw.get("value") or "",300),"enabled":not bool(raw.get("disabled")),"focused":bool(focus_name and focus_name==d["norm"]),"bounds":d["viewport_bounds"]},ensure_ascii=False,sort_keys=True))[:16]
        objects.append({"object_id":object_id,"stable_id":stable_id,"state_hash":state_hash,"role":d["role"],"name":d["name"],"value":clip_text(raw.get("value") or "",300),"sources":sources,"confidence":confidence,"focused":bool(focus_name and focus_name==d["norm"]),"enabled":not bool(raw.get("disabled")),"bounds":{"viewport":d["viewport_bounds"],"document":d["document_bounds"],"screen_estimate":d["screen_bounds_estimate"],"screen":match["bounds"] if match else None},"dom":{"selector":selector,"href":href,"id":dom_id,"tag":raw.get("tag") or ""},"uia":{"automation_id":match["raw"].get("automation_id") if match else "","role":match["role"] if match else ""}})
        if len(objects)>=max_objects:break
    if len(objects)<max_objects:
        for u in uobjs:
            if u["idx"] in used_u:continue
            raw=u["raw"]; object_id="obj_"+sha256_text(f"{row['id']}|uia|{u['role']}|{u['norm']}|{u['idx']}")[:18]
            automation_id=str(raw.get("automation_id") or "")
            stable_key=automation_id or (u["role"]+"|"+u["norm"])
            stable_id="scene_"+sha256_text(f"{row['source_id']}|uia|{stable_key}")[:20]
            state_hash=sha256_text(json.dumps({"name":u["name"],"value":clip_text(raw.get("value") or "",300),"enabled":bool(raw.get("enabled",True)),"focused":bool(focus_name and focus_name==u["norm"]),"bounds":u["bounds"]},ensure_ascii=False,sort_keys=True))[:16]
            objects.append({"object_id":object_id,"stable_id":stable_id,"state_hash":state_hash,"role":u["role"],"name":u["name"],"value":clip_text(raw.get("value") or "",300),"sources":["uia"],"confidence":0.84,"focused":bool(focus_name and focus_name==u["norm"]),"enabled":bool(raw.get("enabled",True)),"bounds":{"viewport":None,"screen_estimate":None,"screen":u["bounds"]},"dom":{"selector":"","href":"","id":"","tag":""},"uia":{"automation_id":automation_id,"role":u["role"]}})
            if len(objects)>=max_objects:break
    fused=sum(1 for o in objects if len(o["sources"])>1)
    return {"frame_id":row["id"],"timestamp":now_iso(float(row["ts"])),"world_state":{"app":row["app"],"window_title":row["window_title"],"url":row["url"],"active_tab":row["active_tab"],"surface":row["surface"],"focus":browser_focus or load_json(row["focus_json"],{}),"cursor":load_json(row["cursor_json"],{}),"viewport":viewport},"source_quality":{"dom_available":bool(dcontrols),"uia_available":bool(uels),"screenshot_available":frame_has_image(row),"browser_bridge_snapshot_id":snapshot_id or None,"document_screen_bounds":doc_bounds,"coordinate_calibration":{"scale":round(scale,4),"dpr":dpr,"content_origin":content_origin,"scroll":{"x":viewport.get("x",0),"y":viewport.get("y",0)}}},"regions":{"headings":headings[:80] if isinstance(headings,list) else [],"landmarks":landmarks[:60] if isinstance(landmarks,list) else []},"objects":objects,"stats":{"objects":len(objects),"fused_dom_uia":fused,"dom_only":sum(1 for o in objects if o["sources"]==["dom"]),"uia_only":sum(1 for o in objects if o["sources"]==["uia"])}}

def auth_source(request: Request) -> sqlite3.Row | None:
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        return None
    digest = token_hash(auth.split(" ", 1)[1].strip())
    with db() as conn:
        return conn.execute("SELECT * FROM sources WHERE token_hash=? LIMIT 1", (digest,)).fetchone()


async def root_page(request: Request):
    return JSONResponse({
        "ok": True, "service": SERVICE, "version": VERSION, "mode": "observer-only",
        "mcp": f"{PUBLIC_URL}/mcp/<token>", "docs": "Semantic Frame + Timeline + Context Bundle",
    })


async def health(request: Request):
    with db() as conn:
        sources = conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
        frames = conn.execute("SELECT COUNT(*) FROM frames").fetchone()[0]
        latest = conn.execute("SELECT MAX(ts) FROM frames").fetchone()[0]
    return JSONResponse({"ok": True, "service": SERVICE, "version": VERSION, "sources": sources, "frames": frames,
                         "latest_frame": now_iso(latest) if latest else None, "storage": storage_status()})


async def register_source(request: Request):
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)
    bootstrap_ok = bool(BOOTSTRAP_TOKEN and payload.get("bootstrap_token") == BOOTSTRAP_TOKEN)
    enrollment_token = str(payload.get("enrollment_token") or "")
    enrollment = None
    if enrollment_token:
        with db() as conn:
            enrollment = conn.execute(
                "SELECT * FROM enrollments WHERE token_hash=? AND used_at IS NULL AND expires_at>=? LIMIT 1",
                (token_hash(enrollment_token), now_ts()),
            ).fetchone()
    if not bootstrap_ok and not enrollment:
        return JSONResponse({"ok": False, "error": "invalid or expired enrollment"}, status_code=403)
    label = clip_text((enrollment["label"] if enrollment else payload.get("label")) or "Windows", 80).strip() or "Windows"
    device_id = clip_text(payload.get("device_id") or str(uuid.uuid4()), 128).strip()
    hostname = clip_text(payload.get("hostname") or "", 128)
    platform = clip_text(payload.get("platform") or "windows", 40)
    agent_version = clip_text(payload.get("agent_version") or "", 40)
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    source_token = secrets.token_urlsafe(36)
    t = now_ts()
    with db() as conn:
        old = conn.execute("SELECT id FROM sources WHERE device_id=?", (device_id,)).fetchone()
        sid = old["id"] if old else "src_" + uuid.uuid4().hex[:20]
        if enrollment:
            conn.execute("UPDATE enrollments SET used_at=? WHERE token_hash=? AND used_at IS NULL", (t, token_hash(enrollment_token)))
        conn.execute("""
          INSERT INTO sources(id,label,device_id,hostname,platform,agent_version,token_hash,created_at,last_seen,metadata_json)
          VALUES(?,?,?,?,?,?,?,?,?,?)
          ON CONFLICT(device_id) DO UPDATE SET label=excluded.label,hostname=excluded.hostname,platform=excluded.platform,
            agent_version=excluded.agent_version,token_hash=excluded.token_hash,last_seen=excluded.last_seen,metadata_json=excluded.metadata_json
        """, (sid, label, device_id, hostname, platform, agent_version, token_hash(source_token), t, t, safe_json(metadata)))
    return JSONResponse({
        "ok": True, "source_id": sid, "source_token": source_token,
        "frame_url": f"{PUBLIC_URL}/api/v1/frame", "heartbeat_url": f"{PUBLIC_URL}/api/v1/heartbeat",
        "recommended": {"sample_ms": 600, "uia_ms": 1800, "idle_heartbeat_seconds": 8, "upload_only_on_change": True},
    })


async def heartbeat(request: Request):
    src = auth_source(request)
    if not src:
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    enabled = bool(src["monitoring_enabled"]) if "monitoring_enabled" in src.keys() else True
    if enabled:
        with db() as conn:
            conn.execute("UPDATE sources SET last_seen=? WHERE id=?", (now_ts(), src["id"]))
    status = storage_status()
    return JSONResponse({"ok": True, "monitoring_enabled": enabled, "server_time": now_iso(),
                         "storage_mode": status["mode"], "screenshot_allowed": status["screenshot_allowed"]})


async def source_control(request: Request):
    src = auth_source(request)
    if not src:
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    t = now_ts()
    with db() as conn:
        conn.execute("UPDATE sources SET last_control_poll=? WHERE id=?", (t, src["id"]))
        row = conn.execute("SELECT monitoring_enabled,control_updated_at FROM sources WHERE id=?", (src["id"],)).fetchone()
    return JSONResponse({
        "ok": True,
        "monitoring_enabled": bool(row["monitoring_enabled"]),
        "control_updated_at": now_iso(float(row["control_updated_at"])) if float(row["control_updated_at"] or 0) else None,
        "poll_seconds": 3,
        "server_time": now_iso(t),
    }, headers={"Cache-Control": "no-store"})


async def browser_control(request: Request):
    src = auth_browser(request)
    if not src:
        return JSONResponse({"ok":False,"error":"unauthorized"},status_code=401)
    t=now_ts()
    with db() as conn:
        conn.execute("UPDATE sources SET last_browser_seen=? WHERE id=?",(t,src["id"]))
        row=conn.execute("SELECT monitoring_enabled,dev_observer_enabled FROM sources WHERE id=?",(src["id"],)).fetchone()
    return JSONResponse({"ok":True,"monitoring_enabled":bool(row["monitoring_enabled"]),"dev_observer_enabled":bool(row["dev_observer_enabled"]),"server_time":now_iso(t)},headers={"Cache-Control":"no-store"})


async def browser_snapshot_ingest(request: Request):
    src=auth_browser(request)
    if not src:
        return JSONResponse({"ok":False,"error":"unauthorized"},status_code=401)
    t=now_ts()
    enabled=bool(src["monitoring_enabled"]) if "monitoring_enabled" in src.keys() else True
    if not enabled:
        with db() as conn: conn.execute("UPDATE sources SET last_browser_seen=? WHERE id=?",(t,src["id"]))
        return JSONResponse({"ok":True,"stored":False,"reason":"monitoring_disabled"},headers={"Cache-Control":"no-store"})
    try: p=await request.json()
    except Exception: return JSONResponse({"ok":False,"error":"invalid json"},status_code=400)
    status=enforce_storage_limits()
    mode=status["mode"]
    if status["database_bytes"] >= DB_MAX_BYTES or status["total_bytes"] >= TOTAL_MAX_BYTES:
        with db() as conn: conn.execute("UPDATE sources SET last_browser_seen=? WHERE id=?",(t,src["id"]))
        return JSONResponse({"ok":True,"stored":False,"reason":"storage_hard_cap","storage_mode":"minimal"})
    try:
        ts=float(p.get("timestamp_unix") or t)
        if abs(ts-t)>86400: ts=t
    except Exception: ts=t
    url=clip_text(p.get("url") or "",4000); title=clip_text(p.get("title") or "",1000)
    visible=clip_text(p.get("visible_text") or "",512 if mode=="minimal" else MAX_VISIBLE_TEXT)
    dom_json="{}" if mode=="minimal" else safe_json(p.get("dom") or {},MAX_JSON_FIELD)
    focus_json="{}" if mode=="minimal" else safe_json(p.get("focus") or {},16000)
    viewport_json=safe_json(p.get("viewport") or {},16000)
    semantic_hash=clip_text(p.get("semantic_hash") or sha256_text("\n".join([url,title,visible[:16000],dom_json[:32000]])),128)
    extver=clip_text(p.get("extension_version") or "",40)
    tab_id=p.get("tab_id"); window_id=p.get("window_id"); active=1 if p.get("active",True) else 0
    try:
      with db() as conn:
        prev=conn.execute("SELECT * FROM browser_snapshots WHERE source_id=? ORDER BY ts DESC LIMIT 1",(src["id"],)).fetchone()
        # Server-side dedup protects against a noisy extension while preserving 10s continuity.
        if prev and prev["semantic_hash"]==semantic_hash and prev["viewport_json"]==viewport_json and (t-float(prev["received_at"]))<8:
            conn.execute("UPDATE sources SET last_browser_seen=? WHERE id=?",(t,src["id"]))
            return JSONResponse({"ok":True,"stored":False,"reason":"dedup","snapshot_id":prev["id"]})
        sid="brs_"+uuid.uuid4().hex[:24]
        conn.execute("""INSERT INTO browser_snapshots(id,source_id,ts,received_at,tab_id,window_id,active,url,title,visible_text,dom_json,focus_json,viewport_json,semantic_hash,extension_version,metadata_json)
                      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                     (sid,src["id"],ts,t,tab_id,window_id,active,url,title,visible,dom_json,focus_json,viewport_json,semantic_hash,extver,safe_json({"page_visibility":p.get("page_visibility"),"revision":p.get("revision")})))
        conn.execute("UPDATE sources SET last_browser_seen=? WHERE id=?",(t,src["id"]))
        for idx, raw_input in enumerate((p.get("input_events") or [])[:30]):
            if not isinstance(raw_input,dict):
                continue
            target=raw_input.get("target") if isinstance(raw_input.get("target"),dict) else {}
            its=float(raw_input.get("timestamp_unix") or ts)
            if abs(its-t)>120: its=ts
            kind=clip_text(raw_input.get("kind") or "input",40)
            iid="inp_"+sha256_text(f"{src['id']}:{its:.4f}:{kind}:{idx}:{target.get('selector','')}")[:24]
            detail={"button":raw_input.get("button"),"tab_id":tab_id,"window_id":window_id}
            conn.execute("""INSERT OR IGNORE INTO interaction_events
              (id,source_id,frame_id,ts,kind,target_role,target_name,selector,key_name,x,y,app,window_title,detail_json,created_at)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
              (iid,src["id"],"",its,kind,clip_text(target.get("role") or "",80),clip_text(target.get("name") or "",300),
               clip_text(target.get("selector") or "",500),clip_text(raw_input.get("key") or "",40),raw_input.get("x"),raw_input.get("y"),
               "browser",title,safe_json(detail,4000),t))
            conn.execute("""INSERT OR IGNORE INTO timeline_events
              (id,source_id,frame_id,ts,event_type,app,window_title,url,active_tab,detail_json,created_at)
              VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
              ("evt_"+iid[4:],src["id"],"",its,"input_"+kind,"browser",title,url,title,
               safe_json({"target":target,"key":raw_input.get("key") or "","button":raw_input.get("button")},8000),t))
        event_types=[]
        if prev:
            if prev["url"]!=url: event_types.append(("browser_url_changed",{"from":prev["url"],"to":url}))
            if prev["title"]!=title: event_types.append(("browser_tab_changed",{"from":prev["title"],"to":title}))
            if prev["semantic_hash"]!=semantic_hash: event_types.append(("browser_dom_changed",{}))
        else: event_types.append(("browser_attached",{}))
        for et,detail in event_types:
            conn.execute("INSERT INTO timeline_events(id,source_id,frame_id,ts,event_type,app,window_title,url,active_tab,detail_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                         ("evt_"+uuid.uuid4().hex[:24],src["id"],"",ts,et,"browser",title,url,title,safe_json(detail),t))
    except sqlite3.OperationalError as e:
        with db() as conn: conn.execute("UPDATE sources SET last_browser_seen=? WHERE id=?",(t,src["id"]))
        if "full" in str(e).lower():
            return JSONResponse({"ok":True,"stored":False,"reason":"database_hard_cap","storage_mode":"minimal"})
        raise
    cleanup_if_needed()
    post=enforce_storage_limits()
    return JSONResponse({"ok":True,"stored":True,"snapshot_id":sid,"server_time":now_iso(t),"storage_mode":post["mode"]})



def _is_dev_url(url: str) -> bool:
    try:
        u = urlparse(url or "")
        host = (u.hostname or "").lower()
        if host in {"localhost", "127.0.0.1", "0.0.0.0", "::1"}:
            return True
        if host.startswith("10.") or host.startswith("192.168."):
            return True
        if host.startswith("172."):
            try:
                n = int(host.split(".")[1])
                if 16 <= n <= 31:
                    return True
            except Exception:
                pass
        if host.endswith(".local") or host.endswith(".test"):
            return True
        if u.port in {3000, 3001, 4000, 4173, 5000, 5173, 5174, 8000, 8080, 8081, 8888}:
            return True
    except Exception:
        pass
    return False


async def browser_dev_event(request: Request):
    src = auth_browser(request)
    if not src:
        return JSONResponse({"ok":False,"error":"unauthorized"},status_code=401)
    t = now_ts()
    enabled = bool(src["monitoring_enabled"]) if "monitoring_enabled" in src.keys() else True
    dev_enabled = bool(src["dev_observer_enabled"]) if "dev_observer_enabled" in src.keys() else True
    if not enabled or not dev_enabled:
        with db() as conn:
            conn.execute("UPDATE sources SET last_browser_seen=? WHERE id=?", (t, src["id"]))
        return JSONResponse({"ok":True,"stored":False,"reason":"monitoring_or_dev_observer_disabled"})
    try:
        p = await request.json()
    except Exception:
        return JSONResponse({"ok":False,"error":"invalid json"},status_code=400)
    try:
        ts = float(p.get("timestamp_unix") or t)
        if abs(ts-t) > 86400: ts=t
    except Exception:
        ts=t
    event_type = clip_text(p.get("event_type") or "dev_event", 80)
    severity = clip_text(p.get("severity") or "info", 20).lower()
    if severity not in {"info","low","medium","high","critical"}:
        severity = "info"
    url = clip_text(p.get("page_url") or p.get("url") or "", 4000)
    summary = clip_text(p.get("summary") or event_type, 1000)
    detail = p.get("detail") if isinstance(p.get("detail"), dict) else {}
    tab_id = p.get("tab_id"); window_id = p.get("window_id")
    extver = clip_text(p.get("extension_version") or "", 40)
    is_dev = 1 if bool(p.get("is_dev_page")) or _is_dev_url(url) else 0
    # Dev Observer only persists browser diagnostic events for dev-like pages.
    if not is_dev:
        return JSONResponse({"ok":True,"stored":False,"reason":"not_dev_page"})
    eid = "dev_" + uuid.uuid4().hex[:24]
    nearest_frame_id = ""
    nearest_frame_ts = 0.0
    promoted_importance = 0.0
    promoted_reasons: list[str] = []
    with db() as conn:
        prev = conn.execute(
            "SELECT id,ts FROM dev_events WHERE source_id=? AND event_type=? AND summary=? AND url=? ORDER BY ts DESC LIMIT 1",
            (src["id"], event_type, summary, url),
        ).fetchone()
        if prev and ts - float(prev["ts"]) < 2.0:
            conn.execute("UPDATE sources SET last_browser_seen=? WHERE id=?", (t, src["id"]))
            return JSONResponse({"ok":True,"stored":False,"reason":"dedup","event_id":prev["id"]})

        nearest = conn.execute(
            "SELECT * FROM frames WHERE source_id=? AND ts BETWEEN ? AND ? AND url=? ORDER BY ABS(ts-?) ASC LIMIT 1",
            (src["id"], ts-8.0, ts+8.0, url, ts),
        ).fetchone() if url else None
        if not nearest:
            nearest = conn.execute(
                "SELECT * FROM frames WHERE source_id=? AND ts BETWEEN ? AND ? ORDER BY ABS(ts-?) ASC LIMIT 1",
                (src["id"], ts-8.0, ts+8.0, ts),
            ).fetchone()
        if nearest:
            nearest_frame_id = nearest["id"]
            nearest_frame_ts = float(nearest["ts"])

        conn.execute(
            "INSERT INTO dev_events(id,source_id,ts,received_at,tab_id,window_id,url,event_type,severity,summary,detail_json,is_dev_page,extension_version) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (eid,src["id"],ts,t,tab_id,window_id,url,event_type,severity,summary,safe_json(detail,32000),is_dev,extver),
        )
        conn.execute(
            "INSERT INTO timeline_events(id,source_id,frame_id,ts,event_type,app,window_title,url,active_tab,detail_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            ("evt_"+uuid.uuid4().hex[:24],src["id"],nearest_frame_id,ts,"dev_"+event_type,"browser-dev","",url,"",safe_json({"severity":severity,"summary":summary,"detail":detail},32000),t),
        )

        promote = severity in {"medium","high","critical"} or event_type in {"js_error","unhandled_rejection","network_error","resource_error"}
        if promote and nearest:
            existing_reasons = load_json(nearest["keyframe_reasons_json"], []) if "keyframe_reasons_json" in nearest.keys() else []
            reason = "dev_" + event_type
            promoted_reasons = list(dict.fromkeys(list(existing_reasons) + [reason]))[:16]
            floor = 0.99 if severity in {"high","critical"} else 0.93 if severity == "medium" else 0.86
            promoted_importance = max(float(nearest["importance"] or 0), floor)
            conn.execute(
                "UPDATE frames SET is_keyframe=1,importance=?,keyframe_reasons_json=? WHERE id=?",
                (promoted_importance, safe_json(promoted_reasons,4000), nearest_frame_id),
            )
        conn.execute("UPDATE sources SET last_browser_seen=? WHERE id=?", (t, src["id"]))

    if nearest_frame_id and promoted_importance:
        try:
            nearest_row = frame_row(nearest_frame_id)
            if nearest_row and frame_has_image(nearest_row):
                _queue_vision_candidate(nearest_frame_id, src["id"], nearest_frame_ts, promoted_importance, promoted_reasons)
                _auto_visual_change(nearest_frame_id, src["id"])
        except Exception:
            pass
    cleanup_if_needed()
    return JSONResponse({"ok":True,"stored":True,"event_id":eid,"frame_id":nearest_frame_id or None,"frame_promoted":bool(promoted_importance),"is_dev_page":bool(is_dev),"server_time":now_iso(t)})


def build_browser_extension_zip(src: sqlite3.Row, browser_token: str) -> bytes:
    buf=io.BytesIO()
    with zipfile.ZipFile(buf,"w",zipfile.ZIP_DEFLATED) as z:
        for name in ("manifest.json","background.js","content.js","dev-bridge.js","dev-main.js"):
            raw=(BROWSER_EXTENSION_DIR/name).read_text(encoding="utf-8")
            if name=="manifest.json": raw=raw.replace("__AVC_SERVER__",PUBLIC_URL)
            z.writestr(name,raw)
        cfg="self.AVC_CONFIG = {server:%s,token:%s,sourceLabel:%s,version:%s};\n" % (
            json.dumps(PUBLIC_URL),json.dumps(browser_token),json.dumps(src["label"]),json.dumps("0.2.0"))
        z.writestr("config.js",cfg)
        z.writestr("README.txt","AImanVisualCopilot Browser Bridge\n\nChrome/Edge: Extensions -> Developer mode -> Load unpacked -> select this extracted folder.\nDownloading a new package rotates the Browser Bridge token and invalidates older packages.\n")
    return buf.getvalue()


async def dashboard_browser_extension(request: Request):
    if not dashboard_ok(request): return PlainTextResponse("Not found",status_code=404)
    try:
        src=resolve_source(request.query_params.get("source")); token=secrets.token_urlsafe(36); t=now_ts()
        with db() as conn:
            conn.execute("UPDATE sources SET browser_token_hash=?,last_browser_seen=0 WHERE id=?",(token_hash(token),src["id"]))
        raw=build_browser_extension_zip(src,token)
        return Response(raw,media_type="application/zip",headers={"Content-Disposition":"attachment; filename=AVC-Browser-Bridge.zip","Cache-Control":"no-store"})
    except Exception as e: return PlainTextResponse(str(e),status_code=400)


def dashboard_ok(request: Request) -> bool:
    supplied = str(request.path_params.get("token") or "")
    if not supplied or not DASHBOARD_TOKEN_HASH:
        return False
    return secrets.compare_digest(hashlib.sha256(supplied.encode("utf-8")).hexdigest(), DASHBOARD_TOKEN_HASH)


def dashboard_bundle(source: str | None = None) -> dict[str, Any]:
    src = resolve_source(source)
    frame = latest_frame(src["id"])
    current = frame_dict(frame, text_limit=24000, include_structures=False) if frame else None
    raw = None
    if frame:
        raw = {
            "frame_id": frame["id"], "seq": frame["seq"], "timestamp": now_iso(float(frame["ts"])),
            "app": frame["app"], "window_title": frame["window_title"], "url": frame["url"],
            "surface": frame["surface"], "summary": frame["summary"],
            "visible_text": clip_text(frame["visible_text"], 24000),
            "uia": load_json(frame["uia_json"], {}), "dom": load_json(frame["dom_json"], {}),
            "events": load_json(frame["events_json"], []), "changes": load_json(frame["changes_json"], []),
            "metadata": load_json(frame["metadata_json"], {}),
            "screenshot_bytes": int(frame["screenshot_bytes"] or 0),
        }
    with db() as conn:
        latest_episode = conn.execute("SELECT * FROM memory_episodes WHERE source_id=? ORDER BY end_ts DESC LIMIT 1", (src["id"],)).fetchone()
        latest_session = conn.execute("SELECT * FROM memory_sessions WHERE source_id=? ORDER BY end_ts DESC LIMIT 1", (src["id"],)).fetchone()
        world = state_engine.current(conn, src["id"])
    memory = {
        "current_activity": world["activity"] if world else None,
        "world_state": world,
        "latest_episode": _episode_dict(latest_episode) if latest_episode else None,
        "latest_session": _session_dict(latest_session) if latest_session else None,
    }
    return {
        "ok": True, "version": VERSION, "server_time": now_iso(), "source": source_dict(src),
        "current": current, "timeline": timeline_data(src["id"], 180, 60)["frames"],
        "browser": browser_snapshot_dict(latest_browser_snapshot(src["id"]), text_limit=6000, include_dom=False),
        "memory": memory, "latest_uploaded_payload": raw, "storage": storage_status(),
    }


async def dashboard_page(request: Request):
    if not dashboard_ok(request):
        return PlainTextResponse("Not found", status_code=404)
    return FileResponse("/opt/aiman-visual-copilot/app/dashboard.html", media_type="text/html", headers={"Cache-Control": "no-store"})


async def dashboard_status(request: Request):
    if not dashboard_ok(request):
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    try:
        return JSONResponse(dashboard_bundle(request.query_params.get("source")), headers={"Cache-Control": "no-store"})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


async def dashboard_control(request: Request):
    if not dashboard_ok(request):
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    try:
        p = await request.json()
        has_monitor = "enabled" in p and isinstance(p.get("enabled"), bool)
        has_dev = "dev_enabled" in p and isinstance(p.get("dev_enabled"), bool)
        if not has_monitor and not has_dev:
            raise ValueError("enabled or dev_enabled must be boolean")
        src = resolve_source(p.get("source"))
        t = now_ts()
        with db() as conn:
            if has_monitor:
                conn.execute("UPDATE sources SET monitoring_enabled=?,control_updated_at=? WHERE id=?", (1 if p["enabled"] else 0, t, src["id"]))
            if has_dev:
                conn.execute("UPDATE sources SET dev_observer_enabled=?,control_updated_at=? WHERE id=?", (1 if p["dev_enabled"] else 0, t, src["id"]))
            row = conn.execute("SELECT * FROM sources WHERE id=?", (src["id"],)).fetchone()
        return JSONResponse({"ok": True, "source": source_dict(row)}, headers={"Cache-Control": "no-store"})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


async def dashboard_snapshot(request: Request):
    if not dashboard_ok(request):
        return PlainTextResponse("Not found", status_code=404)
    try:
        src = resolve_source(request.query_params.get("source"))
        frame = latest_frame(src["id"])
        if not frame or not frame["screenshot_path"] or not Path(frame["screenshot_path"]).exists():
            return PlainTextResponse("No screenshot", status_code=404)
        return FileResponse(frame["screenshot_path"], media_type=frame["screenshot_mime"] or "image/jpeg", headers={"Cache-Control":"no-store"})
    except Exception as e:
        return PlainTextResponse(str(e), status_code=404)



def _classify_frame_importance(changes: list[Any], events: list[Any], title: str, url: str, visible_text: str) -> tuple[float, list[str]]:
    """0.8 persistent keyframe scoring. High-value UI state gets a 24h screenshot window."""
    reasons: list[str] = []
    score = 0.0
    weights = {
        "app_changed": 1.00,
        "url_changed": 0.98,
        "window_changed": 0.92,
        "active_tab_changed": 0.90,
        "dialog_opened": 0.98,
        "dialog_closed": 0.86,
        "browser_attached": 0.82,
        "browser_url_changed": 0.96,
        "browser_tab_changed": 0.88,
        "browser_dom_changed": 0.60,
        "significant_visual_change": 0.74,
        "focus_changed": 0.25,
        "visible_text_changed": 0.18,
    }
    for item in list(changes or []) + list(events or []):
        if isinstance(item, dict):
            typ = str(item.get("type") or "")
            w = weights.get(typ, 0.0)
            if typ == "significant_visual_change":
                try:
                    dist = int(item.get("distance") or 0)
                    if dist >= 20: w = max(w, 0.91)
                    elif dist >= 14: w = max(w, 0.82)
                except Exception:
                    pass
        elif isinstance(item, str):
            typ = item
            w = weights.get(typ, 0.0)
        else:
            continue
        if w > 0:
            score = max(score, w)
            if w >= 0.60 and typ not in reasons:
                reasons.append(typ)
    title_low = (title or "").lower()
    # Only OS-level hung-window captions are strong enough without structured events.
    if "not responding" in title_low or "未响应" in (title or ""):
        score = max(score, 0.97)
        reasons.append("not_responding")
    if url and any(x in url.lower() for x in ("localhost", "127.0.0.1", "0.0.0.0", "::1")):
        score = max(score, 0.68)
        reasons.append("dev_page")
    reasons = list(dict.fromkeys(reasons))[:12]
    return round(min(1.0, score), 3), reasons


def _queue_vision_candidate(frame_id: str, source_id: str, frame_ts: float, importance: float, reasons: list[str]) -> None:
    if importance < 0.78:
        return
    t = now_ts()
    with db() as conn:
        cached = conn.execute("SELECT 1 FROM vision_cache WHERE frame_id=?", (frame_id,)).fetchone()
        if cached:
            return
        conn.execute(
            "INSERT OR IGNORE INTO vision_candidates(frame_id,source_id,frame_ts,priority,reason_json,status,created_at,updated_at) VALUES(?,?,?,?,?,'pending',?,?)",
            (frame_id, source_id, frame_ts, importance, safe_json(reasons, 4000), t, t),
        )


def _auto_visual_change(frame_id: str, source_id: str) -> dict[str, Any]:
    """Compute a visual region only for persistent keyframes, keeping ingest cost bounded."""
    with db() as conn:
        cur = conn.execute("SELECT * FROM frames WHERE id=?", (frame_id,)).fetchone()
        if not cur or not frame_has_image(cur) or not bool(cur["is_keyframe"]):
            return {}
        prev = conn.execute(
            "SELECT * FROM frames WHERE source_id=? AND ts<? AND screenshot_path IS NOT NULL ORDER BY ts DESC LIMIT 1",
            (source_id, cur["ts"]),
        ).fetchone()
    if not prev or float(cur["ts"]) - float(prev["ts"]) > 20:
        return {}
    result, _ = _screenshot_diff(prev, cur)
    if not result.get("available"):
        return result
    result["before_frame_id"] = prev["id"]
    result["after_frame_id"] = cur["id"]
    result["time_delta_ms"] = round((float(cur["ts"]) - float(prev["ts"])) * 1000, 1)
    with db() as conn:
        conn.execute("UPDATE frames SET visual_change_json=? WHERE id=?", (safe_json(result, 16000), frame_id))
        if result.get("bbox") and float(result.get("changed_fraction") or 0) >= 0.001:
            conn.execute(
                "INSERT INTO timeline_events(id,source_id,frame_id,ts,event_type,app,window_title,url,active_tab,detail_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                ("evt_"+uuid.uuid4().hex[:24], source_id, frame_id, float(cur["ts"]), "visual_region_changed", cur["app"], cur["window_title"], cur["url"], cur["active_tab"], safe_json(result,16000), now_ts()),
            )
    return result


async def ingest_frame(request: Request):
    src = auth_source(request)
    if not src:
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    enabled = bool(src["monitoring_enabled"]) if "monitoring_enabled" in src.keys() else True
    if not enabled:
        return JSONResponse({"ok": True, "stored": False, "monitoring_enabled": False,
                             "reason": "monitoring_disabled", "server_time": now_iso()},
                            headers={"Cache-Control": "no-store"})
    try:
        p = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)
    t = now_ts()
    try:
        ts = float(p.get("timestamp_unix") or t)
        if abs(ts - t) > 86400:
            ts = t
    except Exception:
        ts = t

    status = enforce_storage_limits()
    mode = status["mode"]
    app = clip_text(p.get("app") or "", 160)
    title = clip_text(p.get("window_title") or "", 500)
    url = clip_text(p.get("url") or "", 4000)
    active_tab = clip_text(p.get("active_tab") or "", 1000)
    focus_json = safe_json(p.get("focus") or {}, 16000)
    cursor_json = safe_json(p.get("cursor") or {}, 4000)
    visual_hash = clip_text(p.get("visual_hash") or "", 128)
    surface = clip_text(p.get("surface") or ("browser" if url else "desktop"), 40)
    summary = clip_text(p.get("summary") or "", 500 if mode == "minimal" else 2000)
    changes = p.get("changes") if isinstance(p.get("changes"), list) else []
    events = p.get("events") if isinstance(p.get("events"), list) else []
    visible = clip_text(p.get("visible_text") or "", 512 if mode == "minimal" else MAX_VISIBLE_TEXT)
    bridge = None
    if app.lower() in {"chrome","msedge","edge","brave","opera","firefox"}:
        bridge = latest_browser_snapshot(src["id"], ts, 8.0)
        if bridge and not browser_title_matches(title, bridge["title"]):
            bridge = None
        if bridge:
            if bridge["url"]: url = bridge["url"]
            if bridge["title"]: active_tab = bridge["title"]
            if len(bridge["visible_text"] or "") >= len(visible): visible = clip_text(bridge["visible_text"], 512 if mode == "minimal" else MAX_VISIBLE_TEXT)
            focus_json = bridge["focus_json"] or focus_json
            surface = "browser"
            with db() as conn:
                prev_frame = conn.execute("SELECT url,active_tab,metadata_json FROM frames WHERE source_id=? ORDER BY ts DESC LIMIT 1", (src["id"],)).fetchone()
            existing_types = {str(x.get("type") or "") for x in changes if isinstance(x, dict)}
            if prev_frame:
                if prev_frame["url"] != url and "url_changed" not in existing_types:
                    changes.append({"type":"url_changed","from":prev_frame["url"],"to":url,"source":"browser_bridge"})
                if prev_frame["active_tab"] != active_tab and "active_tab_changed" not in existing_types:
                    changes.append({"type":"active_tab_changed","from":prev_frame["active_tab"],"to":active_tab,"source":"browser_bridge"})
                prev_meta = load_json(prev_frame["metadata_json"], {})
                prev_bhash = str(prev_meta.get("browser_bridge",{}).get("semantic_hash") or "")
                if prev_bhash and prev_bhash != bridge["semantic_hash"] and "browser_dom_changed" not in existing_types:
                    changes.append({"type":"browser_dom_changed","source":"browser_bridge"})

    # Under 10 GiB free, keep only meaningful transitions and a sparse 60-second
    # continuity frame. Heartbeats remain independent so online state is unaffected.
    if mode == "minimal":
        with db() as conn:
            last = conn.execute(
                "SELECT ts,app,window_title,url,active_tab FROM frames WHERE source_id=? ORDER BY ts DESC LIMIT 1", (src["id"],)
            ).fetchone()
        if last and (t - float(last["ts"])) < MINIMAL_FRAME_INTERVAL_SECONDS and not changes and not events                 and last["app"] == app and last["window_title"] == title and last["url"] == url and last["active_tab"] == active_tab:
            with db() as conn:
                conn.execute("UPDATE sources SET last_seen=?,agent_version=COALESCE(NULLIF(?,''),agent_version) WHERE id=?",
                             (t, clip_text(p.get("agent_version") or "", 40), src["id"]))
            return JSONResponse({"ok": True, "stored": False, "reason": "minimal_mode_dedup",
                                 "storage_mode": mode, "server_time": now_iso(t)})
        uia_json = "{}"
        dom_json = "{}"
        events_json = "[]"
        metadata_json = safe_json({"minimal": True})
    else:
        uia_json = safe_json(p.get("uia") or {})
        dom_json = bridge["dom_json"] if bridge else safe_json(p.get("dom") or {})
        events_json = safe_json(events)
        meta = p.get("metadata") if isinstance(p.get("metadata"),dict) else {}
        if bridge:
            meta = dict(meta)
            meta["browser_bridge"] = {"snapshot_id":bridge["id"],"snapshot_ts":bridge["ts"],"age_ms":round(abs(ts-float(bridge["ts"]))*1000,1),"extension_version":bridge["extension_version"],"semantic_hash":bridge["semantic_hash"]}
        metadata_json = safe_json(meta)

    # If database/total hard caps are already reached after pruning, preserve liveness
    # but refuse another row. SQLite max_page_count provides a second hard stop.
    if status["database_bytes"] >= DB_MAX_BYTES or status["total_bytes"] >= TOTAL_MAX_BYTES:
        with db() as conn:
            conn.execute("UPDATE sources SET last_seen=?,agent_version=COALESCE(NULLIF(?,''),agent_version) WHERE id=?",
                         (t, clip_text(p.get("agent_version") or "", 40), src["id"]))
        return JSONResponse({"ok": True, "stored": False, "reason": "storage_hard_cap",
                             "storage_mode": "minimal", "server_time": now_iso(t)})

    importance, keyframe_reasons = _classify_frame_importance(changes, events, title, url, visible)
    is_keyframe = 1 if importance >= 0.78 else 0

    frame_id = "frm_" + uuid.uuid4().hex[:24]
    semantic_hash = sha256_text("\n".join([app, title, url, active_tab, surface, visible[:24000]]))
    screenshot_path = None
    screenshot_mime = None
    screenshot_sha = None
    screenshot_bytes = 0
    screenshot_skip_reason = None
    b64 = p.get("screenshot_base64")
    if b64 and status["screenshot_allowed"] and mode == "normal":
        try:
            raw = base64.b64decode(b64, validate=True)
            if len(raw) > MAX_SCREENSHOT_BYTES:
                screenshot_skip_reason = "single_screenshot_too_large"
            elif status["screenshot_bytes"] + len(raw) > SCREENSHOT_MAX_BYTES:
                screenshot_skip_reason = "screenshot_cap"
            elif status["total_bytes"] + len(raw) > TOTAL_MAX_BYTES:
                screenshot_skip_reason = "total_cap"
            else:
                screenshot_sha = hashlib.sha256(raw).hexdigest()
                mime = p.get("screenshot_mime") or "image/jpeg"
                ext = ".png" if mime == "image/png" else ".jpg"
                path = SCREENSHOT_DIR / f"{frame_id}{ext}"
                path.write_bytes(raw)
                screenshot_path = str(path)
                screenshot_mime = mime
                screenshot_bytes = len(raw)
        except Exception:
            screenshot_skip_reason = "invalid_screenshot_base64"
    elif b64:
        if mode == "minimal":
            screenshot_skip_reason = "minimal_mode"
        elif mode == "semantic_only":
            screenshot_skip_reason = "free_disk_below_20gb"
        elif not status["screenshot_allowed"]:
            screenshot_skip_reason = "storage_cap"

    try:
        with db() as conn:
            previous_for_state = conn.execute(
                "SELECT * FROM frames WHERE source_id=? ORDER BY ts DESC LIMIT 1", (src["id"],)
            ).fetchone()
            conn.execute("""
              INSERT INTO frames(id,source_id,seq,ts,received_at,app,window_title,url,active_tab,focus_json,cursor_json,visual_hash,surface,summary,visible_text,uia_json,dom_json,
                events_json,changes_json,screenshot_path,screenshot_mime,screenshot_sha256,screenshot_bytes,width,height,semantic_hash,metadata_json,is_keyframe,importance,keyframe_reasons_json,visual_change_json)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                frame_id, src["id"], int(p.get("seq") or 0), ts, t, app, title, url, active_tab, focus_json, cursor_json, visual_hash, surface, summary, visible,
                uia_json, dom_json, events_json, safe_json(changes), screenshot_path, screenshot_mime, screenshot_sha,
                screenshot_bytes, p.get("width") if screenshot_path else None, p.get("height") if screenshot_path else None,
                semantic_hash, metadata_json, is_keyframe, importance, safe_json(keyframe_reasons,4000), "{}",
            ))
            conn.execute("UPDATE sources SET last_seen=?, agent_version=COALESCE(NULLIF(?,''),agent_version) WHERE id=?",
                         (t, clip_text(p.get("agent_version") or "", 40), src["id"]))
            event_items = []
            for item in changes + events:
                if isinstance(item, dict):
                    etype = clip_text(item.get("type") or "event", 120)
                    detail = item
                elif isinstance(item, str):
                    etype = clip_text(item, 120)
                    detail = {"value": item}
                else:
                    continue
                event_items.append((
                    "evt_" + uuid.uuid4().hex[:24], src["id"], frame_id, ts, etype, app, title, url, active_tab, safe_json(detail, 16000), t
                ))
            if event_items:
                conn.executemany("""
                  INSERT INTO timeline_events(id,source_id,frame_id,ts,event_type,app,window_title,url,active_tab,detail_json,created_at)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?)
                """, event_items)
            frame_focus = load_json(focus_json,{})
            for idx, item in enumerate(events[:50]):
                if not isinstance(item, dict):
                    continue
                kind = clip_text(item.get("kind") or str(item.get("type") or "").removeprefix("input_"), 40)
                if kind not in {"click","pointer","key","submit"}:
                    continue
                target = item.get("target") if isinstance(item.get("target"), dict) else {}
                try:
                    its = float(item.get("timestamp_unix") or ts)
                    if abs(its-t) > 120: its = ts
                except Exception:
                    its = ts
                iid = "inp_"+sha256_text(f"{src['id']}:{frame_id}:{its:.4f}:{kind}:{idx}")[:24]
                conn.execute("""INSERT OR IGNORE INTO interaction_events
                  (id,source_id,frame_id,ts,kind,target_role,target_name,selector,key_name,x,y,app,window_title,detail_json,created_at)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (iid,src["id"],frame_id,its,kind,clip_text(target.get("role") or frame_focus.get("role") or "",80),
                   clip_text(target.get("name") or frame_focus.get("name") or "",300),clip_text(target.get("selector") or "",500),
                   clip_text(item.get("key") or "",40),item.get("x"),item.get("y"),app,title,
                   safe_json({"button":item.get("button"),"observer":"windows"},4000),t))
            inserted_frame = conn.execute("SELECT * FROM frames WHERE id=?", (frame_id,)).fetchone()
            incremental = state_engine.advance(conn, inserted_frame, previous_for_state, _human_event)
    except sqlite3.OperationalError as e:
        if screenshot_path:
            _unlink_paths([screenshot_path])
        with db() as conn:
            conn.execute("UPDATE sources SET last_seen=? WHERE id=?", (t, src["id"]))
        if "full" in str(e).lower():
            return JSONResponse({"ok": True, "stored": False, "reason": "database_hard_cap",
                                 "storage_mode": "minimal", "server_time": now_iso(t)})
        raise

    visual_change = {}
    if is_keyframe:
        try:
            if screenshot_path:
                _queue_vision_candidate(frame_id, src["id"], ts, importance, keyframe_reasons)
            if screenshot_path and importance >= 0.82:
                visual_change = _auto_visual_change(frame_id, src["id"])
        except Exception:
            # Observation ingest must remain reliable even if enrichment fails.
            visual_change = {}

    cleanup_if_needed()
    post = enforce_storage_limits()
    return JSONResponse({"ok": True, "stored": True, "frame_id": frame_id, "received_at": now_iso(t),
                         "semantic_hash": semantic_hash, "storage_mode": post["mode"],
                         "is_keyframe": bool(is_keyframe), "importance": importance, "keyframe_reasons": keyframe_reasons,
                         "visual_change": visual_change,
                         "state_engine": incremental,
                         "screenshot_stored": bool(screenshot_path), "screenshot_skip_reason": screenshot_skip_reason})


async def windows_observer_script(request: Request):
    script = Path("/opt/aiman-visual-copilot/windows/observer.ps1")
    if not script.exists():
        return PlainTextResponse("observer not installed", status_code=404)
    return PlainTextResponse(script.read_text(encoding="utf-8"), media_type="text/plain; charset=utf-8")


server = MCPServer(
    name="AImanVisualCopilot",
    title="AIman Visual Copilot",
    description="Read-only continuous Windows UI understanding: current screen context, semantic UI text and recent interaction timeline.",
    instructions=(
        "AVC is an observer, not a computer-control agent. Start with avc_context for the current state. "
        "For questions about what happened recently, prefer avc_visual_timeline so AVC returns only selected visual keyframes instead of polling. "
        "Use avc_frame_image or avc_visual_query to inspect one exact historical frame, and avc_clip for an explicit time range. "
        "After you have genuinely interpreted a historical screenshot, you may call avc_store_vision_cache with a concise factual visual summary so future queries can reuse it after screenshots expire. "
        "For browser pages use avc_browser_context when URL, page text, DOM controls, focus or viewport matter; use screenshots as the visual fallback. "
        "For localhost/development pages use avc_dev_context for JS errors, HTTP/network failures and page-performance diagnostics. "
        "Use avc_scene_graph for unified UI objects and avc_scene_diff to compare stable objects across Frames. "
        "Use avc_vision_candidates to find important keyframes that still need genuine visual interpretation; never fabricate a visual cache without viewing the screenshot. "
        "Use avc_event_history for lightweight long-lived transitions and avc_frame_details only when deeper fused UIA/DOM structure is needed."
    ),
    version=VERSION,
)
RO = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True)
CACHE_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True)


@server.tool(description="Check AImanVisualCopilot health. Observer-only; AVC exposes no mouse, keyboard or shell actions.", annotations=RO)
def avc_ping() -> dict[str, Any]:
    with db() as conn:
        nsrc = conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
        nframes = conn.execute("SELECT COUNT(*) FROM frames").fetchone()[0]
    return {"ok": True, "service": SERVICE, "version": VERSION, "mode": "observer-only", "sources": nsrc,
            "frames": nframes, "time": now_iso(), "storage": storage_status()}


@server.tool(description="Read AVC server storage usage, hard caps and automatic protection mode.", annotations=RO)
def avc_storage_status() -> dict[str, Any]:
    return {"ok": True, "storage": storage_status()}


@server.tool(description="List Windows observation sources and online state. Usually unnecessary when only one Windows PC is connected.", annotations=RO)
def avc_list_sources() -> dict[str, Any]:
    with db() as conn:
        rows = conn.execute("SELECT * FROM sources ORDER BY last_seen DESC").fetchall()
    return {"ok": True, "sources": [source_dict(r) for r in rows], "count": len(rows)}


@server.tool(description="Primary AVC tool. In ONE call, return the current semantic UI state, recent timeline and current screenshot. Use this first instead of repeated observe calls.", annotations=RO)
def avc_context(source: str | None = None, timeline_seconds: int = 45, timeline_limit: int = 30,
                text_limit: int = 12000, include_image: bool = True) -> CallToolResult:
    try:
        src = resolve_source(source)
        frame = latest_frame(src["id"])
        if not frame:
            payload = {"ok": False, "source": source_dict(src), "error": "source has not uploaded any frames yet"}
            return CallToolResult(content=[TextContent(type="text", text=json.dumps(payload, ensure_ascii=False, indent=2))], is_error=True)
        payload = {
            "ok": True,
            "source": source_dict(src),
            "current": frame_dict(frame, text_limit=text_limit, include_structures=False),
            "timeline": timeline_data(src["id"], timeline_seconds, timeline_limit),
            "browser_current": browser_snapshot_dict(latest_browser_snapshot(src["id"]), text_limit=min(text_limit,16000), include_dom=False),
            "storage": storage_status(),
            "usage_note": "This is a continuous context bundle. Ask for another avc_context only when a newer screen state is needed.",
        }
        with db() as conn:
            payload["world_state"] = state_engine.current(conn, src["id"])
        content: list[Any] = [TextContent(type="text", text=json.dumps(payload, ensure_ascii=False, indent=2))]
        path = frame["screenshot_path"]
        if include_image and path and Path(path).exists():
            content.append(ImageContent(type="image", data=base64.b64encode(Path(path).read_bytes()).decode("ascii"),
                                        mime_type=frame["screenshot_mime"] or "image/jpeg"))
        return CallToolResult(content=content, is_error=False)
    except Exception as e:
        return CallToolResult(content=[TextContent(type="text", text=json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))], is_error=True)


@server.tool(description="Return current semantic UI state as compact JSON without image data.", annotations=RO)
def avc_current_context(source: str | None = None, text_limit: int = 12000) -> dict[str, Any]:
    try:
        src = resolve_source(source)
        frame = latest_frame(src["id"])
        with db() as conn:
            world = state_engine.current(conn, src["id"])
        return {"ok": bool(frame), "source": source_dict(src), "current": frame_dict(frame, text_limit, False) if frame else None,
                "world_state": world}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@server.tool(description="Read the recent continuous UI timeline: app/window/url transitions, semantic changes and observer events.", annotations=RO)
def avc_recent_timeline(source: str | None = None, seconds: int = 120, limit: int = 60) -> dict[str, Any]:
    try:
        src = resolve_source(source)
        return {"ok": True, "source": source_dict(src), "timeline": timeline_data(src["id"], seconds, limit)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@server.tool(description="Read one exact Semantic Frame, including bounded UIA and browser DOM structures when present.", annotations=RO)
def avc_frame_details(frame_id: str, text_limit: int = 16000) -> dict[str, Any]:
    with db() as conn:
        row = conn.execute("SELECT * FROM frames WHERE id=?", (frame_id,)).fetchone()
    if not row:
        return {"ok": False, "error": f"frame not found: {frame_id}"}
    return {"ok": True, "frame": frame_dict(row, text_limit, True)}


@server.tool(description="Return the latest Windows screenshot only. Prefer avc_context when semantic context is also needed.", annotations=RO)
def avc_latest_screenshot(source: str | None = None) -> CallToolResult:
    try:
        src = resolve_source(source)
        frame = latest_frame(src["id"])
        if not frame or not frame["screenshot_path"] or not Path(frame["screenshot_path"]).exists():
            text = json.dumps({"ok": False, "error": "no screenshot available"}, ensure_ascii=False)
            return CallToolResult(content=[TextContent(type="text", text=text)], is_error=True)
        meta = {"ok": True, "source": source_dict(src), "frame_id": frame["id"], "timestamp": now_iso(float(frame["ts"])),
                "app": frame["app"], "window_title": frame["window_title"], "url": frame["url"]}
        return CallToolResult(content=[
            TextContent(type="text", text=json.dumps(meta, ensure_ascii=False, indent=2)),
            ImageContent(type="image", data=base64.b64encode(Path(frame["screenshot_path"]).read_bytes()).decode("ascii"),
                         mime_type=frame["screenshot_mime"] or "image/jpeg"),
        ], is_error=False)
    except Exception as e:
        return CallToolResult(content=[TextContent(type="text", text=json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))], is_error=True)


@server.tool(description="Return the exact historical screenshot for one frame_id as MCP Image Content, together with semantic metadata and any cached vision summary.", annotations=RO)
def avc_frame_image(frame_id: str) -> CallToolResult:
    try:
        row = frame_row(frame_id)
        if not row:
            return CallToolResult(content=[TextContent(type="text", text=json.dumps({"ok": False, "error": f"frame not found: {frame_id}"}, ensure_ascii=False))], is_error=True)
        meta = {"ok": True, "frame": frame_dict(row, 6000, False)}
        if not frame_has_image(row):
            meta["image_retained"] = False
            meta["fallback"] = "Use vision_cache if present, otherwise the original screenshot has expired or was not stored."
            return CallToolResult(content=[TextContent(type="text", text=json.dumps(meta, ensure_ascii=False, indent=2))], is_error=True)
        meta["image_retained"] = True
        return CallToolResult(content=[
            TextContent(type="text", text=json.dumps(meta, ensure_ascii=False, indent=2)),
            ImageContent(type="image", data=base64.b64encode(Path(row["screenshot_path"]).read_bytes()).decode("ascii"),
                         mime_type=row["screenshot_mime"] or "image/jpeg"),
        ], is_error=False)
    except Exception as e:
        return CallToolResult(content=[TextContent(type="text", text=json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))], is_error=True)


@server.tool(description="Return a compact visual timeline for the recent N seconds. AVC selects transition/stable keyframes and returns only those screenshots, not every captured frame.", annotations=RO)
def avc_visual_timeline(source: str | None = None, seconds: int = 30, max_frames: int = 6) -> CallToolResult:
    try:
        src = resolve_source(source)
        seconds = max(1, min(int(seconds), 3600))
        end_ts = now_ts()
        rows = _rows_for_range(src["id"], end_ts - seconds, end_ts, raw_limit=2000)
        if not rows:
            return CallToolResult(content=[TextContent(type="text", text=json.dumps({"ok": False, "error": "no frames in requested timeline"}, ensure_ascii=False))], is_error=True)
        return _visual_result(src, rows, max_frames, f"last_{seconds}_seconds")
    except Exception as e:
        return CallToolResult(content=[TextContent(type="text", text=json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))], is_error=True)


@server.tool(description="Return key visual frames from an explicit time range. start_time/end_time accept Unix seconds or ISO-8601 strings.", annotations=RO)
def avc_clip(start_time: str, end_time: str, source: str | None = None, max_frames: int = 8) -> CallToolResult:
    try:
        src = resolve_source(source)
        start_ts = _parse_time_value(start_time)
        end_ts = _parse_time_value(end_time)
        # Bound one clip request to retained history and one hour to prevent accidental huge responses.
        if abs(end_ts - start_ts) > 3600:
            return CallToolResult(content=[TextContent(type="text", text=json.dumps({"ok": False, "error": "clip range may not exceed 3600 seconds"}, ensure_ascii=False))], is_error=True)
        rows = _rows_for_range(src["id"], start_ts, end_ts, raw_limit=3000)
        if not rows:
            return CallToolResult(content=[TextContent(type="text", text=json.dumps({"ok": False, "error": "no frames in requested clip"}, ensure_ascii=False))], is_error=True)
        return _visual_result(src, rows, max_frames, f"{now_iso(start_ts)}..{now_iso(end_ts)}")
    except Exception as e:
        return CallToolResult(content=[TextContent(type="text", text=json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))], is_error=True)


@server.tool(description="Return one frame's semantic context plus its screenshot so the calling AI can answer a visual question about that exact historical UI state.", annotations=RO)
def avc_visual_query(frame_id: str, question: str) -> CallToolResult:
    try:
        row = frame_row(frame_id)
        if not row:
            return CallToolResult(content=[TextContent(type="text", text=json.dumps({"ok": False, "error": f"frame not found: {frame_id}"}, ensure_ascii=False))], is_error=True)
        payload = {
            "ok": True, "question": clip_text(question, 2000),
            "instruction": "Answer the question using the semantic frame and the following image. If image is unavailable, use vision_cache/semantic data and state the limitation.",
            "frame": frame_dict(row, 12000, True),
        }
        content: list[Any] = [TextContent(type="text", text=json.dumps(payload, ensure_ascii=False, indent=2))]
        if frame_has_image(row):
            content.append(ImageContent(type="image", data=base64.b64encode(Path(row["screenshot_path"]).read_bytes()).decode("ascii"),
                                        mime_type=row["screenshot_mime"] or "image/jpeg"))
            return CallToolResult(content=content, is_error=False)
        return CallToolResult(content=content, is_error=False)
    except Exception as e:
        return CallToolResult(content=[TextContent(type="text", text=json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))], is_error=True)


@server.tool(description="Cache a visual interpretation for one frame after an AI has inspected its screenshot. This stores no computer-control action; it preserves a lightweight summary after screenshots expire.", annotations=CACHE_WRITE)
def avc_store_vision_cache(frame_id: str, description: str, page_type: str = "", objects: list[str] | None = None,
                           controls: list[str] | None = None, entities: list[str] | None = None, model: str = "") -> dict[str, Any]:
    row = frame_row(frame_id)
    if not row:
        return {"ok": False, "error": f"frame not found: {frame_id}"}
    t = now_ts()
    with db() as conn:
        conn.execute("""
          INSERT INTO vision_cache(frame_id,source_id,frame_ts,app,window_title,url,description,page_type,objects_json,controls_json,entities_json,model,generated_at)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
          ON CONFLICT(frame_id) DO UPDATE SET source_id=excluded.source_id,frame_ts=excluded.frame_ts,app=excluded.app,
            window_title=excluded.window_title,url=excluded.url,description=excluded.description,page_type=excluded.page_type,
            objects_json=excluded.objects_json,controls_json=excluded.controls_json,entities_json=excluded.entities_json,
            model=excluded.model,generated_at=excluded.generated_at
        """, (
            frame_id, row["source_id"], float(row["ts"]), row["app"], row["window_title"], row["url"],
            clip_text(description, 8000), clip_text(page_type, 300), safe_json(objects or [], 32000),
            safe_json(controls or [], 32000), safe_json(entities or [], 32000), clip_text(model, 120), t,
        ))
        conn.execute("UPDATE vision_candidates SET status='completed',updated_at=? WHERE frame_id=?",(t,frame_id))
    return {"ok": True, "frame_id": frame_id, "vision_cache": vision_cache_for(frame_id)}


@server.tool(description="Read a cached visual interpretation for one frame, if one has previously been stored.", annotations=RO)
def avc_vision_cache(frame_id: str) -> dict[str, Any]:
    cache = vision_cache_for(frame_id)
    return {"ok": True, "frame_id": frame_id, "cached": bool(cache), "vision_cache": cache}


@server.tool(description="Read lightweight long-lived UI transition events (7 days by default) even after full Semantic Frames have expired.", annotations=RO)
def avc_event_history(source: str | None = None, hours: int = 24, limit: int = 200) -> dict[str, Any]:
    try:
        src = resolve_source(source)
        hours = max(1, min(int(hours), EVENT_RETENTION_HOURS))
        limit = max(1, min(int(limit), 1000))
        cutoff = now_ts() - hours * 3600
        with db() as conn:
            rows = conn.execute(
                "SELECT * FROM timeline_events WHERE source_id=? AND ts>=? ORDER BY ts DESC LIMIT ?",
                (src["id"], cutoff, limit),
            ).fetchall()
        items = [{
            "event_id": r["id"], "frame_id": r["frame_id"], "timestamp": now_iso(float(r["ts"])),
            "type": r["event_type"], "app": r["app"], "window_title": r["window_title"],
            "url": r["url"], "active_tab": r["active_tab"], "detail": load_json(r["detail_json"], {}),
        } for r in reversed(rows)]
        return {"ok": True, "source": source_dict(src), "retention_hours": EVENT_RETENTION_HOURS, "events": items, "count": len(items)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@server.tool(description="Search cached visual interpretations. Useful after original screenshots have expired.", annotations=RO)
def avc_search_vision(query: str, source: str | None = None, limit: int = 20) -> dict[str, Any]:
    q = (query or "").strip()
    if not q:
        return {"ok": False, "error": "query is required"}
    limit = max(1, min(int(limit), 100))
    try:
        source_id = resolve_source(source)["id"] if source else None
        like = f"%{q}%"
        with db() as conn:
            if source_id:
                rows = conn.execute("""
                  SELECT * FROM vision_cache WHERE source_id=? AND
                    (description LIKE ? OR page_type LIKE ? OR entities_json LIKE ? OR controls_json LIKE ?)
                  ORDER BY frame_ts DESC LIMIT ?
                """, (source_id, like, like, like, like, limit)).fetchall()
            else:
                rows = conn.execute("""
                  SELECT * FROM vision_cache WHERE
                    (description LIKE ? OR page_type LIKE ? OR entities_json LIKE ? OR controls_json LIKE ?)
                  ORDER BY frame_ts DESC LIMIT ?
                """, (like, like, like, like, limit)).fetchall()
        results = [vision_cache_for(r["frame_id"]) for r in rows]
        return {"ok": True, "query": q, "results": results, "count": len(results)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@server.tool(description="Return the latest native Browser Bridge semantic snapshot: URL, active tab title, visible page text, compact DOM, focus and viewport.", annotations=RO)
def avc_browser_context(source: str | None = None, text_limit: int = 16000) -> dict[str, Any]:
    try:
        src=resolve_source(source); row=latest_browser_snapshot(src["id"])
        return {"ok":bool(row),"source":source_dict(src),"browser":browser_snapshot_dict(row,text_limit=text_limit,include_dom=True) if row else None}
    except Exception as e: return {"ok":False,"error":str(e)}


@server.tool(description="Read recent Browser Bridge semantic snapshots without screenshots. Useful for URL/tab/DOM/scroll history.", annotations=RO)
def avc_browser_history(source: str | None = None, seconds: int = 120, limit: int = 40) -> dict[str, Any]:
    try:
        src=resolve_source(source)
        return {"ok":True,"source":source_dict(src),"snapshots":browser_history_data(src["id"],seconds,limit)}
    except Exception as e: return {"ok":False,"error":str(e)}



@server.tool(description="Return observer-only input-to-UI-change links with approximate latency. Links require explicit mouse, keyboard, submit or browser navigation evidence.", annotations=RO)
def avc_causal_timeline(source: str | None = None, seconds: int = 180, limit: int = 80) -> dict[str,Any]:
    try:
        src=resolve_source(source); seconds=max(1,min(int(seconds),3600)); limit=max(1,min(int(limit),300))
        cutoff=now_ts()-seconds
        with db() as conn: rows=conn.execute("SELECT * FROM causal_links WHERE source_id=? AND effect_ts>=? ORDER BY effect_ts DESC LIMIT ?",(src["id"],cutoff,limit)).fetchall()
        links=[{"causal_id":r["id"],"cause_frame_id":r["cause_frame_id"],"effect_frame_id":r["effect_frame_id"],"cause_time":now_iso(float(r["cause_ts"])),"effect_time":now_iso(float(r["effect_ts"])),"cause_type":r["cause_type"],"cause_label":r["cause_label"],"effect_type":r["effect_type"],"effect_summary":r["effect_summary"],"latency_ms":r["latency_ms"],"confidence":r["confidence"],"detail":load_json(r["detail_json"],{})} for r in reversed(rows)]
        return {"ok":True,"source":source_dict(src),"links":links,"count":len(links),"note":"Observer-only evidence: each hint requires an explicit input event; confidence is not proof of causation."}
    except Exception as e:return {"ok":False,"error":str(e)}


@server.tool(description="Return recent automatically detected UI anomalies with severity and source frame.", annotations=RO)
def avc_anomalies(source: str | None = None, hours: int = 24, severity: str = "", limit: int = 100) -> dict[str,Any]:
    try:
        src=resolve_source(source); hours=max(1,min(int(hours),72)); limit=max(1,min(int(limit),300))
        cutoff=now_ts()-hours*3600; sev=severity.strip().lower()
        with db() as conn:
            if sev: rows=conn.execute("SELECT * FROM semantic_events WHERE source_id=? AND ts>=? AND anomaly=1 AND lower(severity)=? ORDER BY ts DESC LIMIT ?",(src["id"],cutoff,sev,limit)).fetchall()
            else: rows=conn.execute("SELECT * FROM semantic_events WHERE source_id=? AND ts>=? AND anomaly=1 ORDER BY ts DESC LIMIT ?",(src["id"],cutoff,limit)).fetchall()
        items=[{"event_id":r["id"],"frame_id":r["frame_id"],"timestamp":now_iso(float(r["ts"])),"severity":r["severity"],"summary":r["summary"],"app":r["app"],"window_title":r["window_title"],"url":r["url"],"confidence":r["confidence"],"detail":load_json(r["detail_json"],{})} for r in reversed(rows)]
        return {"ok":True,"source":source_dict(src),"anomalies":items,"count":len(items)}
    except Exception as e:return {"ok":False,"error":str(e)}


@server.tool(description="Read Browser Dev Observer diagnostics such as JS errors, failed resources, HTTP errors, network failures and page performance for local/development pages.", annotations=RO)
def avc_dev_context(source: str | None = None, minutes: int = 30, severity: str = "", limit: int = 100) -> dict[str,Any]:
    try:
        src=resolve_source(source); minutes=max(1,min(int(minutes),1440)); limit=max(1,min(int(limit),300)); cutoff=now_ts()-minutes*60
        sev=(severity or "").strip().lower()
        with db() as conn:
            if sev:
                rows=conn.execute("SELECT * FROM dev_events WHERE source_id=? AND ts>=? AND lower(severity)=? ORDER BY ts DESC LIMIT ?",(src["id"],cutoff,sev,limit)).fetchall()
            else:
                rows=conn.execute("SELECT * FROM dev_events WHERE source_id=? AND ts>=? ORDER BY ts DESC LIMIT ?",(src["id"],cutoff,limit)).fetchall()
        events=[{"event_id":r["id"],"timestamp":now_iso(float(r["ts"])),"event_type":r["event_type"],"severity":r["severity"],"summary":r["summary"],"url":r["url"],"detail":load_json(r["detail_json"],{}),"extension_version":r["extension_version"]} for r in reversed(rows)]
        return {"ok":True,"source":source_dict(src),"minutes":minutes,"events":events,"count":len(events)}
    except Exception as e:return {"ok":False,"error":str(e)}


@server.tool(description="List high-value keyframes waiting for genuine AI visual interpretation. These candidates are queued automatically; use avc_frame_image/avc_visual_query and then avc_store_vision_cache after actually viewing them.", annotations=RO)
def avc_vision_candidates(source: str | None = None, limit: int = 30) -> dict[str,Any]:
    try:
        src=resolve_source(source); limit=max(1,min(int(limit),100))
        with db() as conn:
            rows=conn.execute("SELECT vc.*,f.window_title,f.url,f.app,f.screenshot_path FROM vision_candidates vc LEFT JOIN frames f ON f.id=vc.frame_id WHERE vc.source_id=? AND vc.status='pending' ORDER BY vc.priority DESC,vc.frame_ts DESC LIMIT ?",(src["id"],limit)).fetchall()
        items=[]
        for r in rows:
            items.append({"frame_id":r["frame_id"],"timestamp":now_iso(float(r["frame_ts"])),"priority":r["priority"],"reasons":load_json(r["reason_json"],[]),"app":r["app"] or "","window_title":r["window_title"] or "","url":r["url"] or "","screenshot_available":bool(r["screenshot_path"] and Path(r["screenshot_path"]).exists())})
        return {"ok":True,"source":source_dict(src),"candidates":items,"count":len(items),"note":"Queueing is automatic; visual interpretation is not fabricated server-side."}
    except Exception as e:return {"ok":False,"error":str(e)}


@server.tool(description="Compare two AVC Scene Graphs using stable object IDs. Returns added, removed and state-changed UI objects across Frames.", annotations=RO)
def avc_scene_diff(frame_a: str, frame_b: str, max_objects: int = 300) -> dict[str,Any]:
    try:
        a=frame_row(frame_a); b=frame_row(frame_b)
        if not a or not b:return {"ok":False,"error":"frame not found","missing":[x for x,r in ((frame_a,a),(frame_b,b)) if not r]}
        ga=build_scene_graph(a,max_objects); gb=build_scene_graph(b,max_objects)
        ma={o.get("stable_id"):o for o in ga["objects"] if o.get("stable_id")}; mb={o.get("stable_id"):o for o in gb["objects"] if o.get("stable_id")}
        added=[mb[k] for k in mb.keys()-ma.keys()][:120]
        removed=[ma[k] for k in ma.keys()-mb.keys()][:120]
        changed=[]
        for k in ma.keys() & mb.keys():
            if ma[k].get("state_hash") != mb[k].get("state_hash"):
                changed.append({"stable_id":k,"before":ma[k],"after":mb[k]})
                if len(changed)>=120:break
        return {"ok":True,"before_frame_id":frame_a,"after_frame_id":frame_b,"time_delta_ms":round((float(b["ts"])-float(a["ts"]))*1000,1),"added":added,"removed":removed,"changed":changed,"counts":{"added":len(added),"removed":len(removed),"changed":len(changed)},"world_state":{"before":ga["world_state"],"after":gb["world_state"]}}
    except Exception as e:return {"ok":False,"error":str(e)}


@server.tool(description="Return AVC's unified Scene Graph / World State for one Frame. DOM and UIA controls are fused server-side so GPT does not need to reconcile the raw sources.", annotations=RO)
def avc_scene_graph(frame_id: str | None = None, source: str | None = None, max_objects: int = 200) -> dict[str,Any]:
    try:
        if frame_id:
            row=frame_row(frame_id)
            if not row:return {"ok":False,"error":f"frame not found: {frame_id}"}
        else:
            src=resolve_source(source); row=latest_frame(src["id"])
            if not row:return {"ok":False,"error":"no frames"}
        return {"ok":True,"scene":build_scene_graph(row,max_objects)}
    except Exception as e:return {"ok":False,"error":str(e)}


@server.tool(description="Replay seconds before/after one exact frame_id and return selected visual keyframes.", annotations=RO)
def avc_frame_replay(frame_id: str, before_seconds: int = 5, after_seconds: int = 5, max_frames: int = 8) -> CallToolResult:
    try:
        anchor=frame_row(frame_id)
        if not anchor:
            return CallToolResult(content=[TextContent(type="text",text=json.dumps({"ok":False,"error":f"frame not found: {frame_id}"},ensure_ascii=False))],is_error=True)
        before_seconds=max(0,min(int(before_seconds),300)); after_seconds=max(0,min(int(after_seconds),300))
        rows=_rows_for_range(anchor["source_id"],float(anchor["ts"])-before_seconds,float(anchor["ts"])+after_seconds,raw_limit=2000)
        src=resolve_source(anchor["source_id"])
        return _visual_result(src,rows,max_frames,f"frame_{frame_id}_minus_{before_seconds}s_plus_{after_seconds}s")
    except Exception as e:
        return CallToolResult(content=[TextContent(type="text",text=json.dumps({"ok":False,"error":str(e)},ensure_ascii=False))],is_error=True)


@server.tool(description="Replay around one exact frame: include N neighboring frames and optionally return selected screenshots. Useful for 'what happened just before/after this frame?'.", annotations=RO)
def avc_frame_neighbors(frame_id: str, before_frames: int = 3, after_frames: int = 3, max_images: int = 8) -> CallToolResult:
    try:
        anchor=frame_row(frame_id)
        if not anchor:
            return CallToolResult(content=[TextContent(type="text",text=json.dumps({"ok":False,"error":f"frame not found: {frame_id}"},ensure_ascii=False))],is_error=True)
        before_frames=max(0,min(int(before_frames),20)); after_frames=max(0,min(int(after_frames),20))
        with db() as conn:
            bef=conn.execute("SELECT * FROM frames WHERE source_id=? AND ts<? ORDER BY ts DESC LIMIT ?",(anchor["source_id"],anchor["ts"],before_frames)).fetchall()
            aft=conn.execute("SELECT * FROM frames WHERE source_id=? AND ts>? ORDER BY ts ASC LIMIT ?",(anchor["source_id"],anchor["ts"],after_frames)).fetchall()
        rows=list(reversed(bef))+[anchor]+list(aft)
        src=resolve_source(anchor["source_id"])
        return _visual_result(src,rows,min(max(1,int(max_images)),12),f"neighbors_of_{frame_id}")
    except Exception as e:
        return CallToolResult(content=[TextContent(type="text",text=json.dumps({"ok":False,"error":str(e)},ensure_ascii=False))],is_error=True)


@server.tool(description="Replay visual context around a time point. time accepts Unix seconds or ISO-8601; AVC returns selected keyframes from before/after the point.", annotations=RO)
def avc_timepoint(time_value: str, source: str | None = None, before_seconds: int = 5, after_seconds: int = 5, max_frames: int = 8) -> CallToolResult:
    try:
        src=resolve_source(source); t=_parse_time_value(time_value)
        before_seconds=max(0,min(int(before_seconds),300)); after_seconds=max(0,min(int(after_seconds),300))
        rows=_rows_for_range(src["id"],t-before_seconds,t+after_seconds,raw_limit=2000)
        if not rows:
            return CallToolResult(content=[TextContent(type="text",text=json.dumps({"ok":False,"error":"no frames near requested time"},ensure_ascii=False))],is_error=True)
        return _visual_result(src,rows,max_frames,f"timepoint_{now_iso(t)}_minus_{before_seconds}s_plus_{after_seconds}s")
    except Exception as e:
        return CallToolResult(content=[TextContent(type="text",text=json.dumps({"ok":False,"error":str(e)},ensure_ascii=False))],is_error=True)


@server.tool(description="Build/read AVC semantic memory: Raw Frame -> Semantic Event -> Episode -> Session. Returns compressed human-readable context for recent work.", annotations=RO)
def avc_memory_context(source: str | None = None, hours: int = 6, episode_limit: int = 30, event_limit: int = 80) -> dict[str,Any]:
    try:
        src=resolve_source(source)
        cutoff=now_ts()-max(1,min(int(hours),72))*3600
        with db() as conn:
            engine=conn.execute("SELECT * FROM engine_state WHERE source_id=?",(src["id"],)).fetchone()
            eps=conn.execute("SELECT * FROM memory_episodes WHERE source_id=? AND end_ts>=? ORDER BY start_ts DESC LIMIT ?",(src["id"],cutoff,max(1,min(int(episode_limit),100)))).fetchall()
            ses=conn.execute("SELECT * FROM memory_sessions WHERE source_id=? AND end_ts>=? ORDER BY start_ts DESC LIMIT 20",(src["id"],cutoff)).fetchall()
            evs=conn.execute("SELECT * FROM semantic_events WHERE source_id=? AND ts>=? ORDER BY ts DESC LIMIT ?",(src["id"],cutoff,max(1,min(int(event_limit),300)))).fetchall()
        stats={"mode":"incremental","engine_version":state_engine.ENGINE_VERSION,"last_frame_id":engine["last_frame_id"] if engine else "","processed_frames":engine["processed_frames"] if engine else 0}
        events=[{"event_id":r["id"],"frame_id":r["frame_id"],"timestamp":now_iso(float(r["ts"])),"type":r["event_type"],"summary":r["summary"],"app":r["app"],"window_title":r["window_title"],"url":r["url"],"confidence":r["confidence"],"anomaly":bool(r["anomaly"]),"severity":r["severity"],"detail":load_json(r["detail_json"],{})} for r in reversed(evs)]
        return {"ok":True,"source":source_dict(src),"build":stats,"sessions":[_session_dict(x) for x in reversed(ses)],"episodes":[_episode_dict(x) for x in reversed(eps)],"events":events,"usage_note":"Read-only incremental memory. Default to sessions/episodes; drill down only when needed."}
    except Exception as e:
        return {"ok":False,"error":str(e)}


@server.tool(description="Infer the user's current activity from the newest Episode and recent semantic events. This is lightweight task inference, not an autonomous planner.", annotations=RO)
def avc_current_activity(source: str | None = None) -> dict[str,Any]:
    try:
        src=resolve_source(source)
        with db() as conn:
            world=state_engine.current(conn,src["id"])
            ep=conn.execute("SELECT * FROM memory_episodes WHERE source_id=? ORDER BY end_ts DESC LIMIT 1",(src["id"],)).fetchone()
            ev=conn.execute("SELECT * FROM semantic_events WHERE source_id=? ORDER BY ts DESC LIMIT 8",(src["id"],)).fetchall()
        if not world:return {"ok":False,"source":source_dict(src),"error":"no current world state"}
        age=float(world["age_seconds"]); freshness=1.0 if age<10 else 0.9 if age<30 else 0.7 if age<120 else 0.4
        confidence=float(world["confidence"])*freshness
        return {"ok":True,"source":source_dict(src),"current_activity":world["activity"],"goal_confidence":round(min(0.99,confidence),3),"world_state":world,"episode":_episode_dict(ep) if ep else None,"recent_events":[{"timestamp":now_iso(float(x["ts"])),"type":x["event_type"],"summary":x["summary"]} for x in reversed(ev)]}
    except Exception as e:return {"ok":False,"error":str(e)}


@server.tool(description="Return the reliable Current World State maintained incrementally from the latest accepted frame.", annotations=RO)
def avc_world_state(source: str | None = None) -> dict[str,Any]:
    try:
        src=resolve_source(source)
        with db() as conn:
            world=state_engine.current(conn,src["id"])
            engine=conn.execute("SELECT * FROM engine_state WHERE source_id=?",(src["id"],)).fetchone()
        return {"ok":bool(world),"source":source_dict(src),"world_state":world,
                "engine":{"version":engine["engine_version"],"processed_frames":engine["processed_frames"],
                          "last_error":engine["last_error"],"updated_at":now_iso(float(engine["updated_at"]))} if engine else None}
    except Exception as e:return {"ok":False,"error":str(e)}


@server.tool(description="Read visual-memory queue, cache and today's bounded background-processing budget.", annotations=RO)
def avc_vision_worker_status(source: str | None = None) -> dict[str,Any]:
    try:
        src=resolve_source(source) if source else None
        with db() as conn:
            if src:
                pending=conn.execute("SELECT COUNT(*) FROM vision_candidates WHERE source_id=? AND status='pending'",(src["id"],)).fetchone()[0]
                cached=conn.execute("SELECT COUNT(*) FROM vision_cache WHERE source_id=?",(src["id"],)).fetchone()[0]
            else:
                pending=conn.execute("SELECT COUNT(*) FROM vision_candidates WHERE status='pending'").fetchone()[0]
                cached=conn.execute("SELECT COUNT(*) FROM vision_cache").fetchone()[0]
            budget=conn.execute("SELECT * FROM vision_budget ORDER BY day DESC LIMIT 1").fetchone()
        return {"ok":True,"source":source_dict(src) if src else None,"pending":int(pending),"cached":int(cached),
                "budget":dict(budget) if budget else None}
    except Exception as e:return {"ok":False,"error":str(e)}


@server.tool(description="Unified history search across Frame visible text, UIA, DOM, Browser Bridge, semantic events, Episodes and Vision Cache. Filters app/window/event/time are optional.", annotations=RO)
def avc_search_timeline(query: str = "", source: str | None = None, app: str = "", window: str = "", event: str = "", hours: int = 24, limit: int = 50) -> dict[str,Any]:
    try:
        src=resolve_source(source); hours=max(1,min(int(hours),MEMORY_RETENTION_HOURS)); limit=max(1,min(int(limit),200)); cutoff=now_ts()-hours*3600
        q=(query or "").strip(); like=f"%{q}%"; app_like=f"%{app.strip()}%"; win_like=f"%{window.strip()}%"; ev_like=f"%{event.strip()}%"
        results=[]
        with db() as conn:
            fr=conn.execute("SELECT * FROM frames WHERE source_id=? AND ts>=? AND (?='' OR app LIKE ?) AND (?='' OR window_title LIKE ?) AND (?='' OR (visible_text LIKE ? OR uia_json LIKE ? OR dom_json LIKE ? OR window_title LIKE ? OR url LIKE ?)) ORDER BY ts DESC LIMIT ?",
                            (src["id"],cutoff,app,app_like,window,win_like,q,like,like,like,like,like,limit)).fetchall()
            for r in fr:
                results.append({"kind":"frame","timestamp":now_iso(float(r["ts"])),"frame_id":r["id"],"app":r["app"],"window_title":r["window_title"],"url":r["url"],"snippet":clip_text(r["visible_text"],700)})
            se=conn.execute("SELECT * FROM semantic_events WHERE source_id=? AND ts>=? AND (?='' OR app LIKE ?) AND (?='' OR window_title LIKE ?) AND (?='' OR event_type LIKE ?) AND (?='' OR summary LIKE ?) ORDER BY ts DESC LIMIT ?",
                            (src["id"],cutoff,app,app_like,window,win_like,event,ev_like,q,like,limit)).fetchall()
            for r in se:
                results.append({"kind":"event","timestamp":now_iso(float(r["ts"])),"frame_id":r["frame_id"],"event_type":r["event_type"],"summary":r["summary"],"anomaly":bool(r["anomaly"]),"severity":r["severity"]})
            br=conn.execute("SELECT * FROM browser_snapshots WHERE source_id=? AND ts>=? AND (?='' OR title LIKE ?) AND (?='' OR (visible_text LIKE ? OR dom_json LIKE ? OR title LIKE ? OR url LIKE ?)) ORDER BY ts DESC LIMIT ?",
                            (src["id"],cutoff,window,win_like,q,like,like,like,like,limit)).fetchall()
            for r in br:
                results.append({"kind":"browser","timestamp":now_iso(float(r["ts"])),"snapshot_id":r["id"],"window_title":r["title"],"url":r["url"],"snippet":clip_text(r["visible_text"],700)})
            de=conn.execute("SELECT * FROM dev_events WHERE source_id=? AND ts>=? AND (?='' OR event_type LIKE ?) AND (?='' OR summary LIKE ? OR detail_json LIKE ? OR url LIKE ?) ORDER BY ts DESC LIMIT ?",
                            (src["id"],cutoff,event,ev_like,q,like,like,like,limit)).fetchall()
            for r in de:
                results.append({"kind":"dev_event","timestamp":now_iso(float(r["ts"])),"event_id":r["id"],"event_type":r["event_type"],"severity":r["severity"],"summary":r["summary"],"url":r["url"]})
            ep=conn.execute("SELECT * FROM memory_episodes WHERE source_id=? AND end_ts>=? AND (?='' OR (title LIKE ? OR summary LIKE ?)) ORDER BY end_ts DESC LIMIT ?",(src["id"],cutoff,q,like,like,limit)).fetchall()
            for r in ep:results.append({"kind":"episode","timestamp":now_iso(float(r["start_ts"])),"episode_id":r["id"],"title":r["title"],"summary":r["summary"],"anomaly_count":r["anomaly_count"]})
            vc=conn.execute("SELECT * FROM vision_cache WHERE source_id=? AND frame_ts>=? AND (?='' OR description LIKE ? OR page_type LIKE ? OR entities_json LIKE ? OR controls_json LIKE ?) ORDER BY frame_ts DESC LIMIT ?",(src["id"],cutoff,q,like,like,like,like,limit)).fetchall()
            for r in vc:results.append({"kind":"vision_cache","timestamp":now_iso(float(r["frame_ts"])),"frame_id":r["frame_id"],"description":r["description"],"page_type":r["page_type"]})
        def tsval(x):
            try:return _parse_time_value(x["timestamp"])
            except:return 0
        results=sorted(results,key=tsval,reverse=True)[:limit]
        return {"ok":True,"source":source_dict(src),"query":q,"filters":{"app":app,"window":window,"event":event,"hours":hours},"results":results,"count":len(results)}
    except Exception as e:return {"ok":False,"error":str(e)}


@server.tool(description="Compare two Semantic Frames. Returns added/removed text, navigation/focus changes, perceptual visual distance and screenshot change-region bounding box; includes a diff crop image when possible.", annotations=RO)
def avc_compare_frames(frame_a: str, frame_b: str, include_images: bool = True) -> CallToolResult:
    try:
        a=frame_row(frame_a); b=frame_row(frame_b)
        if not a or not b:
            missing=[x for x,r in ((frame_a,a),(frame_b,b)) if not r]
            return CallToolResult(content=[TextContent(type="text",text=json.dumps({"ok":False,"error":"frame not found","missing":missing},ensure_ascii=False))],is_error=True)
        sa=_frame_strings(a); sb=_frame_strings(b); added=sorted(sb-sa,key=lambda x:(len(x),x))[:120]; removed=sorted(sa-sb,key=lambda x:(len(x),x))[:120]
        def hamming(x,y):
            try:return bin(int(x,16)^int(y,16)).count("1")
            except:return None
        vdist=hamming(str(a["visual_hash"] or ""),str(b["visual_hash"] or ""))
        visual,diff_bytes=_screenshot_diff(a,b)
        payload={"ok":True,"before":keyframe_manifest(a),"after":keyframe_manifest(b),"time_delta_ms":round((float(b["ts"])-float(a["ts"]))*1000,1),"changes":{"app":{"from":a["app"],"to":b["app"],"changed":a["app"]!=b["app"]},"window":{"from":a["window_title"],"to":b["window_title"],"changed":a["window_title"]!=b["window_title"]},"url":{"from":a["url"],"to":b["url"],"changed":a["url"]!=b["url"]},"active_tab":{"from":a["active_tab"],"to":b["active_tab"],"changed":a["active_tab"]!=b["active_tab"]},"focus":{"from":load_json(a["focus_json"],{}),"to":load_json(b["focus_json"],{}),"changed":a["focus_json"]!=b["focus_json"]},"added_text":added,"removed_text":removed,"perceptual_hash_distance":vdist,"visual_region":visual}}
        content=[TextContent(type="text",text=json.dumps(payload,ensure_ascii=False,indent=2))]
        if include_images and frame_has_image(a):
            content += [TextContent(type="text",text="BEFORE"),ImageContent(type="image",data=base64.b64encode(Path(a["screenshot_path"]).read_bytes()).decode("ascii"),mime_type=a["screenshot_mime"] or "image/jpeg")]
        if include_images and frame_has_image(b):
            content += [TextContent(type="text",text="AFTER"),ImageContent(type="image",data=base64.b64encode(Path(b["screenshot_path"]).read_bytes()).decode("ascii"),mime_type=b["screenshot_mime"] or "image/jpeg")]
        if include_images and diff_bytes:
            content += [TextContent(type="text",text="VISUAL DIFF REGION (amplified)"),ImageContent(type="image",data=base64.b64encode(diff_bytes).decode("ascii"),mime_type="image/jpeg")]
        return CallToolResult(content=content,is_error=False)
    except Exception as e:return CallToolResult(content=[TextContent(type="text",text=json.dumps({"ok":False,"error":str(e)},ensure_ascii=False))],is_error=True)


@server.tool(description="Search recent visible UI text for a word or phrase. Useful for finding something the user saw earlier without replaying screenshots.", annotations=RO)
def avc_search_text(query: str, source: str | None = None, hours: int = 2, limit: int = 20) -> dict[str, Any]:
    q = (query or "").strip()
    if not q:
        return {"ok": False, "error": "query is required"}
    try:
        src = resolve_source(source)
        cutoff = now_ts() - max(1, min(int(hours), RETENTION_HOURS)) * 3600
        limit = max(1, min(int(limit), 50))
        with db() as conn:
            rows = conn.execute(
                "SELECT * FROM frames WHERE source_id=? AND ts>=? AND visible_text LIKE ? ORDER BY ts DESC LIMIT ?",
                (src["id"], cutoff, f"%{q}%", limit),
            ).fetchall()
        results = []
        for r in rows:
            text = r["visible_text"] or ""
            idx = text.lower().find(q.lower())
            start = max(0, idx - 300) if idx >= 0 else 0
            snippet = text[start:start + 1000]
            results.append({"frame_id": r["id"], "timestamp": now_iso(float(r["ts"])), "app": r["app"],
                            "window_title": r["window_title"], "url": r["url"], "snippet": snippet})
        return {"ok": True, "query": q, "source": source_dict(src), "results": results, "count": len(results)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


app = server.streamable_http_app(
    streamable_http_path=f"/mcp/{MCP_TOKEN}", stateless_http=True, json_response=True,
    host=MCP_HOST, max_sessions=100, max_request_body_size=20 * 1024 * 1024,
)
for route in reversed([
    Route("/", root_page, methods=["GET"]),
    Route("/health", health, methods=["GET"]),
    Route("/api/v1/register", register_source, methods=["POST"]),
    Route("/api/v1/heartbeat", heartbeat, methods=["POST"]),
    Route("/api/v1/control", source_control, methods=["GET"]),
    Route("/api/v1/browser/control", browser_control, methods=["GET"]),
    Route("/api/v1/browser/snapshot", browser_snapshot_ingest, methods=["POST"]),
    Route("/api/v1/browser/dev-event", browser_dev_event, methods=["POST"]),
    Route("/api/v1/frame", ingest_frame, methods=["POST"]),
    Route("/agent/windows-observer.ps1", windows_observer_script, methods=["GET"]),
    Route("/console/{token}", dashboard_page, methods=["GET"]),
    Route("/console/{token}/", dashboard_page, methods=["GET"]),
    Route("/console/{token}/api/status", dashboard_status, methods=["GET"]),
    Route("/console/{token}/api/control", dashboard_control, methods=["POST"]),
    Route("/console/{token}/browser-extension.zip", dashboard_browser_extension, methods=["GET"]),
    Route("/console/{token}/snapshot", dashboard_snapshot, methods=["GET"]),
]):
    app.routes.insert(0, route)
