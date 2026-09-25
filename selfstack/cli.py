"""命令行入口。

    selfstack init                写一份能跑的默认配置
    selfstack inventory           盘自己
    selfstack radar               去外面捞候选
    selfstack evaluate            打分排序
    selfstack run                 一条龙，日常就用这条
    selfstack show                把最新报告打到屏幕上
    selfstack schedule            注册每天定时跑
"""

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from . import config as C
from . import discover as DV
from . import evaluate as EV
from . import inventory as IV
from . import radar as RD
from . import report as RP
from . import schedule as SC
from .util import expand, now_iso, read_json, write_json


def _load(args):
    return C.load(getattr(args, "config", None))


def _apply_coverage(inv, cands):
    """拿候选当对照，算出哪些主题是外面有但你这边完全没有的。

    这一步必须在有候选之后做，所以放在 inventory 之后单独补一刀。
    """
    clusters = (inv.get("discovered") or {}).get("clusters") or []
    inv.setdefault("summary", {})["coverage_gaps"] = DV.coverage_gaps(clusters, cands)
    return inv


def cmd_init(args):
    cfg = C.default_config()
    cfg["stack_name"] = args.name
    path = C.save(cfg, getattr(args, "config", None))
    print("配置写好了：%s" % path)
    print("")
    print("下一步有两件事，做完就能跑：")
    print("  1. 把 roots 填上你真正的工作区目录")
    print("  2. 把 queries 填上检索词，告诉它去外面捞什么")
    print("")
    print("能力清单不用写。它自己扫你机器上的技能、插件、MCP、仓库，归纳出你有哪些能力面。")
    return 0


def cmd_inventory(args):
    cfg = _load(args)
    inv = IV.build(cfg, probe=args.probe)
    out = C.state_dir(cfg)
    out.mkdir(parents=True, exist_ok=True)
    write_json(out / "inventory-latest.json", inv)
    write_json(out / ("inventory-%s.json" % inv["generated_at"]), inv)

    summ = inv["summary"]
    mode = summ.get("capabilities_mode") or "auto"
    print("盘点完成，结果在 %s" % out)
    print("")
    print("  运行时        %s" % (", ".join("%s=%s" % kv for kv in inv["runtimes"].items()) or "没探到"))
    print("  代码仓库      %d 个，其中 %d 个已经凉了" % (summ["repo_count"], len(summ["stale_repos"])))
    print("  MCP 服务      %d 个" % summ["mcp_servers"])
    print("  部件总数      %d 个" % summ.get("component_count", 0))
    print("  能力面        %d 个（%s 归纳）" % (summ["capabilities_total"], "自动" if mode == "auto" else "手工表"))
    if summ["capability_gaps"]:
        print("  手工表缺项    %s" % ", ".join(summ["capability_gaps"]))
    if summ["missing_tools"]:
        print("  找不到的工具  %s" % ", ".join(summ["missing_tools"]))
    print("")
    shown = [cl for cl in (inv.get("discovered") or {}).get("clusters") or [] if cl["size"] >= 2][:12]
    hidden = summ["capabilities_total"] - len(shown)
    for cl in shown:
        print("    [%s] %s（%d 个部件）" % (cl["id"], cl["name"], cl["size"]))
        print("        %s" % "、".join(cl["members"][:6]))
    if hidden > 0:
        print("    … 另有 %d 个小面没展开，完整清单在报告里" % hidden)
    return 0


def cmd_radar(args):
    cfg = _load(args)
    inv = read_json(C.state_dir(cfg) / "inventory-latest.json")
    if args.offline or not inv:
        if not inv:
            inv = IV.build(cfg, probe=False)
    gaps = inv.get("summary", {}).get("capability_gaps") or []
    cands, degraded, tried = RD.collect(cfg, missing_caps=gaps)
    if not args.offline:
        cands = RD.enrich_github(cfg, cands, limit=int(args.enrich), degraded=degraded)
    _apply_coverage(inv, cands)
    out = C.state_dir(cfg)
    write_json(out / "candidates-latest.json", {
        "generated_at": now_iso(), "candidates": cands, "degraded": degraded, "tried": tried,
    })
    write_json(out / "inventory-latest.json", inv)
    print("捞到 %d 条候选（去重后）" % len(cands))
    print("跑通的源 %d 个，挂掉的源 %d 个" % (len(tried) - len(degraded), len(degraded)))
    cov = inv["summary"].get("coverage_gaps") or []
    print("比对出 %d 个你没覆盖的外部主题：%s" % (len(cov), "、".join(c["term"] for c in cov[:6])))
    for d in degraded[:5]:
        print("  挂了：%s -> %s" % (d.get("source"), d.get("error")))
    return 0


