#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""抽奖期望值的**蒙特卡洛回测** —— 把「一个点估计」升级为「一整条收益分布」。

为什么需要它
------------
``points_engine.analyze_lottery`` 用分层夹逼给出 dominated / robust / likely /
depends 四档判定，每一步都可证 —— 但它回答的是"**期望值**过不过打平线"。
而用户真正面对的问题通常是：**"我这 800 积分抽 33 次，有多大可能亏？"**

期望值达标 ≠ 大概率不亏。一个"大部分时候拿 ¥0.5、偶尔拿 ¥40"的奖池期望很漂亮，
但它绝大多数次让你亏。所以要看的不是期望，而是**分布**与**亏损概率**。

概率从哪来（关键的方法论约束）
------------------------------
MCP 只返回奖品池、**不返回中奖概率**。所以本模块不"猜一个概率"，
而是把概率当成**情景参数**，在多种假设下各跑一遍，输出一份压力测试表：

==================  ====================================================
``uniform``         全池等概率。**有解析解**：等于 ``expected_if_uniform_cents``
``pessimistic``     最高奖永不被抽中（去掉一个最高值），其余等概率。
                    **有解析解**：等于 ``expected_pessimistic_cents``
``long_tail``       概率 ∝ 1/(升序序号+1) —— 小奖占绝大多数，贴合真实抽奖形态
``optimistic``      概率 ∝ 奖品价值 —— 大奖更容易中（对抽奖最有利的假设）
==================  ====================================================

前两种假设的蒙特卡洛均值必须与解析值对上，这是**模拟器正确性的硬校验**
（``tests/test_lottery_mc.py::TestAnalyticCrossCheck``）。

然后看的是：跨所有假设，亏损概率落在什么区间。
如果连最有利的假设下亏损概率都很高，那结论就不依赖概率假设了 ——
这正是本模块相对单点期望值的全部价值。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from points_engine import (  # noqa: E402
    DISCLAIMER, Prize, analyze_lottery, fmt_ypp, fmt_yuan, prize_from_raw,
)
from termtext import columns, rule  # noqa: E402

ROOT = os.path.dirname(HERE)
DEFAULT_SEED = 20261009          # 与项目「实测日期」同源，便于口头复述
DEFAULT_TRIALS = 20_000

A_UNIFORM = "uniform"
A_PESSIMISTIC = "pessimistic"
A_LONG_TAIL = "long_tail"
A_OPTIMISTIC = "optimistic"

ASSUMPTIONS: Tuple[str, ...] = (A_UNIFORM, A_PESSIMISTIC, A_LONG_TAIL, A_OPTIMISTIC)

ASSUMPTION_TEXT = {
    A_UNIFORM: "全池等概率（有解析解，可校验）",
    A_PESSIMISTIC: "最高奖永不中、其余等概率（有解析解，可校验）",
    A_LONG_TAIL: "概率 ∝ 1/升序序号 —— 小奖占绝大多数",
    A_OPTIMISTIC: "概率 ∝ 奖品价值 —— 大奖更容易中（对抽奖最有利）",
}


# --------------------------------------------------------------------------- #
# 概率假设 → 权重
# --------------------------------------------------------------------------- #
def weights_for(values: Sequence[int], assumption: str) -> List[float]:
    """把「概率假设」转成抽样权重。

    ``values`` 必须**已升序**且全为正数；返回值与 ``values`` 等长。

    - ``pessimistic`` 只把**一个**最高值权重置 0 —— 与
      ``analyze_lottery`` 里 ``values[:-1]`` 的解析口径严格一致。
    - 若置零后权重全为 0（奖池只有一项时的退化情形），**回落到均匀权重** ——
      否则 ``random.choices`` 会直接抛 "Total of weights must be greater than zero"。
      这一条是实测踩出来的：单奖池是合法输入，不该让整个回测崩掉。
    """
    if assumption not in ASSUMPTIONS:
        raise ValueError(f"未知假设 {assumption!r}，可选：{ASSUMPTIONS}")

    n = len(values)
    if assumption == A_PESSIMISTIC:
        w = [1.0] * n
        w[-1] = 0.0
    elif assumption == A_LONG_TAIL:
        w = [1.0 / (i + 1) for i in range(n)]
    elif assumption == A_OPTIMISTIC:
        base = values[0] or 1
        w = [max(1.0, v / base) for v in values]
    else:
        w = [1.0] * n

    if sum(w) <= 0:                      # 退化：唯一奖品被置零 → 回落均匀
        return [1.0] * n
    return w


