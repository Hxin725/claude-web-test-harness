# -*- coding: utf-8 -*-
"""QA 控制台 —— **一个端口**，版本为锚，需求可切换。

为什么合并：原先需求拆解评审台和用例执行台各起一个端口，一个版本十个需求
就要开二十个端口，荒谬。改成单端口 + 三级导航：

    版本（v2 / v1 / 未归版）→ 需求（task）→ 视图（解构清单 / 评审 / 执行）

解构清单：decomp.md 各张表的全貌，默认按「模块 → 功能点 → 控件 / 字段 / 规则 / 缺口」展示，
每行再放到「设计明确度 × 验证进度」两条轴上，数据层见 tools/decomp_console.py。
我来补充：「按模块看」里每条缺口旁边直接填「我知道的答案」，按行存 gap_fills.json，数据层见 tools/gap_fill.py。

数据来源一律只读，写入只落这几类台账（都只增不改）：
    artifacts/<task>/gate0_verdicts.json     ← 需求拆解评审裁决
    artifacts/<task>/gap_fills.json          ← 补缺口的答案（agent 读它回填 decomp.md）
    artifacts/_runs/{auto,manual}_runs.jsonl ← 执行记录
（用户在用例评审改用例是第三条写路径，走 tools/cases_edit.py，有快照有审计。）

版本字段来自 `artifacts/<task>/state.md` frontmatter 的 `version:`（orchestrator 维护）。
没有该字段的历史任务归入「未归版」—— **不做推断**，任务名里的 v1/v2 多是功能版本号
而不是产品版本号。

用法：
    python3 tools/qa_console.py serve [端口]     # 默认 8900
    python3 tools/qa_console.py index            # 命令行看一眼索引
"""
import json
import re
import sys
import time
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
ROOT = HERE.parent

try:
    from gate_review import paths, load_verdicts, save_verdicts      # noqa: E402,F401
    from gate_review_ui import build_cards                           # noqa: E402
    import case_console                                              # noqa: E402
    import decomp_console                                            # noqa: E402
    import cases_edit                                                # noqa: E402
    import gap_fill                                                  # noqa: E402
except SyntaxError as _e:
    # 磁盘上的代码有语法错误：说清楚是哪个文件哪一行，而不是甩一屏 traceback。
    # `ensure` 走到这里时还没动过任何进程——正在跑的旧控制台不受影响。
    if len(sys.argv) > 1 and sys.argv[1] == "ensure":
        print(f"✗ 磁盘上的控制台代码有语法错误，没有重启（正在运行的旧进程不受影响）：\n"
              f"  {Path(_e.filename or '?').name} 第 {_e.lineno} 行：{_e.msg}")
        sys.exit(1)
    raise

UNVERSIONED = "未归版"

# ───────────── 代码指纹：让「磁盘上的代码比运行中的新」这件事可被发现 ─────────────
# 事故（2026-10）：控制台是长驻进程，启动时把页面和出卡逻辑读进内存；之后改了磁盘上的代码，
# 老进程浑然不觉，agent 又照文档"端口被占用就沿用"，于是所有改动都不生效。
# 做法：进程启动时算一次指纹（它真正 import 进来的 tools/*.py + 配置），之后每次请求都能拿
# 磁盘现状再算一次比对；页面和 `ensure` 命令都靠它判断是否过期。
import hashlib
import os
import subprocess
import urllib.error
import urllib.request


def _code_files() -> list:
    files = [Path(m.__file__).resolve() for m in list(sys.modules.values())
             if getattr(m, "__file__", None) and Path(m.__file__).resolve().parent == HERE]
    cfg = ROOT / "configs" / "gate_review.json"
    if cfg.exists():
        files.append(cfg.resolve())
    return sorted(set(files))


def code_hash() -> str:
    h = hashlib.sha1()
    for f in _code_files():
        try:
            h.update(f.name.encode() + f.read_bytes())
        except OSError:
            pass
    return h.hexdigest()[:8]


STARTED_HASH = code_hash()
STARTED_AT = time.strftime("%Y-%m-%d %H:%M:%S")


# ─────────────────────────── 索引 ───────────────────────────

def _fm(path: Path) -> dict:
    if not path.exists():
        return {}
    t = path.read_text(encoding="utf-8", errors="ignore")
    if not t.startswith("---"):
        return {}
    body = t.split("---", 2)[1]
    out = {}
    for line in body.splitlines():
        m = re.match(r"^([a-z_]+):\s*(.*)$", line)
        if m:
            out[m.group(1)] = m.group(2).split("#")[0].strip()
    return out


_GENERIC_H1 = ("测试用例", "结构化解构层")


def _title(d: Path) -> str:
    # 优先用产物的 H1；还没跑过产物的任务只有 state.md，取它「# 任务」段的加粗标题
    for f in ("cases.md", "decomp.md"):
        p = d / f
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith("# "):
                raw = line[2:].strip()
                t = re.sub(r"^(测试用例|结构化解构层)\s*[—\-–]+\s*", "", raw)
                # agent 忘了写副标题（H1 只剩泛用模板词本身）——不能就地返回，
                # 否则多个任务会撞出同一个看不出区别的标题；退回 state.md 拿真实任务名
                if t.strip() not in _GENERIC_H1:
                    return t[:60]
                break
    st = d / "state.md"
    if st.exists():
        lines = st.read_text(encoding="utf-8", errors="ignore").splitlines()
        for i, l in enumerate(lines):
            if l.strip() == "# 任务":
                for nx in lines[i + 1:i + 6]:
                    m = re.match(r"^\*\*(.+?)\*\*\s*$", nx.strip())
                    if m:
                        return m.group(1)[:60]
                break
    return d.name


def discover() -> dict:
    """扫 artifacts/ 得到 版本 → 需求 → 可用视图。只读。"""
    groups: "OrderedDict[str, list]" = OrderedDict()
    for d in sorted(ROOT.glob("artifacts/20*"), reverse=True):
        if not d.is_dir():
            continue
        state, decomp, cases = d / "state.md", d / "decomp.md", d / "cases.md"
        if not (state.exists() or decomp.exists() or cases.exists()):
            continue
        sfm, dfm, cfm = _fm(state), _fm(decomp), _fm(cases)
        views, badges = [], []
        cc = None                     # 用例台账只解析一次，解构清单与执行台共用
        if cases.exists():
            try:
                cc = case_console.build(d.name)
            except Exception:
                cc = None

        if decomp.exists():
            views.append("decomp")
            try:
                ds = decomp_console.build(d.name, cc)["summary"]
                if ds["gaps"]:
                    badges.append({"k": "gap", "t": f"设计缺口 {ds['gaps']}"})
                if cases.exists() and ds["uncovered"]:
                    badges.append({"k": "warn", "t": f"未覆盖 {ds['uncovered']} 行"})
            except Exception as e:
                badges.append({"k": "warn", "t": f"解构表解析失败: {type(e).__name__}"})
            views.append("gate0")
            try:
                st = dfm.get("status", "")
                n_cards = len(build_cards(d.name)["cards"]) if st != "" else 0
                answered = len(load_verdicts(d.name))
                if st == "pending_review":
                    badges.append({"k": "gate", "t": f"需求拆解待审 {answered}/{n_cards}"})
                elif n_cards:
                    badges.append({"k": "ok", "t": f"需求拆解已审 {answered}/{n_cards}"})
            except Exception as e:
                badges.append({"k": "warn", "t": f"decomp 解析失败: {type(e).__name__}"})

        if cases.exists():
            views.append("cases")
            try:
                if cc is None:
                    cc = case_console.build(d.name)
                cs = cc["cases"]
                ok = sum(1 for c in cs if c["auto_status"] in ("passed", "xfailed") or c["manual_status"] == "pass")
                dv = sum(1 for c in cs if c["divergent"])
                badges.append({"k": "run", "t": f"执行 {ok}/{len(cs)}"})
                if dv:
                    badges.append({"k": "warn", "t": f"分歧 {dv}"})
                if cfm.get("status") == "pending_review":
                    badges.append({"k": "gate", "t": "用例待审"})
            except Exception as e:
                badges.append({"k": "warn", "t": f"cases 解析失败: {type(e).__name__}"})

        ver = sfm.get("version") or dfm.get("version") or UNVERSIONED
        groups.setdefault(ver, []).append({
            "task_id": d.name,
            "title": _title(d),
            "status": sfm.get("status", "—"),
            "current_step": sfm.get("current_step", ""),
            "views": views,
            "badges": badges,
        })
    # 有版本的排前面，未归版垫底
    ordered = OrderedDict((k, v) for k, v in groups.items() if k != UNVERSIONED)
    if UNVERSIONED in groups:
        ordered[UNVERSIONED] = groups[UNVERSIONED]
    return {"versions": [{"version": k, "tasks": v} for k, v in ordered.items()],
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S")}


def cmd_index() -> None:
    d = discover()
    for g in d["versions"]:
        print(f"\n▌{g['version']}")
        for t in g["tasks"]:
            b = " ".join(f"[{x['t']}]" for x in t["badges"]) or "—"
            print(f"   {t['task_id']:<42}{t['title'][:34]:<36}{b}")
    print(f"\n共 {sum(len(g['tasks']) for g in d['versions'])} 个需求，"
          f"{len(d['versions'])} 个版本分组")


# ─────────────────────────── server ───────────────────────────

class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        raw = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")

    def do_GET(self):
        p = urlparse(self.path).path
        try:
            if p == "/":
                return self._send(200, PAGE.replace("__BUILD__", STARTED_HASH), "text/html; charset=utf-8")
            if p == "/api/version":
                disk = code_hash()
                return self._json({"started": STARTED_HASH, "disk": disk, "stale": disk != STARTED_HASH,
                                   "pid": os.getpid(), "started_at": STARTED_AT, "root": str(ROOT)})
            if p == "/api/index":
                return self._json(discover())
            m = re.match(r"^/api/(gate0|cases|decomp)/(.+)$", p)
            if m:
                kind, task = m.group(1), m.group(2)
                if kind == "decomp":
                    d = decomp_console.build(task)
                    if not d.get("error"):
                        d["fills"] = gap_fill.load(task)      # 「我来补充」已填的答案，按行 key 存
                    return self._json(d)
                return self._json(build_cards(task) if kind == "gate0" else case_console.build(task))
            m = re.match(r"^/api/verdicts/(.+)$", p)
            if m:
                return self._json(load_verdicts(m.group(1)))
            # 设计图缩略图（决策卡「需求原文」区引用 img_NN 时用）。只放行 ui_design/ 下的图片，
            # 文件名只认 [\w.-]，防止 ../ 跳出任务目录
            m = re.match(r"^/asset/([^/]+)/ui_design/([\w.\-]+)$", p)
            if m:
                f = paths(m.group(1))["dir"] / "ui_design" / m.group(2)
                ctype = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                         ".webp": "image/webp", ".gif": "image/gif"}.get(f.suffix.lower())
                if ctype and f.is_file():
                    return self._send(200, f.read_bytes(), ctype)
                return self._json({"error": "not found"}, 404)
        except FileNotFoundError as e:
            return self._json({"error": f"文件不存在: {e}"}, 404)
        except Exception as e:
            return self._json({"error": f"{type(e).__name__}: {e}"}, 500)
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        p = urlparse(self.path).path
        n = int(self.headers.get("Content-Length", 0))
        try:
            payload = json.loads(self.rfile.read(n) or b"{}")
        except Exception as e:
            return self._json({"error": str(e)}, 400)

        m = re.match(r"^/api/verdicts/(.+)$", p)
        if m:
            task, cid = m.group(1), payload.get("cid")
            if not cid:
                return self._json({"error": "cid required"}, 400)
            data = load_verdicts(task)
            if payload.get("clear"):
                data.pop(cid, None)
            else:
                data[cid] = {k: payload.get(k, "") for k in ("choice", "value", "note")}
                data[cid]["answered_at"] = payload.get("answered_at", "")
                if payload.get("rowvals"):
                    data[cid]["rowvals"] = payload["rowvals"]
            save_verdicts(task, data)
            return self._json({"ok": True, "count": len(data)})

        m = re.match(r"^/api/gapfill/(.+)$", p)
        if m:
            if not payload.get("gid"):
                return self._json({"error": "gid required"}, 400)
            data = gap_fill.save_one(m.group(1), payload["gid"], payload)
            return self._json({"ok": True, "fill": data.get(payload["gid"])})

        m = re.match(r"^/api/case/(.+)$", p)
        if m:
            task, op = m.group(1), payload.get("op")
            try:
                if op == "update":
                    r = cases_edit.update(task, payload["case_id"], payload.get("changes") or {})
                elif op == "create":
                    r = cases_edit.create(task, payload.get("cells") or {})
                elif op == "delete":
                    r = cases_edit.delete(task, payload["case_id"], payload.get("reason", ""))
                elif op == "confirm":
                    r = cases_edit.confirm(task)
                else:
                    return self._json({"error": f"未知 op: {op}"}, 400)
                return self._json(r)
            except Exception as e:
                return self._json({"error": f"{type(e).__name__}: {e}"}, 400)

        m = re.match(r"^/api/manual/(.+)$", p)
        if m:
            task = m.group(1)
            if not payload.get("case_id") or payload.get("status") not in case_console.MANUAL_STATUS:
                return self._json({"error": "case_id 与合法 status 必填"}, 400)
            case_console.append_manual({
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "case_id": payload["case_id"],
                "status": payload["status"],
                "note": (payload.get("note") or "")[:2000],
                "actual": (payload.get("actual") or "")[:2000],
                "by": payload.get("by") or "user",
                "task_id": task,
            })
            return self._json({"ok": True})

        self._json({"error": "not found"}, 404)

    def log_message(self, *a):
        pass


