"""自动发现：能力是自己算出来的，不是人手抄出来的。

以前这一步要人先在配置里手写一张能力槽位表，工具再拿它去比对。
那是把最难的一步甩给了人，而且你手写多少就只有多少，清单外的新东西它永远发现不了。

现在改成这样：
  1. 把扫到的每一个部件当成一篇文档（名字 + 从 SKILL.md / README 里读来的描述）
  2. 做 tf-idf，按余弦相似度贪心聚类
  3. 每个簇就是一套能力，簇名取簇内权重最高的几个词，底下挂着支撑它的具体部件
  4. 再把外面捞回来的候选当对照文本，找出「外面热闹但你一个部件都不沾」的主题，
     这些是真缺口，是拿外部数据比出来的，不是人在表里留了个空格留出来的

纯标准库，没有向量库、没有分词库、没有一次模型调用。

如果你确实想手工钉死某些能力，配置里写 capabilities 就行，写了就听你的（manual 模式）。
不写就走自动发现（auto 模式，默认）。
"""

import math
import re
from collections import Counter
from pathlib import Path

# 停用词宁滥勿缺。簇名就是从这些词之外挑的，噪声进来了簇名就废了。
STOP = set("""
a an the and or of to in on for with by from as at is are be been being it its this that
these those there here you your we our us i me my he his she her they them their
not no nor but if then than so such can could may might will would shall should do does did done
have has had having am was were what which who whom when where why how all any both each few more
most other some only own same very s t don now
use used using uses useful make makes made get gets got take takes need needs want wants
new old good bad best better great simple easy fast quick full half one two three first last next
just also only even still already always never usually often maybe perhaps
app apps application applications tool tools util utils utility bin cmd command commands cli
file files folder dir directory dirs path paths src lib libs pkg package packages module modules
api apis http https url urls json yaml toml md txt log logs cache caches tmp temp
skill skills plugin plugins extension extensions other others agent agents
config configure configuration setting settings option options default defaults
version versions install installing setup guide guides doc docs document documentation
readme license changelog example examples sample samples test tests build builds building
com net org io github gitlab bitbucket comhttps www site website page pages
content data type types kind kinds name names id ids key keys value values list lists
item items size count number numbers format formats level levels state states
support supports supported run runs running start starts stop ends end git branch commit
youall index read write create remove add delete update change modify changes
awesome curated collection roundup list toolkit boilerplate template starter demo awesome
alvinunreal anti hd full pro max plus mini lite zh cn en zhcn jp kr ru de fr es
发现 实现 功能 方式 方法 场景
""".split())

# 这些是技能包里的资源子目录，不是部件本身，不能当成独立文档
RESOURCE_DIRS = {
    "assets", "references", "scripts", "nodes", "examples", "__pycache__",
    "bin", "lib", "libs", "src", "dist", "build", "static", "templates",
    "prompts", "tests", "test", "docs", "locales", "fonts", "images",
}

_CJK = "\u3400-\u4dbf\u4e00-\u9fff"


def tokenize(text):
    """中英文混着来。英文按词，中文按二字组，纯连词和数字丢掉。"""
    low = str(text or "").lower()
    low = re.sub(r"[^a-z0-9%s]+" % _CJK, " ", low)
    out = []
    for tok in re.findall(r"[a-z0-9]{2,}", low):
        if not tok.isdigit() and tok not in STOP:
            out.append(tok)
    for chunk in re.findall(r"[%s]{2,}" % _CJK, low):
        for i in range(len(chunk) - 1):
            gram = chunk[i:i + 2]
            if gram not in STOP:
                out.append(gram)
    return out


_DESCRIPTION_KEYS = ("description", "summary", "desc")
_MAX_DESC_CHARS = 400
_MAX_FRONTMATTER_LINES = 40