# --------------------------------------------------------------------------- #
# 结果
# --------------------------------------------------------------------------- #
@dataclass
class MonteCarloResult:
    assumption: str
    trials: int
    seed: int
    draws: int                     # 一次决策里总共抽多少次
    draw_point: int
    required_ev_cents: int         # 打平线：同样积分拿去兑换能保证拿到的价值
    per_draw_mean_cents: float = 0.0
    per_draw_stdev_cents: float = 0.0
    p_loss_single: float = 0.0     # 单次抽奖价值 < 打平线 的概率
    total_mean_cents: float = 0.0
    total_p05: int = 0
    total_p25: int = 0
    total_median: int = 0
    total_p75: int = 0
    total_p95: int = 0
    total_worst: int = 0
    total_best: int = 0
    p_loss_total: float = 0.0      # 抽 draws 次后，总价值 < draws×打平线 的概率
    analytic_mean_cents: Optional[int] = None   # 仅 uniform / pessimistic 有
    histogram: List[Tuple[int, int]] = field(default_factory=list)  # (桶下界, 次数)
    #: 计算打平线时用的影子价格原值。打平线取整到「分」后回除会得到 0.1379 这类数字，
    #: 与基准 0.1380 并存容易被误读成口径不一致，所以原值一并带上、分开写清楚。
    benchmark_ypp: float = 0.0

    @property
    def analytic_gap_cents(self) -> Optional[float]:
        if self.analytic_mean_cents is None:
            return None
        return self.per_draw_mean_cents - self.analytic_mean_cents

    @property
    def value_per_point(self) -> float:
        """实测口径的「每积分实际换回多少元」—— 与影子价格同刻度，可直接比。"""
        if self.draw_point <= 0:
            return 0.0
        return self.per_draw_mean_cents / 100.0 / self.draw_point

    def to_dict(self) -> Dict[str, Any]:
        return {
            "assumption": self.assumption,
            "assumption_text": ASSUMPTION_TEXT.get(self.assumption, self.assumption),
            "trials": self.trials, "seed": self.seed, "draws": self.draws,
            "draw_point": self.draw_point,
            "benchmark_ypp": round(self.benchmark_ypp, 6),
            "required_ev": fmt_yuan(self.required_ev_cents),
            "per_draw_mean": fmt_yuan(round(self.per_draw_mean_cents)),
            "per_draw_stdev": fmt_yuan(round(self.per_draw_stdev_cents)),
            "per_draw_ypp": round(self.value_per_point, 6),
            "p_loss_single": round(self.p_loss_single, 4),
            "total_mean": fmt_yuan(round(self.total_mean_cents)),
            "total_p05": fmt_yuan(self.total_p05),
            "total_p25": fmt_yuan(self.total_p25),
            "total_median": fmt_yuan(self.total_median),
            "total_p75": fmt_yuan(self.total_p75),
            "total_p95": fmt_yuan(self.total_p95),
            "total_range": [fmt_yuan(self.total_worst), fmt_yuan(self.total_best)],
            "p_loss_total": round(self.p_loss_total, 4),
            "analytic_mean": (fmt_yuan(self.analytic_mean_cents)
                              if self.analytic_mean_cents is not None else None),
            "analytic_gap": (round(self.analytic_gap_cents, 2)
                             if self.analytic_gap_cents is not None else None),
        }


