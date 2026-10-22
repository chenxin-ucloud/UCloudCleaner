"""账号配置持久化：账号名称、环境地址、公私钥。存 web/accounts.json。"""
import json
import os
import threading

ACCOUNTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "accounts.json")

_lock = threading.Lock()


def _read_all() -> list:
    if not os.path.exists(ACCOUNTS_FILE):
        return []
    with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        return []
    return data.get("accounts", [])


def _write_all(accounts: list) -> None:
    with open(ACCOUNTS_FILE, "w", encoding="utf-8") as f:
        json.dump({"accounts": accounts}, f, ensure_ascii=False, indent=2)


def list_accounts() -> list:
    with _lock:
        return _read_all()


def get_account(name: str):
    for a in list_accounts():
        if a.get("name") == name:
            return a
    return None


def upsert_account(account: dict) -> dict:
    """新增或按 name 更新账号，返回规范化后的账号。"""
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

    normalized = {
        "name": name,
        "base_url": base_url,
        "public_key": public_key,
        "private_key": private_key,
    }
    with _lock:
        accounts = _read_all()
        for i, a in enumerate(accounts):
            if a.get("name") == name:
                accounts[i] = normalized
                break
        else:
            accounts.append(normalized)
        _write_all(accounts)
    return normalized


def delete_account(name: str) -> bool:
    with _lock:
        accounts = _read_all()
        remaining = [a for a in accounts if a.get("name") != name]
        if len(remaining) == len(accounts):
            return False
        _write_all(remaining)
        return True
