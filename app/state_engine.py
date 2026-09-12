"""Incremental semantic state engine for AVC 0.9.

The engine advances exactly once per new frame. Query tools never rebuild or delete
semantic history. It keeps current state separate from longer-lived task memory.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from typing import Any, Callable


ENGINE_VERSION = "0.9.0"


def _json(raw: str | None, default: Any) -> Any:
    try:
        return json.loads(raw) if raw else default
    except Exception:
        return default


def _dump(value: Any, limit: int = 32_000) -> str:
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return raw if len(raw) <= limit else json.dumps({"truncated": True, "preview": raw[:limit]}, ensure_ascii=False)


def _id(prefix: str, value: str) -> str:
    return prefix + hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def activity_label(row: sqlite3.Row) -> tuple[str, float]:
    app = str(row["app"] or "").strip()
    tab = str(row["active_tab"] or "").strip()
    title = str(row["window_title"] or "").strip()
    if tab:
        return tab, 0.98
    cleaned = re.sub(r"\s+-\s+(Google Chrome|Microsoft Edge|Mozilla Firefox)$", "", title, flags=re.I).strip()
    if cleaned:
        return cleaned, 0.94
    if app:
        return app, 0.82
    return "Unknown activity", 0.35


def _append_summary(existing: str, item: str, max_items: int = 8) -> str:
    items = [x.strip() for x in (existing or "").split(" → ") if x.strip()]
    if item and (not items or items[-1] != item):
        items.append(item)
    return " → ".join(items[-max_items:])[:3000]


def _upsert_world(conn: sqlite3.Connection, row: sqlite3.Row, last_event_id: str = "") -> None:
    activity, confidence = activity_label(row)
    meta = _json(row["metadata_json"], {})
    quality = {
        "browser_bridge": bool(meta.get("browser_bridge")),
        "uia": bool(_json(row["uia_json"], {}).get("elements")),
        "dom": bool(_json(row["dom_json"], {}).get("controls")),
        "screenshot": bool(row["screenshot_path"]),
    }
    now = time.time()
    conn.execute("""
      INSERT INTO current_world_state(source_id,frame_id,ts,app,window_title,url,active_tab,surface,
        focus_json,cursor_json,activity,activity_confidence,last_event_id,freshness_ms,quality_json,updated_at)
      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
      ON CONFLICT(source_id) DO UPDATE SET frame_id=excluded.frame_id,ts=excluded.ts,app=excluded.app,
        window_title=excluded.window_title,url=excluded.url,active_tab=excluded.active_tab,surface=excluded.surface,
        focus_json=excluded.focus_json,cursor_json=excluded.cursor_json,activity=excluded.activity,
        activity_confidence=excluded.activity_confidence,last_event_id=excluded.last_event_id,
        freshness_ms=excluded.freshness_ms,quality_json=excluded.quality_json,updated_at=excluded.updated_at
    """, (row["source_id"], row["id"], float(row["ts"]), row["app"], row["window_title"], row["url"],
          row["active_tab"], row["surface"], row["focus_json"], row["cursor_json"], activity, confidence,
          last_event_id, max(0, int((now-float(row["ts"]))*1000)), _dump(quality), now))


def _update_memory(conn: sqlite3.Connection, row: sqlite3.Row, events: list[dict[str, Any]]) -> tuple[str, str]:
    if not events:
        state = conn.execute("SELECT current_episode_id,current_session_id FROM engine_state WHERE source_id=?", (row["source_id"],)).fetchone()
        return (state[0], state[1]) if state else ("", "")
    ts = float(row["ts"]); now = time.time(); source_id = row["source_id"]
    inserted = []
    for idx, event in enumerate(events):
        eid = _id("sev_", f"{row['id']}:{idx}:{event['type']}")
        before = conn.total_changes
        detail = event.get("detail") or {}
        anomaly = int(event["type"] == "anomaly" or bool(detail.get("anomaly")))
        conn.execute("""INSERT OR IGNORE INTO semantic_events
          (id,source_id,frame_id,ts,event_type,summary,app,window_title,url,confidence,anomaly,severity,detail_json,created_at)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
          (eid, source_id, row["id"], ts, event["type"], event["summary"], row["app"], row["window_title"],
           row["url"], float(event.get("confidence", .8)), anomaly, str(detail.get("severity") or ""), _dump(detail), now))
        if conn.total_changes > before:
            inserted.append((eid, event, anomaly))
    if not inserted:
        state = conn.execute("SELECT current_episode_id,current_session_id FROM engine_state WHERE source_id=?", (source_id,)).fetchone()
        return (state[0], state[1]) if state else ("", "")

    last_ep = conn.execute("SELECT * FROM memory_episodes WHERE source_id=? ORDER BY end_ts DESC LIMIT 1", (source_id,)).fetchone()
    can_extend = bool(last_ep and 0 <= ts-float(last_ep["end_ts"]) <= 75 and ts-float(last_ep["start_ts"]) <= 600)
    summary = "；".join(str(x[1]["summary"]) for x in inserted)
    activity, _ = activity_label(row)
    apps = sorted(set(([str(row["app"])] if row["app"] else []) + (_json(last_ep["app_json"], []) if can_extend else [])))
    types = sorted(set([str(x[1]["type"]) for x in inserted] + (_json(last_ep["event_types_json"], []) if can_extend else [])))
    anomaly_count = sum(x[2] for x in inserted)
    if can_extend:
        episode_id = last_ep["id"]
        new_count = int(last_ep["event_count"]) + len(inserted)
        new_conf = round((float(last_ep["confidence"])*int(last_ep["event_count"]) + sum(float(x[1].get("confidence",.8)) for x in inserted))/new_count, 3)
        conn.execute("""UPDATE memory_episodes SET end_ts=?,title=?,summary=?,end_frame_id=?,event_count=?,confidence=?,
          anomaly_count=anomaly_count+?,app_json=?,event_types_json=?,updated_at=? WHERE id=?""",
          (ts, activity, _append_summary(last_ep["summary"], summary), row["id"], new_count, new_conf,
           anomaly_count, _dump(apps), _dump(types), now, episode_id))
        created_episode = False
    else:
        episode_id = _id("epi_", f"{source_id}:{row['id']}")
        conn.execute("""INSERT OR IGNORE INTO memory_episodes
          (id,source_id,start_ts,end_ts,title,summary,start_frame_id,end_frame_id,event_count,confidence,
           anomaly_count,app_json,event_types_json,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
          (episode_id, source_id, ts, ts, activity, summary[:3000], row["id"], row["id"], len(inserted),
           round(sum(float(x[1].get("confidence",.8)) for x in inserted)/len(inserted),3), anomaly_count,
           _dump(apps), _dump(types), now))
        created_episode = True

    last_session = conn.execute("SELECT * FROM memory_sessions WHERE source_id=? ORDER BY end_ts DESC LIMIT 1", (source_id,)).fetchone()
    can_extend_session = bool(last_session and 0 <= ts-float(last_session["end_ts"]) <= 1200)
    if can_extend_session:
        session_id = last_session["id"]
        episode_ids = _json(last_session["episode_ids_json"], [])
        if episode_id not in episode_ids:
            episode_ids.append(episode_id)
        conn.execute("""UPDATE memory_sessions SET end_ts=?,title=?,summary=?,episode_ids_json=?,episode_count=?,
          anomaly_count=anomaly_count+?,updated_at=? WHERE id=?""",
          (ts, activity, _append_summary(last_session["summary"], summary, 5), _dump(episode_ids[-100:]),
           len(episode_ids[-100:]), anomaly_count, now, session_id))
    else:
        session_id = _id("ses_", f"{source_id}:{episode_id}")
        conn.execute("""INSERT OR IGNORE INTO memory_sessions
          (id,source_id,start_ts,end_ts,title,summary,episode_ids_json,episode_count,anomaly_count,updated_at)
          VALUES(?,?,?,?,?,?,?,?,?,?)""",
          (session_id, source_id, ts, ts, activity, summary[:3000], _dump([episode_id]), 1, anomaly_count, now))
    return episode_id, session_id


def advance(conn: sqlite3.Connection, row: sqlite3.Row, previous: sqlite3.Row | None,
            event_builder: Callable[[sqlite3.Row, sqlite3.Row | None], list[dict[str, Any]]]) -> dict[str, Any]:
    events = event_builder(row, previous)
    episode_id, session_id = _update_memory(conn, row, events)
    last_event_id = _id("sev_", f"{row['id']}:{len(events)-1}:{events[-1]['type']}") if events else ""
    _upsert_world(conn, row, last_event_id)
    now = time.time()
    conn.execute("""INSERT INTO engine_state(source_id,last_frame_id,last_frame_ts,current_episode_id,current_session_id,
      processed_frames,engine_version,last_error,updated_at) VALUES(?,?,?,?,?,1,?,'',?)
      ON CONFLICT(source_id) DO UPDATE SET last_frame_id=excluded.last_frame_id,last_frame_ts=excluded.last_frame_ts,
      current_episode_id=excluded.current_episode_id,current_session_id=excluded.current_session_id,
      processed_frames=engine_state.processed_frames+1,engine_version=excluded.engine_version,last_error='',updated_at=excluded.updated_at""",
      (row["source_id"], row["id"], float(row["ts"]), episode_id, session_id, ENGINE_VERSION, now))
    return {"events": len(events), "episode_id": episode_id, "session_id": session_id, "activity": activity_label(row)[0]}


def initialize_latest(conn: sqlite3.Connection, source_id: str) -> bool:
    row = conn.execute("SELECT * FROM frames WHERE source_id=? ORDER BY ts DESC LIMIT 1", (source_id,)).fetchone()
    if not row:
        return False
    _upsert_world(conn, row)
    existing = conn.execute("SELECT 1 FROM engine_state WHERE source_id=?", (source_id,)).fetchone()
    if not existing:
        ep = conn.execute("SELECT id FROM memory_episodes WHERE source_id=? ORDER BY end_ts DESC LIMIT 1", (source_id,)).fetchone()
        ses = conn.execute("SELECT id FROM memory_sessions WHERE source_id=? ORDER BY end_ts DESC LIMIT 1", (source_id,)).fetchone()
        conn.execute("INSERT INTO engine_state VALUES(?,?,?,?,?,0,?,'',?)",
                     (source_id,row["id"],float(row["ts"]),ep[0] if ep else "",ses[0] if ses else "",ENGINE_VERSION,time.time()))
    return True


def current(conn: sqlite3.Connection, source_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM current_world_state WHERE source_id=?", (source_id,)).fetchone()
    if not row:
        return None
    now = time.time()
    return {
        "source_id": row["source_id"], "frame_id": row["frame_id"], "timestamp_unix": float(row["ts"]),
        "age_seconds": round(max(0, now-float(row["ts"])),2), "app": row["app"],
        "window_title": row["window_title"], "url": row["url"], "active_tab": row["active_tab"],
        "surface": row["surface"], "focus": _json(row["focus_json"],{}), "cursor": _json(row["cursor_json"],{}),
        "activity": row["activity"], "confidence": float(row["activity_confidence"]),
        "quality": _json(row["quality_json"],{}), "last_event_id": row["last_event_id"],
        "engine_version": ENGINE_VERSION,
    }


def catch_up(conn: sqlite3.Connection, source_id: str,
             event_builder: Callable[[sqlite3.Row, sqlite3.Row | None], list[dict[str, Any]]],
             limit: int = 2000) -> dict[str, Any]:
    """Advance only frames newer than the persisted cursor; never rebuild history."""
    state = conn.execute("SELECT * FROM engine_state WHERE source_id=?", (source_id,)).fetchone()
    if not state:
        initialized = initialize_latest(conn, source_id)
        return {"processed": 0, "initialized": initialized, "remaining": 0}
    last_ts = float(state["last_frame_ts"] or 0)
    last_id = str(state["last_frame_id"] or "")
    rows = conn.execute("""SELECT * FROM frames WHERE source_id=? AND
      (ts>? OR (ts=? AND id>?)) ORDER BY ts ASC,id ASC LIMIT ?""",
      (source_id, last_ts, last_ts, last_id, max(1, min(int(limit), 10000)))).fetchall()
    previous = conn.execute("SELECT * FROM frames WHERE id=?", (last_id,)).fetchone() if last_id else None
    for row in rows:
        advance(conn, row, previous, event_builder)
        previous = row
    remaining = conn.execute("""SELECT COUNT(*) FROM frames WHERE source_id=? AND
      (ts>? OR (ts=? AND id>?))""", (source_id,
      float(rows[-1]["ts"]) if rows else last_ts, float(rows[-1]["ts"]) if rows else last_ts,
      str(rows[-1]["id"]) if rows else last_id)).fetchone()[0]
    return {"processed": len(rows), "initialized": False, "remaining": int(remaining)}