PAGE = r"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>QA 控制台</title><style>
:root{--bg:#f4f6f7;--surf:#fff;--surf2:#e9eef1;--ink:#121b23;--ink2:#47555f;--ink3:#6d7c86;
--rule:#d2dae0;--ok:#0f6f5c;--bad:#a03127;--warn:#8f6304;--gate:#9c6a05;--gap:#6b5aa6;--info:#1c5f86}
@media(prefers-color-scheme:dark){:root{--bg:#0d1319;--surf:#151d24;--surf2:#1b252d;--ink:#e5ecf1;
--ink2:#9aaab5;--ink3:#788994;--rule:#2a3943;--ok:#46b39b;--bad:#e0796d;--warn:#dba62c;--gate:#dca62c;--gap:#a693e0;--info:#5fa8d3}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.65 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif}
.mono{font-family:"IBM Plex Mono",ui-monospace,Menlo,Consolas,monospace}
header{position:sticky;top:0;z-index:9;background:var(--surf);border-bottom:1px solid var(--rule);padding:9px 20px}
.nav{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.nav h1{font-size:14.5px;margin:0;font-weight:600;cursor:pointer}
.nav h1:hover{color:var(--info)}
select,button{font:inherit;font-size:13px;cursor:pointer;border:1px solid var(--rule);background:var(--surf);
color:var(--ink);border-radius:5px;padding:4px 9px}
button:hover,select:hover{border-color:var(--ink3)}
button.on{background:var(--ink);color:var(--bg);border-color:var(--ink)}
.nav select{max-width:100%;text-overflow:ellipsis}
.sep{color:var(--ink3)}
.kpi{display:flex;gap:14px;flex-wrap:wrap;margin-left:auto;font-size:12px;align-items:center}
.helpbtn{font-size:12px;padding:4px 10px}
.vlab{font-size:12px;color:var(--ink2);margin:4px 0 6px}
textarea.ans{min-height:64px}
.mask{position:fixed;inset:0;background:rgba(20,20,20,.45);display:none;align-items:flex-start;justify-content:center;z-index:50;padding:48px 16px;overflow:auto}
.mask.on{display:flex}
.dlg{background:var(--surf);border-radius:10px;max-width:720px;width:100%;padding:24px 28px;box-shadow:0 12px 40px rgba(0,0,0,.25);line-height:1.7;font-size:14px}
.dlg h3{margin:0 0 6px;font-size:18px}
.dlg h4{margin:18px 0 6px;font-size:15px}
.dlg .lead{color:var(--ink2);margin:0 0 12px}
.dlg table{width:100%;border-collapse:collapse;font-size:13px;margin:6px 0}
.dlg td,.dlg th{border:1px solid var(--rule);padding:8px 10px;vertical-align:top;text-align:left}
.dlg th{background:var(--surf2);font-weight:600;white-space:nowrap}
.dlg td:first-child{white-space:nowrap;font-weight:600}
.dlg ol{padding-left:20px;margin:6px 0}
.dlg .x{float:right}
.kpi b{font-variant-numeric:tabular-nums}
.fil{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px}
main{max-width:1240px;margin:0 auto;padding:18px 20px 60px}
.pill{font-size:11px;padding:1.5px 7px;border-radius:3px;border:1px solid currentColor;white-space:nowrap;flex:none}
.k-gate{color:var(--gate)}.k-ok{color:var(--ok)}.k-run{color:var(--info)}.k-warn{color:var(--warn)}
/* 索引 */
.vgrp{margin-bottom:26px}
.vgrp h2{font-size:13px;margin:0 0 9px;color:var(--ink3);letter-spacing:.08em;font-weight:500;
border-bottom:1px solid var(--rule);padding-bottom:6px}
.tk{background:var(--surf);border:1px solid var(--rule);border-radius:7px;padding:12px 14px;margin-bottom:8px;
display:flex;align-items:center;gap:11px;flex-wrap:wrap}
.tk .tt{flex:1;min-width:220px}
.tk .tid{font-family:"IBM Plex Mono",monospace;font-size:11.5px;color:var(--ink3)}
/* 决策卡 */
.card{background:var(--surf);border:1px solid var(--rule);border-radius:8px;padding:20px 22px;margin-bottom:16px}
.q{font-size:17px;line-height:1.6;margin:2px 0 8px;font-weight:500}
.q code,.prdr code{background:var(--surf2);padding:1px 6px;border-radius:3px}
.hint{color:var(--ink2);font-size:13px;margin:0 0 16px}
.prd{border:1px solid var(--rule);border-radius:6px;padding:12px 14px;margin-bottom:14px;background:var(--bg)}
.prdt{font-size:11px;letter-spacing:.1em;color:var(--ink3);margin-bottom:9px}
.prdb{padding:8px 10px;border-radius:4px;margin-bottom:6px;background:var(--surf)}
.prdb.lk{box-shadow:inset 0 0 0 1px var(--gate)}
.prdh{display:flex;gap:10px;font-size:11px;color:var(--ink3);margin-bottom:3px}
.prdl{font-size:12px;color:var(--ink2);margin-bottom:4px}
.bgd{font-size:14px;line-height:1.7;color:var(--ink);background:var(--surf2);border-radius:6px;padding:10px 14px;margin:0 0 14px}
.srcs{display:grid;gap:10px;margin-bottom:14px}
.srcs.vs{grid-template-columns:repeat(auto-fit,minmax(260px,1fr))}
.src{border:1px solid var(--rule);border-radius:6px;padding:10px 12px;background:var(--bg)}
.src .sh{display:flex;justify-content:space-between;gap:8px;font-size:12px;color:var(--ink3);margin-bottom:6px}
.src .sh b{color:var(--ink);font-weight:600}
.src pre{white-space:pre-wrap;word-break:break-word;margin:0;font:13px/1.65 inherit;color:var(--ink)}
.thumbs{display:flex;gap:8px;flex-wrap:wrap;margin-top:8px}
.thumbs a{display:block;border:1px solid var(--rule);border-radius:4px;overflow:hidden;background:var(--surf)}
.thumbs img{display:block;max-width:220px;max-height:150px;object-fit:contain}
.thumbs span{display:block;font-size:11px;color:var(--ink3);padding:2px 6px}
.opt em.dflt{display:inline-block;width:auto;margin:0 0 0 8px;vertical-align:1px;font-size:11px;padding:1px 6px;border-radius:3px;
background:color-mix(in srgb,var(--gate) 16%,transparent);color:var(--ink2);font-style:normal}
.prdr{margin:0;font-family:"IBM Plex Mono",monospace;font-size:13px;white-space:pre-wrap;word-break:break-word}
details summary{cursor:pointer;color:var(--ink3);font-size:12px;padding:4px 0}
.ev{background:var(--surf2);border-radius:6px;padding:12px 14px;margin:8px 0}
.ev div{margin-bottom:7px;font-size:13px;word-break:break-word}
.ev b{color:var(--ink3);font-weight:500;font-size:11px}
.evg+.evg{border-top:1px dashed var(--rule);padding-top:9px;margin-top:4px}
.evh{font-size:12px;font-weight:600;color:var(--ink2);margin-bottom:6px}
.ref{background:color-mix(in srgb,var(--gate) 10%,transparent);border-radius:3px;padding:0 2px}
.opts{display:flex;flex-direction:column;gap:8px;margin:14px 0}
.opt{display:flex;gap:10px;text-align:left;padding:11px 14px;width:100%;align-items:flex-start}
.opt.sel{border-color:var(--ok);box-shadow:inset 0 0 0 1px var(--ok)}
.opt kbd{border:1px solid var(--rule);border-radius:3px;padding:0 5px;font-size:11px;color:var(--ink3);flex:none}
.opt em{color:var(--ink3);font-style:normal;font-size:12px;display:block}
input,textarea{width:100%;font:inherit;font-size:13px;padding:8px 10px;border:1px solid var(--rule);
border-radius:5px;background:var(--bg);color:var(--ink);margin-bottom:8px}
textarea{min-height:46px;resize:vertical}
/* 执行台 */
.case{background:var(--surf);border:1px solid var(--rule);border-radius:7px;margin-bottom:10px;overflow:hidden}
.case.div{border-color:var(--warn);box-shadow:inset 3px 0 0 var(--warn)}
.hd{display:flex;align-items:center;gap:10px;padding:11px 14px;cursor:pointer;flex-wrap:wrap}
.hd:hover{background:var(--surf2)}
.cid{font-family:"IBM Plex Mono",monospace;font-size:12.5px;color:var(--ink2);flex:none}
.ttl{flex:1;min-width:230px}
.p-P0{color:var(--bad)}.p-P1{color:var(--warn)}.p-P2{color:var(--ink3)}
.s-passed,.s-pass,.s-xfailed{color:var(--ok)}.s-failed,.s-fail,.s-error,.s-xpassed_strict{color:var(--bad)}
.s-blocked,.s-xpassed{color:var(--warn)}.s-skip,.s-skipped{color:var(--ink3)}.s-none{color:var(--ink3);opacity:.6}
.gapc{color:var(--gap)}
.bd{border-top:1px solid var(--rule);padding:14px;display:none}
.case.open .bd{display:block}
.meta{display:grid;grid-template-columns:74px 1fr;gap:7px 12px;font-size:13px;margin-bottom:14px}
.meta dt{color:var(--ink3);font-size:11.5px}.meta dd{margin:0}
.meta code{background:var(--surf2);padding:1px 5px;border-radius:3px;font-size:12px}
.lanes{display:grid;grid-template-columns:1fr 1fr;gap:12px}
@media(max-width:860px){.lanes{grid-template-columns:1fr}}
.lane{border:1px solid var(--rule);border-radius:6px;padding:11px 13px;background:var(--bg)}
.lane h4{margin:0 0 9px;font-size:12px;color:var(--ink3);letter-spacing:.05em;font-weight:500;
display:flex;justify-content:space-between}
.run{border-top:1px dashed var(--rule);padding:7px 0;font-size:12.5px}
.run:first-of-type{border-top:none}
.run .t{color:var(--ink3);font-size:11px;font-family:"IBM Plex Mono",monospace}
.run pre,.cmd{margin:4px 0 0;white-space:pre-wrap;word-break:break-word;font-size:11.5px;
background:var(--surf2);padding:6px 8px;border-radius:4px;max-height:130px;overflow:auto;
font-family:"IBM Plex Mono",monospace}
.empty{color:var(--ink3);font-size:12.5px;padding:6px 0}
.mbtns{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:8px}
.editbox{border:1px solid var(--gate);border-radius:6px;padding:14px;background:var(--bg)}
.ehd{font-size:12px;color:var(--gate);margin-bottom:10px}
.erow{display:flex;gap:8px;align-items:center;margin-bottom:10px}
.erow2{margin-bottom:9px}
.erow2 label,.erow label{font-size:11.5px;color:var(--ink3);display:block;margin-bottom:3px}
.erow label{margin:0 4px 0 0;display:inline}
.editbox input,.editbox textarea,.editbox select{margin:0;font-size:13px}
.editbox textarea{min-height:auto}
.divmsg{background:var(--surf2);border-left:3px solid var(--warn);padding:8px 11px;font-size:12.5px;
margin-bottom:12px;border-radius:0 4px 4px 0}
.err{background:var(--surf);border:1px solid var(--bad);color:var(--bad);border-radius:7px;padding:14px 18px}
#fresh{background:#fff4d6;color:#5b4300;border-bottom:1px solid #e8d49a;padding:8px 20px;font-size:13px;display:flex;gap:12px;align-items:center;flex-wrap:wrap}
#fresh:empty{display:none}
#fresh code{background:rgba(0,0,0,.07);padding:1px 6px;border-radius:3px}
#fresh.bad{background:#fde8e6;color:#7a1f16;border-color:#efb7b1}
.cbar{background:#fff4d6;color:#5b4300;border:1px solid #e8d49a;border-radius:8px;padding:12px 16px;margin-bottom:14px;font-size:13.5px;line-height:1.7}
.cbar b{font-weight:600}
.cbar ul{margin:6px 0 0;padding-left:20px;color:#6b5513}
.k-gap{color:var(--gap)}
/* 解构清单 */
:root{--d-conf:#0f6f5c;--d-infer:#6b5aa6;--d-miss:#b26a00;--d-conflict:#b3261e;--d-ban:#8a96a0;
--v-pass:#0f6f5c;--v-fail:#b3261e;--v-err:#c2410c;--v-defect:#6b5aa6;--v-notrun:#9aa6ae;--v-uncov:#d9a520}
@media(prefers-color-scheme:dark){:root{--d-conf:#46b39b;--d-infer:#a693e0;--d-miss:#e3a33a;--d-conflict:#ef7a6e;--d-ban:#6f7f8a;
--v-pass:#46b39b;--v-fail:#ef7a6e;--v-err:#f0915a;--v-defect:#a693e0;--v-notrun:#56656f;--v-uncov:#e9c046}}
.dsec{background:var(--surf);border:1px solid var(--rule);border-radius:8px;padding:16px 18px;margin-bottom:16px}
.dsec h3{margin:0 0 4px;font-size:15px}
.dsec .sub{color:var(--ink3);font-size:12.5px;margin:0 0 12px}
.dmeta{display:flex;gap:16px;flex-wrap:wrap;font-size:12.5px;color:var(--ink2);margin-bottom:14px}
.dmeta b{color:var(--ink);font-weight:600}
.ov{width:100%;border-collapse:collapse;font-size:13px}
.ov th{text-align:left;font-weight:500;color:var(--ink3);font-size:11.5px;padding:0 8px 6px;white-space:nowrap}
.ov td{padding:7px 8px;border-top:1px solid var(--rule);vertical-align:middle}
.ov tr.click{cursor:pointer}.ov tr.click:hover td{background:var(--surf2)}
.ov tr.sel td{background:color-mix(in srgb,var(--info) 9%,transparent)}
.ov .num{font-variant-numeric:tabular-nums;text-align:right;width:44px}
.bar{display:flex;height:14px;border-radius:3px;overflow:hidden;background:var(--surf2);min-width:150px}
.bar i{display:block;height:100%}
.bar i+i{box-shadow:inset 1px 0 0 var(--surf)}
.legend{display:flex;gap:12px;flex-wrap:wrap;font-size:11.5px;color:var(--ink2);margin-top:8px}
.legend span{display:inline-flex;align-items:center;gap:5px}
.legend i{width:10px;height:10px;border-radius:2px;display:inline-block}
.xt{border-collapse:collapse;font-size:12.5px}
.xt th{font-weight:500;color:var(--ink3);font-size:11.5px;padding:4px 8px;text-align:center;white-space:nowrap}
.xt th.rh{text-align:right}
.xt td{width:72px;height:34px;text-align:center;border:1px solid var(--surf);font-variant-numeric:tabular-nums;cursor:pointer;border-radius:3px}
.xt td.z{color:var(--ink3);opacity:.35;cursor:default}
.xt td.hot{outline:2px solid var(--ink);outline-offset:-2px}
.xt td:not(.z):hover{outline:2px solid var(--info);outline-offset:-2px}
.chips{display:flex;gap:6px;flex-wrap:wrap}
.drow{border-top:1px solid var(--rule);padding:10px 2px}
.drow:first-child{border-top:none}
.drow .h{display:flex;gap:10px;align-items:flex-start;cursor:pointer}
.drow .rid{font-family:"IBM Plex Mono",monospace;font-size:12px;color:var(--ink2);flex:none;width:62px;padding-top:1px}
.drow .mt{flex:1;min-width:0}
.drow .tags{display:flex;gap:6px;flex:none;align-items:center}
.tg{font-size:11px;padding:1px 7px;border-radius:3px;white-space:nowrap;color:#fff}
.tg.o{background:transparent;border:1px solid currentColor}
.cs{display:flex;gap:5px;flex-wrap:wrap;margin-top:6px}
.cs a{font-family:"IBM Plex Mono",monospace;font-size:11px;padding:1px 6px;border-radius:3px;text-decoration:none;
border:1px solid var(--rule);color:var(--ink2);background:var(--bg)}
.cs a b{font-weight:500}
.dd{margin:8px 0 2px 72px;display:grid;grid-template-columns:112px 1fr;gap:5px 12px;font-size:12.5px}
.dd dt{color:var(--ink3);font-size:11.5px}.dd dd{margin:0;word-break:break-word}
.dd code{background:var(--surf2);padding:0 4px;border-radius:3px}
@media(max-width:700px){.dd{margin-left:0;grid-template-columns:1fr}.drow .rid{width:auto}.drow .h{flex-wrap:wrap}}
.mx{overflow:auto;margin:6px 0 14px}
.mx table{border-collapse:collapse;font-size:12px}
.mx th,.mx td{border:1px solid var(--rule);padding:6px 8px;vertical-align:top;min-width:96px}
.mx th{background:var(--surf2);font-weight:500;white-space:nowrap}
.mx td.m-ok{background:color-mix(in srgb,var(--d-conf) 12%,transparent)}
.mx td.m-inf{background:color-mix(in srgb,var(--d-infer) 14%,transparent)}
.mx td.m-miss{background:color-mix(in srgb,var(--d-miss) 18%,transparent)}
.mx td.m-ban{background:var(--surf2);color:var(--ink3)}
.debt{display:grid;grid-template-columns:76px 1fr auto;gap:4px 12px;padding:8px 0;border-top:1px solid var(--rule);font-size:13px}
.debt:first-child{border-top:none}
.debt .di{font-family:"IBM Plex Mono",monospace;font-size:12px;color:var(--ink2)}
.debt .ds{font-size:12px;color:var(--ink3);max-width:340px}
.debt.closed{opacity:.6}
.srch{max-width:260px;margin:0}
/* 解构清单 · 按模块看（默认视角：模块 → 功能 → 控件 / 字段 / 规则 / 缺口） */
.intro h3{font-size:16px;margin:0 0 6px}
.intro p{margin:0;color:var(--ink2);font-size:13px}
.legacy{border-left:3px solid var(--d-miss);background:var(--surf2);padding:9px 12px;border-radius:0 5px 5px 0;font-size:13px;margin-top:10px;color:var(--ink2)}
.mod{background:var(--surf);border:1px solid var(--rule);border-radius:8px;margin-bottom:14px;overflow:hidden}
.mh{display:flex;gap:10px;align-items:baseline;flex-wrap:wrap;padding:14px 18px;cursor:pointer;background:var(--surf2)}
.mh:hover{filter:brightness(.98)}
.mh .mid{font-family:"IBM Plex Mono",monospace;font-size:12px;color:var(--ink3)}
.mh b{font-size:15.5px}
.mh .ms{color:var(--ink2);font-size:13px;flex-basis:100%;margin-top:2px}
.mh .me{color:var(--ink3);font-size:12px;flex-basis:100%}
.cnt{display:flex;gap:6px;flex-wrap:wrap;margin-left:auto;font-size:12px}
.cnt span{background:var(--surf);border:1px solid var(--rule);border-radius:10px;padding:0 8px;color:var(--ink2);white-space:nowrap}
.cnt span.g{color:var(--d-miss);border-color:color-mix(in srgb,var(--d-miss) 45%,transparent)}
.ft{border-top:1px solid var(--rule)}
.fh{display:flex;gap:10px;align-items:center;flex-wrap:wrap;padding:11px 18px;cursor:pointer}
.fh:hover{background:var(--bg)}
.fh .ar{color:var(--ink3);width:12px;flex:none}
.fh .fid{font-family:"IBM Plex Mono",monospace;font-size:12px;color:var(--ink3);flex:none}
.fh .fn{flex:1;min-width:220px;font-weight:500}
.fh .fr{font-size:12px;color:var(--ink3)}
.fb{padding:4px 18px 16px 46px}
@media(max-width:700px){.fb{padding:4px 12px 14px 14px}.mh,.fh{padding-left:12px;padding-right:12px}}
.sect{font-size:11.5px;letter-spacing:.06em;color:var(--ink3);margin:12px 0 4px;font-weight:600}
.sect.g{color:var(--d-miss)}
.scr{font-size:12px;color:var(--ink2);margin:8px 0 2px;padding-left:2px}
.cl{display:flex;gap:10px;align-items:flex-start;padding:6px 0;border-top:1px dashed var(--rule)}
.cl:first-of-type{border-top:none}
.cl .rid{font-family:"IBM Plex Mono",monospace;font-size:11.5px;color:var(--ink3);width:42px;flex:none;padding-top:2px}
.cl .ct{flex:1;min-width:0;font-size:13.5px;line-height:1.6}
.cl .ct .w{color:var(--info)}
.cl .ct .pr{color:var(--ink3);font-size:12.5px}
.cl .ct .cv{color:var(--ink2);font-size:12.5px}
.cl .ct .miss{color:var(--d-miss)}
.cl .tags{flex:none;display:flex;gap:5px}
.gl{font-size:13px;padding:4px 0;color:var(--ink2)}
.gl b{color:var(--ink);font-weight:500}
.gl .dbt{display:block;color:var(--ink3);font-size:12px;margin-left:2px}
@media(max-width:700px){
.ov tr:first-child{display:none}
.ov tr{display:grid;grid-template-columns:1fr auto;gap:5px 8px;padding:9px 4px;border-top:1px solid var(--rule)}
.ov td{border:none;padding:0}
.ov td:nth-child(3),.ov td:nth-child(4){grid-column:1/-1}
.bar{min-width:0}
.dsec{padding:14px 12px}
main{padding:14px 12px 48px}
}
/* 我来补充（缺口行内表单） */
.gl .fillbtn{margin-left:8px;font-size:12px;padding:1px 8px;color:var(--info);border-color:var(--info)}
.gl .filled{display:block;margin:4px 0 2px 2px;padding:6px 10px;border-left:3px solid var(--ok);background:var(--surf2);border-radius:4px;color:var(--ink);white-space:pre-wrap}
.gl .filled.ask{border-left-color:var(--warn)}.gl .filled.later{border-left-color:var(--rule)}
.gl .filled .fm{color:var(--ink3);font-size:11.5px}
.gl .fform{margin:6px 0 4px 2px;display:flex;flex-direction:column;gap:6px}
.gl .fform textarea{min-height:64px}
.gl .fbtns{display:flex;gap:6px;flex-wrap:wrap;align-items:center}
.gl .fbtns .pri{border-color:var(--ok);color:var(--ok);font-weight:600}
.cnt span.f{color:var(--ok)}
</style></head><body>
<div id="fresh"></div>
<header><div class="nav" id="nav"></div><div class="fil" id="fil"></div></header>
<main id="app">加载中…</main>
<script>
let IDX=null,VIEW='index',TASK=null,DATA=null,F='all',OPEN={},VD={},G0I=0;
const $=s=>document.querySelector(s);
const BUILD='__BUILD__';            // 这个页面是哪一版代码渲染出来的（服务端注入）
// 新鲜度检查：①页面比服务端旧 → 提示刷新；②服务端比磁盘上的代码旧 → 提示重启（进程是长驻的，改了文件它不知道）
async function checkFresh(){
  const box=$('#fresh'); if(!box)return;
  try{
    const v=await (await fetch('/api/version',{cache:'no-store'})).json();
    box.className='';
    if(v.started!==BUILD)box.innerHTML='<span>控制台刚更新过，这个页面还是旧版本。</span><button onclick="location.reload()">刷新页面</button>';
    else if(v.stale)box.innerHTML=`<span>磁盘上的控制台代码已经更新，但正在运行的进程还是旧的——新改动现在<b>不会生效</b>。重启：</span><code>python3 tools/qa_console.py ensure ${location.port}</code>`;
    else box.innerHTML='';
  }catch(e){box.className='bad';box.innerHTML='<span>连不上控制台服务了（可能已停止）。你刚做的选择如果没看到「已存盘」，请重启后再确认一次。</span>'}
}
// 检查点清单没按当前契约写：卡片会难读或缺选项，别让评审人对着它干猜
function contractBar(){
  const c=DATA&&DATA.contract; if(!c||!(DATA.frontmatter||{}).status||DATA.frontmatter.status!=='pending_review')return '';
  const errs=(c.issues||[]).filter(i=>i.level==='error'); if(!errs.length)return '';
  return `<div class="cbar"><b>这份检查点清单没有完全按新版格式写（${errs.length} 处问题）</b>，下面的卡片可能难读，或者缺少具体选项。`+
    `建议先让 test-case-design 按模板（<code>artifacts/templates/decomp.template.md</code>）修订后再审，省得你对着速记猜。`+
    `<details><summary>具体是哪些问题</summary><ul>${errs.map(i=>`<li>${esc(i.msg)}</li>`).join('')}</ul></details></div>`;
}
const esc=s=>(s||'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
const TN={'①':'功能点清单','②':'字段规格表','③':'状态流转表','④':'业务规则表','⑤':'操作流程','⑥':'影响范围','⑦':'控件规格表'};
// 「④-R02, ③-S04」→「业务规则表第 2 条、状态流转表第 4 条」：评审人看不懂编号，agent 对账用的原始编号仍留在 cases.md
const humanRef=s=>(s||'').replace(/([①-⑦])\s*[-:：]?\s*[A-Z]{1,2}(\d{1,3})(?:\s*[–~-]\s*[A-Z]{0,2}(\d{1,3}))?/g,
  (m,t,a,b)=>`${TN[t]}第 ${+a}${b?'–'+(+b):''} 条`).replace(/,\s*/g,'、');
const md=s=>esc(s).replace(/&lt;br\s*\/?&gt;/gi,'<br>').replace(/\*\*([^*]+)\*\*/g,'<b>$1</b>').replace(/`([^`]+)`/g,'<code>$1</code>');
const AS={passed:'通过',failed:'失败',error:'错误',skipped:'跳过',xfailed:'已知缺陷(预期失败)',xpassed:'意外通过',xpassed_strict:'⚠️意外通过(疑似已修复,待复核)'};
const MS={pass:'通过',fail:'失败',blocked:'阻塞',skip:'跳过'};
const VIEWNAME={decomp:'解构清单',gate0:'需求拆解评审',cases:'用例执行'};

function route(){
  const h=location.hash.replace(/^#\/?/,'').split('/');
  if(h[0]==='t'&&h[1]){TASK=decodeURIComponent(h[1]);VIEW=h[2]||'cases'}
  else{VIEW='index';TASK=null}
}
async function boot(){route();IDX=await (await fetch('/api/index')).json();await load()}
window.onhashchange=async()=>{route();FOPEN={};FDRAFT={};OPEN={};F='all';DF={t:null,k:null,c:null,q:'',show:'all'};DOPEN={};TOPEN={};TALL=false;await load()};

async function load(){
  if(VIEW==='index'){DATA=null;return draw()}
  DATA=await (await fetch(`/api/${VIEW}/${encodeURIComponent(TASK)}`)).json();
  if(VIEW==='gate0'&&!DATA.error){
    VD=await (await fetch(`/api/verdicts/${encodeURIComponent(TASK)}`)).json();
    const first=DATA.cards.findIndex(c=>!VD[c.cid]); G0I=first<0?0:first;
  }
  draw();
}
function taskOf(id){for(const g of IDX.versions)for(const t of g.tasks)if(t.task_id===id)return{...t,version:g.version};return null}

function nav(){
  let h=`<h1 onclick="location.hash='#/'" title="控制台代码版本 ${BUILD}">QA 控制台</h1>`;
  if(VIEW==='index'){
    const n=IDX.versions.reduce((a,g)=>a+g.tasks.length,0);
    h+=`<span class="sep">·</span><span class="mono" style="font-size:12px;color:var(--ink3)">${IDX.versions.length} 个版本 / ${n} 个需求</span>`;
  }else{
    const t=taskOf(TASK)||{};
    h+=`<span class="sep">›</span><select onchange="location.hash='#/'">`+
       `<option>${esc(t.version||'')}</option></select>`;
    h+=`<span class="sep">›</span><select onchange="location.hash='#/t/'+encodeURIComponent(this.value)+'/'+'${VIEW}'">`+
       IDX.versions.flatMap(g=>g.tasks).map(x=>
         `<option value="${esc(x.task_id)}"${x.task_id===TASK?' selected':''}>${esc(x.title||x.task_id)}</option>`).join('')+'</select>';
    h+='<span class="sep">›</span>';
    (t.views||[]).forEach(v=>{h+=`<button class="${v===VIEW?'on':''}" onclick="location.hash='#/t/'+encodeURIComponent('${esc(TASK)}')+'/${v}'">${VIEWNAME[v]}</button>`});
  }
  h+='<div class="kpi" id="kpi"></div>';
  $('#nav').innerHTML=h;
}
function draw(){
  nav();$('#fil').innerHTML='';
  if(DATA&&DATA.error)return $('#app').innerHTML=`<div class="err"><b>加载失败</b><br>${esc(DATA.error)}</div>`;
  if(VIEW==='index')return drawIndex();
  if(VIEW==='gate0')return drawGate0();
  if(VIEW==='decomp')return drawDecomp();
  return drawCases();
}
/* ── 索引 ──
   未归版（历史任务）默认折叠：当前在做的版本才该占视线，但不删除入口。 */
let SHOWOLD=false;
function toggleOld(){SHOWOLD=!SHOWOLD;draw()}
function grpHtml(g){
  return `<div class="vgrp"><h2>${esc(g.version)}　·　${g.tasks.length} 个需求</h2>`+
    g.tasks.map(t=>`<div class="tk">`+
      `<div class="tt"><div>${esc(t.title||t.task_id)}</div><div class="tid">${esc(t.task_id)}　${esc(t.current_step||t.status)}</div></div>`+
      t.badges.map(b=>`<span class="pill k-${b.k}">${esc(b.t)}</span>`).join('')+
      t.views.map(v=>`<button onclick="location.hash='#/t/'+encodeURIComponent('${esc(t.task_id)}')+'/${v}'">${VIEWNAME[v]}</button>`).join('')+
      `</div>`).join('')+'</div>';
}
function drawIndex(){
  const cur=IDX.versions.filter(g=>g.version!=='未归版');
  const old=IDX.versions.filter(g=>g.version==='未归版');
  let h=cur.map(grpHtml).join('');
  if(old.length){
    const n=old[0].tasks.length;
    h+=`<div style="margin-top:8px"><button onclick="toggleOld()">`+
       `${SHOWOLD?'▾':'▸'} 未归版历史任务 ${n} 个</button></div>`;
    if(SHOWOLD)h+='<div style="margin-top:12px">'+old.map(grpHtml).join('')+'</div>';
  }
  $('#app').innerHTML=h;
}
/* ── 需求拆解评审 ── */
function g0list(){return F==='all'?DATA.cards:DATA.cards.filter(c=>c.kind===F)}
function drawGate0(){
  const cs=DATA.cards;
  // 只统计「当前队列」里已答的——历史裁决可能有条目现在被自动搁置了，
  // 用全局 VD 条数当分子会出现「已答 21/9」这种反直觉的数字
  const ans=cs.filter(c=>VD[c.cid]).length;
  $('#kpi').innerHTML=`<button class="helpbtn" onclick="showHelp()">ⓘ 这些卡是什么？</button><span>已答 <b>${ans}/${cs.length}</b></span>`+
    (DATA.deferred&&DATA.deferred.length?`<span class="t" style="color:var(--ink3)">（另有 ${DATA.deferred.length} 项已自动搁置）</span>`:'');
  const cnt={};cs.forEach(c=>cnt[c.kind]=(cnt[c.kind]||0)+1);
  $('#fil').innerHTML=['all','conflict','verify','infer','missing'].map(f=>{
    const n=f==='all'?cs.length:(cnt[f]||0);
    const lab=f==='all'?'全部':(cs.find(x=>x.kind===f)||{}).kind_label||f;
    return `<button class="${F===f?'on':''}" onclick="setF('${f}')">${lab} ${n}</button>`}).join('');
  const L=g0list();
  if(!L.length)return $('#app').innerHTML=contractBar()+'<div class="card">这个筛选下没有待决项。</div>';
  if(G0I>=L.length)G0I=L.length-1;
  const c=L[G0I],v=VD[c.cid]||{};
  const blk=b=>`<div class="prdb${b.likely?' lk':''}"><div class="prdh"><span class="mono">需求文档 第 ${b.line} 行</span>`+
    (b.cat?`<span>${esc(b.cat)}${b.side?' › '+esc(b.side):''}</span>`:'')+`</div>`+
    (b.loc?`<div class="prdl">${esc(b.loc)}</div>`:'')+`<pre class="prdr">${esc(b.raw)}</pre></div>`;
  const pl=(c.prd||[]).filter(b=>b.likely),po=(c.prd||[]).filter(b=>!b.likely);
  let h=`<div class="card"><div style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:6px">`+
    // 卡片编号（DBT-xxx）对评审人没有意义，不显示；悬停在类型标签上可看，方便跟 agent 对话时引用
    `<span class="pill k-gate"${c.dbt?` title="编号 ${c.dbt}"`:''}>${c.kind_label}</span>`+
    (c.row_count>1?`<span class="pill k-warn">1 个答案决定 ${c.row_count} 行</span>`:'')+
    (VD[c.cid]?'<span class="pill k-ok">已答（已存盘）</span>':'')+
    '<span id="svtag"></span>'+'</div>';
  h+=`<p class="q">${md(c.question)}</p>`+(c.background?`<p class="bgd">${md(c.background)}</p>`:'')+
     `<p class="hint">${c.kind_hint}　·　这个答案会更新 ${c.row_count} 处解构表内容</p>`;
  const S=c.sources||[],T=S.filter(x=>x.type==='text'),I=S.filter(x=>x.type==='image');
  const th=(f,cap)=>`<a href="/asset/${encodeURIComponent(TASK)}/ui_design/${encodeURIComponent(f)}" target="_blank" title="点击看大图">`+
     `<img src="/asset/${encodeURIComponent(TASK)}/ui_design/${encodeURIComponent(f)}" loading="lazy"><span>${esc(cap)}</span></a>`;
  if(T.length||I.length){
    h+=`<div class="prdt">需求原文${c.kind==='conflict'&&T.length>=2?'（冲突双方并排）':''}</div>`;
    h+=`<div class="srcs${c.kind==='conflict'&&T.length>=2?' vs':''}">`+T.map(x=>
       `<div class="src"><div class="sh"><b>${esc(x.ref)}</b><span class="mono">第 ${x.line} 行</span></div><pre>${esc(x.text)}</pre>`+
       ((x.image_files||[]).length?`<div class="thumbs">${x.image_files.map(f=>th(f,f)).join('')}</div>`:'')+'</div>').join('')+'</div>';
    if(I.length)h+=`<div class="thumbs" style="margin:-4px 0 14px">${I.map(x=>th(x.file,x.ref)).join('')}</div>`;
  }
  if(c.prd&&c.prd.length){h+='<div class="prd"><div class="prdt">需求文档原文</div>'+(pl.length?pl.map(blk).join(''):blk(c.prd[0]));
    const rest=pl.length?po:c.prd.slice(1);
    if(rest.length)h+=`<details><summary>另有 ${rest.length} 处也提到这个字符串</summary>${rest.map(blk).join('')}</details>`;
    h+='</div>'}
  // 证据按条目分组、标题用人话（"业务规则表 · 第 3 条"）。
  // 兜底：契约要求人读列不用代号，万一漏网，把「R02 / ④-R02」就地替换成它指的那条内容，评审人不看编号
  const REFS=c.refs||{};
  const refx=html=>html.replace(/(^|>)([^<]+)/g,(m,a,t)=>a+t.replace(/([①②③④⑤⑥⑦]\s*[-:：]\s*)?\b([A-Z]{1,2}\d{2,3})\b/g,
     (all,pre,id)=>REFS[id]?`<span class="ref" title="${esc(REFS[id].label)}">「${esc(REFS[id].gist)}」这条</span>`:all));
  const groups=[];c.evidence.forEach(e=>{let g=groups.find(x=>x.row===e.row);if(!g)groups.push(g={row:e.row,label:e.row_label||e.row,items:[]});g.items.push(e)});
  h+=`<details><summary>agent 的拆解依据（检查点表里的相关条目，${groups.length} 条）</summary><div class="ev">`+
     groups.map(g=>`<div class="evg"><div class="evh">${esc(g.label)}</div>`+
       g.items.map(e=>`<div><b>${esc(e.col)}</b>　${refx(md(e.val))}</div>`).join('')+'</div>').join('')+
     '</div></details>';
  h+='<div class="opts">'+c.options.map((o,i)=>
     `<button class="opt ${v.choice===o.id?'sel':''}" onclick="pick('${c.cid}','${o.id.replace(/'/g,"\\'")}')"><kbd>${i+1}</kbd>`+
     `<span>${esc(o.label)}${o.default?'<em class="dflt">agent 默认假设</em>':''}${o.hint?`<em>${esc(o.hint)}</em>`:''}</span></button>`).join('')+'</div>';
  const needVal=NEEDVAL.has(v.choice);
  if(needVal&&c.per_row){const rv=v.rowvals||{};
    h+='<div class="vlab">逐行写下正确的内容（不确定的行留空）</div>'+c.rows.map(r=>`<div style="display:flex;gap:8px;align-items:center;margin-bottom:6px">`+
      `<code class="mono" style="flex:none;min-width:112px;font-size:12px">${r.uid}</code>`+
      `<input class="rv" data-row="${r.uid}" value="${esc(rv[r.uid]||'')}" placeholder="这一行正确的内容是什么？" oninput="sync('${c.cid}')" onblur="reSave('${c.cid}')" style="margin:0"></div>`).join('');
  }else if(needVal){h+=`<div class="vlab">你的答案</div><textarea id="val" class="ans" oninput="sync('${c.cid}')" onblur="reSave('${c.cid}')" `+
     `placeholder="用一两句话写下正确的规则或取值，比如「只有企业账号必填，个人账号选填」。界面文案请照原样写，大小写、标点都别改">${esc(v.value)}</textarea>`}
  h+=`<textarea id="note" oninput="sync('${c.cid}')" onblur="reSave('${c.cid}')" placeholder="备注（可选）：补充例外情况、适用范围或你的理由。agent 回填检查点表、生成用例时都会照着它来，写清楚能少一轮来回">${esc(v.note)}</textarea>`;
  h+=`<div style="display:flex;gap:8px;align-items:center"><button onclick="g0save()">下一张未答 ⏎</button>`+
     `<button onclick="g0jump(-1)">← 上一张</button><button onclick="g0jump(1)">下一张 →</button>`+
     (VD[c.cid]?`<button onclick="g0clear('${c.cid}')">清除本卡</button>`:'')+
     `<span class="t mono" style="margin-left:auto">${G0I+1} / ${L.length}　选项点一下即存盘 · 1-9 选项 · j/k 翻卡</span></div></div>`;
  const df=DATA.deferred||[];
  if(df.length){
    h+=`<div style="margin-top:16px"><button onclick="toggleDeferred()">`+
       `${SHOW_DEF?'▾':'▸'} 另有 ${df.length} 项已自动判定为「提测时看一眼就知道」，无需现在处理</button></div>`;
    if(SHOW_DEF){
      h+='<div style="margin-top:8px">'+df.map(c=>
        `<div class="case"><div class="hd"><span class="pill k-${c.kind}">${c.kind_label}</span>`+
        `<span class="ttl" style="font-size:13px;color:var(--ink2)">${md(c.question)}</span>`+
        `<button onclick="promote('${c.cid}')">拉回来自己答</button></div></div>`
      ).join('')+'</div>';
    }
  }
  $('#app').innerHTML=contractBar()+h;
}
let SHOW_DEF=false;
function toggleDeferred(){SHOW_DEF=!SHOW_DEF;draw()}
function promote(cid){
  const c=(DATA.deferred||[]).find(x=>x.cid===cid);
  if(!c)return;
  DATA.deferred=DATA.deferred.filter(x=>x.cid!==cid);
  DATA.cards.push(c);
  const L=g0list();G0I=L.findIndex(x=>x.cid===cid);if(G0I<0)G0I=0;
  draw();
}
function setF(f){F=f;G0I=0;draw()}
// 存盘反馈只就地改这一个小标签，**绝不 draw()**：重绘会重建输入框，用户正在打字的那个框
// 会丢焦点（实锤：选完选项 1.5s 内点进备注框、或从补充框切到备注框触发失焦存盘，都会被重绘打断）
let SVT=null;
function flashSaved(){
  const t=$('#svtag'); if(!t)return;
  t.innerHTML='<span class="pill k-run">✓ 已存盘</span>';
  clearTimeout(SVT); SVT=setTimeout(()=>{const t2=$('#svtag'); if(t2)t2.innerHTML=''},1500);
}
// 需要用户自己写答案的选项：只有选了它们，才出现「你的答案」输入框
const NEEDVAL=new Set(['answer','fix']);
// 把当前 DOM 里的输入采集进 VD —— **必须在任何 draw() 之前调用**，
// 否则重绘会按 VD 重建输入框、把用户刚打的字冲掉（实锤过的 bug）
function grab(cid){
  const v=(VD[cid]=VD[cid]||{});
  const c=DATA.cards.find(x=>x.cid===cid)||{};
  const nt=$('#note'); if(nt)v.note=nt.value;
  if(c.per_row){
    const rv=v.rowvals||{};
    document.querySelectorAll('.rv').forEach(el=>{rv[el.dataset.row]=el.value});
    v.rowvals=rv;
    v.value=Object.entries(rv).filter(([,x])=>x&&x.trim()).map(([k,x])=>k+'='+x).join('; ');
  }else{const iv=$('#val'); if(iv)v.value=iv.value}
  return v;
}
async function pick(cid,oid){
  const body={...grab(cid),cid,choice:oid,answered_at:new Date().toISOString()};
  VD[cid]={...body}; draw();
  if(NEEDVAL.has(oid)){const f=$('#val')||document.querySelector('.rv'); if(f)f.focus()}  // 选了「我来说明」直接聚焦
  try{
    const r=await fetch(`/api/verdicts/${encodeURIComponent(TASK)}`,{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    if(!r.ok)throw new Error('HTTP '+r.status);
    VD[cid]=body; flashSaved();
  }catch(e){
    delete VD[cid]; alert('没保存上：'+e.message+'\n服务可能已停，确认控制台端口还在跑再重试。'); draw();
  }
}
function sync(cid){grab(cid)}                       // 只入内存，不重绘（重绘会抢焦点）
async function reSave(cid){                          // 失焦时把文字也落盘
  const v=grab(cid); if(!v.choice)return;            // 还没选选项就先不存
  const body={...v,cid,answered_at:new Date().toISOString()};
  try{
    const r=await fetch(`/api/verdicts/${encodeURIComponent(TASK)}`,{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    if(!r.ok)throw new Error('HTTP '+r.status);
    VD[cid]={...body}; flashSaved();                 // 不重绘：用户可能正在另一个框里打字
  }catch(e){alert('没保存上：'+e.message)}
}
function g0jump(d){
  const L=g0list(); if(L[G0I])grab(L[G0I].cid);      // 翻卡前先把当前输入收进 VD
  G0I=Math.max(0,Math.min(L.length-1,G0I+d));draw();
}
async function g0save(){
  const L=g0list(),c=L[G0I],v=VD[c.cid]||{};
  if(!v.choice)return alert('先选一个选项');
  const body={cid:c.cid,choice:v.choice,note:$('#note').value,answered_at:new Date().toISOString()};
  if(c.per_row){const rv={};document.querySelectorAll('.rv').forEach(el=>{if(el.value.trim())rv[el.dataset.row]=el.value.trim()});
    body.rowvals=rv;body.value=Object.entries(rv).map(([k,x])=>k+'='+x).join('; ')}
  else body.value=$('#val')?$('#val').value:(NEEDVAL.has(v.choice)?(v.value||''):'');
  await fetch(`/api/verdicts/${encodeURIComponent(TASK)}`,{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  VD[c.cid]=body;
  const nx=DATA.cards.findIndex(x=>!VD[x.cid]);
  if(nx>=0){const j=L.findIndex(x=>x.cid===DATA.cards[nx].cid);if(j>=0)G0I=j}
  draw();
}
async function g0clear(cid){
  await fetch(`/api/verdicts/${encodeURIComponent(TASK)}`,{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({cid,clear:true})});
  delete VD[cid];draw();
}
/* ── 执行台 ── */
function ok(c){return ['passed','xfailed'].includes(c.auto_status)||c.manual_status==='pass'}
function cpass(c){
  if(F==='all')return true;
  if(F==='undone')return !c.auto_status&&!c.manual_status;
  if(F==='div')return c.divergent;
  if(F==='bad')return ['failed','error','xpassed_strict'].includes(c.auto_status)||c.manual_status==='fail';
  if(F==='gap')return c.map_gap;
  if(F==='ok')return ok(c);
  return true;
}
function drawCases(){
  const cs=DATA.cases,n=f=>cs.filter(f).length;
  $('#kpi').innerHTML=
    `<span>自动 <b class="s-passed">${n(c=>c.auto_status==='passed')}</b>/<b class="s-failed">${n(c=>['failed','error'].includes(c.auto_status))}</b>/<b class="s-xfailed">${n(c=>c.auto_status==='xfailed')}</b>已知缺陷/<b>${n(c=>!c.auto_status)}</b></span>`+
    `<span>人工 <b class="s-pass">${n(c=>c.manual_status==='pass')}</b>/<b class="s-fail">${n(c=>c.manual_status==='fail')}</b>/<b>${n(c=>!c.manual_status)}</b></span>`+
    `<span>分歧 <b class="s-blocked">${n(c=>c.divergent)}</b></span><span>已验证 <b>${n(ok)}/${cs.length}</b></span>`+
    (n(c=>c.auto_status==='xpassed_strict')?`<span>⚠️疑似已修复 <b class="s-xpassed_strict">${n(c=>c.auto_status==='xpassed_strict')}</b>（xfail意外通过，待复核）</span>`:'');
  $('#fil').innerHTML=[['all','全部',cs.length],['undone','未执行',n(c=>!c.auto_status&&!c.manual_status)],
    ['div','两边分歧',n(c=>c.divergent)],['bad','有失败',n(c=>['failed','error','xpassed_strict'].includes(c.auto_status)||c.manual_status==='fail')],
    ['gap','卡地图缺口',n(c=>c.map_gap)],['ok','已验证',n(ok)]]
    .map(([k,l,v])=>`<button class="${F===k?'on':''}" onclick="setF('${k}')">${l} ${v}</button>`).join('');
  const gst=(DATA.frontmatter||{}).status||'';
  let body=(ADDING?addForm():'')+
    `<div style="margin-bottom:12px"><button onclick="ADDING=true;draw()">＋ 新增用例</button>`+
    (gst==='pending_review'
      ?`<button style="margin-left:8px;border-color:var(--gate);color:var(--gate)" onclick="confirmGate1()">✓ 标记用例评审完成</button>`
      :gst==='confirmed'
        ?`<span class="pill" style="margin-left:8px;color:var(--ok);border-color:var(--ok)">用例评审已过审</span>`
        :'')+
    `<span class="t" style="margin-left:10px;color:var(--ink3)">改动直接写进 cases.md，`+
    `首次编辑前自动存 cases.pre_gate1.md 快照，每次改动记入 cases_edits.jsonl</span></div>`;
  body+=cs.filter(cpass).map(ccard).join('')||'<div class="case"><div class="hd">这个筛选下没有用例。</div></div>';
  $('#app').innerHTML=body;
}
function badge(k,st){const lab=k==='auto'?(AS[st]||'未跑'):(MS[st]||'未执行');
  return `<span class="pill s-${st||'none'}">${k==='auto'?'自动':'人工'} ${lab}</span>`}
function runs(rs,k){
  if(!rs.length)return '<div class="empty">暂无记录</div>';
  return rs.map(r=>{const lab=k==='auto'?(AS[r.status]||r.status):(MS[r.status]||r.status);
    let h=`<div class="run"><span class="pill s-${r.status}">${lab}</span> <span class="t">${esc(r.ts||'')}</span>`;
    if(k==='auto'){h+=` <span class="t">${r.duration!=null?r.duration+'s':''}</span>`;
      if(r.nodeid)h+=`<div class="t" style="margin-top:3px">${esc(r.nodeid)}</div>`;
      if(r.error)h+=`<pre>${esc(r.error)}</pre>`;
      if(r.artifacts&&r.artifacts.length)h+=`<div class="t" style="margin-top:4px">取证：${r.artifacts.map(esc).join('　')}</div>`}
    else{if(r.actual)h+=`<div style="margin-top:4px"><b>实际看到：</b>${esc(r.actual)}</div>`;
      if(r.note)h+=`<div style="margin-top:3px;color:var(--ink2)">${esc(r.note)}</div>`}
    return h+'</div>'}).join('');
}
function ccard(c){
  const op=OPEN[c.id]?' open':'',dv=c.divergent?' div':'';
  let h=`<div class="case${op}${dv}"><div class="hd" onclick="tog('${c.id}')"><span class="cid">${c.id}</span>`+
    `<span class="pill p-${c.priority}">${c.priority}</span><span class="ttl">${md(c.title)}</span>`+
    (c.map_gap?'<span class="pill gapc">缺地图</span>':'')+badge('auto',c.auto_status)+badge('manual',c.manual_status)+'</div>';
  if(!OPEN[c.id])return h+'</div>';
  h+='<div class="bd">';
  if(c.divergent)h+=`<div class="divmsg"><b>两边结论不一致。</b>自动化说「${AS[c.auto_status]||c.auto_status}」，`+
    `人工说「${MS[c.manual_status]||c.manual_status}」——要么脚本断言写错了，要么人看漏了。先查这条。</div>`;
  if(EDIT===c.id){h+=editForm(c);return h+'</div></div>'}
  h+=`<div style="margin-bottom:10px"><button onclick="startEdit('${c.id}')">编辑这条</button></div>`;
  h+=`<dl class="meta"><dt>前置</dt><dd>${md(c.pre)||'—'}</dd><dt>步骤</dt><dd>${md(c.steps)||'—'}</dd>`+
     `<dt>期望</dt><dd>${md(c.expect)||'—'}</dd><dt>依据</dt><dd>${md(c.prd_ref)||'—'}</dd><dt>page_ref</dt><dd>${md(c.page_ref)||'—'}</dd>`+
     `<dt>对应检查点</dt><dd style="font-size:12.5px">${esc(humanRef(c.derived_from))}<span class="t mono">　·　cases.md 第 ${c.line_no} 行</span></dd></dl>`;
  h+='<div class="lanes"><div class="lane"><h4><span>自动化执行</span><span class="t mono">auto_runs.jsonl</span></h4>';
  if(c.map_gap)h+='<p class="hint">⚠️ 卡在 page_map 缺口上，探索补完前跑不了自动化——先走人工。</p>';
  h+=`<pre class="cmd">python -m pytest -p tools.pytest_case_ledger \\
  -k "${esc(c.id)}" -v --override-ini="addopts="</pre>`;
  h+='<div style="margin-top:9px">'+runs(c.auto_runs,'auto')+'</div></div>';
  h+='<div class="lane"><h4><span>人工执行</span><span class="t mono">manual_runs.jsonl</span></h4><div class="mbtns">'+
     Object.entries(MS).map(([k,v])=>`<button onclick="mark('${c.id}','${k}')">${v}</button>`).join('')+'</div>'+
     `<textarea id="ac-${c.id}" placeholder="实际看到什么（判失败/阻塞时必填，逐字抄）"></textarea>`+
     `<textarea id="nt-${c.id}" placeholder="备注（可选）"></textarea>`+
     '<div style="margin-top:4px">'+runs(c.manual_runs,'manual')+'</div></div></div>';
  return h+'</div></div>';
}
function tog(id){OPEN[id]=!OPEN[id];draw()}
let EDIT=null,ADDING=false;
const F_MAP={title:'测试点',pre:'前置',steps:'步骤',expect:'期望',priority:'优先级',exec:'exec',page_ref:'page_ref'};
function startEdit(id){EDIT=id;draw()}
function cancelEdit(){EDIT=null;ADDING=false;draw()}
async function saveEdit(id){
  const ch={};
  for(const [k,col] of Object.entries(F_MAP)){
    const el=document.getElementById('e-'+k+'-'+id); if(el)ch[col]=el.value;
  }
  const r=await(await fetch(`/api/case/${encodeURIComponent(TASK)}`,{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({op:'update',case_id:id,changes:ch})})).json();
  if(r.error)return alert('保存失败：'+r.error);
  EDIT=null;DATA=await(await fetch(`/api/cases/${encodeURIComponent(TASK)}`)).json();draw();
}
async function delCase(id){
  const reason=prompt('删除 '+id+' 的理由（会记进审计日志，可回溯）：');
  if(reason===null)return;
  const r=await(await fetch(`/api/case/${encodeURIComponent(TASK)}`,{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({op:'delete',case_id:id,reason})})).json();
  if(r.error)return alert('删除失败：'+r.error);
  DATA=await(await fetch(`/api/cases/${encodeURIComponent(TASK)}`)).json();draw();
}
async function addCase(){
  const cells={};
  for(const [k,col] of Object.entries(F_MAP)){
    const el=document.getElementById('n-'+k); if(el&&el.value.trim())cells[col]=el.value;
  }
  if(!cells['测试点'])return alert('至少填「测试点」');
  const r=await(await fetch(`/api/case/${encodeURIComponent(TASK)}`,{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({op:'create',cells})})).json();
  if(r.error)return alert('新增失败：'+r.error);
  ADDING=false;DATA=await(await fetch(`/api/cases/${encodeURIComponent(TASK)}`)).json();
  OPEN[r.case_id]=true;draw();
}
function editForm(c){
  const F=[['title','测试点',1],['pre','前置',1],['steps','步骤',3],['expect','期望',3],
           ['page_ref','page_ref',2]];
  let h='<div class="editbox"><div class="ehd">编辑用例 —— 改动会写进 cases.md 并记入审计日志</div>';
  h+=`<div class="erow"><label>优先级</label><select id="e-priority-${c.id}">`+
     ['P0','P1','P2'].map(p=>`<option${c.priority===p?' selected':''}>${p}</option>`).join('')+'</select>'+
     `<label>exec</label><select id="e-exec-${c.id}">`+
     ['auto','manual','blocked'].map(x=>`<option${c.exec===x?' selected':''}>${x}</option>`).join('')+'</select></div>';
  F.forEach(([k,lab,rows])=>{h+=`<div class="erow2"><label>${lab}</label>`+
    (rows>1?`<textarea id="e-${k}-${c.id}" rows="${rows}">${esc(c[k]||'')}</textarea>`
           :`<input id="e-${k}-${c.id}" value="${esc(c[k]||'')}">`)+'</div>'});
  h+=`<div class="erow"><button onclick="saveEdit('${c.id}')">保存</button>`+
     `<button onclick="cancelEdit()">取消</button>`+
     `<button onclick="delCase('${c.id}')" style="margin-left:auto;color:var(--bad)">删除这条</button></div></div>`;
  return h;
}
function addForm(){
  const F=[['title','测试点',1],['pre','前置',1],['steps','步骤',3],['expect','期望',3],['page_ref','page_ref',2]];
  let h='<div class="case"><div class="bd" style="display:block"><div class="editbox">'+
        '<div class="ehd">新增用例 —— ID 自动分配，`exec` 默认 manual（避免误进自动化）</div>';
  h+='<div class="erow"><label>优先级</label><select id="n-priority">'+
     ['P2','P1','P0'].map(p=>`<option>${p}</option>`).join('')+'</select>'+
     '<label>exec</label><select id="n-exec">'+
     ['manual','auto','blocked'].map(x=>`<option>${x}</option>`).join('')+'</select></div>';
  F.forEach(([k,lab,rows])=>{h+=`<div class="erow2"><label>${lab}</label>`+
    (rows>1?`<textarea id="n-${k}" rows="${rows}"></textarea>`:`<input id="n-${k}">`)+'</div>'});
  h+='<div class="erow"><button onclick="addCase()">新增</button>'+
     '<button onclick="cancelEdit()">取消</button></div></div></div></div>';
  return h;
}
async function confirmGate1(){
  if(!confirm('整份 cases.md 标记为用例评审完成？这一步不检查有没有逐条看过，确认前请自己确认已经看完。'))return;
  const r=await(await fetch(`/api/case/${encodeURIComponent(TASK)}`,{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({op:'confirm'})})).json();
  if(r.error)return alert('标记失败：'+r.error);
  DATA=await(await fetch(`/api/cases/${encodeURIComponent(TASK)}`)).json();draw();
}
async function mark(id,st){
  const actual=$('#ac-'+id).value.trim(),note=$('#nt-'+id).value.trim();
  if((st==='fail'||st==='blocked')&&!actual)return alert('判失败/阻塞时请填「实际看到什么」');
  await fetch(`/api/manual/${encodeURIComponent(TASK)}`,{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({case_id:id,status:st,actual,note})});
  DATA=await (await fetch(`/api/cases/${encodeURIComponent(TASK)}`)).json();draw();
}
// 关页面/刷新前，若当前卡有已输入但未落盘的文字，拦一下
window.addEventListener('beforeunload',e=>{
  if(VIEW!=='gate0')return;
  const L=g0list(),c=L[G0I]; if(!c)return;
  const nt=$('#note'),iv=$('#val'),v=VD[c.cid]||{};
  const dirty=(nt&&nt.value&&nt.value!==(v.note||''))||(iv&&iv.value&&iv.value!==(v.value||''));
  if(dirty){e.preventDefault();e.returnValue=''}
});
document.addEventListener('keydown',e=>{
  if(VIEW!=='gate0'||['INPUT','TEXTAREA'].includes(e.target.tagName))return;
  if($('#helpmask')&&$('#helpmask').classList.contains('on'))return;   // 说明弹窗开着时不响应选项快捷键
  const L=g0list(),c=L[G0I];if(!c)return;
  if(/^[1-9]$/.test(e.key)){const o=c.options[+e.key-1];if(o)pick(c.cid,o.id)}
  else if(e.key==='j')g0jump(1);else if(e.key==='k')g0jump(-1);
  else if(e.key==='Enter')g0save();
});
// ── 卡片类型说明（右上角「这些卡是什么？」）──
const HELP=`<h3>这些卡是什么？为什么要你来判断？</h3>
<p class="lead">agent 先把需求拆成一张张检查点表（每个功能点、每个字段、每种状态切换）。遇到需求没写清楚的地方，
它<b>不会自己瞎猜</b>，而是标出来。这里只推给你<b>必须由人拍板</b>的那几条：你的答案会直接写进检查点表，
后面生成的测试用例就照着你的答案写。答对一张卡，等于提前堵住一类"需求理解错了"的返工。</p>
<h4>三种卡片</h4>
<table>
<tr><th>类型</th><th>是什么意思</th><th>你怎么答</th><th>答完之后会怎样</th></tr>
<tr><td>⚠️ 冲突</td><td>需求里有两处说法打架（比如正文和变更日志、需求和设计图），agent 不知道该信哪一个。</td>
<td>选你认为对的那一方；两边都不对就选「我来说明」写正确规则。</td>
<td>采纳的一方成为用例依据；另一方会被记成"文档勘误"，建议同步给产品改需求。</td></tr>
<tr><td>⚠️ 缺失</td><td>需求根本没提，agent 也推不出来，比如"老数据要不要也遵守新规则"。</td>
<td>你知道答案就直接选或写；不知道就选「留给提测实测」。</td>
<td>有答案的，按你的答案出用例；留给实测的，会变成一条"提测后去看实际行为"的检查项，不会漏掉。</td></tr>
<tr><td>🔶 推断</td><td>需求没明说，agent 参照同类功能先填了一个答案。</td>
<td>认同就选它；不对就改成正确的。</td>
<td>认同后它就从"猜的"变成"确定的"，以后同类需求可以直接复用。</td></tr>
</table>
<h4>几个常见问题</h4>
<ol>
<li><b>「agent 默认假设」是什么？</b>如果你不改，agent 就按这个选项写用例。同意的话点它就行，最省事。</li>
<li><b>拿不准怎么办？</b>选「我不确定，留给提测实测」永远是安全的：它不会被当成同意，只是把这个问题留到功能能用时再看。</li>
<li><b>为什么只有这几张？</b>"提测时看一眼就知道"的问题（比如提示文案的具体措辞）已经自动放到页面底部，不占你的时间；你也可以展开把它们拉回来自己答。</li>
<li><b>答完以后呢？</b>告诉 agent「审完了」→ 它把你的答案回填进检查点表 → 请你确认一次 → 生成正式测试用例和操作步骤（下一轮评审）。</li>
<li><b>备注有什么用？</b>备注会和你的选项一起交给 agent：回填检查点表、生成用例时，它会把你写的例外、适用范围、补充条件一并体现进去。只选选项说不全的，写在备注里。</li>
<li><b>答错了能改吗？</b>能。每张卡选完就自动保存，随时回来改选项；点「清除本卡」可以撤回。</li>
</ol>
<div style="text-align:right;margin-top:14px"><button onclick="hideHelp()">知道了</button></div>`;
/* ── 解构清单 · 按模块看 ──
   评审人点进来先想知道：这个需求拆出了哪些模块、每个模块有哪些功能、每个功能的控件长什么样、缺了什么。
   所以默认视角是一棵树：⓪ 模块 → ① 功能点 → ⑦ 控件 / ② 字段 / ④ 规则 / 缺口；统计图挪到「统计」。 */
let DMODE='tree',TOPEN={},TALL=false;
function dModeBtns(){
  return [['tree','按模块看'],['table','按表看'],['stats','统计']].map(([m,l])=>
    `<button class="${DMODE===m?'on':''}" onclick="DMODE='${m}';draw()">${l}</button>`).join('')+'<span class="sep">｜</span>';
}
function drawDecomp(){if(DMODE==='tree')return drawDecompTree();return drawDecompFlat(DMODE)}
function ttog(k,def){TOPEN[k]=!(k in TOPEN?TOPEN[k]:def);draw()}
function topen(k,def){return k in TOPEN?TOPEN[k]:(TALL||def)}
function talls(v){TALL=v;TOPEN={};draw()}
const ktag=k=>k&&k!=='确定'?`<span class="tg" style="background:${col(KV[k])}">${k}</span>`:'';
function ctrlLine(c){
  const when=c.when?` <span class="w">· ${md(c.when)}</span>`:'';
  const pre=c.pre?` <span class="pr">（${md(c.pre)}）</span>`:'';
  const miss=x=>/⚠/.test(x||'')?'miss':'';
  const shows=[c.state?`<span class="${miss(c.state)}">${md(c.state)}</span>`:'',
               c.text?`文案 <span class="${miss(c.text)}">${md(c.text)}</span>`:''].filter(Boolean).join('，');
  return `<div class="cl"><span class="rid">${esc(c.id)}</span><div class="ct"><b>${md(c.obj)}</b>${when}${pre}`+
    (shows?`<div class="cv">→ ${shows}</div>`:'')+`</div><div class="tags">${ktag(c.kind)}</div></div>`;
}
function fieldLine(c){
  const bits=[['必填',c.need],['取值',c.rng],['默认',c.dflt],['校验',c.check]].filter(x=>x[1]&&x[1]!=='—').map(([k,v])=>`${k}：${md(v)}`);
  return `<div class="cl"><span class="rid">${esc(c.id)}</span><div class="ct"><b>${md(c.obj)}</b>`+
    (bits.length?`<div class="cv">${bits.join('　·　')}</div>`:'')+`</div><div class="tags">${ktag(c.kind)}</div></div>`;
}
function ruleLine(c){
  return `<div class="cl"><span class="rid">${esc(c.id)}</span><div class="ct">${md(c.obj)}</div><div class="tags">${ktag(c.kind)}</div></div>`;
}
/* 我来补充：缺口就地补——人在「按模块看」里看到自己知道的，点一下就填，不用跳到别的页面。
   按行存 gap_fills.json（key = 「④-BR50」这种行 key）；回填 decomp.md、把用例从"记录"改成"断言"由 agent 做（tools/gap_fill.py）。 */
let FOPEN={},FDRAFT={};
const FST={answered:['已补充',''],ask_product:['要问产品','ask'],later:['留到提测看页面','later']};
const FSRC=['我确定','问过产品','问过开发','看过页面'];
function fillsOf(){return (DATA&&DATA.fills)||{}}
function fOpen(k){FOPEN[k]=!FOPEN[k];redrawKeep()}
function fDraft(k,f,v){FDRAFT[k]={...(FDRAFT[k]||{}),[f]:v}}
function redrawKeep(){const y=scrollY;drawDecompTree();scrollTo(0,y)}
async function fSave(k,status,title){
  const old=fillsOf()[k]||{},d=FDRAFT[k]||{};
  const answer=(d.answer??old.answer??'').trim(),source=d.source??old.source??FSRC[0];
  if(status==='answered'&&!answer){alert('先写下你知道的答案再保存。不确定的话点「要问产品」或「留到提测」。');return}
  const r=await (await fetch(`/api/gapfill/${encodeURIComponent(TASK)}`,{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({gid:k,answer,source,status,title,rows:[k]})})).json();
  if(r.error){alert(r.error);return}
  DATA.fills={...fillsOf(),[k]:r.fill};delete FDRAFT[k];FOPEN[k]=false;redrawKeep();
}
async function fClear(k){
  if(!confirm('撤回这条补充？'))return;
  await fetch(`/api/gapfill/${encodeURIComponent(TASK)}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({gid:k,clear:true})});
  const f={...fillsOf()};delete f[k];DATA.fills=f;delete FDRAFT[k];FOPEN[k]=false;redrawKeep();
}
function gapLine(c,dbt){
  const what=c.table==='⑦'?`${c.obj}${c.when?'（'+c.when+'）':''}`:c.obj;
  const cols=(c.gap_cols||[]).length?`：<b>${c.gap_cols.map(esc).join('、')}</b> 没写清`:'';
  const k=c.key,f=fillsOf()[k],st=f?FST[f.status]||FST.answered:null,ka=esc(k).replace(/'/g,"\\'");
  let h=`<div class="gl"><span class="mono" style="color:var(--ink3);font-size:11.5px">${esc(c.id)}</span> ${md(what)}${cols}`+
    (FOPEN[k]?'':`<button class="fillbtn" onclick="fOpen('${ka}')">${f?'修改':'我来补充'}</button>`)+
    (c.dbt||[]).map(i=>dbt[i]?`<span class="dbt">${esc(i)}：${md(dbt[i].desc)}${dbt[i].open?'':'（已结）'}</span>`:'').join('');
  if(f&&!FOPEN[k])h+=`<span class="filled ${st[1]}">${f.answer?esc(f.answer):'（没写内容）'}\n<span class="fm">${st[0]}${f.source?' · '+esc(f.source):''} · ${esc((f.at||'').replace('T',' '))}${f.applied_at?' · 已回填进解构清单':' · 待回填'}</span></span>`;
  if(FOPEN[k]){
    const d=FDRAFT[k]||{},ans=d.answer??(f?f.answer:'')??'',src=d.source??(f?f.source:'')??FSRC[0];
    const title=esc(what).replace(/'/g,'').slice(0,200);
    h+=`<div class="fform"><textarea class="ans" placeholder="写你知道的：取值、上限、谁能做、做了之后会怎样……" oninput="fDraft('${ka}','answer',this.value)">${esc(ans)}</textarea>`+
      `<div class="fbtns"><select onchange="fDraft('${ka}','source',this.value)">`+FSRC.map(x=>`<option${x===src?' selected':''}>${x}</option>`).join('')+`</select>`+
      `<button class="pri" onclick="fSave('${ka}','answered','${title}')">保存</button>`+
      `<button onclick="fSave('${ka}','ask_product','${title}')">要问产品</button><button onclick="fSave('${ka}','later','${title}')">留到提测看页面</button>`+
      (f?`<button onclick="fClear('${ka}')" style="color:var(--ink3)">撤回</button>`:'')+`<button onclick="fOpen('${ka}')" style="color:var(--ink3)">取消</button></div></div>`;
  }
  return h+'</div>';
}
function featHtml(f,dbt,q){
  const all=[...f.controls,...f.fields,...f.rules];
  const gaps=all.filter(x=>GAPK.includes(x.kind));
  const k='f:'+f.id, open=topen(k,!!q);
  let h=`<div class="ft"><div class="fh" onclick="ttog('${k}',${!!q})"><span class="ar">${open?'▾':'▸'}</span><span class="fid">${esc(f.id)}</span>`+
    `<span class="fn">${md(f.name)}</span>`+(f.type?`<span class="fr">${esc(f.type)}</span>`:'')+(f.roles?`<span class="fr">${esc(f.roles)}</span>`:'')+
    `<span class="cnt">`+(f.controls.length?`<span>控件 ${f.controls.length}</span>`:'')+(f.fields.length?`<span>字段 ${f.fields.length}</span>`:'')+
    (f.rules.length?`<span>规则 ${f.rules.length}</span>`:'')+(gaps.length?`<span class="g">缺口 ${gaps.length}</span>`:'')+(gaps.filter(x=>fillsOf()[x.key]).length?`<span class="f">已补 ${gaps.filter(x=>fillsOf()[x.key]).length}</span>`:'')+`</span>${ktag(f.kind)}</div>`;
  if(open){
    h+='<div class="fb">';
    if(f.entry)h+=`<div class="cv" style="font-size:12.5px;color:var(--ink3);margin-top:4px">入口：${md(f.entry)}</div>`;
    if(f.controls.length){
      h+=`<div class="sect">控件（${f.controls.length}）</div>`;
      let last=null;
      for(const c of f.controls){if(c.sub&&c.sub!==last&&new Set(f.controls.map(x=>x.sub)).size>1){h+=`<div class="scr">${md(c.sub)}</div>`;last=c.sub}h+=ctrlLine(c)}
    }
    if(f.fields.length)h+=`<div class="sect">字段（${f.fields.length}）</div>`+f.fields.map(fieldLine).join('');
    if(f.rules.length)h+=`<div class="sect">规则（${f.rules.length}）</div>`+f.rules.map(ruleLine).join('');
    if(gaps.length)h+=`<div class="sect g">缺了哪些描述（${gaps.length}）</div>`+gaps.map(c=>gapLine(c,dbt)).join('');
    if(!all.length)h+='<div class="empty">这个功能点下还没挂控件、字段或规则。</div>';
    h+='</div>';
  }
  return h+'</div>';
}
function drawDecompTree(){
  const D=DATA,T=D.tree,S=D.summary;
  const q=(DF.q||'').toLowerCase();
  $('#kpi').innerHTML=`<button class="helpbtn" onclick="showDHelp()">ⓘ 怎么看这页？</button>`+
    `<span>模块 <b>${T.module_count||'—'}</b></span><span>功能点 <b>${T.feature_count}</b></span><span>设计缺口 <b style="color:var(--d-miss)">${S.gaps}</b></span>`+
    (Object.keys(fillsOf()).length?`<span>已补充 <b style="color:var(--ok)">${Object.keys(fillsOf()).length}</b></span>`:'');
  $('#fil').innerHTML=dModeBtns()+`<button onclick="talls(true)">全部展开</button><button onclick="talls(false)">全部收起</button>`+
    `<input class="srch" placeholder="搜功能 / 控件 / 文案" value="${esc(DF.q)}" oninput="DF.q=this.value.trim();drawDecompTree();const i=$('#fil input');i.focus();i.setSelectionRange(i.value.length,i.value.length)">`;
  const dbt=Object.fromEntries((D.debts||[]).map(d=>[d.id,d]));
  const match=f=>!q||JSON.stringify(f).toLowerCase().includes(q);
  const tot=T.modules.reduce((a,m)=>({c:a.c+m.stats.controls,f:a.f+m.stats.fields,r:a.r+m.stats.rules}),{c:0,f:0,r:0});
  const oc=T.orphan_screens.reduce((a,x)=>a+x.controls.length,0);
  let h=`<div class="dsec intro"><h3>${T.structured?`这个需求拆成了 ${T.module_count} 个模块、${T.feature_count} 个功能点`:`这个需求拆出了 ${T.feature_count} 个功能点`}</h3>`+
    `<p>功能点下面挂了 ${tot.c} 个控件、${tot.f} 个字段、${tot.r} 条规则${oc?`；另有 ${oc} 个控件还没挂到功能点（在最下面，按页面分组）`:''}。`+
    `点模块看它有哪些功能，点功能看它的控件、字段、规则，以及<b>缺了哪些描述</b>（需求文档 / 设计图没写清的地方）。</p>`+
    `<p>缺口里有你知道的，直接点旁边的「<b>我来补充</b>」写上就行。规则类（上限、范围、权限、并发结果、状态怎么变）最值得补；按钮文案、弹层长相这类提测后探页面会抓到，不用凭记忆写。`+
    `补完跟我说一声「<b>缺口补完了</b>」，我回填进解构清单，并把相关用例从"记录"改成"断言"。</p>`+
    (T.structured?'':`<div class="legacy">这份解构是旧格式：还没有「⓪ 模块清单」这一层，控件也没记录属于哪个功能点，所以功能点平铺、控件按页面分组。新需求会按模块拆；这份要补的话，让拆解 agent 跑一次「补模块层」即可。</div>`)+
    '</div>';
  // 旧格式（没有模块层）时控件全在「按页面分组」里——控件是用户最关心的，放最前面
  const orphanHtml=()=>{let h='';
  if(T.orphan_screens.length){
    const k='m:__orphan', open=topen(k,!T.structured);
    const scr=T.orphan_screens.map(sc=>({...sc,controls:sc.controls.filter(c=>!q||JSON.stringify(c).toLowerCase().includes(q))})).filter(sc=>sc.controls.length);
    if(scr.length){
      h+=`<div class="mod"><div class="mh" onclick="ttog('${k}',${!T.structured})"><b>控件（还没挂到功能点，按页面分组）</b><span class="cnt"><span>控件 ${oc}</span></span>`+
        `<span class="ms">这些控件在解构表里没写属于哪个功能点，先按所在页面列出来。</span></div>`;
      if(open)h+='<div class="fb" style="padding-left:18px">'+scr.map(sc=>`<div class="sect">${md(sc.name)}（${sc.controls.length}）</div>`+sc.controls.map(ctrlLine).join('')).join('')+'</div>';
      h+='</div>';
    }
  }
  return h};
  if(!T.structured)h+=orphanHtml();
  for(const m of T.modules){
    const fs=m.features.filter(match);
    if(q&&!fs.length)continue;
    const k='m:'+(m.id||m.name), open=topen(k,true);
    h+=`<div class="mod"><div class="mh" onclick="ttog('${esc(k)}',true)">`+(m.id?`<span class="mid">${esc(m.id)}</span>`:'')+`<b>${md(m.name)}</b>`+
      `<span class="cnt"><span>功能 ${m.stats.features}</span><span>控件 ${m.stats.controls}</span>${m.stats.fields?`<span>字段 ${m.stats.fields}</span>`:''}${m.stats.rules?`<span>规则 ${m.stats.rules}</span>`:''}${m.stats.gaps?`<span class="g">缺口 ${m.stats.gaps}</span>`:''}</span>`+
      (m.scope?`<span class="ms">${md(m.scope)}</span>`:'')+((m.entry||m.roles)?`<span class="me">${m.entry?'入口：'+md(m.entry):''}${m.roles?'　角色：'+esc(m.roles):''}</span>`:'')+'</div>';
    if(open)h+=fs.map(f=>featHtml(f,dbt,q)).join('');
    h+='</div>';
  }
  if(T.structured)h+=orphanHtml();
  $('#app').innerHTML=h;
}
/* ── 解构清单 ──
   decomp.md 七张表的每一行放到两条轴上：设计明确度（需求文档 / 设计图写清了没有）× 验证进度（有没有用例、最近一次跑的结果）。
   数据层 tools/decomp_console.py；这里只画。 */
let DF={t:null,k:null,c:null,q:'',show:'all'},DOPEN={},DSHOWCLOSED=false;
const KV={'冲突':'--d-conflict','缺帧':'--d-conflict','缺失':'--d-miss','推断':'--d-infer','确定':'--d-conf','禁止':'--d-ban'};
const CV={'有失败':'--v-fail','执行出错':'--v-err','已知缺陷':'--v-defect','未执行':'--v-notrun','未覆盖':'--v-uncov','已通过':'--v-pass'};
const GAPK=['冲突','缺帧','缺失','推断'];
const col=v=>`var(${v})`;
function dPass(r){
  if(DF.t&&r.table!==DF.t)return false;
  if(DF.k&&r.kind!==DF.k)return false;
  if(DF.c&&r.cov!==DF.c)return false;
  if(DF.show==='gap'&&!GAPK.includes(r.kind))return false;
  if(DF.show!=='all'&&DF.show!=='gap'&&r.cov!==DF.show)return false;
  if(DF.q){const q=DF.q.toLowerCase();if(!(r.id+' '+Object.values(r.cols).join(' ')+' '+r.cases.map(c=>c.id).join(' ')).toLowerCase().includes(q))return false}
  return true;
}
function dset(k,v){DF[k]=(DF[k]===v?null:v);draw()}
function dshow(v){DF.show=v;draw()}
function dclear(){DF={t:null,k:null,c:null,q:'',show:'all'};draw()}
function dtog(k){DOPEN[k]=!DOPEN[k];draw()}
function dq(v){DF.q=v.trim();const el=$('#drows');if(el)el.innerHTML=dRowsHtml();}
function bar(counts,order,vars,total){
  return '<div class="bar">'+order.filter(k=>counts[k]).map(k=>
    `<i style="width:${(counts[k]/total*100).toFixed(2)}%;background:${col(vars[k])}" title="${k} ${counts[k]} 行"></i>`).join('')+'</div>';
}
function legend(order,vars,labels){
  return '<div class="legend">'+order.map(k=>`<span><i style="background:${col(vars[k])}"></i>${labels?labels[k]||k:k}</span>`).join('')+'</div>';
}
function dRowHtml(r){
  const ktag=`<span class="tg" style="background:${col(KV[r.kind])}" title="设计明确度">${r.kind}</span>`;
  const ctag=r.cov==='未覆盖'||r.cov==='未执行'
    ?`<span class="tg o" style="color:${col(CV[r.cov])}" title="验证进度">${r.cov}</span>`
    :`<span class="tg" style="background:${col(CV[r.cov])}" title="验证进度">${r.cov}</span>`;
  const chips=r.cases.length?`<div class="cs">`+r.cases.map(c=>
    `<a href="#/t/${encodeURIComponent(TASK)}/cases" onclick="event.stopPropagation()" title="${esc(c.title)}　·　${c.state}${c.exec?'　·　'+c.exec:''}">`+
    `${esc(c.id)} <b style="color:${col(CV[c.state])}">●</b></a>`).join('')+'</div>':'';
  let h=`<div class="drow"><div class="h" onclick="dtog('${esc(r.key)}')"><span class="rid">${esc(r.id)}</span>`+
    `<div class="mt">${md(r.main)}${chips}</div><div class="tags">${ktag}${ctag}</div></div>`;
  if(DOPEN[r.key]){
    const dbt=Object.fromEntries((DATA.debts||[]).map(d=>[d.id,d]));
    h+='<dl class="dd">'+Object.entries(r.cols).filter(([k,v])=>k!=='ID'&&v).map(([k,v])=>`<dt>${esc(k)}</dt><dd>${md(v.replace(/<br\s*\/?>/g,' / '))}</dd>`).join('')+
      (r.dbt||[]).map(i=>`<dt>${esc(i)}</dt><dd>${md((dbt[i]||{}).desc||'')}${dbt[i]?`<br><span style="color:var(--ink3)">${esc(dbt[i].status)}</span>`:''}</dd>`).join('')+'</dl>';
  }
  return h+'</div>';
}
function dMatrixHtml(m){
  const cls=v=>/🚫/.test(v)?'m-ban':/⚠/.test(v)?'m-miss':/🔶/.test(v)?'m-inf':/✅/.test(v)?'m-ok':'';
  return `<div class="mx"><div class="vlab">${esc(m.title||'状态矩阵')}</div><table><tr>${m.header.map(x=>`<th>${md(x)}</th>`).join('')}</tr>`+
    m.rows.map(r=>'<tr>'+r.map((c,i)=>i===0?`<th>${md(c)}</th>`:`<td class="${cls(c)}">${md(c)}</td>`).join('')+'</tr>').join('')+'</table></div>';
}
function dRowsHtml(){
  const rows=DATA.rows.filter(dPass);
  if(!rows.length)return '<div class="empty">这个筛选下没有行。</div>';
  let h='';
  for(const t of DATA.tables){
    const rs=rows.filter(r=>r.table===t.key);
    const mats=(DATA.matrices||[]).filter(m=>m.table===t.key);
    if(!rs.length&&!(DF.t===t.key&&mats.length))continue;
    h+=`<div class="dsec"><h3>${t.key} ${esc(t.name)}<span style="color:var(--ink3);font-weight:400;font-size:12.5px">　${rs.length} / ${t.total} 行</span></h3>`;
    if(mats.length)h+=`<details${DF.t===t.key?' open':''}><summary>N×N 迁移矩阵 ${mats.length} 张（格子颜色＝明确度：绿 确定 / 紫 推断 / 橙 缺失 / 灰 禁止）</summary>${mats.map(dMatrixHtml).join('')}</details>`;
    h+=rs.map(dRowHtml).join('')+'</div>';
  }
  return h;
}
function drawDecompFlat(part){
  const D=DATA,S=D.summary,KO=D.kind_order,CO=D.cov_order;
  $('#kpi').innerHTML=`<button class="helpbtn" onclick="showDHelp()">ⓘ 怎么看这页？</button>`+
    `<span>共 <b>${S.rows}</b> 行</span><span>设计缺口 <b style="color:var(--d-miss)">${S.gaps}</b></span>`+
    (D.cases.has_cases?`<span>未覆盖 <b style="color:var(--v-uncov)">${S.uncovered}</b></span><span>有失败 <b style="color:var(--v-fail)">${S.failed}</b></span>`:'')+
    `<span>解构债 <b>${S.debts_open}</b>/${S.debts_total} 未结</span>`;
  const cnt=v=>v==='all'?S.rows:v==='gap'?S.gaps:D.rows.filter(r=>r.cov===v).length;
  const quick=[['all','全部'],['gap','设计缺口']].concat(D.cases.has_cases?CO.filter(c=>c!=='已通过').map(c=>[c,c]):[]);
  let f=dModeBtns();
  if(part==='table')f+=quick.map(([v,l])=>`<button class="${DF.show===v?'on':''}" onclick="dshow('${v}')">${l} ${cnt(v)}</button>`).join('');
  const act=[DF.t&&`表 ${DF.t}`,DF.k&&`明确度＝${DF.k}`,DF.c&&`验证＝${DF.c}`].filter(Boolean);
  if(act.length)f+=`<button onclick="dclear()" title="清除筛选">✕ ${act.join(' · ')}</button>`;
  if(part==='table')f+=`<input class="srch" placeholder="搜 ID / 内容 / 用例号" value="${esc(DF.q)}" oninput="dq(this.value)">`;
  $('#fil').innerHTML=f;

  const M=D.meta;
  let h=`<div class="dmeta">`+(M.prd_version?`<span>需求版本：<b>${esc(M.prd_version)}</b></span>`:'')+
    (M.status?`<span>解构状态：<b>${M.status==='confirmed'?'已过需求拆解评审':M.status==='pending_review'?'需求拆解待审':esc(M.status)}</b></span>`:'')+
    (M.gate0_applied_at?`<span>回填于 ${esc(M.gate0_applied_at.slice(0,10))}</span>`:'')+
    (D.cases.has_cases?`<span>用例 <b>${D.cases.total}</b> 条</span>`:'<span style="color:var(--v-uncov)">还没生成用例（cases.md），验证进度一律"未覆盖"</span>')+
    (D.cases.unlinked.length?`<span title="${esc(D.cases.unlinked.map(x=>x.id).join(', '))}">${D.cases.unlinked.length} 条用例没回指任何解构行</span>`:'')+'</div>';

  if(part==='stats'){
  // 总览：每张表两条条形
  h+=`<div class="dsec"><h3>缺口地图</h3><p class="sub">每行一张解构表。<b>设计明确度</b>＝需求文档 / 设计图把这一行写清了没有；<b>验证进度</b>＝这一行有没有用例覆盖、用例最近一次跑的结果。点一行只看那张表。</p>`+
    `<table class="ov"><tr><th>解构表</th><th class="num">行</th><th>设计明确度</th>${D.cases.has_cases?'<th>验证进度</th>':''}</tr>`+
    D.tables.map(t=>`<tr class="click${DF.t===t.key?' sel':''}" onclick="dset('t','${t.key}')"><td>${t.key} ${esc(t.name)}</td><td class="num">${t.total}</td>`+
      `<td>${t.total?bar(t.kind,KO,KV,t.total):'<span class="empty">只有矩阵</span>'}</td>`+
      (D.cases.has_cases?`<td>${t.total?bar(t.cov,CO,CV,t.total):''}</td>`:'')+'</tr>').join('')+'</table>'+
    legend(KO,KV)+(D.cases.has_cases?legend(CO,CV):'')+'</div>';

  // 交叉表：明确度 × 验证
  if(D.cases.has_cases){
    const kinds=KO.filter(k=>D.rows.some(r=>r.kind===k));
    const grid={};let mx=1;
    D.rows.forEach(r=>{const k=r.kind+'|'+r.cov;grid[k]=(grid[k]||0)+1;mx=Math.max(mx,grid[k])});
    h+=`<div class="dsec"><h3>明确度 × 验证进度</h3><p class="sub">每格＝同时满足这一行、这一列的解构行数，点格子筛出来。<b>上半部（冲突 / 缺失 / 推断）× 左半部（有失败 / 执行出错 / 未执行 / 未覆盖）</b>是"设计没写清、也没验证到"的地方，最该先看。</p>`+
      `<div style="overflow:auto"><table class="xt"><tr><th></th>${CO.map(c=>`<th>${c}</th>`).join('')}</tr>`+
      kinds.map(k=>`<tr><th class="rh">${k}</th>`+CO.map(c=>{const n=grid[k+'|'+c]||0;
        if(!n)return '<td class="z">·</td>';
        const a=Math.round(18+n/mx*62);
        return `<td class="${DF.k===k&&DF.c===c?'hot':''}" style="background:color-mix(in srgb,${col(CV[c])} ${a}%,transparent)" onclick="DF.k='${k}';DF.c='${c}';DF.show='all';draw()" title="${k} × ${c}：${n} 行">${n}</td>`}).join('')+'</tr>').join('')+
      '</table></div></div>';
  }
  }
  if(part==='table')h+=`<div id="drows">${dRowsHtml()}</div>`;

  // 解构债
  const debts=D.debts||[];
  if(part==='table'&&debts.length){
    const op=debts.filter(d=>d.open),cl=debts.filter(d=>!d.open);
    h+=`<div class="dsec"><h3>解构债 <span style="color:var(--ink3);font-weight:400;font-size:12.5px">${op.length} 未结 / ${debts.length}</span></h3>`+
      `<p class="sub">拆解时识别出、但 需求文档和设计图都给不出答案的问题。未结的要么等你裁决，要么留到实测确认。</p>`+
      op.map(d=>`<div class="debt"><span class="di">${esc(d.id)}</span><span>${md(d.desc)}<br><span style="color:var(--ink3);font-size:12px">${esc(d.table)}</span></span><span class="ds">${md(d.status)}</span></div>`).join('')+
      (cl.length?`<div style="margin-top:8px"><button onclick="DSHOWCLOSED=!DSHOWCLOSED;draw()">${DSHOWCLOSED?'▾':'▸'} 已结 ${cl.length} 条</button></div>`:'')+
      (DSHOWCLOSED?cl.map(d=>`<div class="debt closed"><span class="di">${esc(d.id)}</span><span>${md(d.desc)}</span><span class="ds">${md(d.status)}</span></div>`).join(''):'')+'</div>';
  }
  $('#app').innerHTML=h;
}
const DHELP=`<button class="x" onclick="hideDHelp()">✕</button><h3>解构清单怎么看</h3>
<p class="lead">agent 写用例前，先把 需求文档和设计图拆成七张表（功能点、字段、状态机、业务规则、操作旅程、影响面、控件）。这一页把每一行摆出来，并放到两条轴上看。</p>
<h4>按模块看（默认）</h4>
<p class="lead">先看这个需求拆成了几个模块，每个模块下有哪些功能点；点开功能点看它的<b>控件</b>（什么情况下、显示什么、能不能点、文案是什么）、字段、规则，最后一段是<b>缺了哪些描述</b>。控件行的写法：<b>元素</b> · 什么时候（先要做什么）→ 状态，文案。</p>
<p class="lead">「按表看」是原来的七张表逐行清单，「统计」是缺口地图和交叉表。</p>
<h4>设计明确度：需求文档 / 设计图写清了没有</h4>
<table><tr><td>确定</td><td>有出处：需求原文、设计图、你在需求拆解评审的裁决</td></tr>
<tr><td>推断</td><td>agent 按常理补的，没人确认过</td></tr>
<tr><td>缺失</td><td>需求文档和设计图都没写，属于设计缺口</td></tr>
<tr><td>冲突 / 缺帧</td><td>两处说法对不上 / 状态机里该有的状态没定义</td></tr>
<tr><td>禁止</td><td>明确不允许的操作（状态机里的禁止迁移）</td></tr></table>
<h4>验证进度：测到了没有</h4>
<table><tr><td>未覆盖</td><td>没有任何用例回指这一行</td></tr>
<tr><td>未执行</td><td>有用例，但还没跑过</td></tr>
<tr><td>有失败</td><td>最近一次跑，断言不通过（或人工判失败）</td></tr>
<tr><td>执行出错</td><td>没跑到断言就挂了，多是环境、登录、造数问题，不等于产品有问题</td></tr>
<tr><td>已知缺陷</td><td>用例挂着 xfail，缺陷已登记、等开发修</td></tr>
<tr><td>已通过</td><td>覆盖它的用例最近一次都通过</td></tr></table>
<h4>怎么用</h4><ol>
<li>先看「缺口地图」：哪张表橙色 / 紫色多，就是那块设计没写清。</li>
<li>再看「明确度 × 验证进度」交叉表：<b>缺失 × 未覆盖</b>是设计没写、也没人测的地方，最容易漏。</li>
<li>点一行展开看全部列；用例号点了跳到用例执行页。</li></ol>
<p class="lead" style="margin-top:12px">一行被多条用例覆盖时，验证进度取最差的那条。数据每次刷新重新读 decomp.md / cases.md / 执行台账，只读不写。</p>`;
function showDHelp(){let m=$('#dhelpmask');
  if(!m){m=document.createElement('div');m.id='dhelpmask';m.className='mask';
    m.innerHTML='<div class="dlg" role="dialog" aria-modal="true">'+DHELP+'</div>';
    m.addEventListener('click',e=>{if(e.target===m)hideDHelp()});document.body.appendChild(m)}
  m.classList.add('on')}
function hideDHelp(){const m=$('#dhelpmask');if(m)m.classList.remove('on')}
document.addEventListener('keydown',e=>{if(e.key==='Escape')hideDHelp()});
function showHelp(){let m=$('#helpmask');
  if(!m){m=document.createElement('div');m.id='helpmask';m.className='mask';
    m.innerHTML='<div class="dlg" role="dialog" aria-modal="true">'+HELP+'</div>';
    m.addEventListener('click',e=>{if(e.target===m)hideHelp()});document.body.appendChild(m)}
  m.classList.add('on')}
function hideHelp(){const m=$('#helpmask');if(m)m.classList.remove('on')}
document.addEventListener('keydown',e=>{if(e.key==='Escape')hideHelp()});
checkFresh();
document.addEventListener('visibilitychange',()=>{if(!document.hidden)checkFresh()});
setInterval(checkFresh,30000);
boot();
</script></body></html>"""


def _probe(port: int):
    """探端口上跑的是什么：返回 ('none'|'fresh'|'stale'|'old'|'other', 版本信息)。
    old = 是控制台但没有 /api/version（本机制上线前启动的老进程，必然过期）。"""
    base = f"http://127.0.0.1:{port}"
    try:
        with urllib.request.urlopen(base + "/api/version", timeout=2) as r:
            v = json.loads(r.read().decode("utf-8"))
            return ("stale" if v.get("stale") or v.get("started") != code_hash() else "fresh"), v
    except urllib.error.HTTPError:
        pass                                   # 有服务但没这个接口：可能是老控制台，也可能是别的程序
    except Exception:
        try:                                   # 连不上 / 超时：再确认是不是真没人监听
            urllib.request.urlopen(base + "/", timeout=1)
        except urllib.error.HTTPError:
            return ("other", {})
        except Exception:
            return ("none", {})
    try:
        with urllib.request.urlopen(base + "/", timeout=2) as r:
            return ("old", {}) if "QA 控制台" in r.read().decode("utf-8", "ignore") else ("other", {})
    except Exception:
        return ("other", {})


def _listener_pid(port: int):
    out = subprocess.run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
                         capture_output=True, text=True).stdout.split()
    return int(out[0]) if out else None


def cmd_ensure(port: int) -> int:
    """保证 port 上跑的是**当前磁盘代码**的控制台：已是最新就沿用；过期 / 老进程就替换；没有就启动。
    GATE 评审前统一用它起控制台，不要再用「端口被占用就沿用」——长驻进程不会自己更新。"""
    url = f"http://localhost:{port}"
    state, v = _probe(port)
    if state == "fresh":
        print(f"✓ 已是最新代码（版本 {v['started']}，pid {v['pid']}）：{url}")
        return 0
    if state == "other":
        print(f"✗ 端口 {port} 被别的程序占用（不是 QA 控制台），换个端口：python3 tools/qa_console.py ensure {port + 1}")
        return 2
    # 先确认磁盘上的新代码能编译；有语法错误就别杀正在工作的旧进程
    bad = []
    for f in _code_files():
        if f.suffix == ".py":
            try:
                compile(f.read_text(encoding="utf-8"), str(f), "exec")      # 只查语法，不写任何 .pyc
            except SyntaxError as e:
                bad.append(f"{f.name} 第 {e.lineno} 行: {e.msg}")
    if bad:
        print("✗ 磁盘上的控制台代码有语法错误，没有重启（旧进程继续跑旧版）：\n  " + "\n  ".join(bad))
        return 1
    if state in ("stale", "old"):
        pid = _listener_pid(port)
        what = "老进程（没有版本接口）" if state == "old" else f"过期进程（运行的是 {v.get('started')}，磁盘是 {code_hash()}）"
        print(f"· 端口 {port} 上是{what}，pid {pid}，替换它……")
        if pid:
            os.kill(pid, 15)
            for _ in range(30):
                if _listener_pid(port) is None:
                    break
                time.sleep(0.1)
            else:
                os.kill(pid, 9)
                time.sleep(0.3)
    log = Path(os.environ.get("TMPDIR", "/tmp")) / f"qa_console_{port}.log"
    with open(log, "ab") as lf:
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "serve", str(port)],
                         cwd=str(ROOT), stdout=lf, stderr=subprocess.STDOUT, start_new_session=True)
    for _ in range(50):
        time.sleep(0.2)
        st, nv = _probe(port)
        if st == "fresh":
            print(f"✓ 已启动最新代码（版本 {nv['started']}，pid {nv['pid']}）：{url}\n  日志：{log}")
            return 0
    print(f"✗ 启动后 10 秒内没起来，看日志：{log}")
    return 1


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if cmd == "index":
        cmd_index()
    elif cmd == "ensure":
        sys.exit(cmd_ensure(int(sys.argv[2]) if len(sys.argv) > 2 else 8900))
    elif cmd == "serve":
        port = int(sys.argv[2]) if len(sys.argv) > 2 else 8900
        d = discover()
        n = sum(len(g["tasks"]) for g in d["versions"])
        ThreadingHTTPServer.allow_reuse_address = True   # 免得 TIME_WAIT 占位
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        except OSError as e:
            print(f"端口 {port} 起不来：{e}\n"
                  f"先看是谁占着：lsof -nP -iTCP:{port} -sTCP:LISTEN\n"
                  f"或换个端口：python3 tools/qa_console.py serve {port + 1}")
            sys.exit(1)
        print(f"QA 控制台：http://127.0.0.1:{port}/\n"
              f"{len(d['versions'])} 个版本 / {n} 个需求 · 单端口，页面内切换\nCtrl-C 结束")
        srv.serve_forever()
    else:
        print(__doc__)
