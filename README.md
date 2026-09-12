# AImanVisualCopilot (AVC) 0.8.0

AVC is a read-only Visual Context Engine. It observes, understands, compresses and reconstructs Windows/browser context; it deliberately exposes no mouse, keyboard or shell-control MCP tools. Human operation remains primary; AIman can provide optional action capability separately.

## Architecture

```text
Windows Observer 0.2.0                       Chrome / Edge
UIA + screenshot + focus + cursor                 |
perceptual dHash + window/text changes       Browser Bridge 0.2.0
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
                           Semantic Frame                         JS/HTTP/network/perf
                                  |                                     |
                           Scene Graph 2.0 <-----------------------------+
                                  |
              +-------------------+-------------------+
              |                   |                   |
          Keyframes         Semantic Events       Visual Change Region
              |                   |                   |
      24h screenshot        Episode -> Session     before/after bbox
              |                   |
        Vision Candidate     Current Activity
              |                   |
        Vision Cache          Causal Hints / Anomalies
              +-------------------+
                        |
                    MCP / GPT
```

## Observation sources

### Windows Observer 0.2.0
Captures active application/window, bounded UIA structure, visible text, screenshot, focused element, cursor position, perceptual visual hash and typed UI changes. It runs hidden through Task Scheduler and remains independent of terminal windows.

### Browser Bridge 0.2.0
Read-only extension for the real active/focused Chrome/Edge tab. Captures URL, title, visible text, compact DOM controls/headings/landmarks, focus, viewport/scroll state and semantic mutations. Sensitive password/OTP/card-like values are redacted. Browser/Windows fusion requires both temporal proximity and title similarity to avoid cross-profile misfusion.

## Dev Observer

For localhost/private-development pages, Browser Bridge 0.2.0 can record:

- JavaScript errors and stack summaries
- unhandled Promise rejections
- failed resources
- HTTP 4xx/5xx responses
- browser network failures
- main-frame navigation
- Navigation Timing/page-load performance
- repeated reload/navigation-loop anomalies

`avc_dev_context()` reads these diagnostics. Medium/high/critical diagnostics are also promoted into Semantic Memory/Anomalies and can promote the nearest Windows Frame to a 24-hour persistent keyframe.

## Semantic Frame and Scene Graph 2.0

A Semantic Frame fuses Windows and browser evidence. `avc_scene_graph()` exposes unified UI objects. Every scene object has a per-frame `object_id`, cross-frame `stable_id`, `state_hash`, role/name/value, source confidence, focus/enabled state, DOM selector/href/id, UIA automation id and available viewport/screen bounds. `avc_scene_diff()` compares stable objects across two Frames and returns added/removed/state-changed UI objects.

## Memory hierarchy

```text
Raw Frame -> Semantic Event -> Episode -> Session
```

The Timeline Semanticizer runs periodically on USA-Control, reducing raw observation volume into human-readable context. `avc_current_activity()` provides lightweight current-task inference. `avc_causal_timeline()` exposes timing/focus-based causal hints; these are contextual evidence, not proof of a physical click.

## Visual memory and retention

- Semantic Frames: 24 hours
- ordinary screenshots: 6 hours
- persistent keyframe screenshots: 24 hours
- Browser Snapshots: 24 hours
- Event Timeline: 7 days
- Dev Events: 7 days by default
- compressed semantic memory: 30 days by default

Frame importance is scored automatically. Navigation, app/window/tab changes, dialogs, strong visual changes, error states and development diagnostics can promote a Frame to a persistent keyframe. Capacity pressure evicts ordinary/low-importance screenshots before keyframes.

`vision_candidates` is an automatic queue of important Frames that deserve AI interpretation. AVC does not fabricate visual summaries server-side: GPT/model must actually view a retained image and then call `avc_store_vision_cache(...)`; the resulting Vision Cache can survive screenshot expiration.

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

AVC 0.8.0 exposes 32 tools. New 0.8 tools are:

- `avc_dev_context`
- `avc_vision_candidates`
- `avc_scene_diff`

All 0.7 tools remain compatible.

## Monitoring and privacy

Monitor Console can disable all content observation. When Monitoring is Off, Windows screenshot/UIA/Frame capture and Browser DOM/Dev diagnostics stop; only content-free control polling remains so monitoring can later be restored. Dev Observer also has an independent enable/disable switch.

Dashboard, MCP, Windows source, Browser Bridge and enrollment credentials are separate. AVC private Nginx paths have access logging disabled and Uvicorn access logging is disabled so path tokens are not written to normal access logs.

## Storage guard

- screenshots: 500 MiB hard cap
- database: 200 MiB hard cap
- AVC data directory: 1 GiB cap
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
