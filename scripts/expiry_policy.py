#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""跨时间的积分策略 —— 把「影子价格」从**最优标的的效率**改成你余额处的**边际效率**。

v3 现行做法为什么不够
--------------------
影子价格取"周期内最优兑换效率"（实测 0.1380 元/积分）。但那个最优标的是**限量**的：
「50 积分换 6.9 元派」每人每月通常只能换 1 次。换完之后，下一档效率是 0.1090、
再下一档 0.0358…… 所以：

  对一个只有 100 积分的人，0.1380 是真实的；
  对一个有 1500 积分的人，0.1380 只对**最前面那 50 分**成立，
  后面 1450 分的真实机会价值远低于它。

用 0.1380 去判定"要不要用积分抵扣现金"，会**系统性高估**积分的机会成本，
把本来划算的兑换误判为亏损。正确的度量是**边际效率** ——
"再多花 1 个积分，能多换回多少"，在你**实际余额处**取值。

怎么算
------
这是个**有界背包**：预算 = 积分余额，物品 = 各兑换标的（带每期可兑换次数上限），
价值 = 面值（分）。余额规模在千级，所以直接跑**精确 DP**（``O(余额 × 标的数)``），
连"边际曲线"都一并得到：``best[p] - best[p-1]`` 就是第 ``p`` 个积分的边际价值。

时间怎么进来
------------
到期日把"每期可兑换次数"截断：只剩 6 天、某标的每周限换 1 次 ⇒ 它最多只能换 1 次。
于是**时间不够**会直接压低可挽回价值 —— 这就是"积分的到期时间价值"，
而且是算出来的，不是拍出来的。反过来，``start_by`` 给出"最晚必须从哪天开始动手"。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from points_engine import (  # noqa: E402
    DISCLAIMER, RedemptionOffer, WINDOW_ACTIVE, fmt_ypp, fmt_yuan, parse_mcd_time,
    window_state,
)
from termtext import dpad, dwidth  # noqa: E402

ROOT = os.path.dirname(HERE)


# --------------------------------------------------------------------------- #
# 兑换标的
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RedeemableLot:
    """一个可兑换标的，附带**兑换节流**信息。

    ``period_days = 0`` 表示不限次（随时可换）；否则每 ``period_days`` 天最多换
    ``per_period`` 次。这是"每人每月限换 1 次"这类规则的一般化表达。
    """

    name: str
    point: int
    value_cents: int
    period_days: int = 0
    per_period: int = 1
    window_end: Optional[_dt.date] = None

    @property
    def ypp(self) -> float:
        return self.value_cents / 100.0 / self.point if self.point > 0 else 0.0

    def max_qty(self, days_left: int, today: Optional[_dt.date] = None) -> int:
        """到期日前最多能换几次。"""
        if self.point <= 0 or self.value_cents <= 0:
            return 0
        if self.window_end is not None and today is not None and today > self.window_end:
            return 0
        if days_left <= 0:
            return 0
        if self.period_days <= 0:
            return 10 ** 6                       # 不限次（由余额与价值上限兜住）
        periods = (days_left + self.period_days - 1) // self.period_days
        return max(1, periods * self.per_period)

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "point": self.point,
                "value": fmt_yuan(self.value_cents), "ypp": round(self.ypp, 6),
                "period_days": self.period_days, "per_period": self.per_period}


