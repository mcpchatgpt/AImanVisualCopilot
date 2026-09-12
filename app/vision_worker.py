"""Budgeted, de-duplicating visual-memory worker.

The default local backend creates conservative image-backed summaries without sending
screenshots to a third party. A model backend can replace summarize_local later.
"""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image, ImageStat


def _json(raw: str | None, default: Any) -> Any:
    try:
        return json.loads(raw) if raw else default
    except Exception:
        return default


def summarize_local(frame: sqlite3.Row) -> tuple[str, str, list[str], list[str], list[str]]:
    path = Path(str(frame["screenshot_path"] or ""))
    if not path.exists():
        raise FileNotFoundError("screenshot expired")
    with Image.open(path) as image:
        rgb = image.convert("RGB")
        thumb = rgb.copy(); thumb.thumbnail((160, 100))
        stat = ImageStat.Stat(thumb)
        brightness = round(sum(stat.mean)/3)
        visual = "dark" if brightness < 70 else "bright" if brightness > 185 else "balanced"
        dimensions = f"{rgb.width}×{rgb.height}"
        thumb.close(); rgb.close()
    focus = _json(frame["focus_json"], {})
    changes = _json(frame["changes_json"], [])
    title = str(frame["active_tab"] or frame["window_title"] or frame["app"] or "screen")
    description = f"{title}; {dimensions} {visual} interface"
    controls = [str(focus.get("name") or "")] if focus.get("name") else []
    entities = [str(frame["app"] or ""), title]
    objects = [str(x.get("type")) for x in changes if isinstance(x,dict) and x.get("type")][:12]
    page_type = "browser" if frame["url"] else "desktop"
    return description[:1200], page_type, objects, controls, [x for x in entities if x]


def run(conn: sqlite3.Connection, daily_limit: int = 120, batch_limit: int = 8,
        min_priority: float = .88) -> dict[str, int]:
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    now = time.time()
    conn.execute("INSERT OR IGNORE INTO vision_budget(day,processed,duplicates,failed,updated_at) VALUES(?,0,0,0,?)", (day,now))
    budget = conn.execute("SELECT * FROM vision_budget WHERE day=?", (day,)).fetchone()
    remaining = max(0, int(daily_limit)-int(budget["processed"])-int(budget["duplicates"])-int(budget["failed"]))
    limit = min(max(0,int(batch_limit)), remaining)
    result = {"processed":0,"duplicates":0,"failed":0,"remaining":remaining}
    if limit <= 0:
        return result
    rows = conn.execute("""SELECT f.*,vc.priority AS candidate_priority,vc.reason_json AS candidate_reason_json
      FROM vision_candidates vc JOIN frames f ON f.id=vc.frame_id
      WHERE vc.status='pending' AND vc.priority>=? AND f.screenshot_path IS NOT NULL
      ORDER BY vc.priority DESC,vc.frame_ts DESC LIMIT ?""", (float(min_priority), limit)).fetchall()
    for frame in rows:
        try:
            duplicate = None
            if frame["screenshot_sha256"]:
                duplicate = conn.execute("""SELECT v.* FROM vision_cache v JOIN frames f ON f.id=v.frame_id
                  WHERE f.screenshot_sha256=? AND v.frame_id!=? ORDER BY v.generated_at DESC LIMIT 1""",
                  (frame["screenshot_sha256"],frame["id"])).fetchone()
            if duplicate:
                values=(frame["id"],frame["source_id"],frame["ts"],frame["app"],frame["window_title"],frame["url"],
                        duplicate["description"],duplicate["page_type"],duplicate["objects_json"],duplicate["controls_json"],
                        duplicate["entities_json"],"dedupe:"+str(duplicate["model"]),now)
                result["duplicates"] += 1
            else:
                desc,page,objects,controls,entities=summarize_local(frame)
                values=(frame["id"],frame["source_id"],frame["ts"],frame["app"],frame["window_title"],frame["url"],
                        desc,page,json.dumps(objects,ensure_ascii=False),json.dumps(controls,ensure_ascii=False),
                        json.dumps(entities,ensure_ascii=False),"local-visual-memory-v1",now)
                result["processed"] += 1
            conn.execute("""INSERT OR REPLACE INTO vision_cache(frame_id,source_id,frame_ts,app,window_title,url,description,
              page_type,objects_json,controls_json,entities_json,model,generated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""", values)
            conn.execute("UPDATE vision_candidates SET status='completed',updated_at=? WHERE frame_id=?",(now,frame["id"]))
        except Exception as exc:
            result["failed"] += 1
            conn.execute("UPDATE vision_candidates SET status='failed',reason_json=?,updated_at=? WHERE frame_id=?",
                         (json.dumps({"error":str(exc)},ensure_ascii=False)[:4000],now,frame["id"]))
    conn.execute("""UPDATE vision_budget SET processed=processed+?,duplicates=duplicates+?,failed=failed+?,updated_at=? WHERE day=?""",
                 (result["processed"],result["duplicates"],result["failed"],now,day))
    result["remaining"] = max(0, remaining-len(rows))
    return result
