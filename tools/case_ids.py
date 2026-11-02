# -*- coding: utf-8 -*-
"""测试函数 docstring → cases.md 用例 ID 的提取规则（纯标准库）。

台账插件 tools/pytest_case_ledger.py 记录执行结果时用；QA 控制台 tools/case_console.py 给历史台账补用例号时也用——
控制台跑在系统 Python 上，不一定装了 pytest，所以规则单独放这里。
"""
import re

# 早期正则只认两段式 TC-HOME-005，三段式 TC-SRCH-FLT-01a（带 a/b 拆分后缀）、前缀含数字的 TC-R2-054 都识别不出，
# 这些用例的自动化结果在台账里 case_ids=[]，执行台会一直显示"未执行"。
# a/b 后缀是同一条 cases.md 用例拆成的两个函数，归到同一个 ID（TC-SRCH-FLT-01a → TC-SRCH-FLT-01）
CASE_RE = re.compile(r"\bTC-(?:[A-Z0-9]{1,6}-){1,3}[A-Z]?\d{2,4}(?![0-9])")


def case_ids_from_doc(doc: str) -> list[str]:
    """docstring → 用例 ID。只要首行带了 ID 就只取首行：正文里"同 TC-HOME-011""见 TC-SRCH-006"是引用，不是本函数验证的用例"""
    doc = doc or ""
    first = doc.strip().split("\n", 1)[0]
    ids = CASE_RE.findall(first) or CASE_RE.findall(doc)
    return list(dict.fromkeys(ids))
