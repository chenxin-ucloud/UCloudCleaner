"""Flask 入口：路由 + SSE。"""
import os
import uuid
from typing import Optional, Tuple

from flask import Flask, Response, jsonify, request, send_from_directory

from sdk.delete_all_resources import DELETE_OPERATIONS, fetch_project_list
from sdk.runner_core import resolve_env_name
from web import accounts
from web.sse import stream_events
from web.task_manager import Task, TaskManager, _load_regions_for_env


def _err(code: str, http: int, message: Optional[str] = None) -> Tuple[Response, int]:
    return jsonify(error=message or code, code=code), http


def create_app(testing: bool = False) -> Tuple[Flask, TaskManager]:
    app = Flask(
        __name__,
        static_folder=os.path.join(os.path.dirname(__file__), "static"),
        static_url_path="",
    )
    manager = TaskManager(autostart=not testing)

    @app.route("/")
    def index():
        return send_from_directory(app.static_folder, "index.html")

    @app.route("/api/accounts", methods=["GET"])
    def get_accounts():
        return jsonify(accounts=accounts.list_accounts())

    @app.route("/api/accounts", methods=["POST"])
    def save_account():
        payload = request.get_json(silent=True) or {}
        try:
            acct = accounts.upsert_account(payload)
        except ValueError as e:
            return _err("INVALID_PARAM", 400, str(e))
        return jsonify(account=acct)

    @app.route("/api/accounts/<name>", methods=["DELETE"])
    def remove_account(name):
        if not accounts.delete_account(name):
            return _err("ACCOUNT_NOT_FOUND", 404)
        return jsonify(ok=True)

    @app.route("/api/regions")
    def get_regions():
        account_name = (request.args.get("account") or "").strip()
        if not account_name:
            return jsonify({})
        acct = accounts.get_account(account_name)
        if not acct:
            return _err("ACCOUNT_NOT_FOUND", 404)
        env_name = resolve_env_name(acct["base_url"])
        try:
            regions = _load_regions_for_env(
                env_name, acct["public_key"], acct["private_key"], acct["base_url"] or None
            )
            return jsonify(regions)
        except FileNotFoundError as e:
            return _err("INTERNAL", 500, str(e))

    @app.route("/api/resources")
    def get_resources():
        return jsonify([name for name, _ in DELETE_OPERATIONS])

    @app.route("/api/snapshot")
    def get_snapshot():
        return jsonify(manager.snapshot())

    @app.route("/api/projects", methods=["POST"])
    def get_projects():
        payload = request.get_json(silent=True) or {}
        account_name = (payload.get("account") or "").strip()
        if not account_name:
            return _err("INVALID_PARAM", 400, "account 不能为空")
        acct = accounts.get_account(account_name)
        if not acct:
            return _err("ACCOUNT_NOT_FOUND", 404)
        projects = fetch_project_list(
            acct["public_key"], acct["private_key"], base_url=acct["base_url"] or None
        )
        return jsonify(projects=projects)

    VALID_RESOURCE_NAMES = {name for name, _ in DELETE_OPERATIONS}

    def _resolve_account(payload: dict):
        account_name = (payload.get("account") or "").strip()
        if not account_name:
            return None, "account 不能为空"
        acct = accounts.get_account(account_name)
        if not acct:
            return None, "账号不存在"
        return acct, None

    def _validate_payload(payload: dict) -> Optional[str]:
        if not isinstance(payload, dict):
            return "请求体必须是 JSON 对象"
        acct, err = _resolve_account(payload)
        if err:
            return err
        pids = payload.get("project_ids")
        if not isinstance(pids, list) or not pids or not all(isinstance(p, str) and p.strip() for p in pids):
            return "project_ids 必须是非空字符串数组"
        regions = payload.get("selected_regions")
        if not isinstance(regions, list) or not regions:
            return "selected_regions 不能为空"
        resources = payload.get("selected_resources")
        if not isinstance(resources, list) or not resources:
            return "selected_resources 不能为空"
        # 区域必须是该账号对应环境下的合法 key
        env_name = resolve_env_name(acct["base_url"])
        try:
            valid_regions = set(_load_regions_for_env(
                env_name, acct["public_key"], acct["private_key"], acct["base_url"] or None
            ).keys())
        except FileNotFoundError as e:
            return f"区域文件加载失败: {e}"
        invalid_r = set(regions) - valid_regions
        if invalid_r:
            return f"未知区域: {sorted(invalid_r)}"
        # 资源名必须合法
        invalid_res = set(resources) - VALID_RESOURCE_NAMES
        if invalid_res:
            return f"未知资源类型: {sorted(invalid_res)}"
        return None

    @app.route("/api/tasks", methods=["POST"])
    def submit_task():
        payload = request.get_json(silent=True) or {}
        err = _validate_payload(payload)
        if err:
            return _err("INVALID_PARAM", 400, err)
        acct = accounts.get_account((payload.get("account") or "").strip())
        task = Task(
            task_id=uuid.uuid4().hex[:8],
            account_name=acct["name"],
            env_name=resolve_env_name(acct["base_url"]),
            api_url=acct["base_url"],
            public_key=acct["public_key"],
            private_key=acct["private_key"],
            project_ids=[p.strip() for p in payload["project_ids"]],
            selected_regions=list(payload["selected_regions"]),
            selected_resources=list(payload["selected_resources"]),
        )
        pos = manager.submit(task)
        return jsonify(task_id=task.task_id, position=pos)

    @app.route("/api/tasks/<task_id>")
    def get_task(task_id):
        t = manager.get(task_id)
        if not t:
            return _err("TASK_NOT_FOUND", 404)
        return jsonify(t.to_public_dict())

    @app.route("/api/tasks/<task_id>/stop", methods=["POST"])
    def stop_task(task_id):
        t = manager.get(task_id)
        if not t:
            return _err("TASK_NOT_FOUND", 404)
        if t.state in {"succeeded", "failed", "stopped"}:
            return _err("TASK_FINISHED", 409, "任务已结束")
        ok = manager.stop(task_id)
        return jsonify(ok=ok)

    @app.route("/api/tasks/<task_id>/stream")
    def stream_task(task_id):
        t = manager.get(task_id)
        if not t:
            return _err("TASK_NOT_FOUND", 404)
        resp = Response(stream_events(t), mimetype="text/event-stream")
        resp.headers["Cache-Control"] = "no-cache"
        resp.headers["X-Accel-Buffering"] = "no"
        return resp

    return app, manager


def _load_config() -> dict:
    cfg_path = os.path.join(os.path.dirname(__file__), "config.json")
    if not os.path.exists(cfg_path):
        return {"host": "127.0.0.1", "port": 5000}
    import json
    with open(cfg_path, "r", encoding="utf-8") as f:
        return json.load(f)


if __name__ == "__main__":
    cfg = _load_config()
    app, _ = create_app(testing=False)
    host = cfg.get("host", "127.0.0.1")
    port = int(cfg.get("port", 5000))
    print(f"UCloud Cleaner 已启动")
    print(f"访问地址: http://{host}:{port}")
    print(f"按 Ctrl+C 退出")
    app.run(host=host, port=port, threaded=True, use_reloader=False)
