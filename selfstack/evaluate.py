"""阶段三：打分。

评分必须是可解释的。每一项都有明文理由，不搞黑箱总分。
   契合度 40   能不能补上我的缺口
   成熟度 25   项目本身靠不靠谱
   动量   15   现在还活着吗
   成本   10   换进去要多大代价（反向计）
   风险   10   许可证 / 废弃 / 依赖锁定（反向计）
"""

from .util import days_since

WEIGHTS = {"fit": 40, "maturity": 25, "momentum": 15, "cost": 10, "risk": 10}


def _context(inv):
    """拿到两样东西：自己归纳出来的能力簇，以及跟外面比对出来的未覆盖主题。"""
    disc = inv.get("discovered") or {}
    clusters = disc.get("clusters") or []
    gaps = inv.get("summary", {}).get("coverage_gaps") or []
    return clusters, set(str(g.get("term")) for g in gaps if g.get("term"))


def score_fit(cand, clusters, gap_terms):
    """契合度看的是候选的主题词跟你现有能力面的重合程度。

    不依赖人手写过的清单，也不要求候选先被标上槽位。谁的适应面新就在哪得分。
    """
    from . import discover as DV

    text = " ".join([
        str(cand.get("title") or ""),
        str(cand.get("summary") or ""),
        " ".join(str(t) for t in (cand.get("topics") or [])),
    ])
    terms = set(DV.tokenize(text))
    if not terms:
        return 0.0, ["这条候选连一段可判断的描述都没有"]

    best_name, best_sim = None, 0.0
    for cl in clusters:
        cl_terms = set(cl.get("terms") or []) | set(cl.get("vocab") or [])
        if not cl_terms:
            continue
        inter = terms & cl_terms
        if not inter:
            continue
        # 两边各算一次覆盖率再取几何平均，避免大簇或长描述单方面压低分数
        sim = (len(inter) / max(len(terms), 1)) * (len(inter) / max(len(cl_terms), 1))
        sim = sim ** 0.5
        if sim > best_sim:
            best_sim, best_name = sim, cl.get("name")

    reasons = []
    if best_sim >= 0.30:
        pts = 0.5
        reasons.append("跟你现在这套里的「%s」高度重合，属于同类换代（重合度 %.0f%%）" % (best_name, best_sim * 100))
    elif best_sim > 0:
        pts = 0.2 + best_sim
        reasons.append("跟你现有的「%s」沾点边，重合度 %.0f%%" % (best_name, best_sim * 100))
    else:
        pts = 0.15
        reasons.append("在你这套体系里找不到对应的能力面，属于全新领域")

    hit_gaps = sorted(terms & gap_terms)
    if hit_gaps:
        pts = min(1.0, pts + 0.15 * min(len(hit_gaps), 3))
        reasons.append("它主打的「%s」是外面这批候选里反复出现、而你一套部件都不沾的方向" % "、".join(hit_gaps[:3]))

    # 只报版本号却不知道本地现在用的是哪个版本，等于没判断依据，
    # 这种候选的契合度必须打折，否则一堆 registry 包会靠满分挤进榜首。
    if cand.get("kind") == "version_upgrade" and not cand.get("extra", {}).get("current_version"):
        pts *= 0.5
        reasons.append("没配当前版本号，无法判断是不是真落后，契合度减半")

    return pts, reasons


def score_maturity(cand, cfg):
    reasons = []
    stars = cand.get("stars")
    pts = 0.0
    if isinstance(stars, (int, float)):
        if stars >= 5000:
            pts = 1.0
            reasons.append("star %d，社区规模大" % stars)
        elif stars >= 1000:
            pts = 0.8
            reasons.append("star %d，有一定群众基础" % stars)
        elif stars >= 200:
            pts = 0.6
            reasons.append("star %d，体量偏小" % stars)
        else:
            pts = 0.3
            reasons.append("star %s，冷门，出问题大概率没人接盘" % stars)
    else:
        pts = 0.4
        reasons.append("拿不到 star 数据，成熟度按保守估计")

    if cand.get("extra", {}).get("archived"):
        pts *= 0.2
        reasons.append("仓库已归档，基本等于死了")
    return pts, reasons