@dataclass
class StressTest:
    """跨概率假设的压力测试：结论稳不稳，看亏损概率的区间。"""

    rows: List[MonteCarloResult] = field(default_factory=list)

    @property
    def min_loss_prob(self) -> float:
        return min((r.p_loss_total for r in self.rows), default=0.0)

    @property
    def max_loss_prob(self) -> float:
        return max((r.p_loss_total for r in self.rows), default=0.0)

    @property
    def robust_verdict(self) -> str:
        """只看**跨假设稳健**的部分，不下"应该抽"的结论。"""
        if not self.rows:
            return "unknown"
        if self.min_loss_prob >= 0.5:
            return "robust_lose"     # 连最有利的假设都大概率亏
        if self.max_loss_prob <= 0.5:
            return "robust_win"      # 连最不利的假设都大概率不亏
        return "depends"             # 结论确实取决于概率假设 —— 诚实地说"不知道"

    @property
    def robust_text(self) -> str:
        lo, hi = self.min_loss_prob, self.max_loss_prob
        v = self.robust_verdict
        if v == "robust_lose":
            return (f"**结论不依赖概率假设**：即使按对抽奖最有利的假设，亏损概率仍高达 "
                    f"{lo * 100:.0f}%（区间 {lo * 100:.0f}%–{hi * 100:.0f}%）→ 不建议抽。")
        if v == "robust_win":
            return (f"**结论不依赖概率假设**：即使按最不利的假设，亏损概率也不到 "
                    f"{hi * 100:.0f}%（区间 {lo * 100:.0f}%–{hi * 100:.0f}%）→ 抽是安全的。")
        return (f"**结论依赖概率假设**：亏损概率在不同假设下从 {lo * 100:.0f}% "
                f"到 {hi * 100:.0f}% —— 官方不给概率，本工具无法替你判定，"
                f"只能说清它取决于什么。")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rows": [r.to_dict() for r in self.rows],
            "min_loss_prob": round(self.min_loss_prob, 4),
            "max_loss_prob": round(self.max_loss_prob, 4),
            "verdict": self.robust_verdict,
            "conclusion": self.robust_text,
        }


# --------------------------------------------------------------------------- #
# 模拟
# --------------------------------------------------------------------------- #
def _percentile(sorted_vals: Sequence[int], q: float) -> int:
    if not sorted_vals:
        return 0
    pos = (len(sorted_vals) - 1) * min(max(q, 0.0), 1.0)
    return int(sorted_vals[int(round(pos))])


