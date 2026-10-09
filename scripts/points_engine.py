#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""麦麦积分精算师 —— 积分资产价值引擎。

核心命题
--------
麦当劳会员积分（麦享会积分）是一种**会过期、有多种消耗路径、且不同用法价值差极大**
的资产。同样 800 积分，在真实积分商城里：

    换「21.9元巨无霸可乐组合」      → 0.027 元/积分
    换「18.8元麦辣鸡腿汉堡两件套」   → 0.376 元/积分     ← 相差 13.7 倍

也就是说，**选错兑换标的，等于把 93% 的积分价值扔掉**。而积分还有另外两条出路：

    抽奖（24 积分/次，概率收益） · 过期（收益直接归零）

本引擎把这三条路径拉到**同一把尺子**上比较，统一标尺为：

    ypp = 元 / 积分（yuan per point）

三条路径各自的判定方式
----------------------
1. **兑换**（确定收益）：``mall-points-products`` 的每个标的算 ypp，排序并识别"性价比陷阱"。
2. **抽奖**（概率收益）：「单次消耗 × 兑换基准 ypp」= 打平兑换所需的**期望价值门槛**，
   再与奖品池的真实价值分布对比，得出"绝对劣于兑换 / 取决于概率 / 绝对占优"的**严格结论**，
   并在需要时反解出"头部奖品命中概率须达到多少才值得抽"。
3. **过期**（收益归零）：以"将过期积分"为预算，求解**面值最大化**的兑换组合，
   余额再用抽奖兜底，给出最少操作次数的止损方案。

设计原则
--------
- 本模块**不发起任何网络请求**，完全可离线运行、可单元测试。
- 金额一律以 **分（int）** 参与计算，避免浮点误差；仅在输出时转元。
- 折扣类券（如"麦辣三件套5折券"）的**原价**需由 Stage 2 通过 MCP 取得后回填，
  本引擎对未定价标的明确标记为 ``unpriced``，绝不臆造价格。

用法
----
    python points_engine.py matrix --products data/sample_points_products.json
    python points_engine.py lottery --lottery data/sample_lottery.json \
        --products data/sample_points_products.json
    python points_engine.py plan --points 1500 --products data/sample_points_products.json
    python points_engine.py expire --expiring 800 --products data/sample_points_products.json \
        --lottery data/sample_lottery.json
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

DISCLAIMER = (
    "本引擎的结论基于积分商城公开标的与兑换规则估算；"
    "实际可用性（上架状态、门店覆盖）以下单时麦当劳 MCP 返回为准。"
)

# 积分商城 shopId 语义（来自 mall-points-products 实测数据）
SHOP_MEAL_VOUCHER = 2   # 餐品券：需下单核销
SHOP_ACTIVITY = 5       # 派对/体验营等线下活动
SHOP_PHYSICAL = 2       # 实物商品同样落在 shopId=2，用类目区分

# 商品状态码（来自 mall-points-products 字段说明）
STATUS_TEXT = {1: "仓库中", 2: "上架", 3: "售罄", 4: "下架", 5: "预热"}


# --------------------------------------------------------------------------- #
# 金额与数值工具
# --------------------------------------------------------------------------- #
def to_cents(value: Any) -> int:
    """将"元"（int/float/str）安全转换为"分"整数。"""
    if value is None:
        return 0
    if isinstance(value, bool):
        raise TypeError(f"无法解析金额: {value!r}")
    if isinstance(value, int):
        return value * 100
    if isinstance(value, float):
        return int(round(value * 100))
    if isinstance(value, str):
        s = value.strip().replace("¥", "").replace("元", "").replace(",", "")
        return int(round(float(s) * 100))
    raise TypeError(f"无法解析金额: {value!r}")


def fmt_yuan(cents: int) -> str:
    return f"¥{cents / 100:.2f}"


def fmt_ypp(ypp: float) -> str:
    """每积分价值，保留 4 位小数（真实量级在 0.0x 附近，需要足够精度）。"""
    return f"{ypp:.4f} 元/积分"


# --------------------------------------------------------------------------- #
# 上架周期：status 与 downTime 会互相矛盾，必须显式处理
# --------------------------------------------------------------------------- #
#: 状态语义（``mall-points-products`` 的 ``status`` 字段）与 ``upTime``/``downTime``
#: 构成两条独立的可用性信号。**实测中发现二者会冲突**：大量标的活动周期
#: （``downTime``）早已结束，接口却仍返回 ``status=2（上架）``。
#:
#: 处理原则：
#:   - ``status == 2`` 是**兑换可用性**的权威信号（接口自称上架即视为可兑换）；
#:   - ``downTime`` 冲突则降级为**风险提示**，标记 ``stale`` 提醒下单前复核。
#: 这样做既不会臆断接口数据失效，也不会把风险悄悄藏起来。
WINDOW_ACTIVE = "active"    # 当前时间落在 [upTime, downTime] 内 —— 稳健
WINDOW_STALE = "stale"      # 已过 downTime 但 status 仍为上架 —— 待复核
WINDOW_PREHEAT = "preheat"  # 尚未到 upTime
WINDOW_UNKNOWN = "unknown"  # 时间为空或无法解析

