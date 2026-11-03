# -*- coding: utf-8 -*-
"""解构清单视图的数据层 —— 把 decomp.md 的 7 张表 + cases.md 的回指 + 执行台账串成一张"缺口地图"。

为什么要有：test-case-design 阶段 1 把需求拆成功能点 / 字段 / 状态机 / 规则 / 旅程 / 影响面 / 控件七张表，
本意是借拆解找出"设计里缺的、没写清的"，再看"有没有用例、测没测过"。但这些表只躺在 decomp.md 里，需求拆解评审卡片只露出要裁决的那几条，
人看不到全貌。这里把每一行放到两条轴上：

    设计明确度（decomp.md 置信度列）：确定 / 推断 / 缺失 / 冲突 / 缺帧 / 禁止
    验证进度（cases.md derived_from 回指 → 用例 → 最近一次执行）：未覆盖 / 未执行 / 有失败 / 执行出错 / 已知缺陷 / 已通过

两条轴交叉，就能一眼看出：哪些是设计缺口、哪些写清了但没人测、哪些测了但挂着。

只读：decomp.md、cases.md、artifacts/_runs/*.jsonl 都不写。每次请求重新解析，agent 改了文件刷新即可看到。

命令行自检：
    python3 tools/decomp_console.py <task>      # task_id，或指向任务目录的路径
"""
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
ROOT = HERE.parent

from gate_review import resolve_task_dir  # noqa: E402

TABLES = {"⓪": "模块清单", "①": "功能点清单", "②": "字段规格表", "③": "状态机", "④": "业务规则表",
          "⑤": "操作旅程", "⑥": "影响面", "⑦": "控件规格表"}

# 行 ID：A01 / FD03 / FLD12 / BR07 / R12b / C104，允许后面跟「（说明）」；也有少数表直接用「②-3」
ROW_ID_RE = re.compile(r"^([A-Z]{1,3}\d{1,3}[a-z]?)(?:\s*[（(].*)?$")
ALT_ID_RE = re.compile(r"^([①②③④⑤⑥⑦]-\d+)$")
FID_RE = re.compile(r"\b(F\d{1,3}|A\d{1,3}|MD\d{1,3})\b")
DBT_RE = re.compile(r"DBT-\d+")
# derived_from 里的回指：「①-A01」「⑦-C17」，也可能是裸 ID「A01」；「C17–C24」区间要展开
REF_RE = re.compile(r"(?:([①②③④⑤⑥⑦])\s*[-–:：]\s*)?\b([A-Z]{1,3}\d{1,3}[a-z]?)\b(?:\s*[–~～\-]\s*([A-Z]{1,3})?(\d{1,3}))?")

KIND_ORDER = ["冲突", "缺帧", "缺失", "推断", "确定", "禁止"]
GAP_KINDS = {"冲突", "缺帧", "缺失", "推断"}
# 「执行出错」= 用例没跑到断言就挂了（准备步骤 / 环境 / 登录超时），和「有失败」（断言不通过）分开，免得环境抖动被读成产品问题
COV_ORDER = ["有失败", "执行出错", "已知缺陷", "未执行", "未覆盖", "已通过"]


def _cells(line: str) -> list:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _is_sep(cells: list) -> bool:
    return bool(cells) and all(re.match(r"^:?-{2,}:?$", c or "") for c in cells if c != "")


def _strip(s: str) -> str:
    return re.sub(r"\*\*|`", "", s or "").strip()


def _kind(text: str) -> str:
    """按置信度格**最先出现**的标记判定（decomp.md 的计数口径：「✅确定（xx⚠️）」算确定，括号里的 ⚠️ 是附注）"""
    t = text or ""
    pos = {k: t.find(m) for k, m in (("缺失", "⚠"), ("推断", "🔶"), ("确定", "✅"), ("禁止", "🚫"))}
    hit = sorted((p, k) for k, p in pos.items() if p >= 0)
    if not hit:
        return "冲突" if "冲突" in t else "缺帧" if "缺帧" in t else "确定"
    k = hit[0][1]
    if k == "缺失":
        head = t[hit[0][0]:hit[0][0] + 8]
        if "冲突" in head:
            return "冲突"
        if "缺帧" in head:
            return "缺帧"
    return k


