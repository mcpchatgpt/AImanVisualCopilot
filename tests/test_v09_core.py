import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
import sys

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import migrations
import state_engine
import vision_worker


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn


def create_state_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
      CREATE TABLE frames(id TEXT PRIMARY KEY,source_id TEXT,ts REAL,received_at REAL,app TEXT,window_title TEXT,
        url TEXT,active_tab TEXT,surface TEXT,focus_json TEXT,cursor_json TEXT,metadata_json TEXT,uia_json TEXT,
        dom_json TEXT,screenshot_path TEXT,screenshot_sha256 TEXT,changes_json TEXT);
      CREATE TABLE semantic_events(id TEXT PRIMARY KEY,source_id TEXT,frame_id TEXT,ts REAL,event_type TEXT,
        summary TEXT,app TEXT,window_title TEXT,url TEXT,confidence REAL,anomaly INTEGER,severity TEXT,
        detail_json TEXT,created_at REAL);
      CREATE TABLE memory_episodes(id TEXT PRIMARY KEY,source_id TEXT,start_ts REAL,end_ts REAL,title TEXT,
        summary TEXT,start_frame_id TEXT,end_frame_id TEXT,event_count INTEGER,confidence REAL,anomaly_count INTEGER,
        app_json TEXT,event_types_json TEXT,updated_at REAL);
      CREATE TABLE memory_sessions(id TEXT PRIMARY KEY,source_id TEXT,start_ts REAL,end_ts REAL,title TEXT,
        summary TEXT,episode_ids_json TEXT,episode_count INTEGER,anomaly_count INTEGER,updated_at REAL);
    """)
    migrations.apply_migrations(conn)


def add_frame(conn: sqlite3.Connection, fid: str, ts: float, title: str = "Editor") -> sqlite3.Row:
    conn.execute("""INSERT INTO frames VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
      (fid,"src",ts,ts,"code",title,"","", "desktop","{}","{}","{}","{}","{}",None,None,"[]"))
    return conn.execute("SELECT * FROM frames WHERE id=?",(fid,)).fetchone()


class MigrationTests(unittest.TestCase):
    def test_preflight_adds_legacy_source_columns_before_indexes(self):
        conn=connect()
        conn.executescript("""
          CREATE TABLE frames(id TEXT,received_at REAL,screenshot_sha256 TEXT);
          CREATE TABLE semantic_events(id TEXT,frame_id TEXT);
          CREATE TABLE vision_cache(frame_id TEXT,generated_at REAL);
        """)
        migrations.preflight_legacy_schema(conn)
        migrations.apply_migrations(conn)
        self.assertIn("source_id", {r[1] for r in conn.execute("PRAGMA table_info(vision_cache)")})
        self.assertEqual(9, conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0])
        conn.close()


class StateEngineTests(unittest.TestCase):
    def setUp(self):
        self.conn=connect(); create_state_schema(self.conn)

    def tearDown(self):
        self.conn.close()

    @staticmethod
    def events(row, previous):
        return [{"type":"window_changed","summary":row["window_title"],"confidence":.9,"detail":{}}]

    def test_incremental_world_state_and_catchup_are_idempotent(self):
        first=add_frame(self.conn,"f1",100,"First")
        state_engine.advance(self.conn,first,None,self.events)
        add_frame(self.conn,"f2",101,"Second")
        result=state_engine.catch_up(self.conn,"src",self.events)
        again=state_engine.catch_up(self.conn,"src",self.events)
        world=state_engine.current(self.conn,"src")
        self.assertEqual({"processed":1,"initialized":False,"remaining":0},result)
        self.assertEqual(0,again["processed"])
        self.assertEqual("f2",world["frame_id"])
        self.assertEqual("Second",world["activity"])
        self.assertEqual(2,self.conn.execute("SELECT COUNT(*) FROM semantic_events").fetchone()[0])

    def test_session_counts_every_anomaly_not_only_new_episode(self):
        def anomaly(row, previous):
            return [{"type":"anomaly","summary":"structured","confidence":.95,"detail":{"anomaly":True,"severity":"high"}}]
        first=add_frame(self.conn,"a1",100,"App"); state_engine.advance(self.conn,first,None,anomaly)
        second=add_frame(self.conn,"a2",101,"App"); state_engine.advance(self.conn,second,first,anomaly)
        self.assertEqual(2,self.conn.execute("SELECT anomaly_count FROM memory_sessions").fetchone()[0])


class VisionWorkerTests(unittest.TestCase):
    def test_worker_consumes_and_deduplicates(self):
        conn=connect()
        conn.executescript("""
          CREATE TABLE frames(id TEXT PRIMARY KEY,source_id TEXT,ts REAL,app TEXT,window_title TEXT,url TEXT,
            active_tab TEXT,focus_json TEXT,changes_json TEXT,screenshot_path TEXT,screenshot_sha256 TEXT);
          CREATE TABLE vision_candidates(frame_id TEXT PRIMARY KEY,source_id TEXT,frame_ts REAL,priority REAL,
            reason_json TEXT,status TEXT,created_at REAL,updated_at REAL);
          CREATE TABLE vision_cache(frame_id TEXT PRIMARY KEY,source_id TEXT,frame_ts REAL,app TEXT,window_title TEXT,
            url TEXT,description TEXT,page_type TEXT,objects_json TEXT,controls_json TEXT,entities_json TEXT,model TEXT,
            generated_at REAL);
          CREATE TABLE vision_budget(day TEXT PRIMARY KEY,processed INTEGER,duplicates INTEGER,failed INTEGER,updated_at REAL);
        """)
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/"screen.png"; Image.new("RGB",(64,48),(120,120,120)).save(path)
            for i in (1,2):
                conn.execute("INSERT INTO frames VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                  (f"f{i}","src",float(i),"chrome","Page","https://example.test","Page","{}","[]",str(path),"same"))
                conn.execute("INSERT INTO vision_candidates VALUES(?,?,?,?,?,'pending',0,0)",(f"f{i}","src",float(i),1.0,"[]"))
            result=vision_worker.run(conn,daily_limit=10,batch_limit=10,min_priority=.8)
        self.assertEqual(1,result["processed"])
        self.assertEqual(1,result["duplicates"])
        self.assertEqual(2,conn.execute("SELECT COUNT(*) FROM vision_cache").fetchone()[0])
        self.assertEqual(0,conn.execute("SELECT COUNT(*) FROM vision_candidates WHERE status='pending'").fetchone()[0])
        conn.close()


if __name__ == "__main__":
    unittest.main()
