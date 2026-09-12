# AImanVisualCopilot (AVC) 0.9.0

AVC is a read-only Visual Context Engine. It observes, understands, compresses and reconstructs Windows/browser context; it deliberately exposes no mouse, keyboard or shell-control MCP tools. Human operation remains primary; AIman can provide optional action capability separately.

## Architecture

```text
Windows Observer 0.3.0                       Chrome / Edge
UIA + screenshot + focus + cursor                 |
perceptual dHash + input edges               Browser Bridge 0.3.0
             |                               DOM + URL + focus + viewport
             |                               + Dev Observer diagnostics
             +--------------- HTTPS ----------------+
                                                     |
                                                USA-Control
                                                     |
                                  +------------------+------------------+
                                  |                                     |
                           Semantic Fusion                       Dev Event Ingest
                                  |                                     |
                       Incremental State Engine                   JS/HTTP/network/perf
                                  |                                     |
                     Current World State + Scene Graph 2.1 <-------------+
                                  |
              +-------------------+-------------------+
              |                   |                   |
          Keyframes         Semantic Events       Visual Change Region
              |                   |                   |
      24h screenshot        Episode -> Session     before/after bbox
              |                   |
        Vision Candidate     Current Activity
              |                   |
     Budgeted Worker          Evidence Causality / Anomalies
              +-------------------+
                        |
                    MCP / GPT
```

## Observation sources

### Windows Observer 0.3.0
Captures active application/window, bounded UIA structure, visible text, screenshot, focused element, cursor position, perceptual visual hash, and read-only mouse/key edge events. It never injects input.

### Browser Bridge 0.3.0
Read-only extension for the real active/focused Chrome/Edge tab. It adds pointer, click, named-key and submit evidence plus DPI, browser-window and scroll geometry. Character values are never recorded; only the label `character` is retained.

## Dev Observer

For localhost/private-development pages, Browser Bridge can record:

- JavaScript errors and stack summaries
- unhandled Promise rejections
- failed resources
- HTTP 4xx/5xx responses
- browser network failures
- main-frame navigation
- Navigation Timing/page-load performance
- repeated reload/navigation-loop anomalies

`avc_dev_context()` reads these diagnostics. Medium/high/critical diagnostics are also promoted into Semantic Memory/Anomalies and can promote the nearest Windows Frame to a 24-hour persistent keyframe.

## Incremental state and Scene Graph 2.1

Every accepted Frame advances semantic memory once in the same SQLite transaction. Query tools never delete or rebuild history. `current_world_state` is maintained separately from Episodes/Sessions and exposes freshness and evidence quality.

Scene Graph 2.1 calibrates DOM CSS pixels against Windows UIA coordinates using DPR, browser content origin, toolbar residual and scroll offsets. Duplicate names are resolved by compatible role and nearest calibrated position.

## Memory hierarchy

```text
Raw Frame -> Semantic Event -> Episode -> Session
```

The lightweight semanticizer only catches up a persisted cursor. `avc_world_state()` reads the exact latest state. Causal links require an observed click, pointer, key or submit event followed by a navigation/UI/dev effect; focus proximity alone is not accepted.

## Visual memory and retention

- Semantic Frames: 24 hours
- ordinary screenshots: 6 hours
- persistent keyframe screenshots: 24 hours
- Browser Snapshots: 24 hours
- Event Timeline: 7 days
- Dev Events: 7 days by default
- compressed semantic memory: 30 days by default

Frame importance is scored automatically. Navigation, app/window/tab changes, dialogs, strong visual changes, error states and development diagnostics can promote a Frame to a persistent keyframe. Capacity pressure evicts ordinary/low-importance screenshots before keyframes.

`vision_candidates` is consumed every five minutes by a bounded worker. It processes only high-priority frames, deduplicates by screenshot SHA-256, stores short image-backed summaries, and enforces daily and per-run budgets. Vision Cache stays searchable after screenshots expire.

## Visual retrieval and comparison

- `avc_frame_image(frame_id)` — exact retained historical screenshot.
- `avc_visual_timeline(...)` — selected key visual frames.
- `avc_clip(...)` — keyframes from an explicit interval.
- `avc_timepoint(...)` — visual reconstruction around an absolute time.
- `avc_frame_replay(...)` — seconds before/after a known Frame.
- `avc_frame_neighbors(...)` — neighboring Frames.
- `avc_compare_frames(...)` — semantic/text/focus/nav diff plus real screenshot change bbox and diff crop.
- persistent keyframes can receive automatic `visual_change_json` and `visual_region_changed` timeline events.

## Search

`avc_search_timeline()` searches Frames, visible text, UIA, DOM, Browser Snapshots, Dev Events, Semantic Events, Episodes and Vision Cache. `avc_search_text()` and `avc_search_vision()` remain focused alternatives.

## MCP

AVC 0.9.0 adds explicit tools for:

- `avc_world_state`
- `avc_vision_worker_status`

All 0.7 tools remain compatible.

## Monitoring and privacy

Monitor Console can disable all content observation. When Monitoring is Off, Windows screenshot/UIA/Frame capture and Browser DOM/Dev diagnostics stop; only content-free control polling remains so monitoring can later be restored. Dev Observer also has an independent enable/disable switch.

Dashboard, MCP, Windows source, Browser Bridge and enrollment credentials are separate. AVC private Nginx paths have access logging disabled and Uvicorn access logging is disabled so path tokens are not written to normal access logs.

## Storage guard

- screenshots: 6 GiB hard cap
- database: 4 GiB hard cap
- AVC data directory: 11 GiB cap
- free disk <20 GiB: semantic-only mode
- free disk <10 GiB: minimal mode

Under capacity pressure, disposable visual/raw data is removed before durable high-value semantic memory. Vision Cache remains last-resort data.

## Deployment paths

- project: `/opt/aiman-visual-copilot`
- server: `/opt/aiman-visual-copilot/app/main.py`
- browser extension: `/opt/aiman-visual-copilot/browser-extension`
- Windows observer source: `/opt/aiman-visual-copilot/windows/observer.ps1`
- data: `/var/lib/aiman-visual-copilot`
- screenshots: `/var/lib/aiman-visual-copilot/screenshots`
- SQLite: `/var/lib/aiman-visual-copilot/avc.db`
- environment: `/etc/aiman-visual-copilot.env`
- service: `aiman-visual-copilot.service`
- semanticizer timer: `aiman-visual-copilot-semanticizer.timer`
- visual-memory timer: `aiman-visual-copilot-vision.timer`

## Database upgrades

Startup runs idempotent migrations. A preflight phase adds legacy columns before creating dependent indexes, preventing the earlier `source_id` migration crash. Migration 10 also removes only the untrustworthy 0.8 derived keyword-anomaly and focus-causality rows while preserving raw observations. Applied versions are recorded in `schema_migrations`.
