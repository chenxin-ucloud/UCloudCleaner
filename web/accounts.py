"""账号配置持久化：账号 ID（uuid4 主键）、名称、环境地址、公私钥。存用户数据目录。"""
import json
import os
import sys
import threading
import uuid


def _data_dir() -> str:
    """返回用户可写的配置目录（macOS 用 ~/Library/Application Support）。"""
    if sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    elif os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "UCloudCleaner")


ACCOUNTS_FILE = os.path.join(_data_dir(), "accounts.json")

# 旧版本把账号存在源码/包内（__file__ 同级 web/accounts.json）
_LEGACY_ACCOUNTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "accounts.json")

_lock = threading.Lock()


def _migrate_legacy() -> None:
    """一次性迁移：旧路径有账号而新路径不存在时，复制过去。"""
    if os.path.exists(ACCOUNTS_FILE):
        return
    if _LEGACY_ACCOUNTS_FILE == ACCOUNTS_FILE or not os.path.exists(_LEGACY_ACCOUNTS_FILE):
        return
    try:
        with open(_LEGACY_ACCOUNTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        accounts = data.get("accounts", []) if isinstance(data, dict) else []
        _write_all(accounts)
    except Exception:
        pass


def _read_all() -> list:
    _migrate_legacy()
    if not os.path.exists(ACCOUNTS_FILE):
        return []
    with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        return []
    accounts = data.get("accounts", [])
    # 兼容旧数据：缺少 id 的账号补齐 uuid4
    changed = False
    for a in accounts:
        if not a.get("id"):
            a["id"] = str(uuid.uuid4())
            changed = True
    if changed:
        _write_all(accounts)
    return accounts


def _write_all(accounts: list) -> None:
    os.makedirs(os.path.dirname(ACCOUNTS_FILE), exist_ok=True)
    with open(ACCOUNTS_FILE, "w", encoding="utf-8") as f:
        json.dump({"accounts": accounts}, f, ensure_ascii=False, indent=2)


def list_accounts() -> list:
    with _lock:
        return _read_all()


def get_account(account_id: str):
    for a in list_accounts():
        if a.get("id") == account_id:
            return a
    return None


def upsert_account(account: dict) -> dict:
    """按 id 新增/更新账号：无 id 生成 uuid4（新增），有 id 则更新对应账号。

    账号名称允许重复，不再按名称覆盖。
    """
    name = (account.get("name") or "").strip()
    base_url = (account.get("base_url") or "").strip()
    public_key = (account.get("public_key") or "").strip()
    private_key = (account.get("private_key") or "").strip()
    if not name:
        raise ValueError("账号名称不能为空")
    if not base_url:
        raise ValueError("环境地址不能为空")
    if not public_key:
        raise ValueError("公钥不能为空")
    if not private_key:
        raise ValueError("私钥不能为空")

    account_id = (account.get("id") or "").strip()
    normalized = {
        "name": name,
        "base_url": base_url,
        "public_key": public_key,
        "private_key": private_key,
    }

    if account_id:
        # 更新：id 必须已存在
        normalized["id"] = account_id
        with _lock:
            accounts = _read_all()
            for i, a in enumerate(accounts):
                if a.get("id") == account_id:
                    accounts[i] = normalized
                    break
            else:
                raise ValueError("账号不存在")
            _write_all(accounts)
    else:
        # 新增：生成 uuid4
        normalized["id"] = str(uuid.uuid4())
        with _lock:
            accounts = _read_all()
            accounts.append(normalized)
            _write_all(accounts)
    return normalized


def delete_account(account_id: str) -> bool:
    with _lock:
        accounts = _read_all()
        remaining = [a for a in accounts if a.get("id") != account_id]
        if len(remaining) == len(accounts):
            return False
        _write_all(remaining)
        return True