# --------------------------------------------------------------------------- #
# 结果
# --------------------------------------------------------------------------- #
@dataclass
class ExpirySequence:
    """余额在到期日之前的**序贯兑换策略**。"""

    balance: int = 0
    days_left: int = 0
    now: Optional[_dt.datetime] = None
    expiry: Optional[_dt.date] = None
    picks: List[Tuple[RedeemableLot, int]] = field(default_factory=list)
    spent: int = 0
    salvaged_cents: int = 0
    leftover: int = 0
    lost_cents: int = 0
    average_ypp: float = 0.0        # 可挽回总值 / 余额
    marginal_ypp: float = 0.0       # 余额里**最后一份**积分还能换回什么（真实机会成本）
    next_ypp: float = 0.0           # 再**多攒一份**能多换回什么（与 marginal 的差 = 粒度台阶）
    best_lot_ypp: float = 0.0       # 单一最优标的的效率（= v3 现用口径）
    capacity_point: int = 0         # **有效兑换容量**：再多积分也换不出更多价值
    dead_point: int = 0             # 超出容量的「死积分」数量
    curve: List[Tuple[int, float, float]] = field(default_factory=list)
    #: ``(累计积分, 累计平均效率, 本段边际效率)`` —— 断崖在第二列看得最清楚
    capacity_needed: int = 0        # 需要执行几次兑换
    capacity_available: int = 0     # 到期前可执行的兑换次数
    feasible: bool = True
    start_by: Optional[_dt.date] = None   # 最晚必须开始动手的日期
    daily_rate: float = 0.0         # 建议的日均积分消耗
    notes: List[str] = field(default_factory=list)

    def marginal_ypp_at(self, point_spend: int) -> float:
        """花掉 ``point_spend`` 积分的**真实机会成本率**（元/积分）。

        口径是 ``(best[余额] − best[余额 − N]) / N`` —— "如果少了这 N 分，
        最多能挽回的价值会掉多少"，也就是这 N 分本来还能换回什么。

        为什么不用单点差分 ``best[p] − best[p−1]``：整数背包的差分是**块状**的。
        池子里最小面额是 50 分的一份小食，「第 50 个积分」一个人就解锁了 6.9 元，
        于是 ``(best[50]−best[49])/1`` = 6.9 元/积分 —— 数字没错，但当费率用很荒谬。
        按区间平均才有意义。
        """
        if not self._best or self.balance <= 0 or point_spend <= 0:
            return 0.0
        n = min(point_spend, self.balance)
        lo = self._best[self.balance - n]
        return (self._best[self.balance] - lo) / float(n) / 100.0

    def average_ypp_at(self, point_spend: int) -> float:
        """花掉 ``point_spend`` 积分的**平均效率** —— 这条曲线就是"效率断崖"本身。"""
        if not self._best or point_spend <= 0:
            return 0.0
        n = min(point_spend, self.balance)
        return self._best[n] / float(n) / 100.0

    #: 内部：精确背包的完整价值表 ``best[p]``（分），``p = 0..balance``。
    _best: List[int] = field(default_factory=list, repr=False)

    @property
    def ypp_gap(self) -> float:
        """边际效率相对「最优标的效率」的折价 —— 就是现用口径的高估幅度。"""
        return self.best_lot_ypp - self.marginal_ypp

    @property
    def suggested_shadow_ypp(self) -> float:
        """建议用作 ``--shadow-ypp`` 的值：余额处的真实边际机会成本。"""
        return self.marginal_ypp

    @property
    def days_until_start(self) -> Optional[int]:
        if self.start_by is None or self.now is None:
            return None
        return (self.start_by - self.now.date()).days

    def to_dict(self) -> Dict[str, Any]:
        return {
            "balance": self.balance,
            "days_left": self.days_left,
            "expiry": self.expiry.isoformat() if self.expiry else None,
            "picks": [{"name": lot.name, "point": lot.point, "qty": q,
                       "value": fmt_yuan(lot.value_cents * q)} for lot, q in self.picks],
            "spent": self.spent,
            "salvaged": fmt_yuan(self.salvaged_cents),
            "leftover": self.leftover,
            "lost": fmt_yuan(self.lost_cents),
            "average_ypp": round(self.average_ypp, 6),
            "marginal_ypp": round(self.marginal_ypp, 6),
            "next_ypp": round(self.next_ypp, 6),
            "best_lot_ypp": round(self.best_lot_ypp, 6),
            "ypp_gap": round(self.ypp_gap, 6),
            "suggested_shadow_ypp": round(self.suggested_shadow_ypp, 6),
            "curve": [{"upto": p, "average_ypp": round(a, 6), "marginal_ypp": round(m, 6)}
                      for p, a, m in self.curve],
            "capacity": {"needed": self.capacity_needed,
                         "available": self.capacity_available,
                         "point": self.capacity_point,
                         "dead_point": self.dead_point},
            "feasible": self.feasible,
            "start_by": self.start_by.isoformat() if self.start_by else None,
            "daily_rate": round(self.daily_rate, 2),
            "notes": list(self.notes),
        }


