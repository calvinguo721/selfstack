"""阶段二：去外面捞候选。

四种源，覆盖两类升级：
  换新      GitHub 检索 / HN / awesome 列表 -> 找到你还没用的东西
  升版本    GitHub release / PyPI / npm      -> 你已经在用的东西出了新版本

每个源失败都记进 degraded，报告里会写出来。绝不因为某个源挂了就停。
"""

from datetime import datetime, timedelta, timezone

from .util import days_since, http_json

GH_API = "https://api.github.com"
HN_API = "https://hn.algolia.com/api/v1/search"


def _cand(**kw):
    base = {
        "source": "",
        "kind": "",
        "id": "",
        "title": "",
        "url": "",
        "summary": "",
        "stars": None,
        "pushed_at": None,
        "version": None,
        "license": None,
        "caps": [],
        "extra": {},
    }
    base.update(kw)
    if not base["id"]:
        base["id"] = base["url"] or base["title"]
    return base


def _iso(delta_days):
    return (datetime.now(timezone.utc) - timedelta(days=delta_days)).strftime("%Y-%m-%d")


def from_github_search(cfg, query, degraded):
    """GitHub 仓库检索。没配 token 也能跑，只是限流狠（60 次/小时）。"""
    window = int(cfg.get("radar_window_days") or 180)
    stars = int(cfg.get("min_stars") or 0)
    q = "%s stars:>=%d pushed:>=%s" % (query["q"], stars, _iso(window))
    url = "%s/search/repositories?q=%s&sort=stars&order=desc&per_page=15" % (
        GH_API,
        _quote(q),
    )
    ok, data = http_json(
        url,
        timeout=int(cfg.get("timeout") or 20),
        proxy=cfg.get("proxy") or None,
        token=_token(cfg),
    )
    if not ok:
        degraded.append({"source": "github_search", "query": query.get("q"), "error": str(data)})
        return []

    out = []
    for repo in data.get("items", []):
        out.append(_cand(
            source="github_search",
            kind="new_project",
            id="gh:" + str(repo.get("full_name")),
            title=repo.get("full_name"),
            url=repo.get("html_url"),
            summary=(repo.get("description") or "")[:300],
            stars=repo.get("stargazers_count"),
            pushed_at=repo.get("pushed_at"),
            license=((repo.get("license") or {}) or {}).get("spdx_id"),
            caps=list(query.get("caps") or []),
            extra={
                "topics": (repo.get("topics") or [])[:8],
                "open_issues": repo.get("open_issues_count"),
                "forks": repo.get("forks_count"),
                "archived": bool(repo.get("archived")),
                "created_at": repo.get("created_at"),
            },
        ))
    return out


def from_github_release(cfg, watch, degraded):
    """盯已经在用的仓库有没有发新版。这就是「升级」最直接的那一类。"""
    repo = watch if isinstance(watch, str) else watch.get("repo")
    url = "%s/repos/%s/releases/latest" % (GH_API, repo)
    ok, data = http_json(
        url,
        timeout=int(cfg.get("timeout") or 20),
        proxy=cfg.get("proxy") or None,
        token=_token(cfg),
    )
    if not ok:
        degraded.append({"source": "github_release", "repo": repo, "error": str(data)})
        return []

    current = None
    if isinstance(watch, dict):
        current = watch.get("current_version")
    published = data.get("published_at")
    return [_cand(
        source="github_release",
        kind="version_upgrade",
        id="rel:" + repo,
        title=repo + " " + str(data.get("tag_name") or ""),
        url=data.get("html_url") or ("https://github.com/" + repo),
        summary=(data.get("body") or "")[:400],
        pushed_at=published,
        version=data.get("tag_name"),
        caps=list((watch or {}).get("caps", []) if isinstance(watch, dict) else []),
        extra={
            "repo": repo,
            "current_version": current,
            "prerelease": bool(data.get("prerelease")),
            "published_days_ago": days_since(published),
        },
    )]