WINDOW_TEXT = {
    WINDOW_ACTIVE: "周期内",
    WINDOW_STALE: "已过周期(状态仍上架)",
    WINDOW_PREHEAT: "未开售",
    WINDOW_UNKNOWN: "周期未知",
}

_MCD_TIME_FMTS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d")


def parse_mcd_time(text: str) -> Optional[_dt.datetime]:
    """解析麦当劳接口的时间字符串（``2026-12-30 23:59:59``）。"""
    s = (text or "").strip()
    if not s:
        return None
    for fmt in _MCD_TIME_FMTS:
        try:
            return _dt.datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def window_state(offer: "RedemptionOffer", now: Optional[_dt.datetime] = None) -> str:
    """判定标的上架周期状态。"""
    now = now or _dt.datetime.now()
    up = parse_mcd_time(offer.up_time)
    down = parse_mcd_time(offer.down_time)
    if up is None and down is None:
        return WINDOW_UNKNOWN
    if up is not None and now < up:
        return WINDOW_PREHEAT
    if down is not None and now > down:
        return WINDOW_STALE
    return WINDOW_ACTIVE


# --------------------------------------------------------------------------- #
# 面值解析：从标的名称中提取"等价现金价值"
# --------------------------------------------------------------------------- #
# 「18.8元2份麦辣鸡翅」「21.9元巨无霸可乐组合」—— 券面价直接写在名称开头
_RE_PRICE_PREFIX = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*元")
# 「下单立减3元券」
_RE_REDUCE = re.compile(r"立减\s*([0-9]+(?:\.[0-9]+)?)\s*元")
# 「麦辣三件套5折券」「小食四件套5.5折券」
_RE_DISCOUNT = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*折")


def parse_face_value(name: str, reference_price_cents: int = 0) -> Tuple[int, str]:
    """解析标的名称为「等价现金价值（分）」，返回 (面值分, 定价依据)。

    定价依据取值：
      - ``face``     ：券面价直接写在名称中，确定性最高
      - ``reduce``   ：满减券，面值即立减金额
      - ``discount`` ：折扣券，需外部提供原价（reference_price_cents）才能定价
      - ``reference``：无面值信息，但调用方提供了原价（实物/裸券）
      - ``unknown``  ：无法定价
    """
    m = _RE_PRICE_PREFIX.search(name)
    if m:
        return to_cents(float(m.group(1))), "face"

    m = _RE_REDUCE.search(name)
    if m:
        return to_cents(float(m.group(1))), "reduce"

    m = _RE_DISCOUNT.search(name)
    if m and reference_price_cents > 0:
        rate = float(m.group(1)) / 10.0  # 5折 → 0.5
        if 0 < rate < 1:
            return int(round(reference_price_cents * (1 - rate))), "discount"

    if reference_price_cents > 0:
        return reference_price_cents, "reference"
    return 0, "unknown"


# --------------------------------------------------------------------------- #
# 数据模型
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RedemptionOffer:
    """积分商城的一个兑换标的。"""

    spu_id: str
    name: str
    point: int
    face_value_cents: int = 0
    pricing: str = "unknown"
    shop_id: int = SHOP_MEAL_VOUCHER
    category: str = ""
    status: int = 2
    up_time: str = ""
    down_time: str = ""

    # -- 派生属性 -------------------------------------------------------- #
    @property
    def yuan_per_point(self) -> float:
        """统一标尺：每 1 积分可换回多少元价值。"""
        if self.point <= 0:
            return 0.0
        return self.face_value_cents / 100.0 / self.point

    @property
    def priced(self) -> bool:
        return self.face_value_cents > 0 and self.point > 0

    @property
    def redeemable(self) -> bool:
        return self.status == 2 and self.point > 0

    def to_dict(self, now: Optional[_dt.datetime] = None) -> Dict[str, Any]:
        return {
            "spu_id": self.spu_id,
            "name": self.name,
            "point": self.point,
            "face_value": fmt_yuan(self.face_value_cents),
            "yuan_per_point": round(self.yuan_per_point, 6),
            "pricing": self.pricing,
            "category": self.category,
            "status": STATUS_TEXT.get(self.status, str(self.status)),
            "window": WINDOW_TEXT.get(window_state(self, now), "?"),
            "down_time": self.down_time,
        }


@dataclass(frozen=True)
class Prize:
    """抽奖奖品池的一项。"""

    name: str
    kind: str = "coupon"          # coupon | points
    face_value_cents: int = 0     # coupon 的估值
    points: int = 0               # points 奖的积分数
    pricing: str = "unknown"

    def value_cents(self, benchmark_ypp: float) -> int:
        """折算为现金价值（分）。积分奖按其机会价值折算。"""
        if self.kind == "points":
            return int(round(self.points * benchmark_ypp * 100))
        return self.face_value_cents


