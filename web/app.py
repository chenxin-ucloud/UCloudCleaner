"""Flask 入口：路由 + SSE。"""
import os
import uuid
from typing import Optional, Tuple

from flask import Flask, Response, jsonify, request, send_from_directory

from sdk.delete_all_resources import DELETE_OPERATIONS, fetch_project_list
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

    @app.route("/api/regions")
    def get_regions():
        env = request.args.get("env", "prod")
        env_name = "测试环境" if env == "test" else "正式环境"
        try:
            return jsonify(_load_regions_for_env(env_name))
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
        public_key = payload.get("public_key", "").strip()
        private_key = payload.get("private_key", "").strip()
        env_name = payload.get("env_name", "正式环境")
        if not public_key:
            return _err("INVALID_PARAM", 400, "public_key 不能为空")
        if not private_key:
            return _err("INVALID_PARAM", 400, "private_key 不能为空")
        from sdk.runner_core import resolve_api_url
        base_url = resolve_api_url(env_name, "")
        projects = fetch_project_list(public_key, private_key, base_url=base_url or None)
        return jsonify(projects=projects)

    VALID_ENVS = {"正式环境", "测试环境"}
    VALID_RESOURCE_NAMES = {name for name, _ in DELETE_OPERATIONS}

    def _validate_payload(payload: dict) -> Optional[str]:
        if not isinstance(payload, dict):
            return "请求体必须是 JSON 对象"
        if not isinstance(payload.get("public_key"), str) or not payload["public_key"].strip():
            return "public_key 不能为空"
        if not isinstance(payload.get("private_key"), str) or not payload["private_key"].strip():
            return "private_key 不能为空"
        pids = payload.get("project_ids")
        if not isinstance(pids, list) or not pids or not all(isinstance(p, str) and p.strip() for p in pids):
            return "project_ids 必须是非空字符串数组"
        env = payload.get("env_name")
        if env not in VALID_ENVS:
            return f"env_name 必须是 {VALID_ENVS} 之一"
        regions = payload.get("selected_regions")
        if not isinstance(regions, list) or not regions:
            return "selected_regions 不能为空"
        resources = payload.get("selected_resources")
        if not isinstance(resources, list) or not resources:
            return "selected_resources 不能为空"
        # 区域必须是合法 key
        try:
            valid_regions = set(_load_regions_for_env(env).keys())
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
        from sdk.runner_core import resolve_api_url
        api_url = resolve_api_url(payload["env_name"], "")
        task = Task(
            task_id=uuid.uuid4().hex[:8],
            env_name=payload["env_name"],
            api_url=api_url,
            public_key=payload["public_key"],
            private_key=payload["private_key"],
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