# --------------------------------------------------------------------------- #
# 精确背包 + 边际曲线
# --------------------------------------------------------------------------- #
def _split_items(lots: Sequence[RedeemableLot], balance: int, days_left: int,
                 today: Optional[_dt.date]) -> List[Tuple[int, int, RedeemableLot, int]]:
    """把「每期可换 N 次」拆成 0/1 物品（二进制拆分），得到 ``(重量, 价值, 标的, 件数)``。

    二进制拆分是处理**有界**背包的标准手法：把 N 件拆成 1、2、4… 件一组，
    组合数从 ``2^N`` 降到 ``O(log N)``，且不会漏解。
    """
    items: List[Tuple[int, int, RedeemableLot, int]] = []
    for lot in lots:
        w, v = lot.point, lot.value_cents
        if w <= 0 or v <= 0:
            continue
        cap = min(lot.max_qty(days_left, today), balance // w)
        k = 1
        while cap > 0:
            take = min(k, cap)
            items.append((w * take, v * take, lot, take))
            cap -= take
            k <<= 1
    return items


def value_table(lots: Sequence[RedeemableLot], balance: int, days_left: int,
                today: Optional[_dt.date] = None) -> List[int]:
    """``best[p]`` = 花不超过 ``p`` 积分能挽回的最大面值（分）。有界背包的**精确解**。

    余额在千级、标的在十级 → ``O(余额 × log 件数 × 标的数)``，可以忽略不计。
    所以这里不做任何贪心近似：**结论要么对，要么就没有**。
    """
    best = [0] * (balance + 1)
    for ww, vv, _lot, _take in _split_items(lots, balance, days_left, today):
        for p in range(balance, ww - 1, -1):
            cand = best[p - ww] + vv
            if cand > best[p]:
                best[p] = cand
    for p in range(1, balance + 1):        # 单调不减（容量更大不可能更差）
        if best[p] < best[p - 1]:
            best[p] = best[p - 1]
    return best


def solve_picks(lots: Sequence[RedeemableLot], balance: int, days_left: int,
                today: Optional[_dt.date] = None
                ) -> Tuple[List[Tuple[RedeemableLot, int]], int]:
    """精确背包 + **逐层记录选择**，返回 ``(兑换清单, 最大面值)``。

    逐层保留 ``keep[idx][p]``（第 idx 个物品在容量 p 处是否被取），
    这样回溯是精确的 —— 只靠最终 ``best`` 表做 ``best[p] == best[p-w]+v`` 的
    试凑回溯在多层 0/1 背包里会失效，所以宁可多花 O(物品数 × 余额) 的内存。
    """
    items = _split_items(lots, balance, days_left, today)
    best = [0] * (balance + 1)
    keep: List[List[bool]] = []
    for ww, vv, _lot, _take in items:
        prior = best[:]                    # 上一层的结果，用于回溯
        took = [False] * (balance + 1)
        for p in range(balance, ww - 1, -1):
            cand = best[p - ww] + vv
            if cand > best[p]:
                best[p] = cand
                took[p] = True
        keep.append(took)

    counts: Dict[int, int] = {}
    p = balance
    for idx in range(len(items) - 1, -1, -1):
        if p >= items[idx][0] and keep[idx][p]:
            ww, _vv, lot, take = items[idx]
            counts[id(lot)] = counts.get(id(lot), 0) + take
            p -= ww

    by_id = {id(l): l for l in lots}
    # 按**对象**聚合，绝不按名称 —— 积分商城里存在"同名但所需积分不同"的标的
    # （实测就有「10.9元指定小食任选」同时以 100 分和 200 分两种档位出现），
    # 按名称合并会把它们错误地并成一项，直接改变结论。
    picks = [(by_id[k], v) for k, v in counts.items() if v]
    picks.sort(key=lambda x: -x[0].ypp)
    return picks, best[balance]


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #
def plan_sequence(
    balance: int,
    expiry: _dt.date,
    lots: Sequence[RedeemableLot],
    *,
    now: Optional[_dt.datetime] = None,
    best_lot_ypp: Optional[float] = None,
    scan_start_days: int = 400,
) -> ExpirySequence:
    """给出到期前的序贯兑换策略、有效影子价格与最晚开始日期。"""
    now = now or _dt.datetime.now()
    today = now.date()
    days_left = (expiry - today).days
    res = ExpirySequence(balance=balance, days_left=days_left, now=now, expiry=expiry)
    if balance <= 0 or days_left < 0:
        if days_left < 0:
            res.notes.append("积分已过期，无可挽回")
        return res

    usable = [l for l in lots if l.point > 0 and l.value_cents > 0
              and l.max_qty(days_left, today) > 0]
    if not usable:
        res.leftover = balance
        res.lost_cents = int(round(balance * (best_lot_ypp or 0) * 100))
        res.feasible = False
        res.notes.append("到期前没有任何可执行的兑换标的 —— 全部积分将归零")
        return res

    # 池子里最小的一份积分面额 —— 它是"再多花一点"的最小步长。
    # 边际效率必须按这个步长取区间平均，否则会拿到块状差分的荒谬值。
    delta = max(1, min(l.point for l in usable))
    best = value_table(usable, balance + delta, days_left, today)   # 多算一份，取右导数
    picks, salvaged = solve_picks(usable, balance, days_left, today)
    res.picks = picks
    res.salvaged_cents = salvaged
    res.spent = sum(lot.point * q for lot, q in picks)
    res.leftover = balance - res.spent
    res._best = best[:balance + 1]
    res.average_ypp = best[balance] / 100.0 / balance if balance else 0.0

    # 守恒自检：背包最优值必须等于回溯清单的面值。两者不等 ⇒ 回溯写错了。
    # 与 coupon_solver 的「归因异常」同一套思路：宁可自己先喊，也不给一个看着对的错数。
    if salvaged != best[balance]:
        res.lost_cents = max(0, best[balance] - salvaged)
        res.notes.append(
            f"归因异常：背包最优值 {fmt_yuan(best[balance])} ≠ 回溯清单面值 "
            f"{fmt_yuan(salvaged)}，回溯有误。")

    # 边际效率（后向）：余额里**最后一份**积分还能换回什么 —— 这才是"要不要花掉它"的价格。
    # 余额一旦超过有效兑换容量，它就是 0：最后那一份根本换不出东西。
    blk = min(delta, balance)
    res.marginal_ypp = (best[balance] - best[balance - blk]) / float(blk) / 100.0
    # 前向：再**多攒**一份能多换回什么。它与后向值不同，差额就是整数背包的粒度台阶 ——
    # 后向为 0 说明"手上的分已经换不完了"，前向为正说明"再多一点又刚好够买下一件"。
    res.next_ypp = (best[balance + delta] - best[balance]) / float(delta) / 100.0

    # 有效兑换容量：拿到「最大可挽回值」所需的**最少**积分（找 best 表上最后一个台阶的起点）。
    # 超过它的积分就是死积分 —— 再多也换不出任何东西。这是"余额越大、平均效率越低"的定量来源。
    peak = best[balance]
    cap_pt = balance
    while cap_pt > 0 and best[cap_pt - 1] >= peak:
        cap_pt -= 1
    res.capacity_point = cap_pt
    res.dead_point = max(0, balance - cap_pt)

    res.best_lot_ypp = (best_lot_ypp if best_lot_ypp is not None
                        else max(l.ypp for l in usable))

    # 效率曲线：左列是"花掉这么多积分的累计平均效率"，右列是"最后一段的边际效率"。
    # 累计平均那列就是断崖本身 —— 从 0.1380 一路被摊薄到 0.0594。
    step = max(1, balance // 12)
    res.curve = [(p,
                  best[p] / float(p) / 100.0,
                  (best[p] - best[max(0, p - step)]) / float(step) / 100.0)
                 for p in range(step, balance + 1, step)]

    res.capacity_needed = sum(q for _, q in picks)
    res.capacity_available = sum(l.max_qty(days_left, today)
                                 for l in usable if l.period_days > 0) or res.capacity_needed
    res.feasible = res.capacity_needed <= res.capacity_available
    res.daily_rate = res.spent / days_left if days_left > 0 else float(res.spent)

    # 最晚开始日：往后拖延会让"每期可兑换次数"被截断，可挽回值下降。
    # 扫描找出「仍然能拿到全部可挽回值」的最大拖延天数。
    hold_days = 0
    for delay in range(0, min(scan_start_days, days_left) + 1):
        truncated = value_table(usable, balance, days_left - delay, today + _dt.timedelta(days=delay))
        if truncated[balance] < best[balance]:
            break
        hold_days = delay
    res.start_by = today + _dt.timedelta(days=hold_days)
    if hold_days < days_left:
        res.notes.append(
            f"最晚 {res.start_by.isoformat()} 之前必须开始兑换：再拖下去，"
            f"部分标的的『每期限换次数』将因到期而被截断，可挽回价值会下降。")
    if res.dead_point > 0:
        res.notes.append(
            f"有效兑换容量只有 {res.capacity_point} 积分：余额里第 "
            f"{res.capacity_point + 1}～{balance} 分**换不出任何额外价值**"
            f"（死积分 {res.dead_point} 分）。余额越大于容量，"
            f"「最优标的效率」这个口径就越失真。")
    if res.leftover:
        naive = int(round(res.leftover * res.best_lot_ypp * 100))
        honest = int(round(res.leftover * res.marginal_ypp * 100))
        res.notes.append(
            f"有 {res.leftover} 积分换不出东西（标的容量或余额零头）。"
            f"按最优标的效率折算会以为亏了 {fmt_yuan(naive)}，"
            f"按余额处边际效率折算只有 {fmt_yuan(honest)} —— "
            f"这 {res.leftover} 分本来就换不出东西，它的真实机会价值接近 0，"
            f"不该拿 0.1380 去替它喊冤。")
    if res.marginal_ypp < res.best_lot_ypp - 1e-9:
        res.notes.append(
            f"**余额处的边际效率 {fmt_ypp(res.marginal_ypp)} 低于最优标的的 "
            f"{fmt_ypp(res.best_lot_ypp)}** —— 用后者当影子价格会高估积分、"
            f"把划算的抵扣误判为亏损；建议 ``--shadow-ypp`` 改用 "
            f"{fmt_ypp(res.suggested_shadow_ypp)}。")
    return res


def lots_from_offers(offers: Sequence[RedemptionOffer], *,
                     period_days: int = 30, per_period: int = 1,
                     now: Optional[_dt.datetime] = None,
                     only_active: bool = True) -> List[RedeemableLot]:
    """由积分商城标的构造兑换标的列表。

    节流参数（每期可换几次）**官方接口没有**，所以默认取 ``每 30 天 1 次``
    这一常见规则，并由调用方显式覆盖 —— 宁可把假设写在参数里，也不假装知道。
    """
    out: List[RedeemableLot] = []
    for o in offers:
        if o.point <= 0 or o.face_value_cents <= 0:
            continue
        if only_active and window_state(o, now) != WINDOW_ACTIVE:
            continue
        # 注意：``down_time`` 在 RedemptionOffer 里是**原始字符串**，
        # 必须先解析成日期，否则后面的日期比较会直接抛 TypeError。
        end = None
        parsed = parse_mcd_time(o.down_time) if o.down_time else None
        if parsed is not None:
            end = parsed.date()
        out.append(RedeemableLot(
            name=o.name, point=o.point, value_cents=o.face_value_cents,
            period_days=period_days, per_period=per_period, window_end=end))
    return out


# --------------------------------------------------------------------------- #
# 输出
# --------------------------------------------------------------------------- #
def print_sequence(p: ExpirySequence) -> None:
    print(f"余额 {p.balance} 积分 · 距到期 {p.days_left} 天（{p.expiry}）")
    if not p.picks:
        for n in p.notes:
            print(f"  ! {n}")
        print(f"\n{DISCLAIMER}")
        return

    print(f"\n建议兑换 {len(p.picks)} 类、共 {p.capacity_needed} 次：")
    for lot, q in p.picks:
        print(f"  {lot.name} × {q}  → 花 {lot.point * q:>5} 积分换回 "
              f"{fmt_yuan(lot.value_cents * q)}（单件 {fmt_ypp(lot.ypp)}）")
    print(f"\n合计：花 {p.spent} 积分 · 挽回 {fmt_yuan(p.salvaged_cents)}"
          f" · 余 {p.leftover} 积分 · 相对最优解少拿 {fmt_yuan(p.lost_cents)}")
    print(f"建议日均消耗 {p.daily_rate:.1f} 积分")

    print("\n影子价格（这是本模块的核心产出）：")
    rows = [
        ("最优标的效率（v3 现用口径）", p.best_lot_ypp, "只对最前面那几十个积分成立"),
        ("余额处平均效率", p.average_ypp, "全部积分摊平后的真实产出"),
        ("余额处**边际**效率 ← 建议用", p.marginal_ypp, "最后一份积分还能换回什么"),
    ]
    w = max(dwidth(a) for a, _, _ in rows) + 2
    for label, value, why in rows:
        print(f"  {dpad(label, w)}{fmt_ypp(value)}   ← {why}")
    if p.best_lot_ypp > 0:
        print(f"  {dpad('现用口径的高估幅度', w)}{fmt_ypp(p.ypp_gap)}"
              f"（{p.ypp_gap / p.best_lot_ypp * 100:.1f}%）")
    hint = ("手上的分已换不完，别再攒" if p.next_ypp > p.marginal_ypp + 1e-9
            else "尚未触到容量上限，多攒仍有等价收益")
    print(f"  {dpad('前向：再攒一份能多换回', w)}{fmt_ypp(p.next_ypp)}   ← {hint}")

    print(f"\n有效兑换容量 {p.capacity_point} 积分"
          f"（再多积分也换不出更多价值）· 死积分 {p.dead_point} 积分")

    probes = sorted({lot.point for lot, _ in p.picks})
    if probes:
        print("\n按决策点定价（同一笔花费，在不同余额处机会成本不同）：")
        for pt in probes[:6]:
            rate = p.marginal_ypp_at(pt)
            print(f"  花掉 {pt:>5} 积分 → 这批积分的平均机会成本率 "
                  f"{fmt_ypp(rate)} · 合计 {fmt_yuan(int(round(pt * rate * 100)))}")

    if p.start_by:
        print(f"\n最晚开始日：{p.start_by.isoformat()}"
              f"（{p.days_until_start} 天后，还有 {p.days_left} 天到期）")

    if p.curve:
        peak = max((avg for _, avg, _ in p.curve), default=0.0)
        print("\n效率断崖（累计平均 vs 本段边际）：")
        print(f"  {'已花积分':>9}  {'累计平均':>22}  {'本段边际':>10}")
        for upto, avg, marg in p.curve:
            bar = "▇" * max(1, int(round(avg / peak * 18))) if peak > 0 else ""
            print(f"  {upto:>9}  {bar:<18} {fmt_ypp(avg)}  {fmt_ypp(marg)}")

    for n in p.notes:
        print(f"\n  ! {n}")
    print(f"\n{DISCLAIMER}")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="到期前的序贯兑换策略与有效影子价格")
    ap.add_argument("--balance", type=int, required=True)
    ap.add_argument("--expiry", required=True, help="到期日 YYYY-MM-DD")
    ap.add_argument("--products", default=os.path.join(ROOT, "data", "sample_points_products.json"))
    ap.add_argument("--refs", default=os.path.join(ROOT, "data", "sample_reference_prices.json"))
    ap.add_argument("--period-days", type=int, default=30, help="兑换节流周期（天）")
    ap.add_argument("--per-period", type=int, default=1, help="每周期最多兑换次数")
    ap.add_argument("--now", default=None)
    ap.add_argument("--json", action="store_true")
    return ap


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    from points_engine import _load, _load_offers, parse_mcd_time  # noqa: PLC0415

    now = parse_mcd_time(args.now) if args.now else _dt.datetime.now()
    refs = {k: int(v) for k, v in _load(args.refs).items()
            if not str(k).startswith("_") and isinstance(v, (int, float))}
    offers = _load_offers(args.products, refs)
    lots = lots_from_offers(offers, period_days=args.period_days,
                            per_period=args.per_period, now=now)
    expiry = _dt.date.fromisoformat(args.expiry)
    plan = plan_sequence(args.balance, expiry, lots, now=now)
    if args.json:
        print(json.dumps(plan.to_dict(), ensure_ascii=False, indent=2))
    else:
        print_sequence(plan)
    return 0


if __name__ == "__main__":
    sys.exit(main())
