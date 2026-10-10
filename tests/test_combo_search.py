#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""组合枚举剪枝的单元测试。

剪枝最危险的失败模式是"看起来更快了"：它确实少算了，但如果下界不成立，
它会**静悄悄地把最优解剪掉**，返回一个更差（但同样"看起来合理"）的方案，
而且不报任何错。所以这里测的不是"快了多少"，而是四件事：

1. 抵扣上界**真的可采纳** —— 随机券池 × 随机购物车，逐个与引擎实际抵扣对拍
2. 下界**真的可采纳且单调不减** —— 否则"后代更差"不成立，整棵剪枝就是错的
3. ``pruned`` 与 ``full`` 结果**逐字段相同**，且与**独立实现**的暴力枚举对拍
4. 约束剪枝（掩码版）与 ``optimize_deals._satisfies`` 全组合对拍

第 1 条曾经真的红过，见 :class:`TestDiscountCeilingMixedStage`。
"""
from __future__ import annotations

import contextlib
import io
import itertools
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from channel_planner import (  # noqa: E402
    Channel, _channels, _items, apply_coupons, compare_channels, price_in_channel,
)
from combo_search import (  # noqa: E402
    COMBOS_FULL, COMBOS_PRUNED, UNBOUNDED, BoundInfo, ComboIndex, SearchStats,
    brute_force, build_parser, count_combos, coupon_ceiling, discount_ceiling,
    main as combo_main, make_bound, print_bound, print_stats, search,
)
from optimize_deals import Constraints, Coupon, Item, _satisfies, single_coupon_discount  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")
MENU = os.path.join(ROOT, "data", "sample_menu.json")
CHANNELS = os.path.join(ROOT, "data", "sample_channels.json")


# --------------------------------------------------------------------------- #
# 构造工具
# --------------------------------------------------------------------------- #
def mk_item(code: str, cents: int, name: str = "") -> Item:
    return Item(code=code, name=name or code, price_cents=cents)


def mk_channel(code: str = "CH", **kw) -> Channel:
    kw.setdefault("source", "external")
    return Channel(code=code, name=code, **kw)


def mk_coupon(cid: str, ctype: str, **kw) -> Coupon:
    kw.setdefault("name", cid)
    return Coupon(id=cid, type=ctype, **kw)


SMALL_POOL = [mk_item("A", 1000), mk_item("B", 2000), mk_item("C", 3000)]


def all_carts(pool, k_min=1, k_max=None):
    """池子里所有大小在 ``[k_min, k_max]`` 的子集。"""
    n = len(pool)
    hi = n if k_max is None else min(k_max, n)
    for k in range(k_min, hi + 1):
        yield from itertools.combinations(pool, k)


def landed_cost(combo, channels, *, shadow_ypp=0.0, objective="landed"):
    """把购物车折算成与 ``search`` 的键可直接比较的成本；全渠道不可行则 None。"""
    quotes = compare_channels(list(combo), channels, shadow_ypp=shadow_ypp)
    ok = [q for q in quotes if q.feasible]
    if not ok:
        return None
    pick = (lambda q: q.landed_cents) if objective == "landed" else (lambda q: q.cash_cents)
    return min(pick(q) for q in ok)


def make_evaluate(channels, *, shadow_ypp=0.138, objective="landed"):
    def evaluate(combo):
        cost = landed_cost(combo, channels, shadow_ypp=shadow_ypp, objective=objective)
        if cost is None:
            return None
        return (cost, len(combo)), tuple(sorted(i.code for i in combo))
    return evaluate


def cheap_eval(combo):
    """只关心枚举范围、不关心成本时的桩函数：成本恒为 1 分。"""
    return (1, len(combo)), tuple(i.code for i in combo)


def make_spy(seen):
    """记录被报价过的组合，便于断言"哪些组合根本没算"。"""
    def spy(combo):
        seen.append(tuple(i.code for i in combo))
        return (1, len(combo)), tuple(i.code for i in combo)
    return spy


def random_coupon(rng, pool, cid="C") -> Coupon:
    """覆盖全部 7 种券型 + 若干"券面缺字段"的坏券。"""
    codes = [i.code for i in pool]
    kind = rng.choice([
        "threshold_reduce", "item_reduce", "item_free", "buy_get",
        "discount", "item_discount", "bundle_price",
        "unknown_type", "blank_item", "blank_buy_get", "short_bundle",
    ])
    group = rng.choice(["", "", "g1", "g2"])
    base = dict(id=cid, name=cid, stack_group=group,
                point_cost=rng.choice([0, 0, 20, 500]))
    if kind == "threshold_reduce":
        return Coupon(type="threshold_reduce",
                      threshold_cents=rng.choice([0, 2000, 3000]),
                      reduce_cents=rng.choice([100, 500, 800]), **base)
    if kind == "item_reduce":
        return Coupon(type="item_reduce", item_code=rng.choice(codes),
                      reduce_cents=rng.choice([100, 400, 9999]), **base)
    if kind == "item_free":
        return Coupon(type="item_free", item_code=rng.choice(codes), **base)
    if kind == "buy_get":
        return Coupon(type="buy_get", buy_code=rng.choice(codes),
                      get_code=rng.choice(codes), **base)
    if kind == "discount":
        return Coupon(type="discount", threshold_cents=rng.choice([0, 2000]),
                      rate=rng.choice([0.8, 0.88, 0.5]),
                      cap_cents=rng.choice([0, 0, 1200]), **base)
    if kind == "item_discount":
        return Coupon(type="item_discount", item_code=rng.choice(codes),
                      rate=rng.choice([0.5, 0.8]),
                      cap_cents=rng.choice([0, 300]), **base)
    if kind == "bundle_price":
        return Coupon(type="bundle_price",
                      item_codes=tuple(rng.sample(codes, rng.randint(1, min(3, len(codes))))),
                      fixed_cents=rng.choice([0, 1000, 2500]), **base)
    if kind == "unknown_type":
        return Coupon(type="mystery_2027", reduce_cents=9999, **base)
    if kind == "blank_item":
        return Coupon(type="item_free", item_code="", **base)
    if kind == "blank_buy_get":
        return Coupon(type="buy_get", buy_code="", get_code=rng.choice(codes), **base)
    return Coupon(type="bundle_price", item_codes=("NOPE", rng.choice(codes)),
                  fixed_cents=100, **base)          # 池里凑不齐


def random_channel(rng, pool, code="CH") -> Channel:
    return Channel(
        code=code, name=code, source="external",
        delivery_fee_cents=rng.choice([0, 350, 450, 900]),
        packaging_fee_cents=rng.choice([0, 100, 150]),
        min_order_cents=rng.choice([0, 0, 2000]),
        free_delivery_threshold_cents=rng.choice([0, 0, 3900]),
        price_factor=rng.choice([1.0, 1.0, 0.95, 0.9, 1.1]),
        coupons=tuple(random_coupon(rng, pool, f"C{i}") for i in range(rng.randint(0, 5))),
        allow_stack=rng.random() < 0.5,
        max_stack=rng.randint(1, 3),
    )


@contextlib.contextmanager
def captured():
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        yield buf


# --------------------------------------------------------------------------- #
# 组合计数
# --------------------------------------------------------------------------- #
class TestCountCombos(unittest.TestCase):
    def test_matches_manual_binomials(self):
        self.assertEqual(count_combos(10, 1, 3), 10 + 45 + 120)
        self.assertEqual(count_combos(10, 1, 1), 10)

    def test_k_range_clamped_to_pool(self):
        # k_max 超过池子大小 → 按池子大小截断
        self.assertEqual(count_combos(3, 1, 99), 2 ** 3 - 1)

    def test_empty_pool(self):
        self.assertEqual(count_combos(0, 1, 3), 0)

    def test_k_min_above_pool(self):
        self.assertEqual(count_combos(3, 5, 9), 0)

    def test_single_k(self):
        self.assertEqual(count_combos(6, 3, 3), 20)


# --------------------------------------------------------------------------- #
# 候选池索引
# --------------------------------------------------------------------------- #
class TestComboIndex(unittest.TestCase):
    def setUp(self):
        self.pool = [mk_item("C", 3000), mk_item("A", 1000), mk_item("B", 2000)]

    def test_sorted_by_price_then_code(self):
        idx = ComboIndex(self.pool, Constraints(min_items=1, max_items=3))
        self.assertEqual(idx.codes, ("A", "B", "C"))
        self.assertEqual(idx.prices, (1000, 2000, 3000))
        self.assertEqual(len(idx), 3)

    def test_masks_follow_sorted_positions(self):
        cons = Constraints(min_items=1, max_items=3, must_include=("C",), exclude=("A",))
        idx = ComboIndex(self.pool, cons)
        self.assertEqual(idx.must_mask, 0b100)
        self.assertEqual(idx.exclude_mask, 0b001)

    def test_codes_absent_from_pool_are_recorded_not_silently_dropped(self):
        cons = Constraints(min_items=1, max_items=3, must_include=("A", "MISSING"))
        idx = ComboIndex(self.pool, cons)
        self.assertEqual(idx.missing_required, ("MISSING",))
        self.assertEqual(idx.must_mask, 0b001)     # A 仍在

    def test_feasible_matches_satisfies_for_every_subset(self):
        """掩码版与 ``_satisfies`` 全组合对拍 —— 两者必须逐位等同。"""
        cons_kw = dict(min_items=2, max_items=3, must_include=("B",), exclude=("C",))
        idx = ComboIndex(self.pool, Constraints(**cons_kw))
        cons = Constraints(**cons_kw)
        checked = 0
        for mask in range(1 << len(idx)):
            combo = [idx.items[i] for i in range(len(idx)) if mask >> i & 1]
            self.assertEqual(
                idx.feasible(mask, len(combo)), _satisfies(combo, cons),
                msg=f"mask={mask:b} combo={[i.code for i in combo]}",
            )
            checked += 1
        self.assertEqual(checked, 8)

    def test_min_max_items_boundaries(self):
        idx = ComboIndex(self.pool, Constraints(min_items=2, max_items=2))
        self.assertFalse(idx.feasible(0b001, 1))
        self.assertTrue(idx.feasible(0b011, 2))
        self.assertFalse(idx.feasible(0b111, 3))


# --------------------------------------------------------------------------- #
# 单张券的抵扣上界
# --------------------------------------------------------------------------- #
class TestCouponCeiling(unittest.TestCase):
    def test_threshold_reduce_ceiling_is_reduce(self):
        c = mk_coupon("c", "threshold_reduce", threshold_cents=3000, reduce_cents=500)
        self.assertEqual(coupon_ceiling(c, SMALL_POOL), 500)

    def test_item_reduce_floored_by_item_price(self):
        c = mk_coupon("c", "item_reduce", item_code="A", reduce_cents=9999)
        self.assertEqual(coupon_ceiling(c, SMALL_POOL), 1000)

    def test_item_free_is_item_price(self):
        c = mk_coupon("c", "item_free", item_code="B")
        self.assertEqual(coupon_ceiling(c, SMALL_POOL), 2000)

    def test_missing_item_code_is_zero_not_most_expensive_item(self):
        """曾经的实测缺陷：漏掉这条会把上界抬到池子里最贵那件商品。"""
        c = mk_coupon("c", "item_free", item_code="")
        self.assertEqual(coupon_ceiling(c, SMALL_POOL), 0)

    def test_item_code_not_in_pool_is_zero(self):
        c = mk_coupon("c", "item_free", item_code="NOPE")
        self.assertEqual(coupon_ceiling(c, SMALL_POOL), 0)

    def test_buy_get_ceiling_is_the_free_item(self):
        c = mk_coupon("c", "buy_get", buy_code="A", get_code="C")
        self.assertEqual(coupon_ceiling(c, SMALL_POOL), 3000)

    def test_buy_get_without_buy_code_is_zero(self):
        c = mk_coupon("c", "buy_get", buy_code="", get_code="A")
        self.assertEqual(coupon_ceiling(c, SMALL_POOL), 0)

    def test_discount_without_cap_is_unbounded(self):
        c = mk_coupon("c", "discount", rate=0.5, cap_cents=0)
        self.assertEqual(coupon_ceiling(c, SMALL_POOL), UNBOUNDED)

    def test_discount_with_cap(self):
        c = mk_coupon("c", "discount", rate=0.5, cap_cents=1200)
        self.assertEqual(coupon_ceiling(c, SMALL_POOL), 1200)

    def test_item_discount_capped(self):
        no_cap = mk_coupon("a", "item_discount", item_code="C", rate=0.5)
        capped = mk_coupon("b", "item_discount", item_code="C", rate=0.5, cap_cents=300)
        self.assertEqual(coupon_ceiling(no_cap, SMALL_POOL), 1500)
        self.assertEqual(coupon_ceiling(capped, SMALL_POOL), 300)

    def test_bundle_price_ceiling(self):
        c = mk_coupon("c", "bundle_price", item_codes=("A", "B"), fixed_cents=1500)
        self.assertEqual(coupon_ceiling(c, SMALL_POOL), 3000 - 1500)

    def test_bundle_price_never_affordable_is_zero(self):
        c = mk_coupon("c", "bundle_price", item_codes=("A", "NOPE"), fixed_cents=100)
        self.assertEqual(coupon_ceiling(c, SMALL_POOL), 0)
        self.assertEqual(coupon_ceiling(
            mk_coupon("d", "bundle_price", item_codes=(), fixed_cents=100), SMALL_POOL), 0)

    def test_unknown_type_is_unbounded(self):
        self.assertEqual(coupon_ceiling(mk_coupon("c", "mystery"), SMALL_POOL), UNBOUNDED)

    def test_min_items_beyond_pool_is_zero(self):
        c = mk_coupon("c", "threshold_reduce", reduce_cents=500, min_items=9)
        self.assertEqual(coupon_ceiling(c, SMALL_POOL), 0)

    def test_admissible_on_random_pools_and_carts(self):
        """核心性质：上界 ≥ 真实抵扣，对**任意**购物车成立。"""
        rng = random.Random(20261009)
        for trial in range(60):
            n = rng.randint(1, 5)
            pool = [mk_item(f"I{i}", rng.randint(100, 4000)) for i in range(n)]
            for j in range(4):
                c = random_coupon(rng, pool, f"X{trial}-{j}")
                ceiling = coupon_ceiling(c, pool)
                for cart in all_carts(pool):
                    actual = single_coupon_discount(list(cart), c)
                    self.assertGreaterEqual(
                        ceiling, actual,
                        msg=f"上界不成立：{c.type} ceiling={ceiling} actual={actual} "
                            f"cart={[i.code for i in cart]} coupon={c}",
                    )


# --------------------------------------------------------------------------- #
# 整池券的抵扣上界
# --------------------------------------------------------------------------- #
class TestDiscountCeiling(unittest.TestCase):
    def test_empty_pool(self):
        self.assertEqual(discount_ceiling((), SMALL_POOL), 0)

    def test_same_group_keeps_only_the_max(self):
        cs = (
            mk_coupon("a", "threshold_reduce", reduce_cents=500, stack_group="g"),
            mk_coupon("b", "threshold_reduce", reduce_cents=800, stack_group="g"),
        )
        self.assertEqual(discount_ceiling(cs, SMALL_POOL, allow_stack=True), 800)

    def test_different_groups_sum(self):
        cs = (
            mk_coupon("a", "threshold_reduce", reduce_cents=500, stack_group="g1"),
            mk_coupon("b", "threshold_reduce", reduce_cents=800, stack_group="g2"),
        )
        self.assertEqual(discount_ceiling(cs, SMALL_POOL, allow_stack=True), 1300)

    def test_max_stack_limits_order_level_groups(self):
        cs = tuple(
            mk_coupon(f"c{i}", "threshold_reduce", reduce_cents=100 * (i + 1),
                      stack_group=f"g{i}")
            for i in range(3)                     # 100 / 200 / 300
        )
        self.assertEqual(discount_ceiling(cs, SMALL_POOL, allow_stack=True, max_stack=1), 300)
        self.assertEqual(discount_ceiling(cs, SMALL_POOL, allow_stack=True, max_stack=2), 500)
        self.assertEqual(discount_ceiling(cs, SMALL_POOL, allow_stack=True, max_stack=3), 600)
        # 单张竞争的渠道：不分阶段，只留最优的一张
        self.assertEqual(discount_ceiling(cs, SMALL_POOL, allow_stack=False), 300)

    def test_goods_cap_applies(self):
        c = mk_coupon("a", "threshold_reduce", reduce_cents=100_000)
        self.assertEqual(discount_ceiling((c,), SMALL_POOL, goods_cap=2500), 2500)

    def test_unbounded_propagates(self):
        cs = (mk_coupon("a", "threshold_reduce", reduce_cents=500),
              mk_coupon("b", "discount", rate=0.5, cap_cents=0))
        self.assertEqual(discount_ceiling(cs, SMALL_POOL), UNBOUNDED)

    def test_admissible_against_engine_on_random_channels(self):
        """随机渠道 × 随机券池 × 全部购物车：上界 ≥ 引擎实际抵扣。"""
        rng = random.Random(4242)
        for trial in range(40):
            n = rng.randint(1, 4)
            pool = [mk_item(f"I{i}", rng.randint(200, 3000)) for i in range(n)]
            ch = random_channel(rng, pool, f"CH{trial}")
            if not ch.coupons:
                continue
            cap = sum(int(round(i.price_cents * ch.price_factor)) for i in pool)
            ceiling = discount_ceiling(
                ch.coupons, pool, allow_stack=ch.allow_stack,
                max_stack=ch.max_stack, goods_cap=cap)
            if ceiling >= UNBOUNDED:
                continue
            for cart in all_carts(pool):
                actual = apply_coupons(
                    list(cart), ch.coupons, allow_stack=ch.allow_stack,
                    max_stack=ch.max_stack, shadow_ypp=0.138).discount_cents
                self.assertGreaterEqual(
                    ceiling, actual,
                    msg=f"上界不成立：ceiling={ceiling} actual={actual}\n"
                        f"channel pf={ch.price_factor} stack={ch.allow_stack}/{ch.max_stack}\n"
                        f"coupons={[(c.type, c.stack_group) for c in ch.coupons]}\n"
                        f"cart={[i.code for i in cart]}",
                )


class TestDiscountCeilingMixedStage(unittest.TestCase):
    """回归哨兵：单品级与整单级券挂在**同一个 stack_group** 上时，上界必须相加而不是取最大。

    这是实测踩过的坑。引擎是两段式的（单品级先落地 → 整单级作用于剩余金额），
    ``stack_group`` 只表达"同组互斥"，**不含**阶段维度，因此两张同名组但不同阶段的券
    会**同时生效**。若按组取 max，上界就会低于真实抵扣，下界随之被抬高，最优解被静默剪掉。
    """

    def setUp(self):
        self.pool = SMALL_POOL                      # A=1000 B=2000 C=3000
        self.channel = mk_channel(
            "MIX", allow_stack=True, max_stack=2, packaging_fee_cents=0,
            coupons=(
                mk_coupon("free-A", "item_free", item_code="A", stack_group=""),
                mk_coupon("thr", "threshold_reduce", threshold_cents=0,
                          reduce_cents=500, stack_group=""),
                mk_coupon("x", "threshold_reduce", threshold_cents=0,
                          reduce_cents=400, stack_group="x"),
            ),
        )

    def test_ceiling_counts_both_stages(self):
        ceiling = discount_ceiling(
            self.channel.coupons, self.pool,
            allow_stack=True, max_stack=2,
            goods_cap=sum(i.price_cents for i in self.pool))
        # 单品级：默认组 1000。整单级：默认组 500 + x 组 400（max_stack=2）→ 900。
        self.assertEqual(ceiling, 1900)

    def test_naive_group_max_would_underestimate(self):
        """把两段混进一个字典会得到 1000 + 400 = 1400，低于真实抵扣。"""
        naive = {}
        for c in self.channel.coupons:
            naive[c.stack_group] = max(naive.get(c.stack_group, 0),
                                       coupon_ceiling(c, self.pool))
        self.assertLess(sum(naive.values()), 1900)

    def test_engine_really_stacks_both_stages(self):
        cart = [i for i in self.pool if i.code in ("A", "B")]
        app = apply_coupons(list(cart), self.channel.coupons,
                            allow_stack=True, max_stack=2, shadow_ypp=0.0)
        self.assertEqual(app.discount_cents, 1900)
        self.assertEqual(sorted(c.id for c in app.coupons), ["free-A", "thr", "x"])

    def test_bound_still_admissible_on_this_channel(self):
        bound_fn, info = make_bound([self.channel], self.pool, item_count=2)
        self.assertFalse(info.unbounded)
        goods = sum(i.price_cents for i in self.pool if i.code in ("A", "B"))
        landed = landed_cost([i for i in self.pool if i.code in ("A", "B")],
                             [self.channel], shadow_ypp=0.0)
        self.assertLessEqual(bound_fn(goods), landed)


# --------------------------------------------------------------------------- #
# 下界
# --------------------------------------------------------------------------- #
class TestMakeBound(unittest.TestCase):
    def test_no_channels_means_unbounded(self):
        fn, info = make_bound([], SMALL_POOL, item_count=3)
        self.assertTrue(info.unbounded)
        self.assertEqual(fn(99999), 0)
        self.assertIn("不剪枝", info.explain())

    def test_unbounded_coupon_disables_pruning(self):
        ch = mk_channel(coupons=(mk_coupon("u", "discount", rate=0.5, cap_cents=0),))
        fn, info = make_bound([ch], SMALL_POOL, item_count=3)
        self.assertTrue(info.unbounded)
        self.assertEqual(fn(99999), 0)

    def test_explain_names_the_three_pieces(self):
        ch = mk_channel(packaging_fee_cents=150,
                        coupons=(mk_coupon("t", "threshold_reduce",
                                           threshold_cents=0, reduce_cents=100_000),))
        _, info = make_bound([ch], SMALL_POOL, item_count=3)
        text = info.explain()
        self.assertIn("小计", text)
        self.assertIn("6000", text)   # 1000+2000+3000，抵扣上界被 goods_cap 卡在这个数
        self.assertIn("150", text)

    def test_monotone_non_decreasing_in_goods(self):
        rng = random.Random(7)
        for trial in range(25):
            n = rng.randint(2, 5)
            pool = [mk_item(f"I{i}", rng.randint(100, 3000)) for i in range(n)]
            chs = [random_channel(rng, pool, f"CH{i}") for i in range(rng.randint(1, 3))]
            fn, info = make_bound(chs, pool, item_count=4)
            if info.unbounded:
                continue
            prev = fn(0)
            for goods in range(0, 16000, 137):
                cur = fn(goods)
                self.assertGreaterEqual(
                    cur, prev,
                    msg=f"下界不单调：bound({goods})={cur} < 上一个 {prev}（info={info.explain()}）",
                )
                prev = cur

    def test_admissible_on_random_pools_and_channels(self):
        """核心性质：对每个购物车、每个可行报价，``下界 ≤ 到手成本``。"""
        rng = random.Random(31337)
        for trial in range(40):
            n = rng.randint(1, 5)
            pool = [mk_item(f"I{i}", rng.randint(100, 3500)) for i in range(n)]
            chs = [random_channel(rng, pool, f"CH{i}") for i in range(rng.randint(1, 3))]
            k_max = rng.randint(1, 3)
            fn, info = make_bound(chs, pool, item_count=k_max)
            if info.unbounded:
                continue
            for cart in all_carts(pool, 1, k_max):
                goods = sum(i.price_cents for i in cart)
                lb = fn(goods)
                for q in compare_channels(list(cart), chs, shadow_ypp=0.138):
                    if not q.feasible:
                        continue
                    self.assertLessEqual(
                        lb, q.landed_cents,
                        msg=f"下界不成立：bound({goods})={lb} > landed={q.landed_cents} "
                            f"channel={q.channel.name} cart={[i.code for i in cart]}",
                    )
                    self.assertLessEqual(lb, q.cash_cents)

    def test_price_factor_is_taken_from_the_cheapest_channel(self):
        cheep = mk_channel("CHEAP", price_factor=0.5, packaging_fee_cents=0)
        dear = mk_channel("DEAR", price_factor=1.0, packaging_fee_cents=900)
        fn, info = make_bound([cheep, dear], SMALL_POOL, item_count=3)
        self.assertEqual(info.price_factor_min, 0.5)
        self.assertEqual(info.packaging_min_cents, 0)
        self.assertEqual(fn(1000), 1000 // 2 - 3)

    def test_huge_discount_ceiling_floors_at_packaging(self):
        """抵扣上界盖过小计时，下界退化为"只收打包费" —— 松，但仍然可采纳。"""
        pool = [mk_item("A", 1000)]
        ch = mk_channel(packaging_fee_cents=150,
                        coupons=(mk_coupon("t", "threshold_reduce",
                                           threshold_cents=0, reduce_cents=100_000),))
        fn, info = make_bound([ch], pool, item_count=1)
        self.assertFalse(info.unbounded)
        self.assertEqual(fn(1000), 150)

    def test_target_codes_use_distinct_pool_sum(self):
        """组合券的上界是所有目标商品的**合计**，不是最贵那件。"""
        c = mk_coupon("b", "bundle_price", item_codes=("A", "B", "C"), fixed_cents=0)
        self.assertEqual(coupon_ceiling(c, SMALL_POOL), 6000)


# --------------------------------------------------------------------------- #
# 搜索：等价性
# --------------------------------------------------------------------------- #
class TestSearchEquivalence(unittest.TestCase):
    def setUp(self):
        self.pool = [mk_item(f"I{i}", p) for i, p in enumerate([1000, 2500, 800, 3300, 1200, 600])]
        self.channels = [
            mk_channel("SELF", packaging_fee_cents=0),
            mk_channel("APP", price_factor=1.1, packaging_fee_cents=150,
                       delivery_fee_cents=350, min_order_cents=2000,
                       coupons=(mk_coupon("t", "threshold_reduce", threshold_cents=3000,
                                          reduce_cents=800, stack_group="p"),)),
        ]
        self.evaluate = make_evaluate(self.channels)

    def _run(self, engine, *, keep, k_max=3, bound=True, cons=None):
        cons = cons or Constraints(min_items=1, max_items=k_max)
        idx = ComboIndex(self.pool, cons)
        fn, info = (make_bound(self.channels, idx.items, item_count=k_max) if bound
                    else (None, None))
        res, st = search(idx, self.evaluate, bound=fn, keep=keep, engine=engine)
        return res, st, info

    def test_full_equals_pruned_field_by_field(self):
        for k_max in (1, 2, 3):
            for keep in (1, 2, 5, 10, 10 ** 6):
                a, sa, _ = self._run(COMBOS_FULL, keep=keep, k_max=k_max)
                b, sb, _ = self._run(COMBOS_PRUNED, keep=keep, k_max=k_max)
                self.assertEqual(a, b, msg=f"k_max={k_max} keep={keep} 结果不一致")
                self.assertEqual(sa.best_cents, sb.best_cents)
                self.assertEqual(sa.full_nodes, sb.full_nodes)
                # 剪枝只会少算，不会多算
                self.assertLessEqual(sb.leaves, sa.leaves)

    def test_full_matches_independent_brute_force(self):
        """与独立写的暴力枚举对拍（含全部可行组合，不只是前 N）。"""
        cons = Constraints(min_items=1, max_items=3)
        idx = ComboIndex(self.pool, cons)
        got, _ = search(idx, self.evaluate, keep=10 ** 6, engine=COMBOS_FULL)
        want = brute_force(self.pool, cons, self.evaluate, keep=10 ** 6)
        canon = lambda rows: sorted((tuple(k), v) for k, v in rows)  # noqa: E731
        self.assertEqual(canon(got), canon(want))
        self.assertGreater(len(got), 0)

    def test_pruned_matches_brute_force_on_random_pools(self):
        rng = random.Random(99)
        for trial in range(25):
            n = rng.randint(3, 6)
            pool = [mk_item(f"I{i}", rng.randint(200, 3000)) for i in range(n)]
            channels = [random_channel(rng, pool, f"CH{i}") for i in range(rng.randint(1, 3))]
            ev = make_evaluate(channels)
            cons = Constraints(min_items=1, max_items=min(3, n))
            idx = ComboIndex(pool, cons)
            fn, _ = make_bound(channels, idx.items, item_count=cons.max_items)
            got_f, _ = search(idx, ev, bound=fn, keep=5, engine=COMBOS_FULL)
            got_p, _ = search(idx, ev, bound=fn, keep=5, engine=COMBOS_PRUNED)
            want = brute_force(pool, cons, ev, keep=5)
            canon = lambda rows: sorted((tuple(k), v) for k, v in rows)  # noqa: E731
            self.assertEqual(canon(got_p), canon(want), msg=f"trial={trial} pool={pool}")
            self.assertEqual(canon(got_f), canon(want), msg=f"trial={trial} pool={pool}")

    def test_enumeration_order_follows_itertools_combinations(self):
        """两个引擎的枚举顺序都应与 ``itertools.combinations`` 一致。"""
        cons = Constraints(min_items=1, max_items=2)
        idx = ComboIndex(self.pool, cons)
        seen: list = []

        def recorder(combo):
            seen.append(tuple(i.code for i in combo))
            return (1, len(combo)), tuple(i.code for i in combo)

        search(idx, recorder, keep=10 ** 6, engine=COMBOS_FULL)
        want: list = []
        for k in (1, 2):
            want.extend(tuple(i.code for i in c)
                        for c in itertools.combinations(idx.items, k))
        self.assertEqual(seen, want)


# --------------------------------------------------------------------------- #
# 搜索：剪枝确实在起作用
# --------------------------------------------------------------------------- #
class TestPruningEffect(unittest.TestCase):
    def setUp(self):
        self.items = _items(MENU)
        self.channels = _channels(CHANNELS)

    def _search(self, engine, k, *, top=5):
        cons = Constraints(min_items=1, max_items=k)
        idx = ComboIndex(self.items, cons)
        fn, info = make_bound(self.channels, idx.items, item_count=k)
        res, st = search(idx, make_evaluate(self.channels), bound=fn, keep=top, engine=engine)
        return res, st, info

    def test_pruning_saves_work_on_the_shipped_pool(self):
        for k, floor in ((3, 0.15), (4, 0.35), (5, 0.60)):
            _, st_full, _ = self._search(COMBOS_FULL, k)
            _, st_pruned, info = self._search(COMBOS_PRUNED, k)
            self.assertFalse(info.unbounded)
            self.assertGreater(st_pruned.cuts, 0)
            self.assertGreaterEqual(
                st_pruned.saved_ratio, floor,
                msg=f"k={k} 剪枝率 {st_pruned.saved_ratio:.3f} 低于承诺的 {floor}")
            self.assertLess(st_pruned.leaves, st_full.leaves)

    def test_pruning_never_changes_the_answer_on_the_shipped_pool(self):
        for k in (1, 2, 3, 4, 5):
            for top in (1, 3, 5, 20):
                a, _, _ = self._search(COMBOS_FULL, k, top=top)
                b, _, _ = self._search(COMBOS_PRUNED, k, top=top)
                self.assertEqual(a, b, msg=f"k={k} top={top} 剪枝改变了答案")

    def test_full_engine_leaves_equal_feasible_combos(self):
        """``full`` 的枚举量应正好等于可行组合数（约束两段都提前过滤）。"""
        for k in (2, 3):
            _, st, _ = self._search(COMBOS_FULL, k)
            count = 0
            for combo in all_carts(self.items, 1, k):
                if landed_cost(combo, self.channels, shadow_ypp=0.138) is not None:
                    count += 1
            self.assertEqual(st.leaves, count)
            self.assertEqual(st.evaluations, st.leaves)

    def test_no_bound_means_no_bound_cuts(self):
        cons = Constraints(min_items=1, max_items=4)
        idx = ComboIndex(self.items, cons)
        ev = make_evaluate(self.channels)
        for engine in (COMBOS_FULL, COMBOS_PRUNED):
            _, st = search(idx, ev, bound=None, keep=5, engine=engine)
            self.assertEqual(st.cuts, 0)
            self.assertEqual(st.method, COMBOS_FULL)   # 如实记成没剪

    def test_unbounded_bound_disables_pruning(self):
        stuck = mk_channel(coupons=(mk_coupon("u", "discount", rate=0.5, cap_cents=0),))
        cons = Constraints(min_items=1, max_items=3)
        idx = ComboIndex(self.items, cons)
        fn, info = make_bound([stuck], self.items, item_count=3)
        self.assertTrue(info.unbounded)
        _, st = search(idx, make_evaluate(self.channels), bound=fn, keep=5,
                       engine=COMBOS_PRUNED)
        self.assertEqual(st.cuts, 0)

    def test_large_keep_disables_pruning(self):
        """前 N 名没装满时不给阈值 —— 否则会拿"只有 2 个结果"的边界去剪。"""
        cons = Constraints(min_items=1, max_items=3)
        idx = ComboIndex(self.items, cons)
        fn, _ = make_bound(self.channels, idx.items, item_count=3)
        ev = make_evaluate(self.channels)
        _, big = search(idx, ev, bound=fn, keep=10 ** 6, engine=COMBOS_PRUNED)
        _, full = search(idx, ev, bound=None, keep=10 ** 6, engine=COMBOS_FULL)
        self.assertEqual(big.cuts, 0)
        self.assertEqual(big.leaves, full.leaves)


# --------------------------------------------------------------------------- #
# 搜索：退化输入
# --------------------------------------------------------------------------- #
class TestSearchDegenerate(unittest.TestCase):
    def test_empty_pool(self):
        idx = ComboIndex([], Constraints(min_items=1, max_items=3))
        res, st = search(idx, cheap_eval, keep=5)
        self.assertEqual(res, [])
        self.assertEqual(st.full_nodes, 0)
        self.assertEqual(st.leaves, 0)
        self.assertIsNone(st.best_cents)

    def test_k_min_above_pool_size(self):
        idx = ComboIndex(SMALL_POOL, Constraints(min_items=5, max_items=9))
        res, st = search(idx, cheap_eval, keep=5)
        self.assertEqual(res, [])
        self.assertEqual(st.leaves, 0)

    def test_keep_zero_returns_nothing_but_does_not_crash(self):
        idx = ComboIndex(SMALL_POOL, Constraints(min_items=1, max_items=2))
        res, st = search(idx, cheap_eval, keep=0)
        self.assertEqual(res, [])
        self.assertGreater(st.leaves, 0)

    def test_keep_larger_than_total(self):
        idx = ComboIndex(SMALL_POOL, Constraints(min_items=1, max_items=1))
        res, st = search(idx, cheap_eval, keep=99)
        self.assertEqual(len(res), 3)
        self.assertEqual(st.leaves, 3)

    def test_everything_infeasible_returns_empty(self):
        idx = ComboIndex(SMALL_POOL, Constraints(min_items=1, max_items=2))
        res, st = search(idx, lambda c: None, keep=5)
        self.assertEqual(res, [])
        self.assertIsNone(st.best_cents)
        self.assertGreater(st.leaves, 0)

    def test_missing_required_short_circuits(self):
        cons = Constraints(min_items=1, max_items=2, must_include=("NOT-ON-MENU",))
        idx = ComboIndex(SMALL_POOL, cons)
        seen: list = []
        res, st = search(idx, make_spy(seen), keep=5, engine=COMBOS_PRUNED)
        self.assertEqual(res, [])
        self.assertEqual(seen, [])               # 一个报价都没算
        self.assertEqual(st.leaves, 0)
        self.assertEqual(idx.missing_required, ("NOT-ON-MENU",))
        self.assertEqual(st.full_nodes, count_combos(3, 1, 2))

    def test_exclude_removes_subtrees(self):
        cons = Constraints(min_items=1, max_items=2, exclude=("A", "B"))
        idx = ComboIndex(SMALL_POOL, cons)
        seen: list = []
        res, st = search(idx, make_spy(seen), keep=5)
        self.assertEqual(seen, [("C",)])          # 含 A 或 B 的组合全被砍
        self.assertGreater(st.feasibility_cuts, 0)
        self.assertEqual(len(res), 1)

    def test_must_include_keeps_only_covering_combos(self):
        cons = Constraints(min_items=2, max_items=2, must_include=("C",))
        idx = ComboIndex(SMALL_POOL, cons)
        seen: list = []
        search(idx, make_spy(seen), keep=5, engine=COMBOS_PRUNED)
        self.assertEqual(sorted(seen), [("A", "C"), ("B", "C")])

    def test_constraint_pruning_matches_satisfies_filtering(self):
        """约束剪枝与"全枚举 + ``_satisfies`` 过滤"必须给出同一批组合。"""
        cons_kw = dict(min_items=2, max_items=3, must_include=("I4",), exclude=("I0",))
        idx = ComboIndex(self.pool6(), Constraints(**cons_kw))
        seen: list = []
        search(idx, make_spy(seen), keep=10 ** 6, engine=COMBOS_PRUNED)
        want = sorted(sorted(i.code for i in c)
                      for c in all_carts(self.pool6(), 2, 3)
                      if _satisfies(list(c), Constraints(**cons_kw)))
        self.assertEqual(sorted(sorted(c) for c in seen), want)
        self.assertTrue(want)

    @staticmethod
    def pool6():
        return [mk_item(f"I{i}", 100 * (i + 1)) for i in range(6)]


# --------------------------------------------------------------------------- #
# 统计与输出
# --------------------------------------------------------------------------- #
class TestSearchStats(unittest.TestCase):
    def test_properties(self):
        st = SearchStats(full_nodes=100, leaves=25, evaluations=25)
        self.assertEqual(st.saved_nodes, 75)
        self.assertAlmostEqual(st.saved_ratio, 0.75)
        self.assertAlmostEqual(st.eval_ratio, 0.25)

    def test_zero_full_nodes_does_not_divide_by_zero(self):
        st = SearchStats()
        self.assertEqual(st.saved_ratio, 0.0)
        self.assertEqual(st.eval_ratio, 0.0)
        self.assertEqual(st.saved_nodes, 0)

    def test_never_reports_negative_savings(self):
        st = SearchStats(full_nodes=10, leaves=99)
        self.assertEqual(st.saved_nodes, 0)

    def test_to_dict_is_json_serializable(self):
        import json
        payload = json.dumps(SearchStats(full_nodes=1, leaves=1).to_dict())
        self.assertIn("full_nodes", payload)

    def test_print_stats_and_bound_do_not_crash(self):
        with captured() as buf:
            print_stats(SearchStats(pool=10, k_min=1, k_max=3, full_nodes=175,
                                    leaves=132, evaluations=132, cuts=37, nodes=200),
                        title="── pruned ──")
            print_bound(BoundInfo(1.0, 0, 4025, 3, False))
            print_bound(BoundInfo(1.0, 0, 0, 0, True))
        out = buf.getvalue()
        self.assertIn("下界剪枝", out)
        self.assertIn("不剪枝", out)


# --------------------------------------------------------------------------- #
# 端到端：CLI 与线上样例池
# --------------------------------------------------------------------------- #
class TestShippedDataAndCli(unittest.TestCase):
    def test_cli_engines_agree(self):
        with captured() as buf:
            code = combo_main(["--max-items", "4", "--top", "5"])
        out = buf.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("结果一致：是", out)
        self.assertIn("下界剪枝", out)

    def test_cli_json_mode(self):
        import json
        with captured() as buf:
            code = combo_main(["--max-items", "3", "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["full"]["top"], payload["pruned"]["top"])
        self.assertIn("下界 =", payload["bound"])
        self.assertLess(payload["pruned"]["stats"]["leaves"],
                        payload["full"]["stats"]["leaves"])

    def test_parser_defaults(self):
        args = build_parser().parse_args([])
        self.assertEqual(args.shadow_ypp, 0.138)
        self.assertEqual(args.max_items, 3)

    def test_top_result_on_shipped_pool_is_the_cheapest_single_item(self):
        """样例池上的最优解应与"全枚举 + 暴力裁判"一致，且成本不超过任何单件商品。"""
        items = _items(MENU)
        chs = _channels(CHANNELS)
        cons = Constraints(min_items=1, max_items=3)
        idx = ComboIndex(items, cons)
        ev = make_evaluate(chs)
        fn, _ = make_bound(chs, idx.items, item_count=3)
        got, _ = search(idx, ev, bound=fn, keep=3, engine=COMBOS_PRUNED)
        want = brute_force(items, cons, ev, keep=3)
        self.assertEqual([k for k, _ in got], [k for k, _ in want])
        best = got[0][0][0]
        for item in items:
            single = landed_cost([item], chs, shadow_ypp=0.138)
            self.assertIsNotNone(single)
            self.assertLessEqual(best, single)


if __name__ == "__main__":
    unittest.main()
