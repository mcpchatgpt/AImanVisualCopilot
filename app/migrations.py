"""Versioned, idempotent SQLite migrations for AVC.

Migrations are intentionally separate from application startup schema creation so an
older database can be upgraded before indexes reference newly-added columns.
"""
from __future__ import annotations

import sqlite3
import time


SCHEMA_VERSION = 9


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {str(r[0]) for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    if table not in _tables(conn):
        return set()
    return {str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})")}


def _add_column(conn: sqlite3.Connection, table: str, name: str, ddl: str) -> None:
    if table in _tables(conn) and name not in _columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def preflight_legacy_schema(conn: sqlite3.Connection) -> None:
    """Repair columns needed by indexes before the main schema script runs.

    AVC 0.8 could fail during startup because it created an index on source_id before
    adding that column to a legacy table. This preflight must stay dependency-free.
    """
    legacy_source_tables = (
        "frames", "timeline_events", "browser_snapshots", "dev_events",
        "vision_candidates", "semantic_events", "causal_links",
        "memory_episodes", "memory_sessions",
    )
    for table in legacy_source_tables:
        _add_column(conn, table, "source_id", "TEXT NOT NULL DEFAULT ''")
    _add_column(conn, "vision_cache", "source_id", "TEXT NOT NULL DEFAULT ''")
    _add_column(conn, "vision_cache", "frame_ts", "REAL NOT NULL DEFAULT 0")


def apply_migrations(conn: sqlite3.Connection) -> int:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
          version INTEGER PRIMARY KEY,
          name TEXT NOT NULL,
          applied_at REAL NOT NULL
        )
    """)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS engine_state (
          source_id TEXT PRIMARY KEY,
          last_frame_id TEXT NOT NULL DEFAULT '',
          last_frame_ts REAL NOT NULL DEFAULT 0,
          current_episode_id TEXT NOT NULL DEFAULT '',
          current_session_id TEXT NOT NULL DEFAULT '',
          processed_frames INTEGER NOT NULL DEFAULT 0,
          engine_version TEXT NOT NULL DEFAULT '0.9.0',
          last_error TEXT NOT NULL DEFAULT '',
          updated_at REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS current_world_state (
          source_id TEXT PRIMARY KEY,
          frame_id TEXT NOT NULL,
          ts REAL NOT NULL,
          app TEXT NOT NULL DEFAULT '',
          window_title TEXT NOT NULL DEFAULT '',
          url TEXT NOT NULL DEFAULT '',
          active_tab TEXT NOT NULL DEFAULT '',
          surface TEXT NOT NULL DEFAULT '',
          focus_json TEXT NOT NULL DEFAULT '{}',
          cursor_json TEXT NOT NULL DEFAULT '{}',
          activity TEXT NOT NULL DEFAULT '',
          activity_confidence REAL NOT NULL DEFAULT 0.5,
          last_event_id TEXT NOT NULL DEFAULT '',
          freshness_ms INTEGER NOT NULL DEFAULT 0,
          quality_json TEXT NOT NULL DEFAULT '{}',
          updated_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_world_state_ts ON current_world_state(ts DESC);

        CREATE TABLE IF NOT EXISTS interaction_events (
          id TEXT PRIMARY KEY,
          source_id TEXT NOT NULL,
          frame_id TEXT NOT NULL DEFAULT '',
          ts REAL NOT NULL,
          kind TEXT NOT NULL,
          target_role TEXT NOT NULL DEFAULT '',
          target_name TEXT NOT NULL DEFAULT '',
          selector TEXT NOT NULL DEFAULT '',
          key_name TEXT NOT NULL DEFAULT '',
          x INTEGER,
          y INTEGER,
          app TEXT NOT NULL DEFAULT '',
          window_title TEXT NOT NULL DEFAULT '',
          detail_json TEXT NOT NULL DEFAULT '{}',
          created_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_interaction_source_ts ON interaction_events(source_id, ts DESC);

        CREATE TABLE IF NOT EXISTS vision_budget (
          day TEXT PRIMARY KEY,
          processed INTEGER NOT NULL DEFAULT 0,
          duplicates INTEGER NOT NULL DEFAULT 0,
          failed INTEGER NOT NULL DEFAULT 0,
          updated_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_frames_source_received ON frames(source_id, received_at DESC);
        CREATE INDEX IF NOT EXISTS idx_frames_source_sha ON frames(source_id, screenshot_sha256);
        CREATE INDEX IF NOT EXISTS idx_semantic_frame ON semantic_events(source_id, frame_id);
    """)
    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations(version,name,applied_at) VALUES(?,?,?)",
        (SCHEMA_VERSION, "incremental-state-world-interactions-vision", time.time()),
    )
    return SCHEMA_VERSION
