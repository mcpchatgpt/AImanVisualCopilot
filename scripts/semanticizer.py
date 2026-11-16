#!/usr/bin/env python3
"""Catch up or explicitly reseed AVC's derived semantic state."""
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
import main

reseed = '--reseed' in sys.argv
with main.db() as conn:
    sources = conn.execute('SELECT id,label FROM sources ORDER BY last_seen DESC').fetchall()
for src in sources:
    try:
        with main.db() as conn:
            if reseed:
                # Rebuild the trusted semantic layer from all raw Frames still inside
                # the configured raw-observation retention window.
                since = time.time() - main.RETENTION_HOURS * 3600
                result = main.state_engine.reseed_recent(conn, src['id'], main._human_event, since)
                result['mode'] = 'reseed'
            else:
                result = main.state_engine.catch_up(conn, src['id'], main._human_event, 2000)
                result['mode'] = 'catch_up'
        result['causal_link_count'] = main.rebuild_causal_links(src['id'], 6)
        print(src['label'], result)
    except Exception as exc:
        print(src['label'], 'ERROR', exc, file=sys.stderr)
