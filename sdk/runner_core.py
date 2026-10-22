"""共享调度模块：GUI 和 Web 都用此函数执行批量删除。

不依赖 tkinter；通过鸭子类型 log_sink（带 put 方法的对象）输出结构化日志。
"""
import time
from typing import Iterable, List, Optional

from sdk.delete_all_resources import DELETE_OPERATIONS, get_client


TEST_API_URL = "http://api-test03.ucloudadmin.com"


def resolve_api_url(env_name: str, explicit: str) -> str:
    """根据环境名解析 base_url：用户显式指定优先，测试环境默认走测试 endpoint。"""
    if explicit and explicit.strip():
        return explicit.strip()
    if env_name == "测试环境":
        return TEST_API_URL
    return ""


def resolve_env_name(base_url: str) -> str:
    """根据环境地址判断环境名：含 test → 测试环境，否则正式环境。"""
    return "测试环境" if "test" in (base_url or "").lower() else "正式环境"


def _log(sink, level: str, line: str):
    sink.put({"level": level, "line": line, "ts": time.time()})


def run_deletion_core(
    *,
    regions: dict,
    project_ids: List[str],
    public_key: str,
    private_key: str,
    api_url: Optional[str],
    selected_regions: Iterable[str],
    selected_resources: Iterable[str],
    log_sink,
    stop_event,
):
    """按 region → project → resource 三层循环执行删除。

    Args:
        regions: 完整区域 dict（{区域名: {Region, Zone}}，来源可为 region 文件或 SDK GetRegion）
        project_ids: 项目 ID 列表
        public_key/private_key: UCloud API 凭证
        api_url: 自定义 base_url，None 表示用 SDK 默认
        selected_regions: 用户勾选的区域名（regions 的 key 子集）
        selected_resources: 用户勾选的资源名（DELETE_OPERATIONS 名字子集）
        log_sink: 任何带 put(dict) 方法的对象（queue.Queue 或 Task）
        stop_event: threading.Event；置位后尽快退出
    """
    selected_regions = set(selected_regions)
    selected_resources = set(selected_resources)
    filtered_regions = {k: v for k, v in regions.items() if k in selected_regions}
    selected_ops = [op for op in DELETE_OPERATIONS if op[0] in selected_resources]
    base_url = api_url.strip() if api_url and api_url.strip() else None

    _log(log_sink, "INFO", "=" * 60)
    _log(log_sink, "INFO", "开始批量删除资源")
    _log(log_sink, "INFO", "=" * 60)

    for loc_name, loc in filtered_regions.items():
        if stop_event.is_set():
            _log(log_sink, "INFO", "用户已停止操作")
            break
        region = loc.get("Region")
        zone = loc.get("Zone")
        _log(log_sink, "INFO", f"处理区域: {loc_name} ({region})")

        for project_id in project_ids:
            if stop_event.is_set():
                break
            client = get_client(region, project_id, public_key, private_key, base_url)
            for resource_name, delete_func in selected_ops:
                if stop_event.is_set():
                    break
                try:
                    delete_func(client, loc_name, region, zone, project_id, stop_event)
                except Exception as e:
                    _log(log_sink, "ERROR",
                         f"[{loc_name}] 处理 {resource_name} 时发生错误: {e}")

    _log(log_sink, "INFO", "=" * 60)
    _log(log_sink, "INFO", "批量删除资源完成")
    _log(log_sink, "INFO", "=" * 60)
