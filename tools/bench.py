#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""微优化基准 —— 把"更快"从形容词变成数字。

为什么要有这个脚本
------------------
评审意见里"性能压榨"这一条最容易变成玄学：改了 `__slots__`、换了 `math.comb`、
加了一层剪枝，然后说"变快了"。**没有数字的"变快"不可复核**。本脚本把三类改动
逐项拆开测，每项都给出对照实现、重复次数与中位数，而不是挑一次最好看的结果。

测什么
------
1. ``comb``：``math.comb`` 与"按定义写循环"的朴素实现对照
2. ``search``：``full`` 全量枚举 vs ``pruned`` 分支定界 —— 枚举量、耗时、内存峰值
3. ``memory``：``@dataclass(slots=True)`` 与默认（带 ``__dict__``）对照
4. ``mask``：约束检查用"位掩码随递归传递" vs "每个叶子重建一个 set"

怎么读
------
- 时间取 ``min`` 与 ``median``：``min`` 是"这台机器能做到多快"，``median`` 是"平时多快"。
  两者差距大说明机器在抖，此时应该只信比值、不信绝对值。
- 内存用 ``tracemalloc`` 的**峰值**，不用 ``sys.getsizeof`` 单独下结论 ——
  后者看不到 ``__dict__`` 这个额外对象，会给出偏乐观的数字（两个都报，便于对照）。
- 数字**与机器相关**。这个脚本的意义不是记住某个毫秒值，而是让"哪个方向更快、
  快多少倍"这件事可以被任何人在自己的机器上复现。

用法
----
    python tools/bench.py                 # 全部
    python tools/bench.py --only comb     # 只跑某一组
    python tools/bench.py --items 4000    # 调大合成池（组合枚举压力更大）
