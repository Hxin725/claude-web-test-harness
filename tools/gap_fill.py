# -*- coding: utf-8 -*-
"""我来补充 —— 解构清单里没写清的行（⚠️缺失 / 🔶推断 / 冲突），人看到自己知道的就地填答案。

为什么要有：解构清单能看到缺口，但没地方填。缺口就该在「按模块看」的缺口旁边原地补（点「我来补充」），
不要另开页签或视角让人多读一个页面。所以答案**按行**存（key = 「④-R07」这种行 key），本模块负责存取和给 agent 汇总。
缺口分两类，填的价值不一样：
    规则类（取值上限、权限范围、状态流转、并发结果……）—— 看页面也判断不了对错，只能人或产品给答案；
        补上后相关用例能从"跑一遍记录下来再找人确认"变成"第一轮直接判过还是不过"
    文案 / 形态类（按钮字面值、弹层形态、分页条数……）—— 提测后第一次探页面就会原样抓下来，凭记忆填反而容易和部署版本对不上
所以汇总时按解构债分组、打上 rule / copy 标签，方便 agent 回填时一组一组处理。

只写一个台账：artifacts/<task>/gap_fills.json（{行 key: {answer, source, status, at, rows, title}}；早期按组存的 key 是 DBT-xxx，也认）。
**不直接改 decomp.md**：一个答案往往覆盖好几行、好几列，机械回填会填错格。回填由 agent 读台账后改写
decomp.md（来源写「用户（日期 补缺口）」、置信度改 ✅）并把相关用例从"记录"改成"断言"，改完在台账里记 applied_at。

用法：
    python3 tools/gap_fill.py <task>             # 列出已填、待回填的答案（agent 回填前看这个）
    python3 tools/gap_fill.py <task> --all       # 连同没填的缺口一起列
    python3 tools/gap_fill.py <task> --applied <行key> [<行key> ...]   # 回填完给这些答案记 applied_at
"""
import json
import re
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
ROOT = HERE.parent

import decomp_console  # noqa: E402
from gate_review import resolve_task_dir  # noqa: E402

LOCK = threading.Lock()
STATUS = {"answered": "已填答案", "ask_product": "要问产品", "later": "留到提测看页面"}
SOURCES = ["我确定", "问过产品", "问过开发", "看过页面"]
# 这些列没写清 = 文案 / 形态问题，探页面就能拿到
COPY_COLS = {"文案字面值", "校验规则/报错文案", "⚠️待实测项"}
COPY_TABLES = {"⑤", "⑥", "⑦"}


def _path(task_id: str) -> Path:
    return resolve_task_dir(task_id) / "gap_fills.json"


