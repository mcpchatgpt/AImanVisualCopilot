#!/usr/bin/env python3
"""Consume high-value visual-memory candidates under a UTC daily budget."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
import main
import vision_worker

with main.db() as conn:
    result = vision_worker.run(
        conn,
        daily_limit=int(os.environ.get('AVC_VISION_DAILY_LIMIT', '120')),
        batch_limit=int(os.environ.get('AVC_VISION_BATCH_LIMIT', '8')),
        min_priority=float(os.environ.get('AVC_VISION_MIN_PRIORITY', '0.88')),
    )
print(result)