def monte_carlo_lottery(
    prizes: Sequence[Prize],
    draw_point: int,
    benchmark_ypp: float,
    *,
    trials: int = DEFAULT_TRIALS,
    seed: int = DEFAULT_SEED,
    assumption: str = A_UNIFORM,
    draws: int = 1,
    histogram_bins: int = 12,
) -> MonteCarloResult:
    """模拟 ``draws`` 次抽奖的收益分布。

    **固定随机种子** —— 同样入参跑两次结果逐位相同，报告里的每个数字都能被复现。
    ``benchmark_ypp`` 只用来算打平线：同样积分拿去兑换能保证拿到的价值。
    """
    if assumption not in ASSUMPTIONS:
        raise ValueError(f"未知假设 {assumption!r}，可选：{ASSUMPTIONS}")
    if trials <= 0 or draws <= 0:
        raise ValueError("trials 与 draws 必须为正整数")

    required = int(round(draw_point * benchmark_ypp * 100))

    coupons = [p for p in prizes if p.kind != "points"]
    point_prizes = [p for p in prizes if p.kind == "points"]
    values = [p.face_value_cents for p in coupons if p.face_value_cents > 0]
    values += [p.value_cents(benchmark_ypp) for p in point_prizes]
    values = sorted(v for v in values if v > 0)

    result = MonteCarloResult(
        assumption=assumption, trials=trials, seed=seed, draws=draws,
        draw_point=draw_point, required_ev_cents=required,
        benchmark_ypp=benchmark_ypp,
    )
    if not values:
        return result

    weights = weights_for(values, assumption)
    rng = random.Random(seed)                     # ← 固定种子，结果可复现

    singles: List[int] = []                       # 单次抽奖价值（用于 p_loss_single 与直方图）
    totals: List[int] = []
    for _ in range(trials):
        picks = rng.choices(values, weights=weights, k=draws)
        totals.append(sum(picks))
        if draws == 1:
            singles = totals
        elif len(singles) < trials:
            singles.append(picks[0])

    n = len(singles)
    mean_s = sum(singles) / n
    var_s = sum((x - mean_s) ** 2 for x in singles) / n
    result.per_draw_mean_cents = mean_s
    result.per_draw_stdev_cents = math.sqrt(var_s)
    result.p_loss_single = sum(1 for x in singles if x < required) / n

    total_mean = sum(totals) / trials
    result.total_mean_cents = total_mean
    st = sorted(totals)
    result.total_worst, result.total_best = st[0], st[-1]
    result.total_p05 = _percentile(st, 0.05)
    result.total_p25 = _percentile(st, 0.25)
    result.total_median = _percentile(st, 0.50)
    result.total_p75 = _percentile(st, 0.75)
    result.total_p95 = _percentile(st, 0.95)
    line_total = required * draws
    result.p_loss_total = sum(1 for x in st if x < line_total) / trials

    # 直方图：桶下界 + 计数（供前端画分布图）
    lo_v, hi_v = min(singles), max(singles)
    if hi_v == lo_v:
        result.histogram = [(lo_v, n)]
    else:
        step = max(1, math.ceil((hi_v - lo_v + 1) / histogram_bins))
        buckets: Dict[int, int] = {}
        for x in singles:
            buckets[(x - lo_v) // step] = buckets.get((x - lo_v) // step, 0) + 1
        result.histogram = [(lo_v + k * step, v) for k, v in sorted(buckets.items())]

    # 解析解交叉校验：uniform / pessimistic 两种假设有闭式值
    if assumption in (A_UNIFORM, A_PESSIMISTIC):
        if assumption == A_UNIFORM:
            result.analytic_mean_cents = int(round(sum(values) / len(values)))
        else:
            rest = values[:-1]
            result.analytic_mean_cents = (int(round(sum(rest) / len(rest)))
                                          if rest else values[0])
    return result


def stress_test(
    prizes: Sequence[Prize],
    draw_point: int,
    benchmark_ypp: float,
    *,
    trials: int = DEFAULT_TRIALS,
    seed: int = DEFAULT_SEED,
    draws: int = 1,
    assumptions: Sequence[str] = ASSUMPTIONS,
) -> StressTest:
    """跨概率假设各跑一遍蒙特卡洛，看亏损概率的区间。"""
    return StressTest([
        monte_carlo_lottery(prizes, draw_point, benchmark_ypp, trials=trials,
                            seed=seed, assumption=a, draws=draws)
        for a in assumptions
    ])


# --------------------------------------------------------------------------- #
# 输出
# --------------------------------------------------------------------------- #
def print_result(r: MonteCarloResult, analysis: Optional[Any] = None) -> None:
    print(f"  假设：{ASSUMPTION_TEXT.get(r.assumption, r.assumption)}")
    print(f"  模拟 {r.trials:,} 次 × 每次 {r.draws} 抽（种子 {r.seed}，可复现）")
    line_ypp = (r.required_ev_cents / 100.0 / r.draw_point) if r.draw_point else 0.0
    print(f"  打平线 {fmt_yuan(r.required_ev_cents)}/抽"
          f"（= {r.draw_point} 积分 × 基准 {fmt_ypp(r.benchmark_ypp)}，"
          f"取整到分后回折为 {fmt_ypp(line_ypp)}）")
    print(f"  单次：均值 {fmt_yuan(round(r.per_draw_mean_cents))}"
          f" · 标准差 {fmt_yuan(round(r.per_draw_stdev_cents))}"
          f" · 实测 {fmt_ypp(r.value_per_point)}"
          f" · **亏损概率 {r.p_loss_single * 100:.1f}%**")
    if r.analytic_mean_cents is not None:
        print(f"    解析解校验：{fmt_yuan(r.analytic_mean_cents)}"
              f"（差 {r.analytic_gap_cents:+.2f} 分，抽样误差内）")
    print(f"  抽 {r.draws} 次合计：均值 {fmt_yuan(round(r.total_mean_cents))}"
          f" · P5 {fmt_yuan(r.total_p05)} / 中位 {fmt_yuan(r.total_median)}"
          f" / P95 {fmt_yuan(r.total_p95)}"
          f" · 区间 [{fmt_yuan(r.total_worst)}, {fmt_yuan(r.total_best)}]")
    print(f"    **合计亏损概率 {r.p_loss_total * 100:.1f}%**"
          f"（合计价值 < {fmt_yuan(r.required_ev_cents * r.draws)}）")
    if r.histogram:
        total = max(1, sum(c for _, c in r.histogram))
        span = max(c for _, c in r.histogram) or 1
        print("    分布（单次收益）：")
        for lo, c in r.histogram:
            bar = "█" * max(1, int(c / span * 34))
            print(f"      {fmt_yuan(lo):>9} |{bar} {c / total * 100:4.1f}%")
    if analysis is not None:
        print(f"    对照期望值判定：{analysis.verdict.upper()}"
              f"（解析期望 {fmt_yuan(analysis.expected_if_uniform_cents)}）")
    print()


def print_stress(st: StressTest) -> None:
    specs = [("假设", 14, "<"), ("均值/抽", 10, ">"), ("实测 ypp", 16, ">"),
             ("单次亏损概率", 14, ">"), ("合计亏损概率", 14, ">")]
    rows = [(r.assumption, fmt_yuan(round(r.per_draw_mean_cents)),
             fmt_ypp(r.value_per_point), f"{r.p_loss_single * 100:.1f}%",
             f"{r.p_loss_total * 100:.1f}%") for r in st.rows]
    print("  ══ 跨概率假设压力测试 ══")
    for line in columns(specs, rows):
        print("  " + line)
    print("  " + rule(sum(w for _, w, _ in specs)))
    print()
    print(f"  {st.robust_text}")
    print(f"\n  {DISCLAIMER}")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def load_prizes(path: str, refs_path: Optional[str] = None) -> Tuple[List[Prize], int]:
    """从 ``query-lottery-info`` 原始响应读出奖品池与单次消耗积分。"""
    from points_engine import _load  # noqa: PLC0415

    raw = _load(path)
    d = raw.get("data", raw) if isinstance(raw, dict) else raw
    refs: Dict[str, int] = {}
    if refs_path:
        loaded = _load(refs_path)
        refs = {k: int(v) for k, v in loaded.items()
                if not str(k).startswith("_") and isinstance(v, (int, float))}
    prizes = [prize_from_raw(x, refs) for x in d.get("prizes", [])]
    return prizes, int(str(d.get("drawPoint") or 0) or 0)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="抽奖期望值的蒙特卡洛回测（含跨假设压力测试）")
    ap.add_argument("--lottery", required=True)
    ap.add_argument("--products", required=True, help="积分商城标的（用于算影子价格/打平线）")
    ap.add_argument("--refs", default=None)
    ap.add_argument("--draws", type=int, default=1, help="一次决策里总共抽多少次")
    ap.add_argument("--trials", type=int, default=DEFAULT_TRIALS)
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--assumption", default=A_UNIFORM, choices=ASSUMPTIONS)
    ap.add_argument("--json", action="store_true")
    return ap


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    from points_engine import _load, _load_offers, build_value_matrix  # noqa: PLC0415

    refs = {k: int(v) for k, v in _load(args.refs).items()
            if not str(k).startswith("_") and isinstance(v, (int, float))} if args.refs else {}
    matrix = build_value_matrix(_load_offers(args.products, refs))
    ypp = matrix.benchmark_ypp

    prizes, draw_point = load_prizes(args.lottery, args.refs)
    if not prizes or draw_point <= 0:
        print("  奖池为空或单次消耗积分为 0，无法回测")
        return 1

    analysis = analyze_lottery(draw_point, prizes, ypp)
    st = stress_test(prizes, draw_point, ypp, trials=args.trials, seed=args.seed,
                     draws=args.draws)
    if args.json:
        print(json.dumps(st.to_dict(), ensure_ascii=False, indent=2))
        return 0

    print(f"  奖池 {len(prizes)} 项 · 单次消耗 {draw_point} 积分 · 兑换基准 {fmt_ypp(ypp)}")
    print()
    for r in st.rows:
        print(f"  ── {r.assumption} ──")
        print_result(r)
    print_stress(st)
    return 0


if __name__ == "__main__":
    sys.exit(main())
