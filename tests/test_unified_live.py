#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""统一端到端联调脚本的**纯函数**单元测试（离线，不需要 MCP Token）。

重点保护三处最容易出错、且直接决定结论可信度的地方：

1. **菜单归一化**：MCP 返回的价格是字符串（``"33.5"``），漏转或重复乘 100 会让
   所有金额整体偏移 100 倍。
2. **券的商品名解析**：第三方券是用户手抄的，只有**精确命中**才接受；
   近似匹配在本项目中被实测证明会串味（「两件套」被错配到「三件套」）。
3. **积分券的生成判据**：打包价不低于菜单价时必须丢弃；映射到不存在的商品时必须丢弃。
"""
from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from optimize_deals import Item, to_cents  # noqa: E402
from run_unified_live import (  # noqa: E402
    build_parser, candidate_pool, dpad, dwidth, load_channels, map_coupons,
    menu_items, points_coupons, resolve_coupon_rows,
)

DATA = os.path.join(os.path.dirname(__file__), "..", "data")

MENU_RAW = {
    "categories": [
        {"name": "汉堡", "meals": [{"code": "1100"}, {"code": "1406"}]},
        {"name": "小食", "meals": [{"code": "4810"}]},
        {"name": "饮品", "meals": [{"code": "9900008751"}]},
    ],
    "meals": {
        "1100": {"name": "巨无霸", "currentPrice": "26.5", "originalPrice": "26.5"},
        "1406": {"name": "板烧鸡腿堡", "currentPrice": "23.5", "originalPrice": "23.5"},
        "4810": {"name": "薯条", "currentPrice": "14.5", "originalPrice": "14.5"},
        "9900008751": {"name": "可乐", "currentPrice": "9.5", "originalPrice": "9.5"},
        "9999": {"name": "赠品（0 元）", "currentPrice": "0"},
    },
}


class TestMenuItems(unittest.TestCase):
    def test_price_string_to_cents(self):
        items = {i.code: i for i in menu_items(MENU_RAW)}
        self.assertEqual(items["1100"].price_cents, 2650)          # 不是 265000
        self.assertEqual(items["1100"].category, "汉堡")

    def test_zero_price_dropped(self):
        self.assertNotIn("9999", {i.code for i in menu_items(MENU_RAW)})

    def test_empty_menu(self):
        self.assertEqual(menu_items({}), [])
        self.assertEqual(menu_items(None or {}), [])


class TestResolveCouponRows(unittest.TestCase):
    def setUp(self):
        self.items = menu_items(MENU_RAW)
        self.by_code = {i.code: i for i in self.items}
        self.by_name = {i.name: i for i in self.items}

    def test_item_name_resolved(self):
        rows, dropped = resolve_coupon_rows(
            [{"id": "X", "name": "半价巨无霸", "type": "item_discount",
              "item_name": "巨无霸", "rate": 0.5}], self.by_code, self.by_name)
        self.assertEqual(dropped, [])
        self.assertEqual(rows[0]["item_code"], "1100")
        self.assertNotIn("item_name", rows[0])

    def test_buy_get_names_resolved(self):
        rows, dropped = resolve_coupon_rows(
            [{"id": "X", "name": "买薯条送可乐", "type": "buy_get",
              "buy_name": "薯条", "get_name": "可乐"}], self.by_code, self.by_name)
        self.assertEqual(dropped, [])
        self.assertEqual(rows[0]["buy_code"], "4810")
        self.assertEqual(rows[0]["get_code"], "9900008751")

    def test_item_names_resolved_for_bundle(self):
        rows, dropped = resolve_coupon_rows(
            [{"id": "X", "name": "组合", "type": "bundle_price",
              "item_names": ["巨无霸", "可乐"], "fixed": 21.9}], self.by_code, self.by_name)
        self.assertEqual(dropped, [])
        self.assertEqual(rows[0]["item_codes"], ["1100", "9900008751"])

    def test_unknown_name_dropped_not_guessed(self):
        """「巨无霸套餐」不应被近似匹配到「巨无霸」—— 宁可丢弃。"""
        rows, dropped = resolve_coupon_rows(
            [{"id": "X", "name": "巨无霸套餐券", "type": "item_reduce",
              "item_name": "巨无霸套餐", "reduce": 3}], self.by_code, self.by_name)
        self.assertEqual(rows, [])
        self.assertEqual(len(dropped), 1)
        self.assertIn("不在本店菜单", dropped[0])

    def test_unknown_code_dropped(self):
        rows, dropped = resolve_coupon_rows(
            [{"id": "X", "name": "c", "type": "item_free", "item_code": "NOPE"}],
            self.by_code, self.by_name)
        self.assertEqual(rows, [])
        self.assertEqual(len(dropped), 1)


class TestResolveCouponRowsFromText(unittest.TestCase):
    """v3.3：没有 ``type`` 的券行按**券面文本**解析（评审项⑦）。"""

    def setUp(self):
        self.items = menu_items(MENU_RAW)
        self.by_code = {i.code: i for i in self.items}
        self.by_name = {i.name: i for i in self.items}

    def _resolve(self, rows):
        return resolve_coupon_rows(rows, self.by_code, self.by_name)

    def test_threshold_row_from_text(self):
        rows, dropped = self._resolve([{"id": "T1", "name": "满30元减5元"}])
        self.assertEqual(dropped, [])
        self.assertEqual(rows[0]["type"], "threshold_reduce")
        self.assertEqual(rows[0]["threshold"], 30.0)
        self.assertEqual(rows[0]["reduce"], 5.0)

    def test_item_name_is_landed_by_the_parser(self):
        rows, dropped = self._resolve([{"id": "T2", "name": "巨无霸立减4元"}])
        self.assertEqual(dropped, [])
        self.assertEqual(rows[0]["type"], "item_reduce")
        self.assertEqual(rows[0]["item_code"], "1100")

    def test_low_confidence_row_is_dropped_with_a_reason(self):
        # 「限部分门店」= 券在不在本店未知 ⇒ 宁可不用，也不猜
        rows, dropped = self._resolve([{"id": "T3", "name": "满30减5，限部分门店"}])
        self.assertEqual(rows, [])
        self.assertEqual(len(dropped), 1)
        self.assertIn("券面解析未通过", dropped[0])
        self.assertIn("置信度", dropped[0])

    def test_structurally_unsupported_row_is_dropped(self):
        rows, dropped = self._resolve([{"id": "T4", "name": "第二杯半价"}])
        self.assertEqual(rows, [])
        self.assertIn("结构不支持", dropped[0])

    def test_explicit_type_is_never_overridden(self):
        """人工标注优先于文本推断 —— 优先级倒了会让"手抄对了"反而被改写。"""
        rows, _ = self._resolve([{"id": "T5", "name": "满30元减5元",
                                  "type": "threshold_reduce",
                                  "threshold": 99.0, "reduce": 1.0}])
        self.assertEqual(rows[0]["threshold"], 99.0)
        self.assertEqual(rows[0]["reduce"], 1.0)

    def test_parser_output_survives_coupon_from_dict(self):
        from optimize_deals import Coupon
        rows, _ = self._resolve([{"id": "T6", "name": "满30元减5元"}])
        c = Coupon.from_dict(rows[0])
        self.assertEqual((c.id, c.threshold_cents, c.reduce_cents), ("T6", 3000, 500))

    def test_stacking_hint_does_not_break_coupon_construction(self):
        from optimize_deals import Coupon
        rows, _ = self._resolve([{"id": "T7", "name": "买巨无霸送可乐 每单限用一张"}])
        self.assertEqual(rows[0]["_stack_hint"], "single")
        self.assertEqual(Coupon.from_dict(rows[0]).type, "buy_get")


class TestLoadChannels(unittest.TestCase):
    def test_live_third_party_resolves_against_live_menu(self):
        """仓库里的 live_third_party.json 必须能被**实查菜单**完整解析。

        这份数据是照着 MCP 实查菜单（storeCode=1990346）写的，因此这里用同一批
        真实编码做回归 —— 一旦有人改了券里的商品名却忘了同步菜单，测试会立刻失败。
        """
        live_codes = ["1100", "9900008751", "4810", "1401", "1440", "1700", "1406",
                      "9900008752", "9900000888"]
        codes = {c: Item(code=c, name=f"item-{c}", price_cents=1000) for c in live_codes}
        names = {"1100": "巨无霸", "9900008751": "可乐", "4810": "薯条", "1401": "麦乐鸡",
                 "1440": "麦辣鸡腿汉堡", "1700": "麦辣鸡翅", "1406": "板烧鸡腿堡",
                 "9900008752": "派", "9900000888": "巨无霸四件套"}
        by_code = codes
        by_name = {names[c]: codes[c] for c in live_codes}

        channels, dropped = load_channels(
            os.path.join(DATA, "live_third_party.json"), by_code, by_name,
            skip_codes=("mcd-pickup", "mcd-delivery"))
        self.assertEqual(dropped, [], f"有券未解析成功：{dropped}")
        self.assertEqual([c.code for c in channels], ["meituan", "eleme"])
        meituan = channels[0]
        self.assertTrue(meituan.allow_stack)
        self.assertEqual(meituan.max_stack, 2)
        # 买薯条送可乐 解析后应带上真实编码
        bg = next(c for c in meituan.coupons if c.type == "buy_get")
        self.assertEqual((bg.buy_code, bg.get_code), ("4810", "9900008751"))


class TestPointsCoupons(unittest.TestCase):
    OFFERS = [
        {"spuName": "21.9元巨无霸可乐组合", "spuId": 1, "point": "800", "shopId": 2,
         "status": 2, "downTime": "2026-12-30 23:59:59"},
        {"spuName": "28.8元巨无霸四件套", "spuId": 2, "point": "800", "shopId": 2,
         "status": 2, "downTime": "2026-12-30 23:59:59"},
        {"spuName": "免费试吃", "spuId": 3, "point": "0", "shopId": 2, "status": 2},
        {"spuName": "麦当劳亲子读书会", "spuId": 4, "point": "0", "shopId": 5, "status": 2},
    ]

    def _maps(self):
        items = menu_items(MENU_RAW)
        items.append(Item(code="777", name="巨无霸四件套", price_cents=3200))
        by_code = {i.code: i for i in items}
        by_name = {i.name: i for i in items}
        return by_code, by_name

    def test_mapping_and_exact_name(self):
        by_code, by_name = self._maps()
        coupons, details, notes = points_coupons(
            self.OFFERS, os.path.join(DATA, "sample_points_mapping.json"), by_code, by_name)
        got = {d["name"]: d for d in details}
        # 映射表里的条目，只有「标的仍在该店积分商城」且「商品在本店菜单」才被采纳
        self.assertIn("21.9元巨无霸可乐组合", got)
        self.assertNotIn("17.9元板烧可乐组合", got)   # 该标的已不在 OFFERS 里
        # 「28.8元巨无霸四件套」的映射编码不在本菜单，但**精确同名**兜底仍应命中
        self.assertIn("28.8元巨无霸四件套", got)
        self.assertEqual(got["28.8元巨无霸四件套"]["source"], "精确同名")
        self.assertEqual(got["28.8元巨无霸四件套"]["discount_cents"], 320)   # 32.0 − 28.8
        self.assertEqual(got["21.9元巨无霸可乐组合"]["discount_cents"], 1410)
        self.assertTrue(all(c.point_cost > 0 for c in coupons))
        self.assertTrue(all(c.type == "bundle_price" for c in coupons))

    def test_skips_zero_point_and_unknown_offer(self):
        by_code, by_name = self._maps()
        _, details, notes = points_coupons(
            self.OFFERS, os.path.join(DATA, "sample_points_mapping.json"), by_code, by_name)
        names = {d["name"] for d in details}
        self.assertNotIn("免费试吃", names)
        self.assertNotIn("麦当劳亲子读书会", names)
        self.assertTrue(any("映射的商品不在本店菜单" in n for n in notes))

    def test_no_mapping_file_only_exact_names(self):
        by_code, by_name = self._maps()
        coupons, details, _ = points_coupons(self.OFFERS, "", by_code, by_name)
        self.assertEqual([d["name"] for d in details], ["28.8元巨无霸四件套"])
        self.assertEqual(len(coupons), 1)


class TestMapCoupons(unittest.TestCase):
    def test_threshold_reduce(self):
        got = map_coupons([{"couponId": "1", "name": "满30减5",
                            "threshold": 30.0, "reduce": 5.0}])
        self.assertEqual(len(got), 1)
        self.assertEqual((got[0].threshold_cents, got[0].reduce_cents), (3000, 500))

    def test_unmappable_skipped(self):
        self.assertEqual(map_coupons([{"couponName": "麦旋风任选"}]), [])
        self.assertEqual(map_coupons(None), [])


class TestCandidatePool(unittest.TestCase):
    def test_pool_is_coupon_related(self):
        from channel_planner import Channel, point_bundle_coupon
        items = menu_items(MENU_RAW)
        ch = Channel(code="x", name="X", coupons=(
            point_bundle_coupon("P", "组合", ("1100", "9900008751"), to_cents(21.9), 800),))
        pool = candidate_pool(items, [ch])
        self.assertEqual(sorted(i.code for i in pool), ["1100", "9900008751"])


class TestTerminalWidth(unittest.TestCase):
    """表格按**显示列宽**补齐 —— 否则截图与终端里的列会逐行错位。

    这里的断言直接盯住「对齐」这个可观测事实：同一列的右边界必须落在同一列号上。
    """

    def test_cjk_counts_as_two_columns(self):
        self.assertEqual(dwidth("abc"), 3)
        self.assertEqual(dwidth("巨无霸"), 6)
        self.assertEqual(dwidth("17.9元板烧可乐组合"), 18)
        self.assertEqual(dwidth("¥33.00"), 6)      # ¥ 是半角（U+00A5）
        self.assertEqual(dwidth("（）"), 4)

    def test_pad_reaches_exact_columns(self):
        for s in ("abc", "巨无霸", "17.9元板烧可乐组合", ""):
            for align in ("<", ">", "^"):
                self.assertEqual(dwidth(dpad(s, 28, align)), 28)

    def test_overlong_is_not_truncated(self):
        long = "很长很长很长很长很长的名字"
        self.assertEqual(dpad(long, 4), long)
        self.assertGreaterEqual(dwidth(dpad(long, 4)), dwidth(long))

    def test_column_edges_line_up(self):
        """把积分券表的两行按同一套宽度拼出来，每个字段的右边界列号必须相同。"""
        rows = [
            ("17.9元板烧可乐组合", "500", "¥33.00", "¥17.90", "¥15.10"),
            ("5.9元香芋派/菠萝派任选", "50", "¥8.50", "¥5.90", "¥2.60"),
        ]
        edges = []
        for r in rows:
            fields = [dpad(r[0], 28), dpad(r[1], 8, ">"), dpad(r[2], 10, ">"),
                      dpad(r[3], 10, ">"), dpad(r[4], 9, ">")]
            self.assertEqual([dwidth(f) for f in fields], [28, 8, 10, 10, 9])
            acc, cuts = 0, []
            for f in fields[:-1]:
                acc += dwidth(f)
                cuts.append(acc)
            edges.append(cuts)
        # 两行的列边界完全重合 → 目视对齐
        self.assertEqual(edges[0], edges[1])
        self.assertEqual(edges[0], [28, 36, 46, 56])


class TestCliFlags(unittest.TestCase):
    """缓存相关的开关散在 ``main`` 里时测不到，现在 ``build_parser`` 可以单独验。"""

    def test_cache_flags_default_off(self):
        args = build_parser().parse_args([])
        self.assertFalse(args.no_cache)
        self.assertFalse(args.refresh)
        self.assertFalse(args.no_prefetch)
        self.assertIsNone(args.cache_dir)

    def test_cache_flags_are_accepted(self):
        args = build_parser().parse_args(["--no-cache"])
        self.assertTrue(args.no_cache)
        args = build_parser().parse_args(["--refresh"])
        self.assertTrue(args.refresh)
        args = build_parser().parse_args(["--refresh", "--cache-dir", "/tmp/x"])
        self.assertTrue(args.refresh)
        self.assertEqual(args.cache_dir, "/tmp/x")

    def test_refresh_and_no_cache_are_independent(self):
        """``--refresh`` 是"回源但保留缓存"，``--no-cache`` 是"根本不用缓存"，两者不同。"""
        args = build_parser().parse_args(["--refresh", "--no-cache"])
        self.assertTrue(args.refresh)
        self.assertTrue(args.no_cache)

    def test_offline_default_is_false(self):
        self.assertFalse(build_parser().parse_args([]).offline)


if __name__ == "__main__":
    unittest.main(verbosity=2)