@dataclass
class ValueMatrix:
    """兑换标的的价值矩阵（按 ypp 降序）。"""

    ranked: List[RedemptionOffer] = field(default_factory=list)
    unpriced: List[RedemptionOffer] = field(default_factory=list)
    median_ypp: float = 0.0
    best: Optional[RedemptionOffer] = None
    worst: Optional[RedemptionOffer] = None
    spread: float = 0.0           # 最好 / 最差 的倍数
    traps: List[RedemptionOffer] = field(default_factory=list)
    active: List[RedemptionOffer] = field(default_factory=list)   # 周期内
    stale: List[RedemptionOffer] = field(default_factory=list)    # 已过周期但接口仍标上架
    now: Optional[_dt.datetime] = None

    @property
    def benchmark_ypp(self) -> float:
        """兑换基准：**周期内**最优 ypp —— 代表"积分的最高可落地机会价值"。

        优先取周期内标的，避免把已结束的活动价当成可用基准而高估机会成本。
        """
        pool = self.active or self.ranked
        return max((o.yuan_per_point for o in pool), default=0.0)

    @property
    def active_spread(self) -> float:
        """周期内标的的效率倍数差 —— 这是**当下真正可操作**的差距。"""
        ypps = [o.yuan_per_point for o in self.active if o.yuan_per_point > 0]
        return (max(ypps) / min(ypps)) if len(ypps) > 1 else 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "count": len(self.ranked),
            "active_count": len(self.active),
            "stale_count": len(self.stale),
            "best": self.best.to_dict(self.now) if self.best else None,
            "worst": self.worst.to_dict(self.now) if self.worst else None,
            "spread": round(self.spread, 2),
            "active_spread": round(self.active_spread, 2),
            "benchmark_yuan_per_point": round(self.benchmark_ypp, 6),
            "median_yuan_per_point": round(self.median_ypp, 6),
            "traps": [t.to_dict(self.now) for t in self.traps],
            "unpriced_count": len(self.unpriced),
        }


@dataclass
class LotteryAnalysis:
    """抽奖 vs 兑换的期望价值判定。"""

    draw_point: int
    benchmark_ypp: float
    required_ev_cents: int          # 打平兑换所需期望价值
    max_prize_cents: int            # 奖池最高单项价值
    min_coupon_cents: int           # 奖池最低券面值（保守下界）
    expected_if_uniform_cents: int  # 全奖池等概率假设下的期望（参考值）
    breakeven_prob: Optional[float] # 命中最高奖所需最低概率
    verdict: str                    # dominated | robust | likely | depends | unknown
    reason: str
    expected_pessimistic_cents: int = 0  # 悲观情景期望（假设永不中最高奖、其余等概率）
    unpriced_count: int = 0         # 无法定价、未纳入判定的奖品数

    def to_dict(self) -> Dict[str, Any]:
        return {
            "draw_point": self.draw_point,
            "benchmark_yuan_per_point": round(self.benchmark_ypp, 6),
            "required_ev": fmt_yuan(self.required_ev_cents),
            "max_prize": fmt_yuan(self.max_prize_cents),
            "min_prize": fmt_yuan(self.min_coupon_cents),
            "expected_if_uniform": fmt_yuan(self.expected_if_uniform_cents),
            "expected_pessimistic": fmt_yuan(self.expected_pessimistic_cents),
            "breakeven_prob": (round(self.breakeven_prob, 4)
                               if self.breakeven_prob is not None else None),
            "verdict": self.verdict,
            "reason": self.reason,
            "unpriced_prizes": self.unpriced_count,
        }


@dataclass
class BundlePlan:
    """给定积分预算的一组兑换组合。"""

    picks: List[Tuple[RedemptionOffer, int]]   # (标的, 件数)
    spent_point: int
    total_value_cents: int
    leftover_point: int
    leftover_value_cents: int = 0              # 余额的最优小额利用
    note: str = ""

    @property
    def efficiency(self) -> float:
        return self.total_value_cents / 100.0 / self.spent_point if self.spent_point else 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "picks": [{"name": o.name, "point": o.point, "qty": q,
                       "face_value": fmt_yuan(o.face_value_cents)} for o, q in self.picks],
            "spent_point": self.spent_point,
            "total_value": fmt_yuan(self.total_value_cents),
            "efficiency": round(self.efficiency, 6),
            "leftover_point": self.leftover_point,
            "leftover_value": fmt_yuan(self.leftover_value_cents),
            "note": self.note,
        }


@dataclass
class ExpiryPlan:
    """积分到期止损方案。"""

    expiring_point: int
    bundles: List[BundlePlan] = field(default_factory=list)
    lottery_draws: int = 0
    lottery_point: int = 0
    lottery_value_cents: int = 0
    salvaged_cents: int = 0        # 挽回价值
    lost_point: int = 0            # 无法消耗、将归零的积分
    lost_cents: int = 0            # 对应损失价值

    def to_dict(self) -> Dict[str, Any]:
        return {
            "expiring_point": self.expiring_point,
            "bundles": [b.to_dict() for b in self.bundles],
            "lottery_draws": self.lottery_draws,
            "lottery_point": self.lottery_point,
            "lottery_value": fmt_yuan(self.lottery_value_cents),
            "salvaged_value": fmt_yuan(self.salvaged_cents),
            "lost_point": self.lost_point,
            "lost_value": fmt_yuan(self.lost_cents),
        }