def _extract_frontmatter_desc(text):
    """从 Markdown 头部取一段描述。不认识的格式就直接砍前若干行当正文用。"""
    lines = text.splitlines()
    desc = ""
    if lines and lines[0].strip() == "---":
        for line in lines[1:_MAX_FRONTMATTER_LINES]:
            if line.strip() in ("---", "..."):
                break
            if ":" in line:
                key, _, val = line.partition(":")
                if key.strip().lower() in _DESCRIPTION_KEYS:
                    desc = val.strip().strip("\"'").strip()
                    break
    if desc:
        return desc[:_MAX_DESC_CHARS]
    body = [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]
    return " ".join(body)[:_MAX_DESC_CHARS]


def _read_desc(path):
    """读 SKILL.md / README 的描述。读不到就返回空串，绝不在这里抛异常。"""
    for fname in ("SKILL.md", "README.md", "readme.md", "SKILL.MD"):
        p = path / fname
        try:
            if not p.is_file():
                continue
            with p.open("r", encoding="utf-8", errors="replace") as fh:
                head = fh.read(4096)
        except Exception:  # noqa: BLE001
            continue
        desc = _extract_frontmatter_desc(head)
        if desc:
            return desc
    return ""


def _looks_versionish(name):
    """版本号目录是通往真正部件的通道，不是部件本身。

    装技能的那一层常常是 plugins/cache/<包>/<版本>/<技能> 这种，
    如果把版本号当成一个部件，榜首永远是那串谁也读不懂的哈希。
    """
    low = name.lower()
    if re.match(r"^\d+(\.\d+)+", low):
        return True
    if re.search(r"[-.](wb|rc|beta|alpha)\d*\.", low):
        return True
    return bool(re.search(r"[0-9a-f]{8,}", low))


def _walk_components(root, depth, add):
    """往下钻，只把带说明书的目录当成部件。

    有三样东西一律不算部件：隐藏目录、资源子目录（scripts / assets 那类）、
    版本号目录。前两类没有语义，第三类是通道，它们要往下走进去看里面有什么，
    但它们自己不配占一个位置。没有 SKILL.md 或 README 的目录同样不当部件，
    否则一堆叫 hooks、prompts、workflows 的空目录会把真正的东西全挤到后排。
    """
    def walk(cur, left):
        try:
            children = sorted(p for p in cur.iterdir() if p.is_dir())
        except Exception:  # noqa: BLE001
            return
        for child in children:
            name = child.name
            if name.startswith(".") and name not in (".claude", ".github"):
                continue
            if name in RESOURCE_DIRS or _looks_versionish(name):
                if left > 1:
                    walk(child, left - 1)
                continue
            desc = _read_desc(child)
            if desc:
                add(name, desc)
                if left > 1:
                    walk(child, left - 1)
                continue
            if left > 1:
                walk(child, left - 1)

    walk(Path(root), int(depth))


def collect_docs(cfg, inv, depth=4):
    """把盘点结果摊平成一个个「部件文档」，每篇是 ID + 标题 + 可 tf-idf 的文本。"""
    docs = []
    seen = set()

    def add(kind, title, text):
        title = str(title or "").strip()
        if not title:
            return
        key = "%s:%s" % (kind, title)
        # 同一个技能被多个目录反复扫到是常事，只留一条
        if key in seen:
            return
        seen.add(key)
        docs.append({
            "id": key,
            "kind": kind,
            "title": title,
            "text": "%s %s" % (title, text or ""),
        })

    for group in inv.get("dir_groups", []):
        kind = group.get("source") or "dir"
        root_raw = group.get("path")
        # 凭证目录只读到条目名，不进去翻内容，这里连描述都不抽
        if kind == "credentials":
            continue
        root = Path(root_raw) if root_raw else None
        if not root or not root.exists():
            continue

        def localized(name, desc):
            add(kind, name, desc)

        _walk_components(root, int(depth), localized)

    for srv in inv.get("mcp_servers", []):
        if srv.get("status") != "ok":
            continue
        add("mcp", srv.get("name"), "%s %s" % (srv.get("name") or "", srv.get("command") or ""))

    for tool in inv.get("declared_tools", []):
        if tool.get("status") == "ok":
            add("cli", tool.get("name"), tool.get("version") or "")

    for repo in inv.get("repos", []):
        path = Path(str(repo.get("path") or ""))
        add("repo", path.name, _read_desc(path))

    return docs