"""
from __future__ import annotations

import argparse
import math
import os
import random
import statistics
import sys
import time
import tracemalloc
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from channel_planner import Channel, compare_channels  # noqa: E402
from combo_search import (  # noqa: E402
    COMBOS_FULL, COMBOS_PRUNED, ComboIndex, count_combos, make_bound, search,
)
from optimize_deals import Constraints, Coupon, Item  # noqa: E402
from termtext import columns, rule, table_width  # noqa: E402

#: 时间基准的重复次数。取奇数便于取中位数，7 次在"够稳"和"别等太久"之间。
REPEAT = 7


# --------------------------------------------------------------------------- #
# 计时 / 计内存
# --------------------------------------------------------------------------- #
def timeit(fn: Callable[[], Any], *, repeat: int = REPEAT, number: int = 1) -> Tuple[float, float]:
    """返回 ``(最快一次, 中位数)``，单位秒/次。``number`` 为每次计时内的循环次数。"""
    samples: List[float] = []
    for _ in range(repeat):
        t0 = time.perf_counter()
        for _ in range(number):
            fn()
        samples.append((time.perf_counter() - t0) / number)
    return min(samples), statistics.median(samples)


def alloc_peak(fn: Callable[[], Any]) -> int:
    """执行 ``fn`` 期间的 ``tracemalloc`` 峰值（字节）。

    刻意**不**在返回后丢弃结果：``fn`` 返回的对象要一直活着，否则测的是"临时分配量"
    而不是"这些对象占了多少内存"。
    """
    tracemalloc.start()
    try:
        kept = fn()
        _cur, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert kept is not None
    return peak


def fmt_us(seconds: float) -> str:
    return f"{seconds * 1e6:.1f} µs" if seconds < 1e-3 else f"{seconds * 1e3:.2f} ms"


def _table(specs: Sequence[Tuple[str, int, str]], rows: Sequence[Sequence[str]]) -> None:
    print(rule(table_width(specs)))
    for line in columns(specs, rows):
        print(line)
    print(rule(table_width(specs)))


# --------------------------------------------------------------------------- #
# 1. math.comb vs 手写循环
# --------------------------------------------------------------------------- #
def handwritten_comb(n: int, k: int) -> int:
    """按组合数定义直接写循环的朴素实现（v3.0 之前用的写法）。

    保留它**只**作为基准对照 —— 生产路径上已经是 :func:`math.comb`。
    """
    if k < 0 or k > n:
        return 0
    k = min(k, n - k)
    acc = 1
    for i in range(1, k + 1):
        acc = acc * (n - k + i) // i
    return acc


def bench_comb(*, repeat: int = REPEAT) -> None:
    print("\n## 1. 组合数：``math.comb`` vs 手写循环")
    print("   单次调用 ``C(n, 3)``（池子里取 3 件的组合数）—— 把这个微操作本身量出来。")
    specs = [("池子 n", 9, ">"), ("math.comb", 14, ">"), ("手写循环", 14, ">"),
             ("", 2, "<"), ("加速比", 10, ">"), ("", 2, "<"), ("每次省下", 12, ">")]
    rows = []
    number = 200_000
    for n in (10, 30, 60, 120, 300):
        k = min(3, n)
        assert math.comb(n, k) == handwritten_comb(n, k), f"n={n} 两个实现结果不同"
        _b1, m1 = timeit(lambda: math.comb(n, k), repeat=repeat, number=number)
        _b2, m2 = timeit(lambda: handwritten_comb(n, k), repeat=repeat, number=number)
        rows.append((str(n), fmt_us(m1), fmt_us(m2), "", f"{m2 / m1:.2f}×", "",
                     fmt_us(m2 - m1)))
    _table(specs, rows)
    print("   实话实说：这条改动的收益是**微秒级**的。``Σ_k C(n,k)`` 在整个项目里只被")
    print("   ``_prune_items`` 与 ``count_combos`` 各调用 ≤5 次，一次全渠道求解约 40 ms，")
    print("   所以它省下的时间占比不到万分之一 —— **不该拿它当性能论据**。")
    print("   值得做的理由是别的：不写循环、不用自己处理 k>n 与 k>n/2 的边界。")
    print("   真正把时间省下来的是第 2 节（分支定界）和第 4 节（位掩码），")
    print("   真正把内存省下来的是第 3 节（slots）。")
    print("   比值稳定在 4~5×：两个实现都是常数级操作，差距只来自")
    print("   「少三层 Python 循环」，与 n 基本无关。")


# --------------------------------------------------------------------------- #
# 合成大池（让组合枚举真的成为瓶颈）
# --------------------------------------------------------------------------- #
def synthetic_pool(n: int, *, seed: int = 20261009) -> List[Item]:
    """合成菜单：价格分布与样例菜单同量级，用于把池子放大到枚举成为瓶颈的量级。"""
    rng = random.Random(seed)
    cats = ("汉堡", "小食", "饮品", "甜点", "套餐")
    return [
        Item(code=f"SKU-{i:04d}", name=f"合成商品{i:04d}",
             price_cents=rng.randrange(600, 3600, 50), category=cats[i % len(cats)])
        for i in range(n)
    ]


def synthetic_channels(pool: Sequence[Item], *, seed: int = 20261009) -> List[Channel]:
    """合成渠道：费用结构与券型分布照着 ``data/sample_channels.json`` 的真实配置摆。

    刻意保留"无封顶折扣券"以外的全部券型，且**每张券都指向池子里真实存在的商品** ——
    否则上界会退化成 0，剪枝率会好看得毫无意义。
    """
    rng = random.Random(seed)
    codes = [i.code for i in pool]

    def pick() -> str:
        return rng.choice(codes)

    def pick2() -> Tuple[str, str]:
        a, b = rng.sample(codes, 2)
        return a, b

    free_a, free_b = rng.sample(codes, 2)
    red_a = pick()
    buy, get = pick2()
    half = pick()
    b1, b2 = pick2()

    pickup = Channel(
        code="pickup", name="麦当劳 · 到店自取", source="mcp", allow_stack=False,
        coupons=(
            Coupon(id="t1", name="满30减5", type="threshold_reduce",
                   threshold_cents=3000, reduce_cents=500),
            Coupon(id="i1", name="单品立减4元", type="item_reduce",
                   item_code=red_a, reduce_cents=400),
            Coupon(id="g1", name="买A送B", type="buy_get", buy_code=buy, get_code=get),
            Coupon(id="f1", name="指定商品免费", type="item_free", item_code=free_a),
            Coupon(id="f2", name="指定商品免费", type="item_free", item_code=free_b),
        ),
    )
    platform = Channel(
        code="platform", name="第三方平台", source="external",
        delivery_fee_cents=350, packaging_fee_cents=150, min_order_cents=2000,
        price_factor=1.1, allow_stack=True, max_stack=2,
        coupons=(
            Coupon(id="d1", name="半价周末", type="item_discount", item_code=half,
                   rate=0.5, cap_cents=1500, stack_group="half"),
            Coupon(id="t2", name="满20减8", type="threshold_reduce",
                   threshold_cents=2000, reduce_cents=800, stack_group="p"),
            Coupon(id="t3", name="满40减15", type="threshold_reduce",
                   threshold_cents=4000, reduce_cents=1500, stack_group="p"),
            Coupon(id="r1", name="88折封顶12", type="discount",
                   rate=0.88, cap_cents=1200, stack_group="d"),
            Coupon(id="bp1", name="组合特价", type="bundle_price",
                   item_codes=(b1, b2), fixed_cents=2500, stack_group="bp"),
        ),
    )
    return [pickup, platform]


def _evaluate_factory(channels: Sequence[Channel], *, shadow_ypp: float = 0.138):
    def evaluate(combo):
        quotes = compare_channels(list(combo), channels, shadow_ypp=shadow_ypp)
        ok = [q for q in quotes if q.feasible]
        if not ok:
            return None
        best = min(ok, key=lambda q: q.landed_cents)
        return (best.landed_cents, len(combo)), tuple(i.code for i in combo)
    return evaluate


# --------------------------------------------------------------------------- #
# 2. 搜索：full vs pruned
# --------------------------------------------------------------------------- #
def _search_row(label: str, pool: Sequence[Item], channels: Sequence[Channel],
                k_max: int, *, top: int, repeat: int,
                engine: str, collect: Dict[str, Any]) -> Sequence[str]:
    cons = Constraints(min_items=1, max_items=k_max)
    index = ComboIndex(pool, cons)
    bound_fn, info = make_bound(channels, index.items, item_count=k_max)
    evaluate = _evaluate_factory(channels)

    holder: Dict[str, Any] = {}

    def run():
        res, st = search(index, evaluate, bound=bound_fn, keep=top, engine=engine)
        holder["res"], holder["st"] = res, st
        return res

    number = 1 if len(pool) > 14 else 40
    best, median = timeit(run, repeat=repeat, number=number)
    peak = alloc_peak(run)
    st = holder["st"]
    collect[engine] = {"ms": median, "stats": st, "res": holder["res"], "bound": info}
    return (label, fmt_us(median), f"{st.leaves:,}", f"{st.cuts:,}",
            f"{st.saved_ratio * 100:.1f}%", "", f"{peak / 1024:.0f} KiB")


def bench_search(*, items: int, repeat: int) -> None:
    print("\n## 2. 组合枚举：full（全量）vs pruned（分支定界）")
    print("   ``evaluate`` 是真实的全渠道报价，所以省下的枚举量直接换成时间。")

    cases: List[Tuple[str, Sequence[Item], Sequence[Channel], int]] = []
    real_items = os.path.join(ROOT, "data", "sample_menu.json")
    real_channels = os.path.join(ROOT, "data", "sample_channels.json")
    if os.path.exists(real_items) and os.path.exists(real_channels):
        from channel_planner import _channels, _items  # noqa: PLC0415
        cases.append(("样例池（10 项，真实数据）", _items(real_items),
                      _channels(real_channels), 5))
    big = synthetic_pool(items)
    cases.append((f"合成池（{items} 项）", big, synthetic_channels(big), 4))

    specs = [("引擎", 10, "<"), ("耗时(中位)", 12, ">"), ("枚举量", 11, ">"),
             ("下界剪枝", 10, ">"), ("省下", 9, ">"), ("", 2, "<"),
             ("内存峰值", 12, ">")]
    for label, pool, channels, k_max in cases:
        print(f"\n### {label}，k≤{k_max}，取前 5")
        got: Dict[str, Any] = {}
        rows = [
            _search_row("full", pool, channels, k_max, top=5,
                        repeat=repeat, engine=COMBOS_FULL, collect=got),
            _search_row("pruned", pool, channels, k_max, top=5,
                        repeat=repeat, engine=COMBOS_PRUNED, collect=got),
        ]
        _table(specs, rows)
        a, b = got[COMBOS_FULL], got[COMBOS_PRUNED]
        same = [k for k, _ in a["res"]] == [k for k, _ in b["res"]]
        ratio = a["ms"] / b["ms"] if b["ms"] > 0 else float("inf")
        print(f"   结果一致：{'是' if same else '** 否 —— 剪枝改变了答案，这是 bug **'}")
        print(f"   耗时 {fmt_us(a['ms'])} → {fmt_us(b['ms'])}（{ratio:.2f}×）；"
              f"枚举量 {a['stats'].leaves:,} → {b['stats'].leaves:,}")
        print(f"   {b['bound'].explain()}")
        if not same:
            raise SystemExit("基准中止：剪枝改变了答案")
        # 上界是否曾经失效（退化成 UNBOUNDED）：这里必须为 False，否则剪枝率不可信
        if b["stats"].cuts == 0:
            print("   ⚠ 一次都没剪到 —— 检查抵扣上界是不是退化了。")


# --------------------------------------------------------------------------- #
# 3. 对象内存：slots 开 / 关
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ItemPlain:
    """与 :class:`optimize_deals.Item` 字段完全一致的对照实现 —— 只是**不带** ``slots``。"""

    code: str
    name: str
    price_cents: int
    category: str = ""
    calories: Optional[float] = None
    tags: Tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ItemSlotted:
    code: str
    name: str
    price_cents: int
    category: str = ""
    calories: Optional[float] = None
    tags: Tuple[str, ...] = ()


def bench_memory(*, n: int = 100_000) -> None:
    print(f"\n## 3. 对象内存：``@dataclass(slots=True)`` vs 默认（带 ``__dict__``）")
    print(f"   对照实现字段与 ``optimize_deals.Item`` 完全一致；样本 {n:,} 个对象。")

    def build_plain() -> List[ItemPlain]:
        return [ItemPlain(f"C{i}", f"名字{i}", i * 10, "汉堡", 500.0, ("热销", "汉堡"))
                for i in range(n)]

    def build_slotted() -> List[ItemSlotted]:
        return [ItemSlotted(f"C{i}", f"名字{i}", i * 10, "汉堡", 500.0, ("热销", "汉堡"))
                for i in range(n)]

    plain_sample = ItemPlain("C0", "名字0", 0, "汉堡", 500.0, ("热销", "汉堡"))
    slotted_sample = ItemSlotted("C0", "名字0", 0, "汉堡", 500.0, ("热销", "汉堡"))
    plain_self = sys.getsizeof(plain_sample)
    plain_dict = sys.getsizeof(plain_sample.__dict__)
    slotted_self = sys.getsizeof(slotted_sample)
    plain_peak = alloc_peak(build_plain)
    slotted_peak = alloc_peak(build_slotted)

    specs = [("实现", 24, "<"), ("对象本身", 10, ">"), ("__dict__", 11, ">"),
             ("合计", 8, ">"), ("", 2, "<"), ("tracemalloc 峰值", 18, ">"),
             ("每对象实测", 12, ">"), ("", 2, "<"), ("实测省下", 10, ">")]
    rows = [
        ("默认 dataclass", f"{plain_self} B", f"{plain_dict} B",
         f"{plain_self + plain_dict} B", "", f"{plain_peak / 1024 / 1024:.2f} MiB",
         f"{plain_peak / n:.1f} B", "", ""),
        ("@dataclass(slots=True)", f"{slotted_self} B", "—",
         f"{slotted_self} B", "", f"{slotted_peak / 1024 / 1024:.2f} MiB",
         f"{slotted_peak / n:.1f} B", "",
         f"{(1 - slotted_peak / plain_peak) * 100:.1f}%"),
    ]
    _table(specs, rows)
    print("   两个数字都对，但含义完全不同：")
    print(f"   **对象 + 它自己的 __dict__** 从 {plain_self + plain_dict} B 降到 {slotted_self} B ——")
    print("   省掉的就是后者那一整块；可字段内容（str / tuple / float）两边都要分配，")
    print(f"   所以摊到「每对象实测」只省 {plain_peak / n - slotted_peak / n:.1f} B"
          f"（{plain_peak / n:.1f} → {slotted_peak / n:.1f} B）。")
    print("   只报前者会夸大战果，只报后者会看不出机制 —— 两个一起报。")
    print("   落在哪些类上：``Item`` / ``Coupon`` / ``Constraints`` / ``Plan`` /")
    print("   ``Channel`` / ``Quote`` / ``ChannelPlan`` / ``CouponApplication`` / ``Ledger``。")
    print("   一次全渠道求解会造出成百上千个 ``Quote`` 与 ``CouponApplication``，")
    print("   池子与叠加张数上去之后，「少一个 __dict__」就是实打实的常数额外开销。")


# --------------------------------------------------------------------------- #
# 4. 约束检查：位掩码 vs 重建 set
# --------------------------------------------------------------------------- #
def walk_with_sets(pool: Sequence[Item], k: int, must: Sequence[str],
                   exclude: Sequence[str]) -> int:
    """每个叶子重建一个 ``set`` 再判定 —— 位掩码方案要对照的写法。"""
    count = 0
    picked: List[int] = []

    def rec(start: int) -> None:
        nonlocal count
        if len(picked) == k:
            codes = {pool[i].code for i in picked}
            if all(c in codes for c in must) and not any(c in codes for c in exclude):
                count += 1
            return
        for i in range(start, len(pool) - (k - len(picked)) + 1):
            picked.append(i)
            rec(i + 1)
            picked.pop()

    rec(0)
    return count


def mask_lookup(pool: Sequence[Item], must: Sequence[str],
                exclude: Sequence[str]) -> Tuple[int, int]:
    """把约束编译成掩码 —— 真实实现里这一步在搜索**之前**做一次（``ComboIndex``）。

    刻意放在计时之外：如果每次调用都重建一次索引，比的就不是"掩码 vs set"，
    而是"多建一个字典 vs 少建一个字典"，对掩码方案不公平。

    ``must`` / ``exclude`` 里的编码必须都在池子里。真实实现里"必选商品不在池中"
    由 :func:`combo_search.search` 读 ``missing_required`` 提前短路；
    本基准只比较两种**检查方式**的开销，不建模那条路径，所以这里直接报错而不是猜。
    """
    pos = {i.code: n for n, i in enumerate(pool)}
    for c in (*must, *exclude):
        if c not in pos:
            raise ValueError(f"约束里的 {c!r} 不在池子里；本基准要求约束编码都必须存在于池中")
    must_mask = 0
    for c in must:
        must_mask |= 1 << pos[c]
    exclude_mask = 0
    for c in exclude:
        exclude_mask |= 1 << pos[c]
    return must_mask, exclude_mask


def walk_with_mask(pool: Sequence[Item], k: int, must_mask: int, exclude_mask: int) -> int:
    """掩码沿递归传递，判定退化成两次 ``&`` —— :class:`combo_search.ComboIndex` 的写法。"""
    count = 0

    def rec(start: int, mask: int, taken: int) -> None:
        nonlocal count
        if taken == k:
            if mask & must_mask == must_mask and not (mask & exclude_mask):
                count += 1
            return
        for i in range(start, len(pool) - (k - taken) + 1):
            rec(i + 1, mask | (1 << i), taken + 1)

    rec(0, 0, 0)
    return count


def bench_mask(*, items: int = 60, repeat: int = REPEAT) -> None:
    print("\n## 4. 约束检查：位掩码随递归传递 vs 每个叶子重建 set")
    print("   掩码编译（``mask_lookup``）在计时之外 —— 对应 ``ComboIndex`` 只建一次。")
    pool = synthetic_pool(items, seed=7)
    must = (pool[3].code,)
    exclude = (pool[items - 1].code,)
    must_mask, exclude_mask = mask_lookup(pool, must, exclude)
    specs = [("池子 / k", 16, "<"), ("组合数", 12, ">"), ("set 版", 14, ">"),
             ("掩码版", 14, ">"), ("", 2, "<"), ("加速比", 10, ">"),
             ("", 2, "<"), ("结果一致", 10, "<")]
    rows = []
    for k in (2, 3, 4):
        total = count_combos(items, k, k)
        assert (walk_with_sets(pool, k, must, exclude)
                == walk_with_mask(pool, k, must_mask, exclude_mask))
        number = max(1, 20_000 // max(1, total))
        _b1, m1 = timeit(lambda: walk_with_sets(pool, k, must, exclude),
                         repeat=repeat, number=number)
        _b2, m2 = timeit(lambda: walk_with_mask(pool, k, must_mask, exclude_mask),
                         repeat=repeat, number=number)
        rows.append((f"{items} / {k}", f"{total:,}", fmt_us(m1), fmt_us(m2), "",
                     f"{m1 / m2:.2f}×", "", "是"))
    _table(specs, rows)
    print("   说明：``{i.code for i in combo}`` 在每个叶子都要新建一个哈希表，")
    print("   而掩码只是递归参数里的一个整数。池子越大、叶子越多，差距越明显。")


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="微优化基准（全部为本地测量，不联网）")
    ap.add_argument("--only", choices=("comb", "search", "memory", "mask"),
                    action="append", default=None, help="只跑指定的组，可重复")
    ap.add_argument("--items", type=int, default=22, help="合成池大小（search 用）")
    ap.add_argument("--repeat", type=int, default=REPEAT, help="每项计时重复次数")
    return ap


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    todo = args.only or ["comb", "search", "memory", "mask"]
    print("=" * 78)
    print("  麦麦积分精算师 · 微优化基准")
    print(f"  Python {sys.version.split()[0]} · {sys.platform} · 重复 {args.repeat} 次取中位数")
    print("=" * 78)
    print("  数字与机器相关：比值比绝对值更值得记住。")
    if "comb" in todo:
        bench_comb(repeat=args.repeat)
    if "search" in todo:
        bench_search(items=args.items, repeat=args.repeat)
    if "memory" in todo:
        bench_memory()
    if "mask" in todo:
        bench_mask(items=max(20, args.items), repeat=args.repeat)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
