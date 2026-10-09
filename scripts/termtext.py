#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""终端文本的**显示列宽**工具 —— 全项目唯一实现。

为什么要有这个模块
------------------
``f"{'巨无霸':<20}"`` 按**字符数**补空格，而汉字在等宽终端里占 2 列 ——
于是"巨无霸"只补了 17 个空格，看起来比 20 列短一截，**整张表逐行错位**。

这个坑项目里踩过两次：先是 ``run_unified_live`` 的报价表，后是
``lottery_mc`` 的压力测试表。两次都是"每处各写一遍补齐逻辑"的直接后果，
所以第三次直接抽成共享模块：**任何要输出表格的地方都从这里取。**
"""
from __future__ import annotations

import unicodedata
from typing import Iterable, List, Sequence

__all__ = ["dwidth", "dpad", "columns", "rule"]


def dwidth(s: str) -> int:
    """字符串在等宽终端里占多少**列**（汉字/全角 2 列，其余 1 列）。"""
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in s)


def dpad(s: str, width: int, align: str = "<") -> str:
    """按显示列宽补齐到 ``width`` 列。

    超宽时**原样返回、不截断** —— 宁可让这一行溢出，也不丢掉信息。
    """
    gap = width - dwidth(str(s))
    if gap <= 0:
        return str(s)
    if align == ">":
        return " " * gap + str(s)
    if align == "^":
        left = gap // 2
        return " " * left + str(s) + " " * (gap - left)
    return str(s) + " " * gap


def columns(specs: Sequence[tuple], rows: Iterable[Sequence[object]]) -> List[str]:
    """按列规格拼一张表。

    ``specs`` 是 ``(表头, 宽度, 对齐)`` 的序列，对齐取 ``"<"`` / ``">"`` / ``"^"``。
    返回**已经按列宽对齐**的字符串列表（不含分隔线，调用方决定用哪种分隔线）。
    """
    widths = [int(w) for _, w, _ in specs]
    aligns = [a for _, _, a in specs]
    out = ["".join(dpad(h, w, a) for (h, w, a) in specs)]
    for row in rows:
        out.append("".join(dpad(str(v), w, a)
                           for v, w, a in zip(row, widths, aligns)))
    return out


def rule(width: int, char: str = "-") -> str:
    return char * width


def table_width(specs: Sequence[tuple]) -> int:
    return sum(int(w) for _, w, _ in specs)