def _tfidf(docs):
    n = len(docs)
    if not n:
        return [], {}
    dfs = Counter()
    vecs = []
    for doc in docs:
        toks = tokenize(doc.get("text") or "")
        if not toks:
            vecs.append({})
            continue
        tf = Counter(toks)
        total = float(len(toks))
        raw = {t: c / total for t, c in tf.items()}
        for t in raw:
            dfs[t] += 1
        vecs.append(raw)
    # 平滑 idf。语料很小时 idf 仍然稳定，不会出现除零
    idf = {t: math.log((n + 1.0) / (c + 1.0)) + 1.0 for t, c in dfs.items()}
    out = []
    for raw in vecs:
        out.append({t: v * idf.get(t, 1.0) for t, v in raw.items()})
    return out, dfs


def _norm(vec):
    return math.sqrt(sum(v * v for v in vec.values()))


def _components(members, vecs):
    """按共享词做连通分量。凡是沾同一个词的部件就算一伙。"""
    parent = {i: i for i in members}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    buckets = {}
    for i in members:
        for term in vecs[i]:
            buckets.setdefault(term, []).append(i)
    for ids in buckets.values():
        for j in ids[1:]:
            union(ids[0], j)

    groups = {}
    for i in members:
        groups.setdefault(find(i), []).append(i)
    return sorted(groups.values(), key=len, reverse=True)


def _discover_level(docs, max_clusters, use_components=True):
    """在给定的这一批部件里跑一轮归纳，返回若干个成员下标组。

    先按主题词贪心覆盖，再用共享词做连通分量兜底。
    """
    vecs, dfs = _tfidf(docs)
    total = len(docs)
    max_clusters = int(max_clusters or 12)

    # 到处都出现的词是通用词，不能拿来当能力面。
    # 不加这一步，第一轮就会挑出这种词，把所有部件一口气吸成一个大杂烩。
    cut = max(3, int(total * 0.35))
    generic = set(term for term, count in dfs.items() if count >= cut)
    for vec in vecs:
        for term in list(vec):
            if term in generic:
                del vec[term]

    remaining = set(i for i in range(total) if vecs[i])
    groups = []

    while remaining and len(groups) < max_clusters:
        score = Counter()
        for i in remaining:
            for term, weight in vecs[i].items():
                score[term] += weight
        if not score:
            break
        # 至少两个部件都用到这个词才算一个主题，独门的自造词不算
        shared = [(t, w) for t, w in score.items() if dfs.get(t, 0) >= 2]
        term = max(shared, key=lambda kv: kv[1])[0] if shared else score.most_common(1)[0][0]
        members = sorted(i for i in remaining if term in vecs[i])
        if not members:
            break
        groups.append(members)
        remaining -= set(members)

    if remaining and use_components:
        for group in _components(sorted(remaining), vecs)[: max(0, max_clusters - len(groups))]:
            groups.append(group)
            remaining -= set(group)

    if remaining:
        groups.append(sorted(remaining))
    return groups, vecs


