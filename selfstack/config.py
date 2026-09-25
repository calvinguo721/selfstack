"""配置加载。

用 JSON 而不是 YAML，就为了不加依赖。
配置文件默认 ~/.selfstack/config.json，也能用 --config 指定。

配置的核心内容：
  1. roots        要盘点的根目录（工作区、工具库）
  2. skill_dirs   技能安装目录，自动归纳能力面时主要从这里抽描述
  3. queries      去外面捞候选的检索词
  4. capabilities 可选。不写就自动归纳，写了就按你手写的这份来
"""

import json
import os
from pathlib import Path

from .util import expand, read_json

DEFAULT_NAME = "My Stack"


def default_config():
    return {
        "stack_name": DEFAULT_NAME,
        "state_dir": "~/.selfstack/state",
        "proxy": os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY") or "",
        "github_token_env": "GITHUB_TOKEN",
        "timeout": 20,
        "roots": [],
        "skill_dirs": [],
        "credential_dirs": [],
        "declared_tools": [],
        "pinned_tools": [],
        "capabilities": [],
        "max_capability_clusters": 12,
        "queries": [],
        "watched_repos": [],
        "awesome_repos": [],
        "min_stars": 200,
        "max_age_days": 365,
        "risky_licenses": ["AGPL-3.0", "GPL-3.0", "SSPL-1.0"],
        "top_n": 10,
    }


def default_path():
    return Path.home() / ".selfstack" / "config.json"


def _merge(base, override):
    out = dict(base)
    for key, val in (override or {}).items():
        if isinstance(val, list) and isinstance(out.get(key), list):
            out[key] = list(out[key]) + val
        else:
            out[key] = val
    return out


def load(path=None):
    """读配置并与默认值合并。文件不存在就用一份能跑的默认配置。"""
    cfg = default_config()
    cfg_path = expand(path) if path else default_path()
    raw = read_json(cfg_path)
    if isinstance(raw, dict):
        cfg = _merge(cfg, raw)
    cfg["_config_path"] = str(cfg_path)
    return cfg


def save(cfg, path=None):
    cfg_path = Path(path) if path else default_path()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {k: v for k, v in cfg.items() if not k.startswith("_")}
    with cfg_path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    return cfg_path


def state_dir(cfg):
    return expand(cfg["state_dir"])


def github_token(cfg):
    env_name = cfg.get("github_token_env") or "GITHUB_TOKEN"
    return os.environ.get(env_name, "").strip()