def cmd_evaluate(args):
    cfg = _load(args)
    st = C.state_dir(cfg)
    inv = read_json(st / "inventory-latest.json")
    blob = read_json(st / "candidates-latest.json") or {}
    cands = blob.get("candidates") or []
    if not inv:
        print("先跑 selfstack inventory")
        return 2
    if not cands:
        print("先跑 selfstack radar")
        return 2

    _apply_coverage(inv, cands)
    proposals = EV.score_all(cfg, inv, cands)
    md_path = RP.write_reports(cfg, inv, proposals, blob.get("degraded") or [], blob.get("tried") or [], st)

    print("打分完成，%d 条进榜" % len(proposals))
    print("报告：%s" % md_path)
    print("")
    for i, p in enumerate(proposals[: int(cfg.get("top_n") or 10)], 1):
        print("  %2d. [%3s分] %-46s %s" % (i, p["score"], (p.get("title") or "")[:46], p["verdict"]))
    return 0


def cmd_run(args):
    cfg = _load(args)
    inv = IV.build(cfg, probe=args.probe)
    gaps = inv.get("summary", {}).get("capability_gaps") or []
    cands, degraded, tried = RD.collect(cfg, missing_caps=gaps)
    if not args.offline:
        cands = RD.enrich_github(cfg, cands, limit=int(args.enrich), degraded=degraded)

    _apply_coverage(inv, cands)
    proposals = EV.score_all(cfg, inv, cands)
    st = C.state_dir(cfg)
    write_json(st / "candidates-latest.json", {
        "generated_at": now_iso(), "candidates": cands, "degraded": degraded, "tried": tried,
    })
    write_json(st / "inventory-latest.json", inv)
    md_path = RP.write_reports(cfg, inv, proposals, degraded, tried, st)

    cov = inv["summary"].get("coverage_gaps") or []
    print("跑完了。")
    print("  部件 %d 个，归纳出 %d 个能力面（%s）" % (
        inv["summary"].get("component_count", 0),
        inv["summary"]["capabilities_total"],
        "自动归纳" if inv["summary"].get("capabilities_mode") == "auto" else "手工表",
    ))
    print("  候选 %d 条，进榜 %d 条" % (len(cands), len(proposals)))
    print("  外面有但我没覆盖的方向 %d 个：%s" % (len(cov), "、".join(c["term"] for c in cov[:6]) or "没有"))
    print("  报告 %s" % md_path)
    print("")
    if proposals:
        print("排前三的：")
        for p in proposals[:3]:
            print("  %s分  %s" % (p["score"], p.get("title")))
            print("        %s" % (p.get("reasons") or [""])[0])
    return 0


def cmd_show(args):
    cfg = _load(args)
    path = C.state_dir(cfg) / "report-latest.md"
    if not path.exists():
        print("还没有报告，先跑 selfstack run")
        return 2
    text = path.read_text(encoding="utf-8")
    limit = args.lines
    if limit and limit > 0:
        text = "\n".join(text.splitlines()[:limit])
    print(text)
    return 0


def cmd_schedule(args):
    cfg = _load(args)
    if args.remove:
        ok, msg = SC.uninstall()
    else:
        ok, msg = SC.install(cfg, hour=args.hour, minute=args.minute)
    print(("搞定：%s" % msg) if ok else ("没成：%s" % msg))
    return 0 if ok else 1


def build_parser():
    p = argparse.ArgumentParser(prog="selfstack", description="自体系盘点 + 自主升级雷达")
    p.add_argument("--version", action="version", version="selfstack " + __version__)
    sub = p.add_subparsers(dest="cmd")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", help="指定配置文件路径")

    s = sub.add_parser("init", parents=[common], help="写一份默认配置")
    s.add_argument("--name", default="My Stack")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("inventory", parents=[common], help="盘点自身体系")
    s.add_argument("--probe", action="store_true", help="顺带探对外通道连通性")
    s.set_defaults(func=cmd_inventory)

    s = sub.add_parser("radar", parents=[common], help="抓取外部候选")
    s.add_argument("--offline", action="store_true", help="跳过网络只用已有快照")
    s.add_argument("--enrich", type=int, default=15, help="最多给多少条缺元数据的候选补齐 GitHub 信息")
    s.set_defaults(func=cmd_radar)

    s = sub.add_parser("evaluate", parents=[common], help="打分并出报告")
    s.set_defaults(func=cmd_evaluate)

    s = sub.add_parser("run", parents=[common], help="一条龙跑完")
    s.add_argument("--probe", action="store_true")
    s.add_argument("--offline", action="store_true", help="不联网，只用上一次的候选快照")
    s.add_argument("--enrich", type=int, default=15, help="最多给多少条缺元数据的候选补齐 GitHub 信息")
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("show", parents=[common], help="打印最新报告")
    s.add_argument("--lines", type=int, default=0)
    s.set_defaults(func=cmd_show)

    s = sub.add_parser("schedule", parents=[common], help="注册每天定时跑")
    s.add_argument("--hour", type=int, default=9)
    s.add_argument("--minute", type=int, default=30)
    s.add_argument("--remove", action="store_true")
    s.set_defaults(func=cmd_schedule)

    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "cmd", None):
        parser.print_help()
        return 0
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
