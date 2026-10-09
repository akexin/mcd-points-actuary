#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""影子价格的**动态化与保守估计** —— 把「0.1380 元/积分」从单点快照升级为区间。

为什么需要它
------------
v3 现行做法：取积分商城**当前**周期内最优兑换效率作为影子价格（实测 0.1380）。
这是一个**乐观**估计 —— 它隐含假设用户"总能换到那个最优标的"。

现实里有两件事会打破这个假设：

1. 积分商城**会变**：标的上下架、门槛调整，最优效率本身在波动；
2. 最优标的**限量/限次**：被换完之后下一档效率立刻下降，
   剩余积分的真实机会价值低于单点估计。

用乐观值去判定「用积分抵扣现金」，会**高估**积分的机会成本，
从而把本来划算的兑换误判为亏损。所以除了单点值，还需要一个**保守分位值**。

三条口径（全部可复现，不做无根据的平滑）
--------------------------------------
=================  ==========================================================
``point``          最新一次实测的最优兑换效率 —— **主口径**，即现行的 0.1380
``conservative``   近 ``window`` 次快照的 ``quantile`` 分位（默认 P25）
``range``          历史最小 / 最大值，给出波动区间
=================  ==========================================================

样本不足 ``min_samples`` 时 ``conservative`` **退化为 point 并显式声明**，
不假装有历史 —— 这是本模块最重要的一条诚实约束。

历史从哪来
----------
``run_unified_live.py --record-shadow`` 在每次实跑后 append 一条快照到
``data/shadow_price_history.json``（按 门店+日期 去重）。**不编造历史。**
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

from points_engine import fmt_ypp  # noqa: E402
from termtext import columns  # noqa: E402

DEFAULT_HISTORY = os.path.join(ROOT, "data", "shadow_price_history.json")

MODE_CURRENT = "current"            # 主口径：最新实测最优效率
MODE_CONSERVATIVE = "conservative"  # 保守：近窗分位

MODE_TEXT = {
    MODE_CURRENT: "最新实测（主口径）",
    MODE_CONSERVATIVE: "保守分位（近窗 P25）",
}


# --------------------------------------------------------------------------- #
# 统计小工具（标准库实现，不引 numpy）
# --------------------------------------------------------------------------- #
def quantile(values: Sequence[float], q: float) -> float:
    """线性插值分位数（与 numpy 默认口径一致，便于交叉核对）。"""
    xs = sorted(values)
    if not xs:
        return 0.0
    if len(xs) == 1:
        return float(xs[0])
    pos = (len(xs) - 1) * min(max(q, 0.0), 1.0)
    lo = int(pos)
    hi = min(lo + 1, len(xs) - 1)
    frac = pos - lo
    return float(xs[lo] * (1.0 - frac) + xs[hi] * frac)


def median(values: Sequence[float]) -> float:
    return quantile(values, 0.5)


def slope(xs: Sequence[float], ys: Sequence[float]) -> float:
    """最小二乘斜率（x 为等距序号即可），用于判断效率在升还是在降。"""
    n = len(xs)
    if n < 2:
        return 0.0
    mx = sum(xs) / n
    my = sum(ys) / n
    den = sum((x - mx) ** 2 for x in xs)
    if den == 0:
        return 0.0
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den


# --------------------------------------------------------------------------- #
# 快照
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ShadowSnapshot:
    """一次实测得到的影子价格快照。"""

    date: str                  # YYYY-MM-DD
    best_ypp: float            # 周期内最优兑换效率（现行主口径的来源）
    median_ypp: float = 0.0    # 全库中位效率
    worst_ypp: float = 0.0     # 周期内最差效率
    active_offers: int = 0     # 周期内可定价标的数
    priced_offers: int = 0     # 可定价标的总数
    store_code: str = ""
    source: str = ""

    def key(self) -> str:
        return f"{self.date}|{self.store_code}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "date": self.date, "best_ypp": round(self.best_ypp, 6),
            "median_ypp": round(self.median_ypp, 6), "worst_ypp": round(self.worst_ypp, 6),
            "active_offers": self.active_offers, "priced_offers": self.priced_offers,
            "store_code": self.store_code, "source": self.source,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ShadowSnapshot":
        return cls(
            date=str(d.get("date", "")),
            best_ypp=float(d.get("best_ypp", 0) or 0),
            median_ypp=float(d.get("median_ypp", 0) or 0),
            worst_ypp=float(d.get("worst_ypp", 0) or 0),
            active_offers=int(d.get("active_offers", 0) or 0),
            priced_offers=int(d.get("priced_offers", 0) or 0),
            store_code=str(d.get("store_code", "") or ""),
            source=str(d.get("source", "") or ""),
        )


