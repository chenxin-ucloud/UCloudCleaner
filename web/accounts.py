"""账号配置持久化：账号 ID（uuid4 主键）、名称、环境地址、公私钥。存 web/accounts.json。"""
import json
import os
import threading
import uuid

ACCOUNTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "accounts.json")

_lock = threading.Lock()


def _read_all() -> list:
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
