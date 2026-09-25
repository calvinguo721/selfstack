"""阶段四：出报告。

报告是给人看的，所以每一条都要能回答「为什么是我」「要花多大代价」「有啥坑」。
只写文件，不改任何东西。
"""

from pathlib import Path

from .util import write_json

VERDICT_ORDER = {"强烈建议试": 0, "值得排期评估": 1, "留着观察": 2, "暂时别碰": 3}


def render_markdown(cfg, inv, proposals, degraded, tried, new_since_last):
    lines = []
    stack = cfg.get("stack_name") or "My Stack"
    lines.append("# %s 体检与升级建议" % stack)
    lines.append("")
    lines.append("生成日期 %s" % inv.get("generated_at"))
    lines.append("")

    summ = inv.get("summary", {})
    clusters = (inv.get("discovered") or {}).get("clusters") or []
    mode = summ.get("capabilities_mode") or "auto"
    lines.append("## 一、我的体系现在长什么样")
    lines.append("")
    lines.append("- 运行时：%s" % (_fmt_runtimes(inv.get("runtimes", {})) or "没探到"))
    lines.append("- 代码仓库：%d 个，其中 %d 个超过 %s 天没提交" % (
        summ.get("repo_count", 0),
        len(summ.get("stale_repos") or []),
        cfg.get("stale_repo_days", 120),
    ))
    lines.append("- MCP 服务：%d 个已注册" % summ.get("mcp_servers", 0))
    lines.append("- 部件：%d 个（技能、插件、MCP、CLI、仓库加起来）" % summ.get("component_count", 0))
    lines.append("- 能力面：%d 个，来源「%s」" % (
        summ.get("capabilities_total", 0),
        "自动归纳" if mode == "auto" else "手工清单",
    ))
    missing_tools = summ.get("missing_tools") or []
    if missing_tools:
        lines.append("- 声明要盯但机器上找不到的工具：%s" % ", ".join(missing_tools))
    lines.append("")

    if clusters:
        lines.append("### 能力面清单（工具自己归纳的，没人手写）")
        lines.append("")
        lines.append("| 能力面 | 主题词 | 部件数 | 代表部件 |")
        lines.append("|---|---|---|---|")
        shown = clusters[:20]
        for cl in shown:
            lines.append("| %s | %s | %d | %s |" % (
                cl.get("name"),
                "、".join((cl.get("terms") or [])[:4]),
                cl.get("size", 0),
                "、".join((cl.get("members") or [])[:4]) or "无",
            ))
        lines.append("")
        rest = len(clusters) - len(shown)
        if rest > 0:
            lines.append("还有 %d 个小面没进这张表（%s），完整清单在 inventory-latest.json 里。" % (
                rest, "、".join((cl.get("name") or "") for cl in clusters[20:26]) or "都是一两件部件的小面",
            ))
            lines.append("")
        lines.append("归纳办法：把每个部件的名字和描述当成一篇文档算 tf-idf，每一轮挑一个能把最多部件"
                     "解释掉的主题词，重复到解释完；超过总量一成二的大簇会在子集上重算 tf-idf 再往下拆。"
                     "没有调任何模型，没有向量库，纯标准库。")
        lines.append("")

    cov = summ.get("coverage_gaps") or []
    if cov:
        lines.append("### 外面有、而我这套完全没有的方向")
        lines.append("")
        lines.append("这些主题词在外部候选里反复出现，但在我所有部件的描述里一次都没出现过。"
                     "这是拿候选真比对出来的缺口，不是谁在表里留了个空格。")
        lines.append("")
        lines.append("| 主题 | 多少个候选主打它 | 例子 |")
        lines.append("|---|---|---|")
        for g in cov[:10]:
            lines.append("| **%s** | %d | %s |" % (
                g.get("term"), g.get("candidates", 0), "、".join(g.get("examples") or []) or "无",
            ))
        lines.append("")
    else:
        gaps = summ.get("capability_gaps") or []
        if gaps:
            lines.append("手工清单里还没配齐的：%s" % "、".join(gaps))
            lines.append("")

    lines.append("## 二、外面找到的候选")
    lines.append("")
    if not proposals:
        lines.append("这轮没捞到值得报的东西。要么网络没通，要么 queries 里的检索词没配。")
        lines.append("")
    else:
        lines.append("合计 %d 条，按分数排下来是这样：" % len(proposals))
        lines.append("")
        lines.append("| 排名 | 项目 | 类型 | 分数 | 结论 |")
        lines.append("|---|---|---|---|---|")
        for idx, p in enumerate(proposals[: int(cfg.get("top_n") or 10)], 1):
            kind_map = {"new_project": "新工具", "version_upgrade": "版本升级", "signal": "社区信号"}
            lines.append("| %d | [%s](%s) | %s | **%s** | %s |" % (
                idx,
                (p.get("title") or "")[:60],
                p.get("url") or "",
                kind_map.get(p.get("kind"), p.get("kind")),
                p.get("score"),
                p.get("verdict"),
            ))
        lines.append("")

        lines.append("### 逐条说说")
        lines.append("")
        for idx, p in enumerate(proposals[: int(cfg.get("top_n") or 10)], 1):
            lines.append("#### %d. %s —— %s（%s 分）" % (idx, p.get("title"), p.get("verdict"), p.get("score")))
            lines.append("")
            if p.get("summary"):
                lines.append("> %s" % p["summary"].replace("\n", " ")[:400])
                lines.append("")
            lines.append("- 地址：%s" % p.get("url"))
            parts = p.get("parts") or {}
            lines.append("- 打分明细：契合 %.1f / 成熟 %.1f / 动量 %.1f / 成本 %.1f / 风险 %.1f（满分 40/25/15/10/10）" % (
                parts.get("fit", 0), parts.get("maturity", 0), parts.get("momentum", 0),
                parts.get("cost", 0), parts.get("risk", 0),
            ))
            lines.append("- 来自：%s" % ", ".join(p.get("sources") or []))
            if p.get("license"):
                lines.append("- 许可证：%s" % p["license"])
            if p.get("stars"):
                lines.append("- star：%s" % p["stars"])
            lines.append("- 判断依据：")
            for r in p.get("reasons") or []:
                lines.append("  - %s" % r)
            lines.append("")

    if new_since_last:
        lines.append("## 三、跟上一次比，新冒出来的")
        lines.append("")
        for item in new_since_last:
            lines.append("- [%s](%s)（%s 分，%s）" % (
                item.get("title"), item.get("url"), item.get("score"), item.get("verdict"),
            ))
        lines.append("")

    if degraded:
        lines.append("## 数据源健康度")
        lines.append("")
        lines.append("这些源这次没跑通，报告结论要打个折看：")
        lines.append("")
        for d in degraded[:20]:
            lines.append("- `%s`（%s）：%s" % (d.get("source"), d.get("query") or d.get("repo") or d.get("pkg") or "", d.get("error")))
        lines.append("")

    lines.append("## 底线说明")
    lines.append("")
    lines.append("这份报告只读它到的东西，**没有安装任何新包、没有改任何配置、没有花一分钱**。")
    lines.append("要不要动手、怎么动手，人说了算。")
    lines.append("")
    return "\n".join(lines)


