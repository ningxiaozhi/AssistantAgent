"""文本匹配工具：工具路由和记忆检索共用一套，避免两处实现漂移。

为什么用字符二元组而不是分词：
  中文没有空格，落地上要么引入分词库（jieba 等），要么用 bigram。
  bigram 对"检索命中"这种粗粒度任务够用，且零依赖、可解释、结果可复现。
  真需要语义，那是 embedding 的活，不该由这里硬扛。
"""
from __future__ import annotations

import re

_WS = re.compile(r"\s+")


def normalize(text: str) -> str:
    return _WS.sub("", (text or "").lower())


def grams(text: str) -> set[str]:
    s = normalize(text)
    return {s[i : i + 2] for i in range(len(s) - 1)}


def bigram_overlap(query: str, text: str) -> float:
    """query 的二元组有多少比例出现在 text 里（0~1）。"""
    qg, tg = grams(query), grams(text)
    if not qg or not tg:
        return 0.0
    return len(qg & tg) / len(qg)


def contains_any(text: str, needles: list[str]) -> int:
    """命中的触发词个数。空串不算命中。"""
    t = (text or "").lower()
    return sum(1 for n in needles if n and n.lower() in t)