def _frontmatter(text: str) -> dict:
    if not text.startswith("---"):
        return {}
    body = text.split("---", 2)[1]
    out = {}
    for line in body.splitlines():
        m = re.match(r"^([a-z_0-9]+):\s*(.*)$", line)
        if m and m.group(2).strip():
            out[m.group(1)] = m.group(2).split("  #")[0].strip().strip('"')
        m = re.match(r"^\s+([a-z_]+):\s*(\{.*\}|\d+)", line)       # outputs 下的嵌套统计行
        if m:
            out.setdefault("_outputs", {})[m.group(1)] = m.group(2).split("  #")[0].strip()
    return out


def _pick(cols: dict, *names) -> str:
    """按列名关键字取第一个非空值（表头措辞各需求略有出入：「可流转到（编辑结果）」也算「可流转到」）"""
    for n in names:
        for k, v in cols.items():
            if n in k and _strip(v):
                return _strip(v)
    return ""


def _main(tab: str, cols: dict) -> str:
    """一行的人话摘要：每张表挑最能说明"这一行讲什么"的列组合，不显示 page_map 路径之类的机器信息"""
    if tab == "③":
        a, b = _pick(cols, "当前状态", "状态"), _pick(cols, "可流转到", "动作")
        trig = _pick(cols, "触发")
        if a or b:
            return f"{a} → {b}" + (f"（{trig}）" if trig else "")
    if tab == "④":
        a, b = _pick(cols, "条件"), _pick(cols, "结果")
        if a or b:
            return f"{a} → {b}" if a and b else a or b
    if tab == "⑥":
        a, b = _pick(cols, "新功能点"), _pick(cols, "关联已有模块")
        if a or b:
            return f"{a} ↔ {b}" if a and b else a or b
    if tab == "⑦":
        a = _pick(cols, "元素含义", "含义")
        if a:
            return a
    if tab == "①" and _pick(cols, "功能点"):
        return _pick(cols, "功能点")
    if tab == "⓪" and _pick(cols, "模块"):
        return _pick(cols, "模块")
    main_key = next((k for k in cols if k not in ("ID", "page_ref", "所属模块", "所属功能点") and _strip(cols[k])), "")
    return _strip(cols.get(main_key, ""))


