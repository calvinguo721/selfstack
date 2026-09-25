"""通用小工具：HTTP、JSON 读写、版本探测。

设计原则：任何一步失败都不许让整条命令崩掉。
抓不到的源就记为 degraded，报告里明说，不许假装成功。
"""

import json
import os
import subprocess
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


def now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def today():
    return datetime.now().strftime("%Y-%m-%d")


def http_json(url, timeout=20, proxy=None, headers=None, token=None):
    """GET 一个 JSON 接口。

    返回 (ok, payload_or_error)。ok=False 时第二项是错误字符串。
    proxy 形如 http://127.0.0.1:7897。
    """
    req_headers = {
        "User-Agent": "selfstack/0.1 (+local self-audit tool)",
        "Accept": "application/json",
    }
    if token:
        req_headers["Authorization"] = "Bearer " + token
    if headers:
        req_headers.update(headers)

    opener = urllib.request.build_opener()
    if proxy:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        )

    req = urllib.request.Request(url, headers=req_headers)
    try:
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        return True, json.loads(raw)
    except urllib.error.HTTPError as exc:
        return False, "HTTP %s" % exc.code
    except Exception as exc:  # noqa: BLE001 - 任何网络异常都不能中断主流程
        return False, "%s: %s" % (type(exc).__name__, exc)


def read_json(path, default=None):
    p = Path(path)
    if not p.exists():
        return default
    try:
        with p.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return default


def write_json(path, obj):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2)
    return p


def run_version(cmd, timeout=8):
    """跑一条命令拿版本号。拿不到就返回 None，不抛异常。"""
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
        )
    except Exception:  # noqa: BLE001
        return None
    out = (proc.stdout or "").strip() or (proc.stderr or "").strip()
    if not out:
        return None
    return out.splitlines()[0][:200]


def expand(path_str):
    return Path(os.path.expandvars(os.path.expanduser(str(path_str))))


def days_since(iso_str):
    """距今天数。解析不了返回 None。"""
    if not iso_str:
        return None
    text = str(iso_str).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - dt
    return max(delta.days, 0)


def truthy_env(name):
    val = os.environ.get(name, "")
    return val.strip().lower() in ("1", "true", "yes", "on")