def from_hn(cfg, query, degraded):
    """Hacker News 上的讨论热度，用来判断一个东西是不是真有人在关心。"""
    window = int(cfg.get("radar_window_days") or 180)
    url = "%s?query=%s&tags=story&numericFilters=created_at_i>%d" % (
        HN_API, _quote(query["q"]), _epoch(window),
    )
    ok, data = http_json(
        url, timeout=int(cfg.get("timeout") or 20), proxy=cfg.get("proxy") or None,
    )
    if not ok:
        degraded.append({"source": "hn", "query": query.get("q"), "error": str(data)})
        return []

    out = []
    for hit in (data.get("hits") or [])[:10]:
        out.append(_cand(
            source="hn",
            kind="signal",
            id="hn:" + str(hit.get("objectID")),
            title=hit.get("title"),
            url=hit.get("url") or ("https://news.ycombinator.com/item?id=" + str(hit.get("objectID"))),
            summary=(hit.get("story_text") or "")[:300],
            stars=hit.get("points"),
            pushed_at=hit.get("created_at"),
            caps=list(query.get("caps") or []),
            extra={"comments": hit.get("num_comments"), "points": hit.get("points")},
        ))
    return out


def from_registry(cfg, pkg, degraded):
    """PyPI / npm 拉最新版本，用来算手上的包落后了多少。"""
    eco = pkg.get("eco")
    name = pkg.get("name")
    if eco == "pypi":
        url = "https://pypi.org/pypi/%s/json" % name
    elif eco == "npm":
        url = "https://registry.npmjs.org/%s/latest" % name
    else:
        return []

    ok, data = http_json(
        url, timeout=int(cfg.get("timeout") or 20), proxy=cfg.get("proxy") or None,
    )
    if not ok:
        degraded.append({"source": "registry", "pkg": name, "error": str(data)})
        return []

    latest = data.get("info", {}).get("version") if eco == "pypi" else data.get("version")
    return [_cand(
        source="registry:" + str(eco),
        kind="version_upgrade",
        id="pkg:%s:%s" % (eco, name),
        title="%s (%s)" % (name, eco),
        url="https://pypi.org/project/%s/" % name if eco == "pypi" else "https://www.npmjs.com/package/" + name,
        summary=(data.get("info", {}).get("summary") or data.get("description") or "")[:300],
        version=latest,
        caps=list(pkg.get("caps") or []),
        extra={"eco": eco, "name": name, "current_version": pkg.get("current_version")},
    )]


def from_awesome(cfg, repo, degraded):
    """抓 awesome 类清单的 README，把里面提到的仓库抽出来。

    awesome 列表是人工筛过的，噪声比纯检索小得多。
    """
    import re

    raw_url = "https://raw.githubusercontent.com/%s/HEAD/README.md" % (
        repo if isinstance(repo, str) else repo.get("repo")
    )
    caps = (repo or {}).get("caps", []) if isinstance(repo, dict) else []
    from .util import http_json as _hj
    opener_note = None
    ok, payload = _hj_text(raw_url, cfg)
    if not ok:
        degraded.append({"source": "awesome", "repo": str(repo), "error": str(payload)})
        return []

    links = re.findall(r"https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)", payload or "")
    seen, out = set(), []
    for full in links:
        if full in seen:
            continue
        seen.add(full)
        out.append(_cand(
            source="awesome:" + str(repo if isinstance(repo, str) else repo.get("repo")),
            kind="new_project",
            id="gh:" + full,
            title=full,
            url="https://github.com/" + full,
            summary="from curated list " + str(repo if isinstance(repo, str) else repo.get("repo")),
            caps=list(caps),
            extra={"note": opener_note or ""},
        ))
    return out[:100]


def _hj_text(url, cfg):
    """拿纯文本。跟 http_json 分开，因为 README 不是 JSON。"""
    import urllib.error
    import urllib.request

    proxy = cfg.get("proxy") or None
    opener = urllib.request.build_opener()
    if proxy:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        )
    req = urllib.request.Request(url, headers={"User-Agent": "selfstack/0.1"})
    try:
        with opener.open(req, timeout=int(cfg.get("timeout") or 20)) as resp:
            return True, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return False, "HTTP %s" % exc.code
    except Exception as exc:  # noqa: BLE001
        return False, "%s: %s" % (type(exc).__name__, exc)


def _quote(text):
    from urllib.parse import quote
    return quote(text)


