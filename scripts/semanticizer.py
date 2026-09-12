#!/opt/aiman-visual-copilot/venv/bin/python
import sys
from pathlib import Path
sys.path.insert(0, '/opt/aiman-visual-copilot/app')
import main

with main.db() as conn:
    sources = conn.execute('SELECT id,label FROM sources ORDER BY last_seen DESC').fetchall()
for src in sources:
    try:
        result = main.rebuild_semantic_memory(src['id'], 6)
        print(src['label'], result)
    except Exception as exc:
        print(src['label'], 'ERROR', exc, file=sys.stderr)
