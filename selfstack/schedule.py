"""阶段五：定时跑起来。

Windows 走 schtasks，Linux / macOS 走 crontab。
只是注册任务本身，任务内容依然是自己那条只读的 report 命令。
"""

import subprocess
import sys
from pathlib import Path


def run_daily_cmd(cfg):
    """拼出定时任务真正要执行的那条命令。

    必须用当前解释器的绝对路径加 `-m selfstack.cli`，不能直接写 `selfstack`：
    这东西常常装在隔离 venv 里，压根不在全局 PATH 上，任务到点拉不起来，
    而且失败得很安静，你根本不知道它没跑。
    """
    exe = sys.executable
    return '"%s" -m selfstack.cli run --config "%s"' % (exe, cfg["_config_path"])


def install_windows(cfg, hour=9, minute=30):
    task_name = "selfstack-daily"
    cmd = run_daily_cmd(cfg)
    full = [
        "schtasks", "/Create", "/F", "/SC", "DAILY",
        "/TN", task_name, "/TR", cmd,
        "/ST", "%02d:%02d" % (hour, minute),
    ]
    proc = subprocess.run(full, capture_output=True, text=True)
    return proc.returncode == 0, (proc.stdout or proc.stderr or "").strip()


def install_cron(cfg, hour=9, minute=30):
    line = "%d %d * * * %s >> %s 2>&1" % (
        minute, hour, run_daily_cmd(cfg),
        Path.home() / ".selfstack" / "cron.log",
    )
    try:
        proc = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
        existing = proc.stdout if proc.returncode == 0 else ""
    except FileNotFoundError:
        return False, "这台机器上没有 crontab"

    if "selfstack run" in existing:
        return True, "任务已经在了，没重复添加：\n" + line

    new = existing.rstrip("\n") + "\n" + line + "\n"
    proc = subprocess.run(["crontab", "-"], input=new, capture_output=True, text=True)
    return proc.returncode == 0, (proc.stderr or line).strip()


def install(cfg, hour=9, minute=30):
    if sys.platform.startswith("win"):
        return install_windows(cfg, hour, minute)
    return install_cron(cfg, hour, minute)


def uninstall():
    if sys.platform.startswith("win"):
        proc = subprocess.run(
            ["schtasks", "/Delete", "/F", "/TN", "selfstack-daily"],
            capture_output=True, text=True,
        )
        return proc.returncode == 0, (proc.stdout or proc.stderr or "").strip()
    proc = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    if proc.returncode != 0:
        return False, "读不到 crontab"
    kept = [ln for ln in proc.stdout.splitlines() if "selfstack run" not in ln]
    proc2 = subprocess.run(["crontab", "-"], input="\n".join(kept) + "\n",
                           capture_output=True, text=True)
    return proc2.returncode == 0, "已移除定时任务"