# --------------------------------------------------------------------------- #
# 载入与归一化
# --------------------------------------------------------------------------- #
def offer_from_raw(raw: Dict[str, Any], reference_prices: Optional[Dict[str, int]] = None) -> RedemptionOffer:
    """把 ``mall-points-products`` 的一条原始数据归一化为 ``RedemptionOffer``。

    ``reference_prices`` 可选，用于给"裸名称券"（如「麦麦脆汁鸡两件套」）补原价。
    """
    name = str(raw.get("spuName") or raw.get("name") or "")
    ref = int((reference_prices or {}).get(name, 0))
    face, pricing = parse_face_value(name, ref)
    return RedemptionOffer(
        spu_id=str(raw.get("spuId") or raw.get("spu_id") or ""),
        name=name,
        point=int(str(raw.get("point") or 0) or 0),
        face_value_cents=face,
        pricing=pricing,
        shop_id=int(raw.get("shopId") or SHOP_MEAL_VOUCHER),
        category=str(raw.get("catName") or raw.get("category") or ""),
        status=int(raw.get("status") or 2),
        up_time=str(raw.get("upTime") or ""),
        down_time=str(raw.get("downTime") or ""),
    )


def _median(nums: Sequence[float]) -> float:
    if not nums:
        return 0.0
    s = sorted(nums)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2


# --------------------------------------------------------------------------- #
# 引擎 1：兑换价值矩阵
# --------------------------------------------------------------------------- #
def build_value_matrix(
    offers: Sequence[RedemptionOffer],
    *,
    trap_ratio: float = 0.5,
    include_unredeemable: bool = False,
    now: Optional[_dt.datetime] = None,
) -> ValueMatrix:
    """构建价值矩阵并识别"性价比陷阱"。

    陷阱定义：ypp 低于全库中位数的 ``trap_ratio`` 倍 —— 即"用同样的积分，
    只换回不到一半的中位价值"。这类标的往往面值看着大（如"21.9元巨无霸组合"），
    实际是积分黑洞。
    """
    now = now or _dt.datetime.now()
    priced = [o for o in offers if o.priced and (include_unredeemable or o.redeemable)]
    unpriced = [o for o in offers if not o.priced]

    ranked = sorted(priced, key=lambda o: (-o.yuan_per_point, o.point))
    m = ValueMatrix(ranked=ranked, unpriced=unpriced, now=now)
    m.active = [o for o in ranked if window_state(o, now) == WINDOW_ACTIVE]
    m.stale = [o for o in ranked if window_state(o, now) == WINDOW_STALE]
    if not ranked:
        return m

    m.best, m.worst = ranked[0], ranked[-1]
    m.median_ypp = _median([o.yuan_per_point for o in ranked])
    if m.worst.yuan_per_point > 0:
        m.spread = m.best.yuan_per_point / m.worst.yuan_per_point
    cutoff = m.median_ypp * trap_ratio
    m.traps = [o for o in ranked if o.yuan_per_point < cutoff]
    return m


def opportunity_cost_cents(offer: RedemptionOffer, benchmark_ypp: float) -> int:
    """机会成本：这笔兑换相比"最优用法"损失了多少价值（分）。

    这是本引擎最有杀伤力的指标 —— 它把"积分怎么花"变成一个可以报出具体金额的
    决策问题，而不是模糊的"这个好像不划算"。
    """
    if offer.point <= 0:
        return 0
    ideal = benchmark_ypp * offer.point * 100
    return max(0, int(round(ideal - offer.face_value_cents)))


# --------------------------------------------------------------------------- #
# 引擎 2：抽奖期望值边界分析
# --------------------------------------------------------------------------- #
def analyze_lottery(
    draw_point: int,
    prizes: Sequence[Prize],
    benchmark_ypp: float,
) -> LotteryAnalysis:
    """判定"抽奖"相对"直接兑换"的优劣，**无需知道中奖概率**。

    推导
    ----
    设单次抽奖消耗 ``d`` 积分，兑换基准为 ``b``（元/积分），则抽奖要**不亏**，
    其单次期望价值必须满足::

        E ≥ d × b            （记作 required）

    期望值 E 是各奖品价值的凸组合，因此恒有 ``min(values) ≤ E ≤ max(values)``。
    由于 MCP **只返回奖品名称、不返回概率**，常规做法只能靠猜；本引擎改用
    **分层夹逼**，把"猜"变成"可证"：

    ============ ====================================================
    判定          成立条件（越靠前越强）
    ============ ====================================================
    ``dominated``  ``max < required`` —— 上限都不够，**必亏**，无需任何概率假设
    ``robust``     ``E(悲观情景) ≥ required`` —— 假设"永不上头奖、其余等概率"
                   仍然划算，结论不依赖头奖命中率
    ``likely``     ``E(全池等概率) ≥ required`` —— 划算，但依赖头奖有一定命中率
    ``depends``    仅当头奖命中率 ``p`` 达到阈值才划算，反解一元不等式::

                       p × max + (1 − p) × E(其余) ≥ required
    ============ ====================================================

    这套分层让结论始终**可复核**：每个判定都注明了它在哪个假设下成立。
    """
    benchmark_cents = int(round(draw_point * benchmark_ypp * 100))

    coupons = [p for p in prizes if p.kind != "points"]
    point_prizes = [p for p in prizes if p.kind == "points"]

    unpriced = sum(1 for p in coupons if p.face_value_cents <= 0)
    values = [p.face_value_cents for p in coupons if p.face_value_cents > 0]
    values += [p.value_cents(benchmark_ypp) for p in point_prizes]
    unpriced += sum(1 for p in point_prizes if p.value_cents(benchmark_ypp) <= 0)

    if not values:
        return LotteryAnalysis(
            draw_point=draw_point, benchmark_ypp=benchmark_ypp,
            required_ev_cents=benchmark_cents, max_prize_cents=0, min_coupon_cents=0,
            expected_if_uniform_cents=0, breakeven_prob=None,
            verdict="unknown", reason="奖品池无可定价项，无法判定。",
            unpriced_count=unpriced,
        )

    values.sort()
    lo, hi = values[0], values[-1]
    e_full = int(round(sum(values) / len(values)))
    rest = values[:-1]                      # 扣掉一项最高奖 → 悲观情景
    e_rest = int(round(sum(rest) / len(rest))) if rest else lo
    ratio = (e_full / benchmark_cents) if benchmark_cents else 0.0

    if benchmark_cents <= 0:
        verdict, reason, breakeven = "unknown", "兑换基准为 0，无法比较。", None
    elif hi < benchmark_cents:
        verdict, breakeven = "dominated", None
        reason = (f"奖池最高单项 {fmt_yuan(hi)} 仍低于打平线 {fmt_yuan(benchmark_cents)}，"
                  f"即使必中最大奖也劣于直接兑换 —— 结论不需要任何概率假设。")
    elif lo >= benchmark_cents:
        verdict, breakeven = "robust", 0.0
        reason = (f"奖池最低单项 {fmt_yuan(lo)} 已高于打平线 {fmt_yuan(benchmark_cents)}，"
                  f"无论抽到什么都划算（保底占优）。")
    elif e_rest >= benchmark_cents:
        verdict, breakeven = "robust", 0.0
        reason = (f"即便假设你**永远抽不到最高奖**（{fmt_yuan(hi)}），"
                  f"其余奖品按等概率计的期望仍有 {fmt_yuan(e_rest)}，"
                  f"高于打平线 {fmt_yuan(benchmark_cents)}（{e_rest / benchmark_cents:.1f} 倍）。"
                  f"结论不依赖头奖命中率。")
    elif e_full >= benchmark_cents:
        verdict, breakeven = "likely", 0.0
        reason = (f"全奖池等概率期望 {fmt_yuan(e_full)} 高于打平线 {fmt_yuan(benchmark_cents)}"
                  f"（{ratio:.1f} 倍），倾向抽奖；但若最高奖 {fmt_yuan(hi)} 几乎抽不到，"
                  f"期望会跌破打平线（悲观值 {fmt_yuan(e_rest)}）。")
    else:
        denom = hi - e_rest
        p = (benchmark_cents - e_rest) / denom if denom > 0 else None
        verdict, breakeven = "depends", p
        if p is None:
            reason = "奖池价值分布无法解出概率门槛。"
        else:
            reason = (f"抽奖是否划算取决于头奖命中率：最高奖 {fmt_yuan(hi)} 的命中率须 ≥ "
                      f"{p * 100:.1f}%，否则不如直接兑换"
                      f"（打平线 {fmt_yuan(benchmark_cents)}，悲观期望 {fmt_yuan(e_rest)}）。")

    return LotteryAnalysis(
        draw_point=draw_point, benchmark_ypp=benchmark_ypp,
        required_ev_cents=benchmark_cents, max_prize_cents=hi, min_coupon_cents=lo,
        expected_if_uniform_cents=e_full, expected_pessimistic_cents=e_rest,
        breakeven_prob=breakeven, verdict=verdict, reason=reason,
        unpriced_count=unpriced,
    )


def prize_from_raw(raw: Dict[str, Any], reference_prices: Optional[Dict[str, int]] = None) -> Prize:
    """把 ``query-lottery-info`` 的奖品项归一化。"""
    name = str(raw.get("name") or "")
    kind = "points" if str(raw.get("typeText") or "") in ("积分", "积分奖") else "coupon"
    if kind == "points":
        m = re.search(r"([0-9]+)", name)
        return Prize(name=name, kind="points", points=int(m.group(1)) if m else 0, pricing="points")
    ref = int((reference_prices or {}).get(name, 0))
    face, pricing = parse_face_value(name, ref)
    return Prize(name=name, kind="coupon", face_value_cents=face, pricing=pricing)