def _fmt_runtimes(rts):
    if not rts:
        return None
    return "，".join("%s %s" % (k, v) for k, v in rts.items())


def diff_with_last(state_dir, proposals):
    """跟上一次的评分结果比，挑出新面孔。"""
    from .util import read_json

    prev = read_json(Path(state_dir) / "proposals-latest.json")
    if not prev:
        return []
    prev_ids = set()
    for p in prev.get("proposals", []) if isinstance(prev, dict) else prev:
        prev_ids.add(p.get("id"))
    return [p for p in proposals if p.get("id") not in prev_ids]


def write_reports(cfg, inv, proposals, degraded, tried, out_dir):
    """写三份东西：快照 JSON、Markdown 报告、给下轮对比用的 latest。"""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = inv.get("generated_at")

    new_items = diff_with_last(str(out), proposals)

    md = render_markdown(cfg, inv, proposals, degraded, tried, new_items)
    md_path = out / ("report-%s.md" % stamp)
    with md_path.open("w", encoding="utf-8") as fh:
        fh.write(md)
    (out / "report-latest.md").write_text(md, encoding="utf-8")

    snap = {
        "generated_at": stamp,
        "inventory": inv,
        "proposals": proposals,
        "degraded": degraded,
        "sources_tried": tried,
    }
    write_json(out / ("snapshot-%s.json" % stamp), snap)
    write_json(out / "proposals-latest.json", {"generated_at": stamp, "proposals": proposals})
    write_json(out / "inventory-latest.json", inv)

    return md_path
