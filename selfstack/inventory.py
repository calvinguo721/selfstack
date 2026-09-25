"""阶段一：把自己摸清楚。

产出一份体系画像 inventory，回答五个问题：
  我跑在什么环境上
  我由哪些部件组成（技能、工具、数据源、自动化、通道）
  我的关键通道现在还活着吗
  哪些能力槽位是空的
  哪些部件已经落伍了

硬规矩：碰凭证目录时只记文件名和大小，绝不读内容。
"""

import os
from pathlib import Path

from .util import days_since, expand, http_json, run_version, today

# 凭证类文件名里只要出现这些词，就只记存在，不记内容、不打开
SENSITIVE_HINTS = [
    "credential", "secret", "token", "key", "password", "auth",
    "session", "cookie", ".env", "keystore", "keyblob",
]


def _looks_sensitive(name):
    low = name.lower()
    return any(hint in low for hint in SENSITIVE_HINTS)


def scan_runtimes(cfg):
    """跑一遍基础运行时，确认地基。"""
    specs = [
        ("python", ["python", "--version"]),
        ("node", ["node", "--version"]),
        ("git", ["git", "--version"]),
        ("rustc", ["rustc", "--version"]),
        ("go", ["go", "version"]),
        ("docker", ["docker", "--version"]),
        ("uv", ["uv", "--version"]),
    ]
    found = {}
    for name, cmd in specs:
        val = run_version(cmd)
        if val:
            found[name] = val
    return found


def scan_declared_tools(cfg):
    """逐个探测配置里声明要盯的 CLI 工具，掉了的要报到。"""
    result = []
    for item in cfg.get("declared_tools") or []:
        if isinstance(item, str):
            name, cmd = item, [item, "--version"]
        else:
            name = item.get("name")
            cmd = item.get("version_cmd") or [name, "--version"]
        version = run_version(cmd)
        result.append({
            "name": name,
            "version": version,
            "status": "ok" if version else "missing",
        })
    return result


def scan_dir_entries(dir_list, label, depth=2):
    """列出目录下的条目概要，往下钻 depth 层。不进敏感文件内部。

    默认钻两层，因为技能常常是 plugins/cache/<bundle>/<skill> 这种嵌套结构，
    只看一层会把大半装备漏在外面。
    """
    entries = []
    for raw in dir_list or []:
        path = expand(raw)
        if not path.exists():
            entries.append({"source": label, "path": str(path), "status": "missing"})
            continue

        names = []

        def collect(cur, left):
            try:
                children = sorted(p for p in cur.iterdir())
            except Exception:  # noqa: BLE001
                return
            for child in children:
                names.append(child.name)
                if left > 1 and child.is_dir():
                    collect(child, left - 1)

        try:
            collect(path, int(depth))
        except Exception:  # noqa: BLE001
            entries.append({"source": label, "path": str(path), "status": "unreadable"})
            continue

        entries.append({
            "source": label,
            "path": str(path),
            "status": "ok",
            "count": len(names),
            "items": sorted(set(names))[:400],
        })
    return entries


def scan_files(file_list, label):
    """列出关注的具体文件是否存在、多大、多久没动。"""
    out = []
    for raw in file_list or []:
        path = expand(raw)
        if not path.exists():
            out.append({"source": label, "path": str(path), "status": "missing"})
            continue
        try:
            stat = path.stat()
        except Exception:  # noqa: BLE001
            out.append({"source": label, "path": str(path), "status": "unreadable"})
            continue
        mtime = Path(path).stat().st_mtime
        import datetime as _dt
        modified = _dt.datetime.fromtimestamp(mtime).isoformat(timespec="seconds")
        out.append({
            "source": label,
            "path": str(path),
            "status": "ok",
            "bytes": stat.st_size,
            "modified": modified,
            "age_days": days_since(modified),
            "sensitive": _looks_sensitive(path.name),
        })
    return out


# 注意：这里绝对不能放 ".git"。仓库扫描要靠 .git 判定目录是不是 git 仓库，
# 一旦剪掉，所有 root 都会被当成普通目录，盘点结果永远是 0 个仓库。
PRUNE_DIRS = {
    "node_modules", ".venv", "venv", "__pycache__", "site-packages",
    "dist-info", ".cache", "binaries", "blobs", "sessions", "logs",
}

MAX_REPO_DEPTH = 3


def scan_repos(cfg):
    """在 roots 下找 git 仓库，带上最后一次提交时间。

    深度卡在 MAX_REPO_DEPTH，遇到依赖/缓存目录直接剪掉。
    大目录全量递归太慢，跑一次几分钟就没人愿意用了。
    """
    import datetime as _dt

    max_depth = int(cfg.get("repo_scan_depth") or MAX_REPO_DEPTH)
    repos = []
    seen = set()
    for raw in cfg.get("roots") or []:
        root = expand(raw)
        if not root.exists():
            continue
        base_depth = len(Path(root).parts)
        for dirpath, dirnames, _files in os.walk(str(root)):
            depth = len(Path(dirpath).parts) - base_depth
            if depth >= max_depth:
                dirnames[:] = []
            dirnames[:] = [d for d in dirnames if d not in PRUNE_DIRS]

            if ".git" not in dirnames:
                continue
            real = str(Path(dirpath).resolve())
            if real in seen:
                continue
            seen.add(real)
            age = None
            head_file = Path(dirpath) / ".git" / "HEAD"
            log_file = Path(dirpath) / ".git" / "logs" / "HEAD"
            if log_file.exists():
                try:
                    lines = log_file.read_text(encoding="utf-8", errors="replace").strip().splitlines()
                    if lines and ">" in lines[-1]:
                        ts = lines[-1].split(">")[1].strip().split(" ")[0]
                        last_iso = _dt.datetime.fromtimestamp(int(ts)).isoformat(timespec="seconds")
                        age = days_since(last_iso)
                except Exception:  # noqa: BLE001
                    pass
            repos.append({
                "path": real,
                "has_head": head_file.exists(),
                "last_commit_days": age,
                "stale_days_over": (age is not None and age > int(cfg.get("stale_repo_days") or 120)),
            })
            dirnames.remove(".git")
    return repos


def scan_mcp_servers(cfg):
    """读本机 MCP 配置里的 server 清单。只读名字和命令，不碰密钥字段。"""
    servers = []
    for raw in cfg.get("mcp_configs") or []:
        path = expand(raw)
        data = None
        try:
            import json
            with path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:  # noqa: BLE001
            servers.append({"config": str(path), "status": "missing"})
            continue
        block = data.get("mcpServers") if isinstance(data, dict) else None
        if not isinstance(block, dict):
            servers.append({"config": str(path), "status": "empty"})
            continue
        for name, spec in block.items():
            if not isinstance(spec, dict):
                continue
            servers.append({
                "config": str(path),
                "name": name,
                "status": "ok",
                "command": spec.get("command", ""),
                "disabled": bool(spec.get("disabled")),
                "has_env_keys": bool(spec.get("env")),
            })
    return servers


def probe_channel(name, cfg):
    """探一条对外通道是否还活着。只探连通性，不发真实内容。"""
    probe = (cfg.get("channel_probes") or {}).get(name)
    if not probe:
        return {"name": name, "status": "not-configured"}
    kind = probe.get("kind")
    if kind == "http":
        ok, info = http_json(
            probe["url"],
            timeout=int(cfg.get("timeout") or 20),
            proxy=cfg.get("proxy") or None,
            headers={"Accept": "*/*"},
        )
        if ok:
            return {"name": name, "status": "alive", "detail": "reachable"}
        return {"name": name, "status": "down", "detail": str(info)[:120]}
    return {"name": name, "status": "unknown", "detail": "unsupported probe kind"}