def parse_decomp(path: Path) -> dict:
    """decomp.md → {meta, rows, matrices, debts}。

    表头按 markdown 语法认：分隔行 |---| 的上一行才是表头；一个 section 里可以有多张表（③ 常见先 N×N 矩阵、后迁移明细）。
    N×N 矩阵（表头首格含「＼ / \\ / from」）整张原样保留成 matrices，用来画格子图。
    """
    text = path.read_text(encoding="utf-8")
    meta = _frontmatter(text)
    title = next((l[2:].strip() for l in text.splitlines() if l.startswith("# ")), "")
    rows, matrices, debts = [], [], []
    tab = sub = header = cand = None
    matrix = None
    for line in text.split("\n"):
        m = re.match(r"^##\s+([⓪①②③④⑤⑥⑦])\s*(.*)", line)
        if m:
            tab, sub, header, cand, matrix = m.group(1), m.group(2).strip(), None, None, None
            continue
        if re.match(r"^##\s+解构债清单", line):
            tab, sub, header, cand, matrix = "DBT", "解构债清单", None, None, None
            continue
        if line.startswith("## "):
            tab = sub = header = cand = matrix = None
            continue
        m = re.match(r"^###\s+(.*)", line)
        if m and tab:
            sub, header, cand, matrix = m.group(1).strip(), None, None, None
            continue
        if not tab:
            continue
        if not line.lstrip().startswith("|"):
            header = cand = matrix = None
            continue
        cells = _cells(line)
        if _is_sep(cells):
            header, cand = cand, None
            first_h = _strip(header[0]) if header else ""
            if header and re.search(r"[＼\\]|from|当前", first_h) and tab != "DBT":
                matrix = {"table": tab, "title": sub, "header": [_strip(h) for h in header], "rows": []}
                matrices.append(matrix)
            continue
        if header is None:
            cand = cells
            continue
        if matrix is not None:
            matrix["rows"].append([_strip(c) if i == 0 else c for i, c in enumerate(cells)])
            continue
        if tab == "DBT":
            first = _strip(cells[0])
            if first.startswith("DBT-"):
                d = dict(zip(header, cells))
                desc = next((v for k, v in d.items() if "描述" in k), "")
                status = next((v for k, v in d.items() if "状态" in k), "")
                owner = next((v for k, v in d.items() if "所属" in k), "")
                pick = lambda key: _strip(next((v for k, v in d.items() if key in k), ""))  # noqa: E731
                st = _strip(status).lower()
                # answered = 人已裁决、只是还没回填成 closed，不算"还开着"
                debts.append({"id": first, "desc": _strip(desc), "status": _strip(status),
                              "table": _strip(owner), "open": not (st.startswith("closed") or st.startswith("answered")),
                              "question": pick("问题"), "background": pick("背景"), "options": pick("选项")})
            continue
        first = _strip(cells[0])
        mm = ROW_ID_RE.match(first) or ALT_ID_RE.match(first)
        if not mm:
            continue
        rid = mm.group(1)
        if len(cells) < len(header):
            cells = cells + [""] * (len(header) - len(cells))
        elif len(cells) > len(header):
            cells = cells[:len(header) - 1] + [" | ".join(cells[len(header) - 1:])]
        cols = dict(zip([_strip(h) for h in header], cells))
        conf_col = next((k for k in cols if "置信度" in k), list(cols)[-1])
        conf = cols.get(conf_col, "")
        blob = " ".join(cells)
        rows.append({
            "table": tab, "sub": sub, "id": rid, "key": f"{tab}-{rid}",
            "main": _main(tab, cols)[:300], "cols": cols, "conf_col": conf_col,
            "kind": _kind(conf), "conf": _strip(conf)[:160],
            "dbt": sorted(set(DBT_RE.findall(blob))),
        })
    return {"meta": meta, "title": title, "rows": rows, "matrices": matrices, "debts": debts}


# ─────────────────────────── 人话翻译（控件行） ───────────────────────────
# 表⑦ 的 情境 / 前置交互 / 状态 是受控写法（decomposition.md「情境的受控维度」），翻成一句人话给评审人读

_STATE = {"✓可用": "可用", "⊘置灰": "置灰、点不了", "✗隐藏": "不显示", "—不适用": "不适用"}


def human_when(ctx: str) -> str:
    """`data=empty（上线初始）; role=普通成员` → 「没有数据时（上线初始），普通成员看到时」；`*` → 空串（任何时候）

    受控维度见 decomposition.md「情境的受控维度」；项目自己加的横切维度（locale 之类）原样显示为 `维度=值`。"""
    t = _strip(ctx)
    if not t or t in ("*", "—", "-"):
        return ""
    out = []
    for part in re.split(r"[;；]\s*", t):
        m = re.match(r"^\s*(\w+)\s*=\s*([^（(]+?)\s*([（(].*)?$", part)
        if not m:
            out.append(part.strip())
            continue
        k, v, note = m.group(1), m.group(2).strip(), (m.group(3) or "")
        if k == "data":
            w = {"empty": "没有数据时", "has_data": "有数据时"}.get(v, f"数据={v}")
        elif k == "role":
            w = "、".join(x.strip() for x in re.split(r"[/,，|]", v)) + "看到时"
        elif k == "status":
            w = f"状态为 {v} 时"
        elif k == "step":
            w = "最后一步" if v == "last" else f"第 {v} 步"
        elif k == "time":
            w = f"时间：{v}"
        else:
            w = f"{k}={v}"
        out.append(w + note)
    return "，".join(out)


def human_pre(pre: str) -> str:
    """`hover:"+n" 标签 → click:more` → 「先悬停 "+n" 标签，再点 more」"""
    t = _strip(pre)
    if not t or t in ("—", "-", "*"):
        return ""
    verbs = {"hover": "悬停", "click": "点", "expand": "展开", "open": "打开"}
    steps = []
    for i, part in enumerate(re.split(r"\s*(?:→|->)\s*", t)):
        m = re.match(r"^(hover|click|expand|open)\s*[:：]\s*(.+)$", part)
        steps.append((verbs[m.group(1)] + " " + m.group(2).strip()) if m else part)
    return "先" + "，再".join(steps)


