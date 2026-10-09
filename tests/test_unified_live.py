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
    candidate_pool, load_channels, map_coupons, menu_items, points_coupons,
    resolve_coupon_rows,
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