def _epoch(days):
    import calendar
    dt = datetime.now(timezone.utc) - timedelta(days=days)
    return calendar.timegm(dt.timetuple())


def _token(cfg):
    from .config import github_token
    return github_token(cfg)


def collect(cfg, missing_caps=None):
    """跑一遍所有源，返回 (候选列表, 降级记录, 已尝试的源)。"""
    degraded = []
    candidates = []
    tried = []

    queries = cfg.get("queries") or []
    for q in queries:
        caps = set(q.get("caps") or [])
        if missing_caps and not (caps & set(missing_caps)) and not q.get("always"):
            # 只查跟当前缺口相关的关键词，省限流额度
            continue
        tried.append("github_search:" + q.get("q", ""))
        candidates.extend(from_github_search(cfg, q, degraded))

    for watch in cfg.get("watched_repos") or []:
        tried.append("github_release:" + str(watch if isinstance(watch, str) else watch.get("repo")))
        candidates.extend(from_github_release(cfg, watch, degraded))

    for pkg in cfg.get("watched_packages") or []:
        tried.append("registry:" + str(pkg.get("name")))
        candidates.extend(from_registry(cfg, pkg, degraded))

    for repo in cfg.get("awesome_repos") or []:
        tried.append("awesome:" + str(repo if isinstance(repo, str) else repo.get("repo")))
        candidates.extend(from_awesome(cfg, repo, degraded))

    if cfg.get("use_hackernews", True):
        for q in queries:
            if not (set(q.get("caps") or []) & set(missing_caps or [])) and not q.get("always"):
                continue
            if not q.get("hn", False):
                continue
            tried.append("hn:" + q.get("q", ""))
            candidates.extend(from_hn(cfg, q, degraded))

    return dedupe(candidates), degraded, tried


def enrich_github(cfg, cands, limit=15, degraded=None):
    """给只有仓库名的候选补 GitHub 元数据。

    awesome 清单里抽出来的东西只有个 `<owner>/<repo>`，没有 star、没有更新时间。
    不补的话它们全落同一个保底分，一堆陌生项目会集体挤进排行榜把真信号埋掉。
    这里只对缺数据的 GitHub 候选补全，且一次最多打 limit 个请求，防止把限流额度烧光。
    """
    degraded = degraded if degraded is not None else []
    need = [
        c for c in cands
        if c.get("stars") is None and str(c.get("id", "")).startswith("gh:")
    ]
    done = 0
    for cand in need:
        if done >= limit:
            break
        repo = str(cand["id"])[3:]
        ok, data = http_json(
            "%s/repos/%s" % (GH_API, repo),
            timeout=int(cfg.get("timeout") or 20),
            proxy=cfg.get("proxy") or None,
            token=_token(cfg),
        )
        done += 1
        if not ok:
            degraded.append({"source": "enrich", "repo": repo, "error": str(data)})
            continue
        cand["stars"] = data.get("stargazers_count")
        cand["pushed_at"] = data.get("pushed_at")
        cand["license"] = ((data.get("license") or {}) or {}).get("spdx_id")
        if not cand.get("summary"):
            cand["summary"] = (data.get("description") or "")[:300]
        cand.setdefault("extra", {}).update({
            "topics": (data.get("topics") or [])[:8],
            "open_issues": data.get("open_issues_count"),
            "forks": data.get("forks_count"),
            "archived": bool(data.get("archived")),
        })
    return cands


def dedupe(cands):
    """同一个仓库被多个源抓到时合并，把来源和佐证累加起来。"""
    merged = {}
    order = []
    for c in cands:
        key = c["id"]
        if key not in merged:
            merged[key] = dict(c)
            merged[key]["sources"] = [c["source"]]
            order.append(key)
        else:
            cur = merged[key]
            if c["source"] not in cur["sources"]:
                cur["sources"].append(c["source"])
            for field in ("stars", "version", "summary", "license"):
                if not cur.get(field) and c.get(field):
                    cur[field] = c[field]
            for cap in c.get("caps") or []:
                if cap not in cur["caps"]:
                    cur["caps"].append(cap)
            for k, v in (c.get("extra") or {}).items():
                cur["extra"].setdefault(k, v)
    return [merged[k] for k in order]