def resolve_capabilities(cfg, inventory):
    """把能力槽位表跟盘点结果对上，标出哪些有、哪些空着。

    这是整套东西最关键的一步。没有这一步，后面的候选打得再热闹，
    也不知道跟你有什么关系。
    """
    haystack_parts = []
    haystack_parts.extend(str(v) for v in inventory.get("runtimes", {}).values())
    for tool in inventory.get("declared_tools", []):
        haystack_parts.append(str(tool.get("name")))
    for group in inventory.get("dir_groups", []):
        haystack_parts.extend(group.get("items") or [])
        haystack_parts.append(group.get("path", ""))
    for f in inventory.get("files", []):
        haystack_parts.append(str(f.get("path")))
    for repo in inventory.get("repos", []):
        haystack_parts.append(str(repo.get("path")))
    for srv in inventory.get("mcp_servers", []):
        haystack_parts.append(str(srv.get("name")))
    haystack = " | ".join(haystack_parts).lower()

    resolved = []
    for cap in cfg.get("capabilities") or []:
        cid = cap.get("id")
        keys = [k.lower() for k in (cap.get("match") or [])]
        hits = [k for k in keys if k in haystack]
        resolved.append({
            "id": cid,
            "name": cap.get("name") or cid,
            "why_it_matters": cap.get("why", ""),
            "status": "present" if hits else "missing",
            "evidence": hits[:6],
        })
    return resolved


def build(cfg, probe=False):
    """跑完整盘点，返回画像 dict。"""
    inv = {
        "generated_at": today(),
        "stack_name": cfg.get("stack_name"),
        "runtimes": scan_runtimes(cfg),
        "declared_tools": scan_declared_tools(cfg),
        "dir_groups": [],
        "files": [],
        "repos": scan_repos(cfg),
        "mcp_servers": scan_mcp_servers(cfg),
        "channels": [],
    }

    for label, key in (("skills", "skill_dirs"), ("credentials", "credential_dirs")):
        inv["dir_groups"].extend(scan_dir_entries(cfg.get(key) or [], label))

    for key in ("env_files", "key_files"):
        inv["files"].extend(scan_files(cfg.get(key) or [], key))

    if probe:
        for name in (cfg.get("channel_probes") or {}):
            inv["channels"].append(probe_channel(name, cfg))

    # 能力面默认是算出来的，不用人先在表里抄一遍清单。
    # 只有在配置里真的写了 capabilities 的时候，才听人手写的那份。
    from . import discover as DV

    disc = DV.discover(cfg, inv)
    inv["discovered"] = disc

    if cfg.get("capabilities"):
        inv["capabilities"] = resolve_capabilities(cfg, inv)
        inv["capabilities_mode"] = "manual"
    else:
        inv["capabilities"] = [
            {
                "id": cl["id"],
                "name": cl["name"],
                "status": "present",
                "evidence": cl["members"][:6],
                "size": cl["size"],
                "strength": cl["strength"],
            }
            for cl in disc.get("clusters") or []
        ]
        inv["capabilities_mode"] = "auto"

    present = sum(1 for c in inv["capabilities"] if c["status"] == "present")
    gaps = [c for c in inv["capabilities"] if c["status"] == "missing"]
    missing_tools = [t for t in inv["declared_tools"] if t["status"] == "missing"]
    inv["summary"] = {
        "capabilities_mode": inv["capabilities_mode"],
        "component_count": disc.get("component_count", 0),
        "capabilities_total": len(inv["capabilities"]),
        "capabilities_present": present,
        "capability_gaps": [c["id"] for c in gaps],
        "coverage_gaps": [],
        "missing_tools": [t["name"] for t in missing_tools],
        "repo_count": len(inv["repos"]),
        "stale_repos": [r["path"] for r in inv["repos"] if r.get("stale_days_over")],
        "mcp_servers": len([s for s in inv["mcp_servers"] if s.get("status") == "ok"]),
    }
    return inv