def snapshot_from_matrix(matrix: Any, *, store_code: str = "",
                         now: Optional[Any] = None, source: str = "run_unified_live") -> ShadowSnapshot:
    """由 ``points_engine.ValueMatrix`` 直接生成快照（避免手工誊写数字）。"""
    import datetime as _dt

    if now is None:
        now = _dt.datetime.now()
    active = list(getattr(matrix, "active", []) or [])
    ranked = list(getattr(matrix, "ranked", []) or [])
    ypps = [o.yuan_per_point for o in active] or [o.yuan_per_point for o in ranked]
    return ShadowSnapshot(
        date=now.strftime("%Y-%m-%d"),
        best_ypp=max(ypps) if ypps else 0.0,
        median_ypp=median([o.yuan_per_point for o in ranked]) if ranked else 0.0,
        worst_ypp=min(ypps) if ypps else 0.0,
        active_offers=len(active),
        priced_offers=len(ranked),
        store_code=store_code,
        source=source,
    )


# --------------------------------------------------------------------------- #
# 历史序列
# --------------------------------------------------------------------------- #
@dataclass
class ShadowSeries:
    snapshots: List[ShadowSnapshot] = field(default_factory=list)

    def sorted(self) -> List[ShadowSnapshot]:
        return sorted(self.snapshots, key=lambda s: (s.date, s.store_code))

    def append(self, snap: ShadowSnapshot) -> bool:
        """按 门店+日期 去重后写入；同一天同门店重跑不产生重复样本。"""
        keys = {s.key() for s in self.snapshots}
        if snap.key() in keys:
            for i, s in enumerate(self.snapshots):
                if s.key() == snap.key():
                    self.snapshots[i] = snap      # 同日重跑：覆盖为最新实测值
                    return False
        self.snapshots.append(snap)
        return True

    @classmethod
    def load(cls, path: str) -> "ShadowSeries":
        if not os.path.exists(path):
            return cls()
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        rows = raw.get("snapshots", []) if isinstance(raw, dict) else raw
        return cls([ShadowSnapshot.from_dict(r) for r in rows or []])

    def save(self, path: str) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        payload = {
            "_note": ("影子价格实测历史。每次 run_unified_live.py --record-shadow "
                      "追加一条（按 门店+日期 去重）。仅记录真实实测值，不编造。"),
            "snapshots": [s.to_dict() for s in self.sorted()],
        }
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.write("\n")


# --------------------------------------------------------------------------- #
# 估计
# --------------------------------------------------------------------------- #
@dataclass
class ShadowEstimate:
    """影子价格估计结果：单点 + 保守分位 + 区间。"""

    point: float = 0.0             # 主口径
    conservative: float = 0.0      # 保守分位
    low: float = 0.0               # 历史最小
    high: float = 0.0              # 历史最大
    samples: int = 0
    window: int = 6
    quantile: float = 0.25
    degraded: bool = False         # 样本不足 → 退化
    clamped: bool = False          # 分位数高于最新值 → 已压回最新值
    trend: float = 0.0             # 每次快照的效率变化（元/积分）
    dates: List[str] = field(default_factory=list)

    @property
    def spread_ratio(self) -> float:
        return round(self.high / self.low, 2) if self.low > 0 else 0.0

    def value(self, mode: str = MODE_CURRENT) -> float:
        if mode == MODE_CONSERVATIVE:
            return self.conservative
        return self.point

    def note(self) -> str:
        if self.degraded:
            return (f"历史样本仅 {self.samples} 次（<{self.window} 次窗口要求）"
                    f"→ 保守分位退化为单点值，**不假装有历史**；"
                    f"多跑几次 run_unified_live.py --record-shadow 即可积累")
        extra = ("；且近窗分位数高于最新实测 → 已压回最新值（保守值绝不高于今天）"
                 if self.clamped else "")
        return (f"近 {len(self.dates)} 次实测 P{int(self.quantile * 100)}；"
                f"波动区间 [{self.low:.4f}, {self.high:.4f}]"
                f"（{self.spread_ratio} 倍）{extra}")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "point": round(self.point, 6),
            "conservative": round(self.conservative, 6),
            "low": round(self.low, 6), "high": round(self.high, 6),
            "samples": self.samples, "window": self.window,
            "quantile": self.quantile, "degraded": self.degraded,
            "clamped": self.clamped, "trend": round(self.trend, 8),
            "note": self.note(),
        }


def estimate(series: ShadowSeries, *, window: int = 6, quantile_q: float = 0.25,
             min_samples: int = 3) -> ShadowEstimate:
    """由历史序列给出影子价格估计。

    ``window`` 只用**最近**这么多次快照 —— 更早的商城结构与现在差异太大，
    纳入反而失真。样本数不足 ``min_samples`` 时保守值退化为单点值并标记。
    """
    snaps = series.sorted()
    if not snaps:
        return ShadowEstimate(degraded=True, window=window, quantile=quantile_q)

    recent = snaps[-window:]
    bests = [s.best_ypp for s in recent if s.best_ypp > 0]
    point = bests[-1] if bests else 0.0

    enough = len(bests) >= max(min_samples, 2)
    if enough:
        # 分位数兜底**不足以防住"刚发生的一次下跌"**：
        # 例 [0.16, 0.15, 0.14, 0.13, 0.12] 的 P25 是 0.13，反而**高于**最新的 0.12。
        # 那样就成了一份"看起来保守、实际比今天还乐观"的估计。所以再取一次 min：
        # 保守值永远不高于今天实测到的最优效率。
        raw_p = quantile(bests, quantile_q)
        cons = min(raw_p, point)
        clamped = raw_p > point
    else:
        cons = point
        clamped = False

    return ShadowEstimate(
        point=point,
        conservative=cons,
        low=min(bests) if bests else 0.0,
        high=max(bests) if bests else 0.0,
        samples=len(bests),
        window=window,
        quantile=quantile_q,
        degraded=not enough,
        clamped=clamped,
        trend=slope(list(range(len(bests))), bests) if len(bests) >= 2 else 0.0,
        dates=[s.date for s in recent if s.best_ypp > 0],
    )


