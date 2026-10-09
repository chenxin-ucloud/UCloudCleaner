# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Status

**Web version is the only maintained entry point** (Flask + native HTML/CSS/JS). The legacy tkinter GUI and its macOS .app packaging have been removed.

## Commands

**Run Web version:**
```bash
python3 -m web.app
# Open browser at http://127.0.0.1:8080
```

**Build macOS .app of the Web version (Apple Silicon only):**
```bash
./build.sh
# Output: dist/UCloudCleanerWeb.app
```

**Run tests:**
```bash
pytest -v
```

No linter is configured.

## Architecture

The project has two layers: a shared deletion engine and a Flask-based Web UI.

### `sdk/delete_all_resources.py`
The core deletion engine. Key points:
- `get_client(region, project_id, public_key, private_key, base_url=None)` — constructs the UCloud `Client`; `base_url` is only set for the test environment (`http://api-test03.ucloudadmin.com`)
- `fetch_project_list(public_key, private_key, base_url=None)` — returns project list sorted by `CreateTime` ascending; format `[{"project_id": "...", "name": "...", "create_time": 123}, ...]`
- `DELETE_OPERATIONS` — module-level list of `(resource_name, delete_func)` tuples; Web filters this list based on user selection
- `logger` has **no handlers at module level** — Web attaches `BufferHandler`; CLI callers call `setup_file_logging()`
- Deletion order matters: UHost → UDisk (15-second wait after UHost to let disks detach) → NATGW → ... → VPC

### `sdk/runner_core.py`
Scheduling loop shared by the Web worker:
- `run_deletion_core(..., log_sink, stop_event)` — duck-typed `log_sink` (any object with `put(dict)`)
- `resolve_api_url(env_name, explicit)` — auto-maps "测试环境" + empty URL to `http://api-test03.ucloudadmin.com`

### `web/` — Flask + native HTML/CSS/JS
Entry point: `web/app.py` → `create_app()`.

**Routes:**
- `GET /api/regions?env=prod|test` — loads `region/region.json` or `region_test.json`
- `GET /api/resources` — returns `[name for name, _ in DELETE_OPERATIONS]`
- `POST /api/projects` — calls `fetch_project_list()`, returns ordered list
- `POST /api/tasks` — validates payload, creates `Task`, submits to `TaskManager`
- `GET /api/tasks/<id>/stream` — SSE stream with state + log replay + incremental logs + heartbeat

**Task queue:** `web/task_manager.py` — single worker thread, FIFO serial execution.
- `TaskManager.submit(task)` → returns queue position (0 = immediate)
- `TaskManager.stop(task_id)` — queued tasks removed; running tasks get `stop_event.set()`
- Worker bridges SDK logger (`sdk.delete_all_resources`) to task buffer via `BufferHandler`
- Private key erased in `finally` block after task completion

**SSE protocol:** `web/sse.py` — 4 event types: `state`, `log`, `heartbeat`, `end`
- Multi-subscriber via `threading.Condition`
- Log replay: first 500 buffered lines sent on connection
- Heartbeat: every 15s to prevent proxy timeout

**Frontend:** `web/static/index.html` + `app.js` + `style.css`
- Two-column layout: left config panel, right runtime panel (log + running + pending + history)
- `localStorage` persists: env, public_key, selected projects/regions/resources (never private_key)
- Collapsible log panel, auto-scroll toggle, clear button
- Select-all / select-none buttons for projects, regions, and resources

### `region/`
- `region.json` — production regions
- `region_test.json` — test regions

Format: `{"区域名": {"Region": "cn-bj2", "Zone": "cn-bj2-02"}}`

Web (`web/task_manager.py`) reads from these files. The CLI (`sdk/delete_all_resources.py`) also reads from `../region/region.json`.

### PyInstaller packaging
`UCloudCleaner.spec` bundles `region/*.json`, `sdk/*.py`, `web/`, `web_launcher.py`. Target: Apple Silicon Mac, macOS 11.0+. Build: `./build.sh` → `dist/UCloudCleaner.app`.

### Config files
- `web/config.json` — optional; keys: `host` (default `"127.0.0.1"`), `port` (default `8080`)
- `web/config.json.example` — template