def _refine(members, docs, max_clusters, too_big, depth=0):
    """太大的组再拆一层。拆分就在子集上重算 tf-idf，
    子集的 idf 才能把这一小撮部件之间的差别放大出来。
    """
    if depth >= 4 or len(members) <= too_big or len(members) < 6:
        return [members]
    sub_docs = [docs[i] for i in members]
    sub_groups, _sub_vecs = _discover_level(sub_docs, max_clusters=max(4, int(max_clusters) // 2))
    if len(sub_groups) < 2:
        return [members]
    out = []
    for group in sub_groups:
        out.extend(_refine([members[i] for i in group], docs, max_clusters, too_big, depth + 1))
    return out


def discover_capabilities(docs, max_clusters=12, threshold=None):
    """主题驱动的贪心覆盖，太大的簇再往下拆一层。

    早期试过两两算余弦相似度去聚类，138 个部件糊成一坨、其余全是单体，
    因为短文本之间要么相似度为 1 要么为 0，没有中间地带。
    """
    if not docs:
        return {"mode": "auto", "clusters": [], "component_count": 0}

    max_clusters = int(max_clusters or 12)
    groups, vecs = _discover_level(docs, max_clusters)
    total = len(docs)

    # 超过总量四分之一的大簇等于把清单又糊回去了，反复往下拆，直到拆不动
    too_big = max(10, int(total * 0.12))
    max_clusters_root = max_clusters
    expanded = []
    for members in groups:
        expanded.extend(_refine(members, docs, max_clusters_root, too_big))

    out = []
    for members in expanded:
        if not members:
            continue
        merged = Counter()
        for m in members:
            for term, weight in vecs[m].items():
                merged[term] += weight
        picked = [t for t, _w in merged.most_common(4)] or ["未归类部件"]
        # 按权重留前 80 个。千万别用字母序截断，
        # 那等于把字母排在后面的词全扔掉，浏览器那种能力面的关键词会被砍没。
        vocab = [t for t, _w in merged.most_common(80)]
        out.append({
            "id": "auto-0",
            "name": " ".join(picked[:2]) if len(picked) > 1 else picked[0],
            "terms": picked,
            "size": len(members),
            "strength": round(sum(_norm(vecs[m]) for m in members) / max(len(members), 1), 3),
            "members": [docs[m]["title"] for m in members][:12],
            "vocab": vocab,
        })

    out.sort(key=lambda c: (c["size"], c["strength"]), reverse=True)
    for pos, cluster in enumerate(out, 1):
        cluster["id"] = "auto-%d" % pos
    return {"mode": "auto", "clusters": out, "component_count": len(docs)}


def coverage_gaps(clusters, candidates, top_n=10, min_candidates=2):
    """拿外部候选当对照，算出哪些主题是外面热闹但你一套部件都不沾的。

    返回有序列表，每条是这个主题词 + 有多少个候选主打它 + 代表项目。
    """
    local_vocab = set()
    for cl in clusters:
        local_vocab.update(cl.get("vocab") or [])
        local_vocab.update(cl.get("terms") or [])

    if not local_vocab or not candidates:
        return []

    hits = Counter()
    examples = {}
    for cand in candidates:
        title = str(cand.get("title") or "")
        # GitHub 仓库名常写成 owner/repo，owner 是人名，跟能力没关系，摘掉
        if "/" in title:
            title = title.rsplit("/", 1)[-1]
        text = " ".join([
            title,
            str(cand.get("summary") or ""),
            " ".join(str(t) for t in (cand.get("topics") or [])),
            " ".join(str(t) for t in (cand.get("sources") or [])),
        ])
        for term in tokenize(text):
            if term in local_vocab:
                continue
            hits[term] += 1
            bucket = examples.setdefault(term, [])
            if len(bucket) < 3 and cand.get("title"):
                bucket.append(cand.get("title"))

    out = []
    for term, count in hits.most_common():
        if count < int(min_candidates):
            continue
        out.append({"term": term, "candidates": count, "examples": examples.get(term) or []})
        if len(out) >= int(top_n):
            break
    return out


def discover(cfg, inv):
    """给一份画像归纳出能力面。"""
    docs = collect_docs(cfg, inv)
    return discover_capabilities(docs, max_clusters=int(cfg.get("max_capability_clusters") or 12))