def load(task_id: str) -> dict:
    p = _path(task_id)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_one(task_id: str, gid: str, payload: dict) -> dict:
    with LOCK:
        data = load(task_id)
        if payload.get("clear"):
            data.pop(gid, None)
        else:
            old = data.get(gid, {})
            data[gid] = {
                "answer": (payload.get("answer") or "")[:4000],
                "source": payload.get("source") or "",
                "status": payload.get("status") if payload.get("status") in STATUS else "answered",
                "rows": payload.get("rows") or old.get("rows", []),
                "title": (payload.get("title") or old.get("title", ""))[:300],
                "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
        _path(task_id).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return data


def mark_applied(task_id: str, keys: list) -> list:
    """agent 回填进 decomp.md 后调用；返回台账里不存在的 key（多半是抄错了）"""
    with LOCK:
        data = load(task_id)
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        missing = []
        for k in keys:
            if k in data:
                data[k]["applied_at"] = now
            else:
                missing.append(k)
        if len(missing) == len(keys):
            return missing
        _path(task_id).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return missing


def _clean_title(desc: str) -> str:
    """解构债描述去掉「［可提测］」「→ 已裁决…」这类给 agent 看的尾巴"""
    t = re.sub(r"［[^］]*］", "", desc or "")
    t = re.split(r"\s*→\s*", t)[0]
    return t.strip(" ；;，,")


def _locator(rows: list) -> tuple:
    """行 ID → (功能点名, 模块名)，让每个缺口读出来知道是哪个模块哪个功能"""
    mods = {r["id"]: decomp_console._strip(r["cols"].get("模块", r["main"])) for r in rows if r["table"] == "⓪"}
    feats = {}
    for r in rows:
        if r["table"] == "①":
            m = decomp_console._ids(r["cols"].get("所属模块", ""))
            feats[r["id"]] = (decomp_console._strip(r["cols"].get("功能点", r["main"])), mods.get(m[0], "") if m else "")
    return feats, mods


def _where(r: dict, feats: dict) -> str:
    if r["table"] == "①":
        return feats.get(r["id"], ("", ""))[1]
    fids = [i for i in decomp_console._ids(r["cols"].get("所属功能点", "")) if i in feats]
    if not fids:
        return r.get("sub") or ""
    f, m = feats[fids[0]]
    return f"{m} › {f}" if m else f


_META_COLS = {"ID", "来源", "置信度", "规则回指", "page_ref", "所属模块", "所属功能点"}
# ② 字段表常把缺口写成「刷新时机未写」「报错文案缺」而不带 ⚠️，只有置信度格才有标记
_MISS_WORDS = re.compile(r"未写|未给|未知|未定义|未画|未提|待实测|缺")


def _unknowns(r: dict) -> list:
    """这一行具体哪几格没写清，原文带出来（人要看到"缺的是什么"才答得出来）"""
    cols = r["cols"]
    keys = decomp_console._gap_cols(cols) or [
        k for k, v in cols.items() if k not in _META_COLS and _MISS_WORDS.search(decomp_console._strip(v))]
    out = [{"col": k, "text": decomp_console._strip(cols[k])[:400]} for k in keys]
    return out or [{"col": "", "text": r["main"][:400]}]


def _is_copy(r: dict) -> bool:
    if r["table"] in COPY_TABLES:
        return True
    cols = set(decomp_console._gap_cols(r["cols"]))
    return bool(cols) and cols <= COPY_COLS


# 债描述里的关键词比行更准：「…上限与提示文案」是规则（上限），「保存键文案」是文案
_RULE_WORDS = re.compile(r"上限|限制|范围|权限|并发|处理|规则|时机|状态|行为|N 的显示")
_COPY_WORDS = re.compile(r"文案|形态|单复数|写法|分页|示意|入口路径|菜单项|时区|命名|展示")


def _category(g: dict) -> str:
    if g["judge"]:
        return "rule"
    t = g["title"] if g["debt"] else ""
    if t and _RULE_WORDS.search(t):
        return "rule"
    if t and _COPY_WORDS.search(t):
        return "copy"
    return "copy" if all(x["copy"] for x in g["rows"]) else "rule"


def build(task_id: str) -> dict:
    b = decomp_console.build(task_id)
    if b.get("error"):
        return b
    rows = b["rows"]
    feats, _ = _locator(rows)
    debts = {d["id"]: d for d in b["debts"]}
    gap_rows = [r for r in rows if r["kind"] in decomp_console.GAP_KINDS]

    groups, by_gid = [], {}

    def group_for(gid, debt=None):
        if gid not in by_gid:
            g = {"gid": gid, "debt": debt["id"] if debt else "", "title": "", "question": "", "background": "",
                 "options": "", "judge": False, "rows": [], "cases": {}}
            if debt:
                g["title"] = _clean_title(debt["desc"])
                g["judge"] = "需判断" in debt["desc"]
                for k in ("question", "background", "options"):
                    v = debt.get(k, "")
                    g[k] = "" if v in ("—", "-") else v
            by_gid[gid] = g
            groups.append(g)
        return by_gid[gid]

    for r in gap_rows:
        open_dbts = [i for i in r["dbt"] if i in debts and debts[i]["open"]]
        g = group_for(open_dbts[0], debts[open_dbts[0]]) if open_dbts else group_for(r["key"])
        g["rows"].append({"key": r["key"], "id": r["id"], "table": r["table"], "kind": r["kind"],
                          "where": _where(r, feats), "obj": r["main"][:300],
                          "unknown": _unknowns(r), "copy": _is_copy(r)})
        for c in r.get("cases", []):
            g["cases"][c["id"]] = {"id": c["id"], "title": c["title"], "state": c["state"], "priority": c["priority"]}
    # 状态还是 open、但它挂的行都已写清的债（比如人已在别处口头确认）不进列表——没有可填的格子

    for g in groups:
        if not g["title"]:
            r0 = g["rows"][0]
            g["title"] = f"{r0['obj']}"
        g["cat"] = _category(g)
        g["cases"] = sorted(g["cases"].values(), key=lambda c: c["id"])
    groups.sort(key=lambda g: (not g["judge"], g["cat"] != "rule", -len(g["cases"]), g["gid"]))

    fills = load(task_id)
    for g in groups:
        g["fills"] = {k: fills[k] for k in [g["gid"]] + [r["key"] for r in g["rows"]] if k in fills}
    live = {g["gid"] for g in groups} | {r["key"] for g in groups for r in g["rows"]}
    applied = {k: v for k, v in fills.items() if k not in live or v.get("applied_at")}
    pending = {k: v for k, v in fills.items() if k in live and not v.get("applied_at")}
    summary = {
        "groups": len(groups),
        "rule": sum(1 for g in groups if g["cat"] == "rule"),
        "copy": sum(1 for g in groups if g["cat"] == "copy"),
        "answered": sum(1 for v in pending.values() if v.get("status") == "answered"),
        "ask_product": sum(1 for v in pending.values() if v.get("status") == "ask_product"),
        "later": sum(1 for v in pending.values() if v.get("status") == "later"),
        "applied": len(applied),
    }
    return {"task_id": task_id, "title": b["title"], "summary": summary, "groups": groups,
            "fills": fills, "applied": applied, "sources": SOURCES, "status_names": STATUS}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(0)
    if "--applied" in sys.argv:
        keys = sys.argv[sys.argv.index("--applied") + 1:]
        miss = mark_applied(sys.argv[1], keys)
        print(f"已记 applied_at：{len(keys) - len(miss)} 条" + (f"；台账里没有：{', '.join(miss)}" if miss else ""))
        sys.exit(1 if miss else 0)
    d = build(sys.argv[1])
    if d.get("error"):
        print(d["error"])
        sys.exit(1)
    s = d["summary"]
    print(f"{d['title']}\n缺口 {s['groups']} 组（规则 {s['rule']} / 文案形态 {s['copy']}）· 已填 {s['answered']} · "
          f"要问产品 {s['ask_product']} · 留到提测 {s['later']} · 已回填 {s['applied']}")
    show_all = "--all" in sys.argv
    for g in d["groups"]:
        if not g["fills"] and not show_all:
            continue
        print(f"\n■ {g['gid']}［{'规则' if g['cat'] == 'rule' else '文案形态'}］{g['title']}")
        for r in g["rows"]:
            f = g["fills"].get(r["key"])
            tag = f"［{STATUS.get(f['status'], '')}］" if f else ""
            print(f"   - {r['key']}{tag} {r['where']}：" + "；".join(f"{u['col']} {u['text']}".strip() for u in r["unknown"])[:200])
            if f and f.get("answer"):
                print(f"       答案（{f.get('source') or '未注明来源'}，{f['at']}）：{f['answer']}")
        f = g["fills"].get(g["gid"])
        if f and f.get("answer"):
            print(f"   整组答案（{f.get('source') or '未注明来源'}，{f['at']}）：{f['answer']}")
        if g["cases"]:
            print("   用例：" + ", ".join(c["id"] for c in g["cases"]))