def score_momentum(cand, cfg):
    reasons = []
    window = int(cfg.get("max_age_days") or 365)
    age = cand.get("pushed_days_ago")
    if age is None:
        pushed = cand.get("pushed_at")
        age = days_since(pushed)
        cand["pushed_days_ago"] = age
    if age is None:
        return 0.4, ["没有时间信息"]
    if age <= 30:
        reasons.append("%d 天前刚动过，活的" % age)
        return 1.0, reasons
    if age <= 90:
        reasons.append("%d 天前动过，正常节奏" % age)
        return 0.8, reasons
    if age <= window:
        reasons.append("%d 天没动了，偏冷" % age)
        return 0.5, reasons
    reasons.append("%d 天没更新，很可能已被作者放弃" % age)
    return 0.2, reasons


def score_cost(cand, cfg, inv):
    """换进去的成本。pin 过的、或者是核心通道的，成本高。"""
    reasons = []
    name = (cand.get("title") or "").lower()
    pinned = [str(p).lower() for p in (cfg.get("pinned_tools") or [])]
    for p in pinned:
        if p and p in name:
            reasons.append("属于钉死不动的核心件 %s，替换代价高" % p)
            return 0.2, reasons

    if cand.get("kind") == "version_upgrade":
        cur = cand.get("extra", {}).get("current_version")
        if cur and cand.get("version"):
            same = _norm(cur) == _norm(cand.get("version"))
            if same:
                reasons.append("当前已经是 %s，没有新版，不动" % cur)
                return 0.0, reasons
            reasons.append("从 %s 升到 %s，属于版本升级不是换工具，成本低" % (cur, cand.get("version")))
            return 0.9, reasons
        reasons.append("已经是版本类候选，改造面小")
        return 0.8, reasons

    reasons.append("引入新工具，要算学习和迁移时间")
    return 0.5, reasons


def _norm(ver):
    return str(ver or "").strip().lstrip("vV")


def score_risk(cand, cfg):
    reasons = []
    deduct = 0.0
    lic = cand.get("license")
    risky = cfg.get("risky_licenses") or []
    if lic and any(str(r).upper() in str(lic).upper() for r in risky):
        deduct += 0.6
        reasons.append("许可证 %s 有传染性，商用前要过一遍法务" % lic)
    if cand.get("extra", {}).get("prerelease"):
        deduct += 0.4
        reasons.append("这是预发布版本，别上生产")
    pts = max(1.0 - deduct, 0.0)
    if not reasons:
        reasons.append("没看出明显风险信号")
    return pts, reasons


def _verdict(total):
    if total >= 70:
        return "强烈建议试"
    if total >= 55:
        return "值得排期评估"
    if total >= 40:
        return "留着观察"
    return "暂时别碰"


def score_all(cfg, inv, cands):
    clusters, gap_terms = _context(inv)
    scored = []
    for cand in cands:
        parts = {}
        all_reasons = []
        total = 0.0
        for key, fn in (
            ("fit", lambda c: score_fit(c, clusters, gap_terms)),
            ("maturity", lambda c: score_maturity(c, cfg)),
            ("momentum", lambda c: score_momentum(c, cfg)),
            ("cost", lambda c: score_cost(c, cfg, inv)),
            ("risk", lambda c: score_risk(c, cfg)),
        ):
            pts, reasons = fn(cand)
            contrib = pts * WEIGHTS[key]
            parts[key] = round(contrib, 1)
            total += contrib
            all_reasons.extend(reasons)

        # 跟这套体系八竿子打不着的，就不占版面了
        if parts["fit"] <= 0 and cand.get("kind") != "version_upgrade":
            continue

        scored.append({
            "id": cand.get("id"),
            "title": cand.get("title"),
            "url": cand.get("url"),
            "kind": cand.get("kind"),
            "sources": cand.get("sources") or [cand.get("source")],
            "caps": cand.get("caps") or [],
            "version": cand.get("version"),
            "license": cand.get("license"),
            "stars": cand.get("stars"),
            "summary": cand.get("summary"),
            "score": round(total, 1),
            "parts": parts,
            "verdict": _verdict(total),
            "reasons": all_reasons,
        })

    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored
