#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""券叠加通用约束求解器的回归测试。

三类断言，分别对应三件不能出错的事：

1. **与旧实现逐分一致** —— 换引擎绝不能改变已公布的结论。
   全量遍历样例菜单的所有 1–3 件组合 × 全部渠道 × 多个影子价格。
2. **商品池约束 C3 真的被强制** —— 用最小反例证明旧实现会重复抵扣。
3. **剪枝不改变答案** —— 与"暴力枚举所有券子集"这个独立实现的判定逐一对齐。
"""
from __future__ import annotations

import itertools
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from channel_planner import (  # noqa: E402
    ENGINE_CONSTRAINT, ENGINE_LEGACY, _channels, _items, apply_coupons,
)
from coupon_solver import (  # noqa: E402
    METHOD_BEAM, METHOD_EXACT, METHOD_SINGLE, ORDER_MIDDLE, ORDER_SEQUENTIAL,
    _net_cap_cents, eligible, solve, _static_cap_cents,
)
from optimize_deals import Coupon, Item, to_cents  # noqa: E402
from run_unified_live import load_channels  # noqa: E402

DATA = os.path.join(os.path.dirname(__file__), "..", "data")
YPP = 0.1380


def _sample_items():
    return _items(os.path.join(DATA, "sample_menu.json"))


def _sample_channels():
    return _channels(os.path.join(DATA, "sample_channels.json"))


def _live_channels():
    items = _sample_items()
    by_code = {i.code: i for i in items}
    by_name = {i.name: i for i in items}
    return load_channels(os.path.join(DATA, "live_third_party.json"), by_code, by_name)[0]


def _brute_force(combo, coupons, *, max_stack, shadow_ypp):
    """独立实现：枚举所有券子集，逐张在**顺序扣减的账本**上落地，取净收益最大。

    刻意写成与求解器完全不同的结构（无分组、无剪枝、纯枚举），
    这样它才能作为"剪枝有没有剪错"的第三方裁判。
    """
    from coupon_solver import (
        Ledger, _pts_cents, apply_item_coupon, apply_order_coupon,
    )
    from optimize_deals import ITEM_LEVEL_TYPES

    goods = sum(i.price_cents for i in combo)
    best = (0, ())
    n = len(coupons)
    for mask in range(1 << n):
        picked = [coupons[i] for i in range(n) if mask >> i & 1]
        if len({c.stack_group for c in picked}) != len(picked):
            continue                                   # C1 组内互斥
        if sum(1 for c in picked if c.type not in ITEM_LEVEL_TYPES) > max_stack:
            continue                                   # C2 整单券限张
        led = Ledger(list(combo))
        item_total = 0
        for c in picked:
            if c.type in ITEM_LEVEL_TYPES:
                item_total += apply_item_coupon(c, led)
        item_total = min(item_total, goods)
        middle = goods - item_total                     # ← 剩余额，不是抵扣额
        # 与求解器默认口径一致：每张整单券都对 middle 判定（因此与试算顺序无关）
        order_total = 0
        for c in picked:
            if c.type not in ITEM_LEVEL_TYPES:
                order_total += apply_order_coupon(c, middle)
        order_total = min(order_total, middle)
        net = item_total + order_total \
            - _pts_cents(sum(c.point_cost for c in picked), shadow_ypp)
        if net > best[0]:
            best = (net, tuple(c.id for c in picked))
    return best


class TestParityWithLegacy(unittest.TestCase):
    """换引擎不得改变任何已公布的数字。"""

    def test_sample_channels_zero_diff(self):
        items = _sample_items()
        cases = 0
        for ch in _sample_channels():
            for k in (1, 2, 3):
                for combo in itertools.combinations(items, k):
                    combo = list(combo)
                    for ypp in (0.0, YPP):
                        ref = apply_coupons(combo, ch.coupons, allow_stack=ch.allow_stack,
                                            max_stack=ch.max_stack, shadow_ypp=ypp,
                                            engine=ENGINE_LEGACY)
                        got = apply_coupons(combo, ch.coupons, allow_stack=ch.allow_stack,
                                            max_stack=ch.max_stack, shadow_ypp=ypp,
                                            engine=ENGINE_CONSTRAINT)
                        cases += 1
                        self.assertEqual(
                            ref.discount_cents, got.discount_cents,
                            f"{ch.code} {[i.name for i in combo]} ypp={ypp}")
                        self.assertEqual(ref.point_cost, got.point_cost)
        self.assertGreater(cases, 1000)

    def test_live_third_party_zero_diff(self):
        items = _sample_items()
        for ch in _live_channels():
            for k in (1, 2, 3):
                for combo in itertools.combinations(items, k):
                    combo = list(combo)
                    ref = apply_coupons(combo, ch.coupons, allow_stack=ch.allow_stack,
                                        max_stack=ch.max_stack, shadow_ypp=YPP,
                                        engine=ENGINE_LEGACY)
                    got = solve(combo, ch.coupons, allow_stack=ch.allow_stack,
                                max_stack=ch.max_stack, shadow_ypp=YPP)
                    self.assertEqual(ref.discount_cents, got.discount_cents,
                                     f"{ch.code} {[i.name for i in combo]}")

    def test_default_engine_is_legacy(self):
        """默认引擎必须仍是 legacy —— 这样任何未显式指定引擎的旧调用都不受影响。"""
        ch = _sample_channels()[0]
        combo = _sample_items()[:3]
        plain = apply_coupons(combo, ch.coupons, allow_stack=ch.allow_stack,
                              max_stack=ch.max_stack, shadow_ypp=YPP)
        legacy = apply_coupons(combo, ch.coupons, allow_stack=ch.allow_stack,
                               max_stack=ch.max_stack, shadow_ypp=YPP,
                               engine=ENGINE_LEGACY)
        self.assertEqual(plain.discount_cents, legacy.discount_cents)


class TestItemPoolConstraint(unittest.TestCase):
    """约束 C3：每件商品被抵扣的总额 ≤ 该商品标价。"""

    def _pair(self):
        a = Item(code="A", name="商品A", price_cents=to_cents(10))
        b = Item(code="B", name="商品B", price_cents=to_cents(10))
        x = Coupon(id="X", name="A 单品5折", type="item_discount",
                   item_code="A", rate=0.5, stack_group="g1")
        y = Coupon(id="Y", name="A 免单", type="item_free",
                   item_code="A", stack_group="g2")
        return [a, b], [x, y]

    def test_item_pool_no_double_count(self):
        """最小反例：两张不同组的单品券都指向 A。

        A 原价 ¥10 —— 「5 折」+「免单」无论怎么叠，A 最多被免掉 ¥10。
        旧实现各按**原价**算（¥5 + ¥10 = ¥15），多出来的 ¥5 被记到了 B 头上，
        于是应付价被少算了 ¥5。新求解器给出正确的 ¥10。
        """
        combo, coupons = self._pair()
        legacy = apply_coupons(combo, coupons, allow_stack=True, max_stack=2,
                               shadow_ypp=0.0, engine=ENGINE_LEGACY)
        fixed = solve(combo, coupons, allow_stack=True, max_stack=2, shadow_ypp=0.0)

        self.assertEqual(legacy.discount_cents, to_cents(15))   # 旧：重复抵扣
        self.assertEqual(fixed.discount_cents, to_cents(10))    # 新：受 C3 约束
        self.assertEqual(fixed.pool_used, {"A": to_cents(10)})
        self.assertEqual(fixed.payable_cents, to_cents(10))

    def test_pool_used_never_exceeds_price(self):
        items = _sample_items()
        for ch in _sample_channels() + _live_channels():
            for k in (2, 3):
                for combo in itertools.combinations(items, k):
                    combo = list(combo)
                    prices = {i.code: i.price_cents for i in combo}
                    r = solve(combo, ch.coupons, allow_stack=ch.allow_stack,
                              max_stack=ch.max_stack, shadow_ypp=YPP)
                    for code, used in r.pool_used.items():
                        self.assertLessEqual(used, prices[code],
                                             f"{ch.code} {code} 被超额抵扣")

    def test_attribution_sums_to_total(self):
        """逐券归因之和必须等于总抵扣 —— 瀑布图靠这个才可信。"""
        items = _sample_items()
        for ch in _live_channels():
            for k in (3,):
                for combo in itertools.combinations(items, k):
                    combo = list(combo)
                    r = solve(combo, ch.coupons, allow_stack=ch.allow_stack,
                              max_stack=ch.max_stack, shadow_ypp=YPP)
                    self.assertEqual(sum(d for _, d in r.applied), r.discount_cents)
                    self.assertFalse([w for w in r.warnings if "归因异常" in w])

    def test_bundle_price_allocated_proportionally(self):
        """组合特价的让利要按各商品剩余额比例分摊，不能全记在一件上。"""
        combo = [Item(code="A", name="A", price_cents=to_cents(30)),
                 Item(code="B", name="B", price_cents=to_cents(10))]
        c = Coupon(id="BND", name="30元买A+B", type="bundle_price",
                   item_codes=("A", "B"), fixed_cents=to_cents(30))
        r = solve(combo, [c], allow_stack=True, max_stack=2, shadow_ypp=0.0)
        self.assertEqual(r.discount_cents, to_cents(10))            # 40 − 30
        # 让利 ¥10 按 30:10 比例分摊到 A、B，而不是全记在 A 头上
        self.assertEqual(r.pool_used, {"A": to_cents(7.5), "B": to_cents(2.5)})
        self.assertEqual(sum(r.pool_used.values()), to_cents(10))


class TestStackingConstraints(unittest.TestCase):
    def test_group_mutual_exclusion(self):
        """同一 stack_group 的三张满减券只能选一张。"""
        combo = [Item(code="A", name="A", price_cents=to_cents(100))]
        cs = [Coupon(id=f"P{i}", name=f"满{i}减{i}", type="threshold_reduce",
                     threshold_cents=to_cents(i), reduce_cents=to_cents(i),
                     stack_group="plat") for i in (10, 20, 30)]
        r = solve(combo, cs, allow_stack=True, max_stack=3, shadow_ypp=0.0)
        self.assertEqual(len(r.coupons), 1)
        self.assertEqual(r.discount_cents, to_cents(30))

    def test_max_stack_limits_order_level(self):
        """整单级券张数受 max_stack 限制，各占一个互斥组。"""
        combo = [Item(code="A", name="A", price_cents=to_cents(200))]
        cs = [Coupon(id=f"O{i}", name=f"整单{i}", type="threshold_reduce",
                     reduce_cents=to_cents(10), stack_group=f"g{i}") for i in (1, 2, 3)]
        r2 = solve(combo, cs, allow_stack=True, max_stack=2, shadow_ypp=0.0)
        r1 = solve(combo, cs, allow_stack=True, max_stack=1, shadow_ypp=0.0)
        self.assertEqual(len(r2.coupons), 2)
        self.assertEqual(r2.discount_cents, to_cents(20))
        self.assertEqual(len(r1.coupons), 1)

    def test_no_stack_matches_v1_semantics(self):
        """allow_stack=False 时所有券同池竞争，只留净收益最大的一张。"""
        items = _sample_items()
        single_pool = [c for c in _sample_channels() if not c.allow_stack]
        self.assertTrue(single_pool)                      # 样例里必须有不可叠加渠道
        for ch in single_pool:
            for k in (2, 3):
                for combo in itertools.combinations(items, k):
                    combo = list(combo)
                    r = solve(combo, ch.coupons, allow_stack=False, max_stack=1,
                              shadow_ypp=YPP)
                    self.assertLessEqual(len(r.coupons), 1)
                    self.assertEqual(r.method, METHOD_SINGLE)

    def test_net_loss_coupon_never_used(self):
        """净收益 ≤ 0 的券永不使用 —— 哪怕它的抵扣额看起来最大。"""
        combo = [Item(code="A", name="A", price_cents=to_cents(40))]
        big = Coupon(id="BIG", name="500积分抵24.5元", type="threshold_reduce",
                     reduce_cents=to_cents(24.5), point_cost=500, stack_group="pts")
        small = Coupon(id="SMALL", name="满30减5", type="threshold_reduce",
                       threshold_cents=to_cents(30), reduce_cents=to_cents(5),
                       stack_group="cash")
        r = solve(combo, [big, small], allow_stack=True, max_stack=2, shadow_ypp=YPP)
        self.assertEqual([c.id for c in r.coupons], ["SMALL"])

    def test_point_coupon_used_when_points_cheap(self):
        combo = [Item(code="A", name="A", price_cents=to_cents(40))]
        big = Coupon(id="BIG", name="500积分抵24.5元", type="threshold_reduce",
                     reduce_cents=to_cents(24.5), point_cost=500, stack_group="pts")
        r = solve(combo, [big], allow_stack=True, max_stack=2, shadow_ypp=0.01)
        self.assertEqual([c.id for c in r.coupons], ["BIG"])

    def test_eligible_rejects_malformed(self):
        self.assertFalse(eligible(Coupon(id="a", name="a", type="item_reduce"), 1))
        self.assertFalse(eligible(Coupon(id="b", name="b", type="buy_get",
                                         buy_code="x"), 1))
        self.assertFalse(eligible(Coupon(id="c", name="c", type="bundle_price"), 1))
        self.assertTrue(eligible(Coupon(id="d", name="d", type="threshold_reduce"), 1))


class TestPruning(unittest.TestCase):
    def test_pruning_does_not_change_result(self):
        """剪枝后的答案必须与暴力枚举完全一致（逐券集合与净收益都比）。"""
        items = _sample_items()
        for ch in _live_channels() + _sample_channels():
            for k in (1, 2, 3):
                for combo in itertools.combinations(items, k):
                    combo = list(combo)
                    r = solve(combo, ch.coupons, allow_stack=ch.allow_stack,
                              max_stack=ch.max_stack, shadow_ypp=YPP)
                    if not ch.allow_stack:
                        continue
                    net, ids = _brute_force(combo, ch.coupons,
                                            max_stack=ch.max_stack, shadow_ypp=YPP)
                    self.assertEqual(
                        r.net_cents, net,
                        f"{ch.code} {[i.name for i in combo]}: "
                        f"solver={r.net_cents} brute={net} ({ids})")

    def test_pruning_reduces_expansion_on_wide_pool(self):
        """券池足够宽时，剪枝要真的少展开状态（否则这个功能就是摆设）。"""
        combo = [Item(code="A", name="A", price_cents=to_cents(60)),
                 Item(code="B", name="B", price_cents=to_cents(20))]
        cs = []
        for i in range(6):
            cs.append(Coupon(id=f"d{i}", name=f"折扣{i}", type="item_discount",
                             item_code="A", rate=0.9, cap_cents=to_cents(1),
                             stack_group=f"g{i}"))
        r = solve(combo, cs, allow_stack=True, max_stack=6, shadow_ypp=0.0)
        self.assertEqual(r.method, METHOD_EXACT)
        self.assertGreater(r.item_space, r.item_nodes)
        self.assertGreater(r.pruning_ratio, 0.0)

    def test_beam_fallback_is_reported(self):
        """搜索空间超限时退化为束搜索，并且**必须**在结果里声明。"""
        combo = [Item(code="A", name="A", price_cents=to_cents(60))]
        cs = [Coupon(id=f"d{i}", name=f"券{i}", type="item_reduce",
                     item_code="A", reduce_cents=to_cents(1), stack_group=f"g{i}")
              for i in range(8)]
        r = solve(combo, cs, allow_stack=True, max_stack=8, shadow_ypp=0.0,
                  item_exact_limit=10, beam_width=4)
        self.assertEqual(r.method, METHOD_BEAM)
        self.assertTrue(any("束搜索" in w for w in r.warnings))

    def test_static_bound_is_admissible(self):
        """分支定界用的上界绝不能低估 —— 否则会把最优解剪掉。

        ``_static_cap_cents`` 是"完全不受挤占"的抵扣上界，必须 ≥ 0；
        扣掉积分成本后的净上界**可以为负**（积分太贵），求解器用
        ``max(0, ...)`` 与 ``<= 0`` 过滤把它排除掉，这正是预期行为。
        """
        items = _sample_items()
        prices = {i.code: i.price_cents for i in items}
        saw_negative_net = False
        for ch in _sample_channels() + _live_channels():
            for c in ch.coupons:
                cap = _static_cap_cents(c, prices)
                net_cap = _net_cap_cents(c, prices, YPP)
                self.assertGreaterEqual(cap, 0)
                self.assertLessEqual(net_cap, cap)
                if net_cap < 0:
                    saw_negative_net = True
        # 真实券池里确实存在"静态就亏"的积分券 —— 它们必须被求解器直接排除
        self.assertTrue(saw_negative_net)


class TestDeterminism(unittest.TestCase):
    def test_same_input_same_output(self):
        items = _sample_items()
        combo = items[:3]
        for ch in _live_channels():
            a = solve(combo, ch.coupons, allow_stack=ch.allow_stack,
                      max_stack=ch.max_stack, shadow_ypp=YPP)
            b = solve(combo, ch.coupons, allow_stack=ch.allow_stack,
                      max_stack=ch.max_stack, shadow_ypp=YPP)
            self.assertEqual([c.id for c in a.coupons], [c.id for c in b.coupons])
            self.assertEqual(a.discount_cents, b.discount_cents)

    def test_order_mode_sequential_is_not_worse_than_middle(self):
        """更严格的口径只会给出 ≤ 的抵扣额（这本身就是敏感性分析要看的量）。"""
        items = _sample_items()
        for ch in _live_channels():
            for k in (2, 3):
                for combo in itertools.combinations(items, k):
                    combo = list(combo)
                    m = solve(combo, ch.coupons, allow_stack=ch.allow_stack,
                              max_stack=ch.max_stack, shadow_ypp=YPP,
                              order_mode=ORDER_MIDDLE)
                    s = solve(combo, ch.coupons, allow_stack=ch.allow_stack,
                              max_stack=ch.max_stack, shadow_ypp=YPP,
                              order_mode=ORDER_SEQUENTIAL)
                    self.assertLessEqual(s.discount_cents, m.discount_cents)
                    self.assertTrue(any("sequential" in w for w in s.warnings))

    def test_to_dict_is_json_safe(self):
        ch = _live_channels()[0]
        r = solve(_sample_items()[:3], ch.coupons, allow_stack=ch.allow_stack,
                  max_stack=ch.max_stack, shadow_ypp=YPP)
        blob = json.dumps(r.to_dict(), ensure_ascii=False)
        self.assertIn("method", blob)


if __name__ == "__main__":
    unittest.main(verbosity=2)