# --------------------------------------------------------------------------- #
# 引擎 3：兑换组合求解（背包）
# --------------------------------------------------------------------------- #
def plan_bundles(
    points: int,
    offers: Sequence[RedemptionOffer],
    *,
    top: int = 3,
    max_kinds: int = 5,
) -> List[BundlePlan]:
    """给定积分预算，在**最多 max_kinds 种标的**的约束下求解面值最大组合。

    为什么要限制件数
    ----------------
    不加约束时，DP 的数学最优解会退化成"用 1500 积分换 11 张不同的券"——
    面值确实最大，但现实中没有人会为了 6.9 元去核销第 11 张券。**约束件数之后，
    输出才是可执行的方案**，这是本引擎从"能算"到"能用"的关键一步。

    实现
    ----
    二维 0/1 背包：``dp[k][p]`` = "用至多 k 种标的、**恰好**花掉 p 积分"的最大面值。
    求解后扫描全部 ``k ≤ K, p ≤ 预算`` 取最大 —— 即"**不超预算**"而非"必须用满"
    （用满会让 1500 积分因凑不出整额而无解）。

    候选池按"效率前 N"与"面值前 N"**取并集**：只按效率裁剪会让大额预算只能选到
    一堆 50 积分的小食，永远凑不出大方案。

    最后用"排除法"依次禁用最优解中的每一项重算，得到次优备选。
    """
    if points <= 0:
        return []

    K = max(1, max_kinds)

    def solve(banned: frozenset = frozenset()) -> Optional[BundlePlan]:
        pool = [o for o in offers
                if o.redeemable and o.priced and o.point <= points and o.spu_id not in banned]
        if not pool:
            return None
        pool.sort(key=lambda o: (-o.yuan_per_point, o.point))
        cap = max(K * 12, 30)
        if len(pool) > cap:
            merged = {o.spu_id: o for o in pool[:cap]}
            for o in sorted(pool, key=lambda x: (-x.face_value_cents, x.point))[:cap]:
                merged.setdefault(o.spu_id, o)
            pool = sorted(merged.values(), key=lambda o: (-o.yuan_per_point, o.point))

        limit = min(K, len(pool))
        # dp[k][p] = (最大面值, 已选下标 tuple)；None 表示该状态不可达
        dp: List[List[Optional[Tuple[int, Tuple[int, ...]]]]] = [
            [None] * (points + 1) for _ in range(limit + 1)
        ]
        dp[0][0] = (0, ())
        for idx, o in enumerate(pool):
            for k in range(min(limit, idx + 1), 0, -1):
                row, prev_row = dp[k], dp[k - 1]
                for p in range(points, o.point - 1, -1):
                    prev = prev_row[p - o.point]
                    if prev is None:
                        continue
                    cand = prev[0] + o.face_value_cents
                    cur = row[p]
                    if cur is None or cand > cur[0]:
                        row[p] = (cand, prev[1] + (idx,))

        # 不超预算：扫描所有可达状态取最大面值
        best: Optional[Tuple[int, Tuple[int, ...]]] = None
        for k in range(limit + 1):
            for p in range(points + 1):
                cell = dp[k][p]
                if cell and cell[0] > 0 and (best is None or cell[0] > best[0]):
                    best = cell
        if best is None:
            return None

        counts: Dict[int, int] = {}
        for i in best[1]:
            counts[i] = counts.get(i, 0) + 1
        picks = [(pool[i], q) for i, q in sorted(counts.items())]
        spent = sum(o.point * q for o, q in picks)
        return BundlePlan(picks=picks, spent_point=spent, total_value_cents=best[0],
                          leftover_point=points - spent)

    best_plan = solve()
    if best_plan is None:
        return []

    results: List[BundlePlan] = [best_plan]
    for o, _ in best_plan.picks:
        if len(results) >= top:
            break
        alt = solve(frozenset({o.spu_id}))
        if alt is None:
            continue
        sig = tuple(sorted((x.spu_id, q) for x, q in alt.picks))
        if all(tuple(sorted((x.spu_id, q) for x, q in r.picks)) != sig for r in results):
            results.append(alt)

    for r in results:
        r.leftover_value_cents = _best_small_use(r.leftover_point, offers)
    results.sort(key=lambda r: (-r.total_value_cents, r.spent_point))
    return results[:top]


def _best_small_use(leftover: int, offers: Sequence[RedemptionOffer]) -> int:
    """余数的最优小额利用：把剩余积分单独换成面值最高的一件。"""
    if leftover <= 0:
        return 0
    cands = [o for o in offers if o.redeemable and o.priced and o.point <= leftover]
    if not cands:
        return 0
    return max(c.face_value_cents for c in cands)


