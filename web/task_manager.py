"""Task 数据模型与 TaskManager 队列管理。"""
import json
import logging
import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import List, Optional

from sdk.runner_core import run_deletion_core
from web.log_handler import BufferHandler

# SDK 内部 delete_* 函数通过 Python logging 输出日志（INFO/ERROR）。
# Worker 在每个任务执行期间把该 logger 的输出桥接到 task 的 buffer。
_SDK_LOGGER_NAME = "sdk.delete_all_resources"


_REGION_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "region",
)


def _load_regions_for_env(env_name: str) -> dict:
    filename = "region_test.json" if env_name == "测试环境" else "region.json"
    with open(os.path.join(_REGION_DIR, filename), "r", encoding="utf-8") as f:
        data = json.load(f)
    # 按名称（区域名）升序返回，保证前端与执行顺序一致
    return dict(sorted(data.items()))


def _redact_public_key(pk: str) -> str:
    if not pk:
        return ""
    if len(pk) <= 4:
        return pk + "***"
    return pk[:4] + "***"


@dataclass
class Task:
    task_id: str
    state: str = "queued"
    submitted_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    account_id: str = ""
    account_name: str = ""
    env_name: str = "正式环境"
    api_url: str = ""
    project_ids: List[str] = field(default_factory=list)
    selected_regions: List[str] = field(default_factory=list)
    selected_resources: List[str] = field(default_factory=list)
    public_key: str = ""
    private_key: str = ""  # 进终态后由 TaskManager 立刻置空
    log_buffer: deque = field(default_factory=lambda: deque(maxlen=500))
    log_cond: threading.Condition = field(default_factory=threading.Condition)
    stop_event: threading.Event = field(default_factory=threading.Event)
    subscribers: int = 0

    def append_log(self, item: dict) -> None:
        with self.log_cond:
            self.log_buffer.append(item)
            self.log_cond.notify_all()

    # log_sink 接口别名
    def put(self, item: dict) -> None:
        self.append_log(item)

    def to_public_dict(self) -> dict:
        """脱敏视图：不含 private_key，public_key 截断。"""
        return {
            "task_id": self.task_id,
            "state": self.state,
            "submitted_at": self.submitted_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "account_id": self.account_id,
            "account_name": self.account_name,
            "env_name": self.env_name,
            "api_url": self.api_url,
            "project_ids": list(self.project_ids),
            "selected_regions": list(self.selected_regions),
            "selected_resources": list(self.selected_resources),
            "public_key": _redact_public_key(self.public_key),
        }


class TaskManager:
    """单 worker、全局串行队列的任务管理器。"""

    def __init__(self, autostart: bool = True, max_history: int = 20):
        self._pending: deque = deque()
        self._running: Optional[Task] = None
        self._history: deque = deque(maxlen=max_history)
        self._lock = threading.Lock()
        self._wake = threading.Condition(self._lock)
        self._worker: Optional[threading.Thread] = None
        if autostart:
            self.start()

    # 测试辅助（生产中不直接用）
    def _move_to_running(self, task: Task) -> None:
        with self._lock:
            if task in self._pending:
                self._pending.remove(task)
            self._running = task
            task.state = "running"
            task.started_at = time.time()

    def start(self) -> None:
        if self._worker and self._worker.is_alive():
            return
        # worker 的实现在 Task 8 添加，这里先 placeholder 防 None 启动
        self._worker = threading.Thread(target=self._loop, daemon=True)
        self._worker.start()

    def _loop(self) -> None:
        while True:
            with self._lock:
                while not self._pending:
                    self._wake.wait()
                task = self._pending.popleft()
                self._running = task
                task.state = "running"
                task.started_at = time.time()
            # 把 SDK 的 logging 输出桥接到 task.buffer（否则 delete_* 的 INFO/ERROR
            # 只会出现在后端控制台，前端日志面板看不到）
            sdk_logger = logging.getLogger(_SDK_LOGGER_NAME)
            sdk_logger.setLevel(logging.INFO)
            bridge = BufferHandler(task)
            sdk_logger.addHandler(bridge)
            try:
                regions = _load_regions_for_env(task.env_name)
                run_deletion_core(
                    regions=regions,
                    project_ids=task.project_ids,
                    public_key=task.public_key,
                    private_key=task.private_key,
                    api_url=task.api_url or None,
                    selected_regions=task.selected_regions,
                    selected_resources=task.selected_resources,
                    log_sink=task,
                    stop_event=task.stop_event,
                )
                terminal = "stopped" if task.stop_event.is_set() else "succeeded"
            except Exception as e:
                task.append_log({"level": "ERROR", "line": f"任务异常: {e}", "ts": time.time()})
                terminal = "failed"
            finally:
                # 移除桥接 handler，避免下个任务收到上个任务遗留的日志
                sdk_logger.removeHandler(bridge)
                with self._lock:
                    task.state = terminal
                    task.finished_at = time.time()
                    task.private_key = ""
                    self._history.append(task)
                    self._running = None
                # 终态后唤醒所有 SSE 订阅者
                with task.log_cond:
                    task.log_cond.notify_all()

    def submit(self, task: Task) -> int:
        """入队，返回任务前面还有多少个任务（含 running）。0 = 立即执行。"""
        with self._lock:
            self._pending.append(task)
            pos = (len(self._pending) - 1) + (1 if self._running else 0)
            self._wake.notify()
        return pos

    def stop(self, task_id: str) -> bool:
        with self._lock:
            # 排队中：直接出队
            for t in list(self._pending):
                if t.task_id == task_id:
                    self._pending.remove(t)
                    t.state = "stopped"
                    t.finished_at = time.time()
                    t.private_key = ""
                    self._history.append(t)
                    return True
            # 运行中：set stop_event，worker 自然收尾
            if self._running and self._running.task_id == task_id:
                self._running.stop_event.set()
                return True
        return False

    def get(self, task_id: str) -> Optional[Task]:
        with self._lock:
            if self._running and self._running.task_id == task_id:
                return self._running
            for t in self._pending:
                if t.task_id == task_id:
                    return t
            for t in self._history:
                if t.task_id == task_id:
                    return t
        return None

    def position_of(self, task: Task) -> int:
        """0 = 运行中或队首；其后递增。返回 -1 表示已结束/未知。"""
        with self._lock:
            if self._running is task:
                return 0
            for i, t in enumerate(self._pending):
                if t is task:
                    return i + (1 if self._running else 0)
        return -1

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "running": self._running.to_public_dict() if self._running else None,
                "pending": [t.to_public_dict() for t in self._pending],
                "history": [t.to_public_dict() for t in self._history],
            }
