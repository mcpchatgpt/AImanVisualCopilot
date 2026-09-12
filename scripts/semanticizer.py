#!/usr/bin/env python3
"""Catch up the incremental state cursor and refresh evidence-based causal links."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
import main

with main.db() as conn:
    sources = conn.execute('SELECT id,label FROM sources ORDER BY last_seen DESC').fetchall()
for src in sources:
    try:
        with main.db() as conn:
            result = main.state_engine.catch_up(conn, src['id'], main._human_event, 2000)
        result['causal_link_count'] = main.rebuild_causal_links(src['id'], 6)
        print(src['label'], result)
    except Exception as exc:
        print(src['label'], 'ERROR', exc, file=sys.stderr)