# --------------------------------------------------------------------------- #
# 引擎 4：过期止损
# --------------------------------------------------------------------------- #
def plan_expiry(
    expiring_point: int,
    offers: Sequence[RedemptionOffer],
    *,
    draw_point: int = 0,
    lottery: Optional[LotteryAnalysis] = None,
    top: int = 2,
    max_kinds: int = 5,
) -> ExpiryPlan:
    """积分即将过期时的止损方案。

    顺序（与用户直觉一致，且经济上最优）：
      1. 先用**面值最大化**的兑换组合吃掉大头积分（确定收益最高）；
      2. 余额若够一次抽奖且抽奖未被严格劣汰，则用抽奖清零（避免归零）；
      3. 仍无法消耗的部分，明确告知**具体损失金额**。
    """
    plan = ExpiryPlan(expiring_point=expiring_point)
    if expiring_point <= 0:
        return plan

    benchmark = lottery.benchmark_ypp if lottery else 0.0
    if benchmark <= 0:
        priced = [o for o in offers if o.redeemable and o.priced]
        best = max(priced, key=lambda o: o.yuan_per_point, default=None)
        benchmark = best.yuan_per_point if best else 0.0

    remaining = expiring_point
    bundles = plan_bundles(remaining, offers, top=top, max_kinds=max_kinds)
    if bundles:
        take = bundles[0]
        plan.bundles = [take]
        plan.salvaged_cents += take.total_value_cents
        remaining -= take.spent_point

    if draw_point > 0 and remaining >= draw_point and (lottery is None or lottery.verdict != "dominated"):
        draws = remaining // draw_point
        plan.lottery_draws = draws
        plan.lottery_point = draws * draw_point
        plan.lottery_value_cents = int(round(plan.lottery_point * benchmark * 100))
        plan.salvaged_cents += plan.lottery_value_cents
        remaining -= plan.lottery_point

    plan.lost_point = remaining
    plan.lost_cents = int(round(remaining * benchmark * 100))
    return plan


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _load(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _load_offers(path: str, refs: Optional[Dict[str, int]] = None) -> List[RedemptionOffer]:
    data = _load(path)
    rows = data.get("data", data) if isinstance(data, dict) else data
    return [offer_from_raw(r, refs) for r in rows]


def _lottery_from(path: str, refs_path: Optional[str], benchmark_ypp: float) -> LotteryAnalysis:
    """从 ``query-lottery-info`` 的原始响应构建抽奖分析。"""
    raw = _load(path)
    d = raw.get("data", raw) if isinstance(raw, dict) else raw
    refs: Dict[str, int] = {}
    if refs_path:
        loaded = _load(refs_path)
        # 映射表内金额已是「分」，无需再换算；下划线开头的键为元数据说明。
        refs = {k: int(v) for k, v in loaded.items()
                if not str(k).startswith("_") and isinstance(v, (int, float))}
    prizes = [prize_from_raw(x, refs) for x in d.get("prizes", [])]
    draw_point = int(str(d.get("drawPoint") or 0) or 0)
    return analyze_lottery(draw_point, prizes, benchmark_ypp)


def _print_matrix(m: ValueMatrix, only_active: bool = False) -> None:
    rows = m.active if only_active else m.ranked
    scope = "（仅周期内）" if only_active else ""
    print(f"可定价标的 {len(m.ranked)} 项{scope}"
          f"（周期内 {len(m.active)} · 已过周期 {len(m.stale)} · 无法定价 {len(m.unpriced)}）")
    print(f"中位效率 {fmt_ypp(m.median_ypp)}")
    print(f"效率倍数差：全库 {m.spread:.1f} 倍 | **周期内 {m.active_spread:.1f} 倍**"
          f"（拿掉历史活动价后的真实差距）\n")
    print("── 性价比排行（同积分换到的价值）──")
    for i, o in enumerate(rows, 1):
        tags = []
        if o in m.traps:
            tags.append("陷阱")
        if window_state(o, m.now) == WINDOW_STALE:
            tags.append("待复核")
        flag = ("  ← " + "/".join(tags)) if tags else ""
        print(f"{i:>2}. {fmt_ypp(o.yuan_per_point):>14}  {o.point:>5} 积分  {o.name}{flag}")
    if m.traps:
        cutoff = m.median_ypp * 0.5
        print(f"\n⚠ {len(m.traps)} 个标的低于中位效率一半（{fmt_ypp(cutoff)}），属积分黑洞：")
        for o in m.traps:
            loss = opportunity_cost_cents(o, m.benchmark_ypp)
            print(f"   · {o.name}（{o.point} 积分）机会成本 {fmt_yuan(loss)}")
    if m.stale:
        print(f"\n注：{len(m.stale)} 个标的已过 downTime 但接口仍返回「上架」，"
              f"本引擎按接口状态视为可兑换，建议下单前复核。")
    print(f"\n{DISCLAIMER}")


def _print_lottery(a: LotteryAnalysis) -> None:
    print(f"单次消耗 {a.draw_point} 积分 | 兑换基准 {fmt_ypp(a.benchmark_ypp)}")
    print(f"打平直接兑换所需期望价值：{fmt_yuan(a.required_ev_cents)}")
    excluded = f"（另有 {a.unpriced_count} 项无法定价、未纳入判定）" if a.unpriced_count else ""
    print(f"奖池已定价项{excluded}")
    print(f"  最高单项 {fmt_yuan(a.max_prize_cents)} | 最低 {fmt_yuan(a.min_coupon_cents)}"
          f" | 全池等概率期望 {fmt_yuan(a.expected_if_uniform_cents)}"
          f" | 悲观期望 {fmt_yuan(a.expected_pessimistic_cents)}")
    verdict_cn = {
        "dominated": "抽奖被严格劣汰 —— 必亏，不如直接兑换",
        "robust": "抽奖占优 —— 结论不依赖头奖命中率",
        "likely": "倾向抽奖 —— 但依赖头奖命中率",
        "depends": "取决于头奖命中率",
        "unknown": "无法判定",
    }.get(a.verdict, a.verdict)
    print(f"\n判定：{verdict_cn}（{a.verdict.upper()}）")
    print(f"  {a.reason}")
    print(f"\n{DISCLAIMER}")


def _print_bundles(points: int, plans: List[BundlePlan]) -> None:
    if not plans:
        print(f"{points} 积分内找不到可兑换组合。")
        return
    print(f"预算 {points} 积分，面值最大的 {len(plans)} 种兑换组合：\n")
    for i, p in enumerate(plans, 1):
        items = " + ".join(f"{o.name}×{q}" if q > 1 else o.name for o, q in p.picks)
        print(f"{i}. 面值 {fmt_yuan(p.total_value_cents)} | 花 {p.spent_point} 积分 "
              f"| 效率 {fmt_ypp(p.efficiency)} | 余 {p.leftover_point}")
        print(f"   {items}")
        if p.leftover_value_cents:
            print(f"   余额可再换一件面值 {fmt_yuan(p.leftover_value_cents)} 的标的")
    print(f"\n{DISCLAIMER}")


def _print_expiry(p: ExpiryPlan) -> None:
    print(f"将过期积分：{p.expiring_point}")
    if p.bundles:
        b = p.bundles[0]
        items = " + ".join(f"{o.name}×{q}" if q > 1 else o.name for o, q in b.picks)
        print(f"\n① 兑换（挽回 {fmt_yuan(b.total_value_cents)}）：{items}")
    if p.lottery_draws:
        print(f"② 余额抽奖 {p.lottery_draws} 次（消耗 {p.lottery_point} 积分，"
              f"按基准折价 {fmt_yuan(p.lottery_value_cents)}）")
    print(f"\n合计挽回：{fmt_yuan(p.salvaged_cents)}")
    if p.lost_point:
        print(f"⚠ 仍有 {p.lost_point} 积分无法消耗，预计损失 {fmt_yuan(p.lost_cents)}")
    print(f"\n{DISCLAIMER}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="麦麦积分精算师 · 积分资产价值引擎")
    p.add_argument("--now", default=None,
                   help="基准时间（YYYY-MM-DD[ HH:MM:SS]），用于判定上架周期；默认取系统当前时间")
    sub = p.add_subparsers(dest="cmd", required=True)

    m = sub.add_parser("matrix", help="兑换价值矩阵与性价比陷阱")
    m.add_argument("--products", required=True)
    m.add_argument("--only-active", action="store_true", help="只显示上架周期内的标的")
    m.add_argument("--json", action="store_true")

    lo = sub.add_parser("lottery", help="抽奖 vs 兑换的期望值判定")
    lo.add_argument("--lottery", required=True)
    lo.add_argument("--products", required=True)
    lo.add_argument("--refs", default=None, help="折扣券原价映射 JSON（奖品名 -> 原价分）")
    lo.add_argument("--json", action="store_true")

    pl = sub.add_parser("plan", help="给定积分预算的面值最大化组合")
    pl.add_argument("--points", type=int, required=True)
    pl.add_argument("--products", required=True)
    pl.add_argument("--top", type=int, default=3)
    pl.add_argument("--max-kinds", type=int, default=5, help="组合中最多包含几种标的（默认 5）")
    pl.add_argument("--json", action="store_true")

    ex = sub.add_parser("expire", help="积分到期止损方案")
    ex.add_argument("--expiring", type=int, required=True)
    ex.add_argument("--products", required=True)
    ex.add_argument("--lottery", default=None)
    ex.add_argument("--refs", default=None, help="折扣券原价映射 JSON（奖品名 -> 原价分）")
    ex.add_argument("--max-kinds", type=int, default=5, help="兑换组合最多含几种标的（默认 5）")
    ex.add_argument("--json", action="store_true")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    now = parse_mcd_time(args.now) if args.now else _dt.datetime.now()

    if args.cmd == "matrix":
        m = build_value_matrix(_load_offers(args.products), now=now)
        if args.json:
            print(json.dumps(m.to_dict(), ensure_ascii=False, indent=2))
        else:
            _print_matrix(m, only_active=args.only_active)
        return 0 if m.ranked else 1

    if args.cmd == "lottery":
        m = build_value_matrix(_load_offers(args.products), now=now)
        a = _lottery_from(args.lottery, getattr(args, "refs", None), m.benchmark_ypp)
        print(json.dumps(a.to_dict(), ensure_ascii=False, indent=2)) if args.json else _print_lottery(a)
        return 0

    if args.cmd == "plan":
        plans = plan_bundles(args.points, _load_offers(args.products),
                             top=args.top, max_kinds=args.max_kinds)
        if args.json:
            print(json.dumps([p.to_dict() for p in plans], ensure_ascii=False, indent=2))
        else:
            _print_bundles(args.points, plans)
        return 0 if plans else 1

    if args.cmd == "expire":
        offers = _load_offers(args.products)
        lottery = None
        draw_point = 0
        if args.lottery:
            m = build_value_matrix(offers, now=now)
            lottery = _lottery_from(args.lottery, getattr(args, "refs", None), m.benchmark_ypp)
            draw_point = lottery.draw_point
        p = plan_expiry(args.expiring, offers, draw_point=draw_point, lottery=lottery,
                        max_kinds=args.max_kinds)
        print(json.dumps(p.to_dict(), ensure_ascii=False, indent=2)) if args.json else _print_expiry(p)
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