# --------------------------------------------------------------------------- #
# 输出
# --------------------------------------------------------------------------- #
def print_estimate(est: ShadowEstimate, mode: str = MODE_CURRENT) -> None:
    print(f"  影子价格（{MODE_TEXT.get(mode, mode)}）：{est.value(mode):.4f} 元/积分")
    if est.samples:
        print(f"    主口径 {est.point:.4f} · 保守 P{int(est.quantile * 100)} "
              f"{est.conservative:.4f} · 区间 [{est.low:.4f}, {est.high:.4f}]"
              f" · 样本 {est.samples} 次")
    print(f"    {est.note()}")
    if est.trend < 0:
        print("    注意：历史最优效率**在下降** —— 此时保守估计尤其重要，"
              "用最新单点值会高估积分、误判划算的兑换为亏损")
    elif est.trend > 0:
        print("    历史最优效率在上升；保守估计会略微低估积分价值（偏安全）")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="影子价格历史与保守分位估计")
    ap.add_argument("command", nargs="?", default="report",
                    choices=("report", "record", "estimate"))
    ap.add_argument("--history", default=DEFAULT_HISTORY)
    ap.add_argument("--products", default=os.path.join(ROOT, "data", "sample_points_products.json"))
    ap.add_argument("--refs", default=os.path.join(ROOT, "data", "sample_reference_prices.json"))
    ap.add_argument("--store", default="")
    ap.add_argument("--window", type=int, default=6)
    ap.add_argument("--quantile", type=float, default=0.25)
    return ap


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    series = ShadowSeries.load(args.history)

    if args.command == "record":
        sys.path.insert(0, HERE)
        from points_engine import _load, _load_offers, build_value_matrix  # noqa: PLC0415

        refs = {k: int(v) for k, v in _load(args.refs).items()
                if not str(k).startswith("_") and isinstance(v, (int, float))}
        offers = _load_offers(args.products, refs)
        matrix = build_value_matrix(offers)
        snap = snapshot_from_matrix(matrix, store_code=args.store)
        added = series.append(snap)
        series.save(args.history)
        print(f"  {'已追加' if added else '已覆盖同日快照'}：{snap.key()} "
              f"best={snap.best_ypp:.4f} 中位={snap.median_ypp:.4f} "
              f"周期内 {snap.active_offers} 项 → {args.history}")
        return 0

    if not series.snapshots:
        print(f"  暂无历史快照（{args.history}）。"
              f"跑一次 `python scripts/run_unified_live.py --record-shadow` 即可开始积累。")
        return 0

    print(f"  共 {len(series.snapshots)} 条快照：")
    for s in series.sorted():
        print(f"    {s.date}  门店 {s.store_code or '—':<10} "
              f"最优 {s.best_ypp:.4f}  中位 {s.median_ypp:.4f}  "
              f"最差 {s.worst_ypp:.4f}  （周期内 {s.active_offers} 项）")
    est = estimate(series, window=args.window, quantile_q=args.quantile)

    # 两个口径**并列**展示：前两列按显示列宽对齐，第三列是自由文本，不参与对齐。
    specs = [("口径", 22, "<"), ("建议值", 15, ">"), ("", 0, "<")]
    rows = [
        (MODE_TEXT[MODE_CURRENT], fmt_ypp(est.value(MODE_CURRENT)),
         "  ← 主口径：对外公布、跨期可比，刻意偏保守"),
        (MODE_TEXT[MODE_CONSERVATIVE], fmt_ypp(est.value(MODE_CONSERVATIVE)),
         "  ← 下界：历史攒够多期后才与主口径分叉"),
    ]
    print()
    for i, line in enumerate(columns(specs, rows)):
        print(("    " + line) if i else ("  " + line))
    print(f"\n  主口径 {est.point:.4f} · 保守 P{int(est.quantile * 100)} "
          f"{est.conservative:.4f} · 区间 [{est.low:.4f}, {est.high:.4f}]"
          f" · 样本 {est.samples} 次 · 窗口 {est.window}")
    print(f"  {est.note()}")
    if est.trend < 0:
        print("  注意：历史最优效率**在下降** —— 此时保守估计尤其重要，"
              "用最新单点值会高估积分、误判划算的兑换为亏损")
    elif est.trend > 0:
        print("  历史最优效率在上升；保守估计会略微低估积分价值（偏安全）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