def human_state(st: str) -> str:
    t = _strip(st)
    for k, v in _STATE.items():
        if t.startswith(k):
            return v + t[len(k):]
    return t


def _gap_cols(cols: dict) -> list:
    """一行里哪些列没写清（带 ⚠️ / 🔶 的人读列）"""
    return [k for k, v in cols.items()
            if k not in ("ID", "来源", "置信度", "规则回指", "page_ref", "所属模块", "所属功能点")
            and ("⚠" in v or "🔶" in v)]


def _ids(text: str) -> list:
    return list(dict.fromkeys(FID_RE.findall(_strip(text))))


def _brief(r: dict) -> dict:
    """树视图用的精简行：带人话、缺口列、验证进度"""
    c = r["cols"]
    b = {"key": r["key"], "id": r["id"], "table": r["table"], "kind": r["kind"], "cov": r.get("cov", ""),
         "gap_cols": _gap_cols(c), "dbt": r["dbt"], "cases": [x["id"] for x in r.get("cases", [])],
         "sub": r.get("sub") or ""}
    if r["table"] == "⑦":
        b.update(obj=_strip(c.get("元素含义", "")), when=human_when(c.get("情境", "")),
                 pre=human_pre(c.get("前置交互", "")), state=human_state(c.get("状态", "")),
                 text=_strip(c.get("文案字面值", "")), type=_strip(c.get("类型", "")))
    elif r["table"] == "②":
        b.update(obj=_strip(c.get("字段", "")), need=_strip(c.get("必填", "")), rng=_strip(c.get("取值/约束", "")),
                 dflt=_strip(c.get("默认值", "")), check=_strip(c.get("校验规则/报错文案", "")))
    else:
        b.update(obj=r["main"])
    return b


def build_tree(rows: list) -> dict:
    """⓪ 模块 → ① 功能点 → ②④⑦ 特性。没有 ⓪ / 层级列的旧解构，按表⑦ 的屏分组兜底"""
    mods = [r for r in rows if r["table"] == "⓪"]
    feats = [r for r in rows if r["table"] == "①"]
    fmap = {f["id"]: f for f in feats}
    has_mod = bool(mods) and any(_ids(f["cols"].get("所属模块", "")) for f in feats)
    feat_nodes = {}
    prev_entry = ""
    for f in feats:
        c = f["cols"]
        entry = _strip(c.get("入口页面", ""))
        if re.match(r"^同(上|左)", entry):          # 表里的「同上」脱离表格就没意义，换成上一行的实际入口
            entry = prev_entry + entry[2:]
        prev_entry = entry or prev_entry
        feat_nodes[f["id"]] = {
            "id": f["id"], "name": _strip(c.get("功能点", f["main"])), "type": _strip(c.get("类型", "")),
            "roles": _strip(c.get("涉及角色", "")), "entry": entry,
            "kind": f["kind"], "cov": f.get("cov", ""), "gap_cols": _gap_cols(c),
            "controls": [], "fields": [], "rules": [], "cases": [x["id"] for x in f.get("cases", [])],
        }
    orphans = {"controls": [], "fields": [], "rules": []}
    slot = {"⑦": "controls", "②": "fields", "④": "rules"}
    for r in rows:
        if r["table"] not in slot:
            continue
        owner = r["cols"].get("所属功能点", "")
        ids = [i for i in _ids(owner) if i in feat_nodes]
        b = _brief(r)
        if ids:
            for i in ids:
                feat_nodes[i][slot[r["table"]]].append(b)
        else:
            orphans[slot[r["table"]]].append(b)

    def stats(nodes):
        # 一行可能同时挂在两个功能点下（② 的「A02/A03」），按行去重再数
        uniq = lambda k: {x["key"]: x for n in nodes for x in n[k]}  # noqa: E731
        c, f, r = uniq("controls"), uniq("fields"), uniq("rules")
        items = list(c.values()) + list(f.values()) + list(r.values())
        return {"features": len(nodes), "controls": len(c), "fields": len(f), "rules": len(r),
                "gaps": sum(1 for x in items if x["kind"] in GAP_KINDS) + sum(1 for n in nodes if n["kind"] in GAP_KINDS)}

    modules = []
    if has_mod:
        for m in mods:
            c = m["cols"]
            fs = [feat_nodes[f["id"]] for f in feats if m["id"] in _ids(f["cols"].get("所属模块", ""))]
            modules.append({"id": m["id"], "name": _strip(c.get("模块", m["main"])),
                            "scope": _strip(c.get("一句话范围", "")), "entry": _strip(c.get("入口位置", "")),
                            "roles": _strip(c.get("涉及角色", "")), "kind": m["kind"],
                            "features": fs, "stats": stats(fs)})
        loose = [feat_nodes[f["id"]] for f in feats
                 if not any(i in {m["id"] for m in mods} for i in _ids(f["cols"].get("所属模块", "")))]
        if loose:
            modules.append({"id": "", "name": "没挂到模块的功能点", "scope": "", "entry": "", "roles": "",
                            "kind": "缺失", "features": loose, "stats": stats(loose)})
    else:
        allf = list(feat_nodes.values())
        modules.append({"id": "", "name": "全部功能点", "scope": "这份解构是旧格式，还没拆模块，功能点平铺在这里",
                        "entry": "", "roles": "", "kind": "", "features": allf, "stats": stats(allf)})
    # 没挂到功能点的控件按屏分组（旧解构的控件全部在这里）
    screens = []
    for b in orphans["controls"]:
        if not screens or screens[-1]["name"] != b["sub"]:
            screens.append({"name": b["sub"] or "（未分屏）", "controls": []})
        screens[-1]["controls"].append(b)
    return {"structured": has_mod, "modules": modules, "orphan_screens": screens,
            "orphan_fields": orphans["fields"], "orphan_rules": orphans["rules"],
            "feature_count": len(feats), "module_count": len(mods)}


def _expand_refs(text: str) -> list:
    """derived_from 文本 → [(表号或 None, 行ID), ...]；「C17–C24」展开成 C17..C24"""
    out = []
    for m in REF_RE.finditer(text or ""):
        tab, rid, pre2, n2 = m.group(1), m.group(2), m.group(3), m.group(4)
        out.append((tab, rid))
        if n2:
            pm = re.match(r"^([A-Z]{1,3})(\d{1,3})$", rid)
            if pm and (pre2 in (None, pm.group(1))):
                a, b = int(pm.group(2)), int(n2)
                if 0 < b - a <= 60:
                    width = len(pm.group(2))
                    out += [(tab, f"{pm.group(1)}{i:0{width}d}") for i in range(a + 1, b + 1)]
    return out


def _case_state(c: dict) -> str:
    a, m = c.get("auto_status"), c.get("manual_status")
    if a in ("failed", "xpassed_strict") or m == "fail":
        return "有失败"
    if a == "error":
        return "执行出错"
    if a == "xfailed":
        return "已知缺陷"
    if a == "passed" or m == "pass":
        return "已通过"
    return "未执行"


def build(task_id: str, cc: dict | None = None) -> dict:
    """cc：调用方已算好的 case_console.build(task_id) 结果（控制台索引页复用，免得同一份台账解析两遍）"""
    d = resolve_task_dir(task_id)
    f = d / "decomp.md"
    if not f.exists():
        return {"error": f"{task_id} 没有 decomp.md"}
    doc = parse_decomp(f)
    rows = doc["rows"]
    by_key = {r["key"]: r for r in rows}
    by_id = {}
    for r in rows:
        by_id.setdefault(r["id"], []).append(r)
    for r in rows:
        r["cases"] = []

    cases_info = {"has_cases": False, "total": 0, "unlinked": []}
    if (d / "cases.md").exists():
        if cc is None:
            import case_console
            cc = case_console.build(task_id)
        cases_info["has_cases"] = True
        cases_info["total"] = len(cc["cases"])
        for c in cc["cases"]:
            st = _case_state(c)
            ref = {"id": c["id"], "title": (c.get("title") or "")[:120], "exec": c.get("exec", ""),
                   "priority": c.get("priority", ""), "state": st,
                   "auto": c.get("auto_status"), "manual": c.get("manual_status")}
            hit = False
            for tab, rid in _expand_refs(c.get("derived_from", "")):
                targets = [by_key[f"{tab}-{rid}"]] if tab and f"{tab}-{rid}" in by_key else by_id.get(rid, [])
                for t in targets:
                    if all(x["id"] != c["id"] for x in t["cases"]):
                        t["cases"].append(ref)
                        hit = True
            if not hit:
                cases_info["unlinked"].append(ref)

    for r in rows:
        states = {x["state"] for x in r["cases"]}
        r["cov"] = ("未覆盖" if not r["cases"] else next(s for s in COV_ORDER if s in states))

    tables = []
    for k, name in TABLES.items():
        rs = [r for r in rows if r["table"] == k]
        mats = [m for m in doc["matrices"] if m["table"] == k]
        if not rs and not mats:
            continue
        tables.append({
            "key": k, "name": name, "total": len(rs),
            "kind": {x: sum(1 for r in rs if r["kind"] == x) for x in KIND_ORDER},
            "cov": {x: sum(1 for r in rs if r["cov"] == x) for x in COV_ORDER},
            "matrices": len(mats),
        })
    debts = doc["debts"]
    summary = {
        "rows": len(rows),
        "gaps": sum(1 for r in rows if r["kind"] in GAP_KINDS),
        "conflicts": sum(1 for r in rows if r["kind"] in ("冲突", "缺帧")),
        "uncovered": sum(1 for r in rows if r["cov"] == "未覆盖"),
        "notrun": sum(1 for r in rows if r["cov"] == "未执行"),
        "failed": sum(1 for r in rows if r["cov"] == "有失败"),
        "errored": sum(1 for r in rows if r["cov"] == "执行出错"),
        "defect": sum(1 for r in rows if r["cov"] == "已知缺陷"),
        "passed": sum(1 for r in rows if r["cov"] == "已通过"),
        "debts_open": sum(1 for x in debts if x["open"]),
        "debts_total": len(debts),
    }
    meta = doc["meta"]
    return {
        "task_id": task_id, "title": doc["title"],
        "meta": {"status": meta.get("status", ""), "prd_version": meta.get("prd_version", ""),
                 "input_tier": meta.get("input_tier", ""), "gate0_applied_at": meta.get("gate0_applied_at", ""),
                 "created_at": meta.get("created_at", "")},
        "summary": summary, "tables": tables, "rows": rows,
        "matrices": doc["matrices"], "debts": debts, "cases": cases_info, "tree": build_tree(rows),
        "kind_order": KIND_ORDER, "cov_order": COV_ORDER,
    }


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(0)
    b = build(sys.argv[1])
    if b.get("error"):
        print(b["error"])
        sys.exit(1)
    s = b["summary"]
    print(f"{b['title']}\n共 {s['rows']} 行 · 设计缺口 {s['gaps']}（冲突/缺帧 {s['conflicts']}）· "
          f"未覆盖 {s['uncovered']} · 未执行 {s['notrun']} · 有失败 {s['failed']} · 执行出错 {s['errored']} · 已知缺陷 {s['defect']} · 已通过 {s['passed']}"
          f" · 解构债 {s['debts_open']}/{s['debts_total']} open")
    for t in b["tables"]:
        k = " ".join(f"{x}{n}" for x, n in t["kind"].items() if n)
        c = " ".join(f"{x}{n}" for x, n in t["cov"].items() if n)
        print(f"  {t['key']} {t['name']:<6} {t['total']:>3} 行 | {k} | {c}" + (f" | 矩阵 {t['matrices']}" if t["matrices"] else ""))
    if b["cases"]["unlinked"]:
        print(f"  没回指到任何解构行的用例 {len(b['cases']['unlinked'])} 条：" + ", ".join(x["id"] for x in b["cases"]["unlinked"][:10]))
