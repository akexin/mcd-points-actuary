#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""券面文本解析层 —— 单元测试（离线，无需 MCP Token）。

覆盖重点：
  1. 中文数字 / 折扣率 / 金额单位的解析（`八八折` 不是 `8+8`）
  2. 七种券型的文本形态识别
  3. **置信度闸门**：低置信度的结果必须**不产出券**，而不是"低分券"
  4. `refused`（结构不支持）与 `ambiguous`（不敢确定）必须分开 ——
     前者是模型边界，后者是信息不足，混在一起会让人以为"项目不支持券"
  5. 文本没写的字段（封顶 / 门槛 / 互斥组）必须进 `unstated`，**绝不推断**
  6. 对 `data/` 里真实券面对拍：文本能推出来的字段必须与人工标注的一致
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from optimize_deals import Coupon, Item  # noqa: E402
from rule_parser import (  # noqa: E402
    DEFAULT_THRESHOLD, MAX_CONFIDENCE, MenuIndex, ParseOutcome, cn_number, cn_rate,
    main, parse_rule, parse_rows, split_tail, unit_after,
)

DATA = os.path.join(os.path.dirname(__file__), "..", "data")


def load_menu() -> list:
    with open(os.path.join(DATA, "sample_menu.json"), encoding="utf-8") as fh:
        return [Item.from_dict(d) for d in json.load(fh)]


MENU = load_menu()
INDEX = MenuIndex.build(MENU)

#: `data/sample_channels.json` 里的**全部券面文本**（去重后 17 条）。
#: 这是"真实数据"而不是我编的用例 —— 解析层的第一验收标准是能覆盖它们。
REAL_TEXTS = [
    "满30元减5元", "麦乐鸡（5块）立减4元", "买中薯条送中杯可乐", "圣代免费",
    "积分兑换·巨无霸（500积分）", "积分兑换·圣代（20积分）",
    "麦乐送满50减12", "美团平台券 满39减8", "美团平台券 满69减15",
    "美团平台券 满99减20", "麦当劳商家券 满30减8", "必点榜专属无门槛券 减5",
    "半价周末·巨无霸 5折", "超级吃货卡红包 无门槛减6", "店铺满减 满35减10",
    "店铺满减 满60减18", "饿了么整单 88折（封顶12元）",
]


def run_cli(*argv: str) -> tuple:
    """跑 CLI，返回 ``(退出码, stdout)``。"""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = main(list(argv))
    return code, buf.getvalue()


class TestNumberParsing(unittest.TestCase):
    """中文数字与折扣率 —— 这里错一位就是"满100减20"读成"满3减20"。"""

    def test_arabic(self):
        self.assertEqual(cn_number("30"), 30.0)
        self.assertEqual(cn_number("8.8"), 8.8)

    def test_chinese_digits(self):
        self.assertEqual(cn_number("八"), 8.0)
        self.assertEqual(cn_number("十"), 10.0)
        self.assertEqual(cn_number("十五"), 15.0)
        self.assertEqual(cn_number("二十"), 20.0)
        self.assertEqual(cn_number("二十五"), 25.0)

    def test_digit_run_is_concatenated_not_added(self):
        # 「八八折」= 88 折，不是 8+8=16
        self.assertEqual(cn_number("八八"), 88.0)

    def test_half(self):
        self.assertEqual(cn_number("半"), 0.5)
        self.assertEqual(cn_number("一半"), 0.5)

    def test_unsupported_forms_raise_instead_of_guessing(self):
        for bad in ("一百", "一百二十", "", "abc", "3十2"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                cn_number(bad)

    def test_rate_arabic_and_chinese_agree(self):
        for raw in ("88", "八八"):
            self.assertAlmostEqual(cn_rate(raw), 0.88, places=4)
        for raw in ("8", "八"):
            self.assertAlmostEqual(cn_rate(raw), 0.8, places=4)
        for raw in ("5", "五"):
            self.assertAlmostEqual(cn_rate(raw), 0.5, places=4)

    def test_rate_accepts_trailing_zhe(self):
        self.assertAlmostEqual(cn_rate("八八折"), 0.88, places=4)

    def test_half_price(self):
        self.assertAlmostEqual(cn_rate("半价"), 0.5, places=4)

    def test_unit_after(self):
        self.assertEqual(unit_after("满30元减5元", 3), "元")
        self.assertEqual(unit_after("满30减5", 3), "")
        self.assertEqual(unit_after("5块钱", 1), "块钱")


class TestMenuIndex(unittest.TestCase):
    """商品名 → 菜单编码。定位不了时**必须说定位不了**。"""

    def test_exact_code(self):
        item, how = INDEX.resolve("FRIES-M")
        self.assertIsNotNone(item)
        self.assertEqual(item.code, "FRIES-M")
        self.assertEqual(how, "编码")

    def test_exact_name_with_fullwidth_parens(self):
        item, how = INDEX.resolve("麦乐鸡（5块）")
        self.assertEqual(item.code, "CHICKEN-MCNUGGETS-5")
        self.assertEqual(how, "全名")

    def test_alias(self):
        item, how = INDEX.resolve("中薯")
        self.assertEqual(item.code, "FRIES-M")
        self.assertEqual(how, "别名")

    def test_ambiguous_name_is_refused_not_guessed(self):
        # 「巨无霸套餐」同时"包含" BURGER-BIGMAC（巨无霸）与 COMBO-BIGMAC
        # （巨无霸套餐（含中薯+中可）），包含匹配给不出唯一答案 ⇒ 必须判歧义，
        # 而不是先撞上谁就返回谁。
        item, how = INDEX.resolve("巨无霸套餐")
        self.assertIsNone(item)
        self.assertTrue(how.startswith("歧义"), how)

    def test_exact_name_wins_before_containment(self):
        # 「巨无霸」本身是 BURGER-BIGMAC 的全名，虽然它也是套餐名的前缀，
        # 但全名命中排在包含之前，所以不该判歧义。
        item, how = INDEX.resolve("巨无霸")
        self.assertEqual(item.code, "BURGER-BIGMAC")
        self.assertEqual(how, "全名")

    def test_unique_containment_resolves(self):
        item, how = INDEX.resolve("麦乐鸡")
        self.assertEqual(item.code, "CHICKEN-MCNUGGETS-5")
        self.assertEqual(how, "包含")

    def test_unknown_name(self):
        item, how = INDEX.resolve("双层鳕鱼堡")
        self.assertIsNone(item)
        self.assertEqual(how, "未命中")

    def test_empty_index_resolves_nothing(self):
        empty = MenuIndex.build(None)
        self.assertFalse(empty.enabled)
        self.assertIsNone(empty.resolve("巨无霸")[0])


class TestSplitTail(unittest.TestCase):
    """尾部附加说明的切分。切错了会误伤锚点，切多了会丢约束。"""

    def test_plain_text_untouched(self):
        self.assertEqual(split_tail("满30元减5元"), ("满30元减5元", ""))

    def test_cap_is_not_a_tail_clause(self):
        # 「封顶」是金额结构的一部分，切掉它折扣就没上界了
        body, tail = split_tail("饿了么整单 88折（封顶12元）")
        self.assertEqual(tail, "")
        self.assertIn("封顶12元", body)

    def test_scope_bracket_is_cut(self):
        self.assertEqual(split_tail("圣代免费（限部分门店）"),
                         ("圣代免费", "（限部分门店）"))

    def test_frequency_clause_is_cut(self):
        self.assertEqual(split_tail("买中薯条送中杯可乐 每单限用一张"),
                         ("买中薯条送中杯可乐", "每单限用一张"))

    def test_body_is_always_a_prefix_so_spans_stay_valid(self):
        for text in REAL_TEXTS:
            body, _ = split_tail(text)
            self.assertTrue(text.startswith(body), text)


class TestThresholdReduce(unittest.TestCase):
    def test_with_units(self):
        out = parse_rule("满30元减5元", menu=INDEX)
        self.assertTrue(out.accepted)
        self.assertEqual(out.fields["type"], "threshold_reduce")
        self.assertEqual(out.fields["threshold"], 30.0)
        self.assertEqual(out.fields["reduce"], 5.0)

    def test_without_units(self):
        out = parse_rule("美团平台券 满39减8", menu=INDEX)
        self.assertTrue(out.accepted)
        self.assertEqual((out.fields["threshold"], out.fields["reduce"]), (39.0, 8.0))
        # 没写单位 ⇒ 拿不到"金额带单位"那 0.06
        self.assertLess(out.confidence, parse_rule("满39元减8元", menu=INDEX).confidence)

    def test_no_threshold_means_zero(self):
        out = parse_rule("必点榜专属无门槛券 减5", menu=INDEX)
        self.assertTrue(out.accepted, out.reason)
        self.assertEqual(out.fields["threshold"], 0.0)
        self.assertEqual(out.fields["reduce"], 5.0)

    def test_threshold_above_reduce_is_flagged(self):
        out = parse_rule("满5元减30元", menu=INDEX)
        self.assertTrue(any("疑似抄错" in n for n in out.notes), out.notes)

    def test_free_order_is_not_stolen_by_item_reduce(self):
        # 顺序 bug：单品模式「X 减 Y」会先抢走「无门槛减6」，把它读成某商品的立减
        out = parse_rule("超级吃货卡红包 无门槛减6", menu=INDEX)
        self.assertEqual(out.fields["type"], "threshold_reduce")
        self.assertEqual(out.fields["reduce"], 6.0)


class TestItemPatterns(unittest.TestCase):
    def test_item_reduce_resolves_code(self):
        out = parse_rule("麦乐鸡（5块）立减4元", menu=INDEX)
        self.assertTrue(out.accepted)
        self.assertEqual(out.fields["type"], "item_reduce")
        self.assertEqual(out.fields["item_code"], "CHICKEN-MCNUGGETS-5")
        self.assertEqual(out.fields["reduce"], 4.0)

    def test_item_free(self):
        out = parse_rule("圣代免费", menu=INDEX)
        self.assertEqual(out.fields, {"type": "item_free", "item_code": "SUNDAE"})

    def test_item_free_with_suffix_word(self):
        self.assertTrue(parse_rule("圣代免费券", menu=INDEX).accepted)

    def test_buy_get_resolves_both_codes(self):
        out = parse_rule("买中薯条送中杯可乐", menu=INDEX)
        self.assertEqual(out.fields["type"], "buy_get")
        self.assertEqual(out.fields["buy_code"], "FRIES-M")
        self.assertEqual(out.fields["get_code"], "COLA-M")

    def test_bundle_price(self):
        out = parse_rule("21.9元买巨无霸+中杯可乐", menu=INDEX)
        self.assertTrue(out.accepted)
        self.assertEqual(out.fields["type"], "bundle_price")
        self.assertEqual(out.fields["fixed"], 21.9)
        self.assertEqual(set(out.fields["item_codes"]), {"BURGER-BIGMAC", "COLA-M"})

    def test_bundle_with_one_known_item_is_capped(self):
        out = parse_rule("21.9元买巨无霸+儿童套餐", menu=INDEX)
        self.assertLess(out.confidence, DEFAULT_THRESHOLD)
        self.assertEqual(out.status, "ambiguous")

    def test_bundle_codes_are_in_menu_order(self):
        # 顺序必须可复现，否则 item_codes 元组随文案微调而变，回归对拍失效
        first = parse_rule("21.9元买巨无霸+中杯可乐", menu=INDEX).fields["item_codes"]
        second = parse_rule("21.9元买中杯可乐+巨无霸", menu=INDEX).fields["item_codes"]
        self.assertEqual(tuple(first), tuple(second))

    def test_redeem_carries_point_cost(self):
        out = parse_rule("积分兑换·巨无霸（500积分）", menu=INDEX)
        self.assertEqual(out.fields["type"], "item_free")
        self.assertEqual(out.fields["item_code"], "BURGER-BIGMAC")
        self.assertEqual(out.fields["point_cost"], 500)

    def test_redeem_alt_form(self):
        out = parse_rule("巨无霸（500积分）", menu=INDEX)
        self.assertTrue(out.accepted)
        self.assertEqual(out.fields["point_cost"], 500)

    def test_redeem_name_has_no_leading_separator(self):
        # 曾经把「·巨无霸」当成商品名，导致下游按名字查菜单时命中不了
        out = parse_rule("积分兑换·巨无霸（500积分）", menu=INDEX)
        self.assertEqual(out.fields["item_code"], "BURGER-BIGMAC")


class TestDiscountPatterns(unittest.TestCase):
    def test_item_discount_when_item_is_identifiable(self):
        out = parse_rule("半价周末·巨无霸 5折", menu=INDEX)
        self.assertTrue(out.accepted, out.reason)
        self.assertEqual(out.fields["type"], "item_discount")
        self.assertEqual(out.fields["item_code"], "BURGER-BIGMAC")
        self.assertAlmostEqual(out.fields["rate"], 0.5, places=4)

    def test_rate_is_always_present(self):
        # 漏写 rate 的话 Coupon.from_dict 会静默取 0.0 ⇒ "这张券没优惠"
        for text in ("全场8折（封顶20元）", "半价周末·巨无霸 5折"):
            out = parse_rule(text, menu=INDEX)
            self.assertIn("rate", out.fields, text)
            self.assertGreater(out.fields["rate"], 0.0)

    def test_order_discount_when_declared(self):
        out = parse_rule("饿了么整单 88折（封顶12元）", menu=INDEX)
        self.assertEqual(out.fields["type"], "discount")
        self.assertAlmostEqual(out.fields["rate"], 0.88, places=4)
        self.assertEqual(out.fields["cap"], 12.0)

    def test_corroborating_statements_raise_confidence(self):
        both = parse_rule("半价周末·巨无霸 5折", menu=INDEX)
        # 去掉「半价」后交叉印证消失，分数应下降
        without = parse_rule("周末·巨无霸 5折", menu=INDEX)
        self.assertGreater(both.confidence, without.confidence)

    def test_conflicting_statements_are_rejected(self):
        out = parse_rule("半价周末·巨无霸 8折", menu=INDEX)
        self.assertFalse(out.accepted)
        self.assertTrue(any(e.kind == "交叉矛盾" for e in out.evidence), out.evidence)

    def test_uncapped_discount_is_penalised(self):
        capped = parse_rule("全场8折（封顶20元）", menu=INDEX)
        uncapped = parse_rule("全场8折", menu=INDEX)
        self.assertGreater(capped.confidence, uncapped.confidence)
        self.assertTrue(any(e.kind == "无封顶" for e in uncapped.evidence))

    def test_unknown_scope_is_penalised_not_guessed(self):
        out = parse_rule("半价周末 5折", menu=INDEX)
        self.assertFalse(out.accepted)
        self.assertTrue(any(e.kind == "适用对象不明" for e in out.evidence))

    def test_no_menu_never_invents_an_item_code(self):
        # 曾经：没有菜单时 _resolve_names 直接回传 token，于是「全场」被判成商品编码，
        # 凭空造出一张绑定商品的券
        out = parse_rule("全场8折", menu=MenuIndex.build(None))
        self.assertNotIn("item_code", out.fields)


class TestConfidenceGate(unittest.TestCase):
    """闸门：低置信度必须**不产出券**，而不是产出一张"低分券"。"""

    def test_low_confidence_clears_fields(self):
        out = parse_rule("满30减5，限部分门店", menu=INDEX)
        self.assertEqual(out.status, "ambiguous")
        self.assertFalse(out.accepted)
        self.assertEqual(out.fields, {})
        self.assertLess(out.confidence, DEFAULT_THRESHOLD)

    def test_scope_restriction_bites_hard(self):
        clean = parse_rule("圣代免费", menu=INDEX)
        scoped = parse_rule("圣代免费（限部分门店）", menu=INDEX)
        self.assertTrue(clean.accepted)
        self.assertFalse(scoped.accepted)
        self.assertTrue(any(e.kind == "门店范围限定" for e in scoped.evidence))

    def test_threshold_is_a_real_knob(self):
        strict = parse_rule("全场8折", menu=INDEX, threshold=0.75)
        loose = parse_rule("全场8折", menu=INDEX, threshold=0.70)
        self.assertFalse(strict.accepted)
        self.assertTrue(loose.accepted)

    def test_boundary_is_inclusive(self):
        out = parse_rule("全场8折", menu=INDEX, threshold=0.70)
        self.assertAlmostEqual(out.confidence, 0.70, places=3)
        self.assertTrue(out.accepted)

    def test_zero_threshold_accepts_everything_parseable(self):
        out = parse_rule("半价周末 5折", menu=INDEX, threshold=0.0)
        self.assertTrue(out.accepted)

    def test_confidence_never_exceeds_one(self):
        for text in REAL_TEXTS:
            self.assertLessEqual(parse_rule(text, menu=INDEX).confidence, MAX_CONFIDENCE)

    def test_rejection_reason_is_actionable(self):
        out = parse_rule("全场8折", menu=INDEX)
        self.assertIn("补上封顶额", out.reason)

    def test_reason_does_not_mislead_when_blocker_is_not_a_field(self):
        # 门店范围是拦路虎时，"补上 stack_group 即可纳入"会把人带偏
        out = parse_rule("满30减5，限部分门店", menu=INDEX)
        self.assertNotIn("即可纳入计算", out.reason)


class TestStructuralRefusals(unittest.TestCase):
    """结构不支持 ≠ 听不懂。前者是模型边界，必须**大声说**。"""

    def _assert_refused(self, text: str):
        out = parse_rule(text, menu=INDEX)
        self.assertEqual(out.status, "refused", f"{text} → {out.status}")
        self.assertIn("结构不支持", out.reason)
        self.assertEqual(out.fields, {})
        return out

    def test_second_cup_half_price(self):
        self._assert_refused("第二杯半价")

    def test_buy_one_get_one(self):
        self._assert_refused("买一送一")

    def test_item_count_threshold(self):
        self._assert_refused("满2件减5元")

    def test_random_discount(self):
        out = self._assert_refused("随机立减最高5元")
        self.assertIn("保守分位", out.reason)

    def test_refusal_survives_trailing_clause(self):
        # 「第二杯半价（每单限1张）」的尾部说明不能把结构问题盖掉
        self._assert_refused("第二杯半价（每单限1张）")

    def test_frequency_limit_is_a_note_not_a_refusal(self):
        # 它不改变"这一单省多少"，拒绝它等于白扔一张算得对的券
        out = parse_rule("买中薯条送中杯可乐 每单限用一张", menu=INDEX)
        self.assertTrue(out.accepted, out.reason)
        self.assertEqual(out.fields["buy_code"], "FRIES-M")
        self.assertTrue(any("频次" in n for n in out.notes), out.notes)

    def test_single_use_per_order_gets_a_stack_hint(self):
        out = parse_rule("买中薯条送中杯可乐 每单限用一张", menu=INDEX)
        self.assertEqual(out.fields.get("_stack_hint"), "single")

    def test_unknown_shape_is_reported_not_guessed(self):
        out = parse_rule("神秘优惠", menu=INDEX)
        self.assertEqual(out.status, "ambiguous")
        self.assertIn("没有命中任何已知券型", out.reason)

    def test_empty_text(self):
        self.assertEqual(parse_rule("   ").status, "refused")


class TestUnstatedFields(unittest.TestCase):
    """文本没写的字段必须进 unstated —— 本模块**绝不推断平台规则**。"""

    def test_cap_and_threshold_are_listed_when_absent(self):
        out = parse_rule("半价周末·巨无霸 5折", menu=INDEX)
        self.assertIn("封顶额（cap）", out.unstated)
        self.assertIn("门槛（threshold）", out.unstated)

    def test_cap_present_but_threshold_absent(self):
        out = parse_rule("饿了么整单 88折（封顶12元）", menu=INDEX)
        self.assertNotIn("封顶额（cap）", out.unstated)
        self.assertIn("门槛（threshold）", out.unstated)

    def test_stack_group_is_never_derivable_from_text(self):
        for text in REAL_TEXTS:
            out = parse_rule(text, menu=INDEX)
            if out.accepted:
                self.assertIn("stack_group（互斥组）", out.unstated, text)

    def test_threshold_reduce_has_no_cap_entry(self):
        # 满减券不看 cap，列出来只会稀释信号
        out = parse_rule("满30元减5元", menu=INDEX)
        self.assertEqual(out.unstated, ["stack_group（互斥组）"])

    def test_unstated_threshold_really_changes_the_estimate(self):
        """这不是文案问题：discount 的 threshold 留 0 会让券变成"无条件可用"。"""
        d = parse_rule("饿了么整单 88折（封顶12元）", menu=INDEX)
        coupon = d.to_coupon("ELE-DISC-88")
        self.assertEqual(coupon.threshold_cents, 0)
        shipped = Coupon(id="ELE-DISC-88", name="饿了么整单 88折（封顶12元）",
                         type="discount", rate=0.88, cap_cents=1200,
                         threshold_cents=3000)
        self.assertNotEqual(coupon.threshold_cents, shipped.threshold_cents)


class TestNoMenuPath(unittest.TestCase):
    """没有菜单时也要能用 —— 商品名留给下游 resolve_coupon_rows 兜底。"""

    def test_names_are_kept_for_downstream_resolution(self):
        out = parse_rule("圣代免费", menu=MenuIndex.build(None))
        self.assertTrue(out.accepted)
        self.assertEqual(out.fields["item_name"], "圣代")
        self.assertNotIn("item_code", out.fields)

    def test_missing_menu_does_not_penalise_shape_confidence(self):
        with_menu = parse_rule("满30元减5元", menu=INDEX).confidence
        without = parse_rule("满30元减5元").confidence
        self.assertEqual(with_menu, without)

    def test_item_reduce_keeps_name(self):
        out = parse_rule("麦乐鸡（5块）立减4元")
        self.assertEqual(out.fields["item_name"], "麦乐鸡（5块）")
        self.assertTrue(out.accepted)

    def test_pool_argument_is_equivalent_to_menu(self):
        a = parse_rule("圣代免费", pool=MENU)
        b = parse_rule("圣代免费", menu=INDEX)
        self.assertEqual(a.fields, b.fields)
        self.assertEqual(a.confidence, b.confidence)


class TestToCoupon(unittest.TestCase):
    def test_builds_a_real_coupon(self):
        out = parse_rule("满30元减5元", menu=INDEX)
        c = out.to_coupon("C1", channel="meituan", stack_group="platform")
        self.assertIsInstance(c, Coupon)
        self.assertEqual(c.id, "C1")
        self.assertEqual(c.threshold_cents, 3000)
        self.assertEqual(c.reduce_cents, 500)
        self.assertEqual(c.channel, "meituan")
        self.assertEqual(c.stack_group, "platform")
        self.assertEqual(c.name, "满30元减5元")

    def test_refuses_to_build_from_a_rejected_parse(self):
        out = parse_rule("第二杯半价", menu=INDEX)
        with self.assertRaises(ValueError):
            out.to_coupon("X")

    def test_refuses_to_build_from_low_confidence(self):
        out = parse_rule("全场8折", menu=INDEX)
        self.assertFalse(out.accepted)
        with self.assertRaises(ValueError):
            out.to_coupon("X")

    def test_discount_coupon_is_usable_by_the_engine(self):
        from optimize_deals import single_coupon_discount
        out = parse_rule("饿了么整单 88折（封顶12元）", menu=INDEX)
        c = out.to_coupon("EL")
        combo = [Item("BURGER-BIGMAC", "巨无霸", 2450),
                 Item("FRIES-M", "中薯条", 1200),
                 Item("COLA-M", "中杯可乐", 950)]
        # 小计 4600 → 88 折省 552，封顶 1200 不生效
        self.assertEqual(single_coupon_discount(combo, c), 552)

    def test_bought_coupon_works_end_to_end(self):
        from optimize_deals import single_coupon_discount
        out = parse_rule("买中薯条送中杯可乐", menu=INDEX)
        c = out.to_coupon("C3")
        combo = [Item("FRIES-M", "中薯条", 1200), Item("COLA-M", "中杯可乐", 950)]
        self.assertEqual(single_coupon_discount(combo, c), 950)   # 送的那杯中可免单


class TestRoundTripAgainstShippedData(unittest.TestCase):
    """对 `data/sample_channels.json` 的真实券面对拍。

    只对 **文本里真的写了** 的字段做等值断言；文本没写的字段必须出现在
    ``unstated`` 里 —— 这条分界线本身就是这个模块的产出。
    """

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(DATA, "sample_channels.json"), encoding="utf-8") as fh:
            cls.data = json.load(fh)

    def _rows(self):
        for ch in self.data["channels"]:
            for row in ch["coupons"]:
                yield ch, row

    def test_every_real_coupon_text_parses_above_the_gate(self):
        for ch, row in self._rows():
            with self.subTest(name=row["name"], channel=ch["code"]):
                out = parse_rule(row["name"], menu=INDEX)
                self.assertTrue(out.accepted, out.reason)

    def test_stated_fields_match_the_handwritten_rows(self):
        fields = ("type", "threshold_cents", "reduce_cents", "item_code",
                  "buy_code", "get_code", "rate", "cap_cents", "point_cost",
                  "item_codes", "fixed_cents")
        for ch, row in self._rows():
            with self.subTest(name=row["name"]):
                out = parse_rule(row["name"], menu=INDEX)
                want = Coupon.from_dict(row)
                got = out.to_coupon(row["id"], stack_group=row.get("stack_group", ""))
                for f in fields:
                    a, b = getattr(want, f), getattr(got, f)
                    if a != b:
                        # 差异只允许出现在"文本压根没写"的字段上，且必须已登记
                        self.assertIn(self._label(f), out.unstated,
                                      f"{row['name']}：{f} 原文={a!r} 解析={b!r}，"
                                      f"但未登记进 unstated={out.unstated}")

    @staticmethod
    def _label(field: str) -> str:
        return {"cap_cents": "封顶额（cap）", "threshold_cents": "门槛（threshold）"}.get(field, "")

    def test_half_price_weekend_really_is_missing_its_cap(self):
        """「半价周末·巨无霸 5折」里没有封顶，但结构化行写了 cap=15。

        这不是解析器的 bug —— 它证明了**券面文本的信息量小于结构化行**，
        所以文本解析永远只能是"入口"，不能替代人工标注。
        """
        out = parse_rule("半价周末·巨无霸 5折", menu=INDEX)
        self.assertTrue(out.accepted)
        self.assertNotIn("cap", out.fields)
        self.assertIn("封顶额（cap）", out.unstated)

    def test_extracted_coupon_set_reproduces_the_engine_result(self):
        """把解析出来的券丢进真实引擎，跑出来的抵扣必须与手工标注一致。"""
        from optimize_deals import single_coupon_discount
        combo = [Item("BURGER-BIGMAC", "巨无霸", 2450),
                 Item("CHICKEN-MCNUGGETS-5", "麦乐鸡（5块）", 1300),
                 Item("FRIES-M", "中薯条", 1200),
                 Item("COLA-M", "中杯可乐", 950),
                 Item("SUNDAE", "圣代", 800)]
        # 只比"文本能推全"的那几张；折扣券因缺封顶/门槛故意排除，见上一条测试
        comparable = {"满30元减5元", "麦乐鸡（5块）立减4元", "买中薯条送中杯可乐",
                      "圣代免费", "积分兑换·巨无霸（500积分）"}
        for ch, row in self._rows():
            if row["name"] not in comparable:
                continue
            with self.subTest(name=row["name"]):
                got = parse_rule(row["name"], menu=INDEX).to_coupon(row["id"])
                want = Coupon.from_dict(row)
                self.assertEqual(single_coupon_discount(combo, got),
                                 single_coupon_discount(combo, want))


class TestParseRows(unittest.TestCase):
    def test_batch_splits_pass_and_reject(self):
        rows = [{"id": "A", "name": "满30元减5元"},
                {"id": "B", "name": "第二杯半价"},
                {"id": "C", "name": "全场8折"}]
        ok, bad = parse_rows(rows, pool=MENU)
        self.assertEqual([r["id"] for r in ok], ["A"])
        self.assertEqual({label for label, _ in bad}, {"B", "C"})

    def test_explicit_fields_win_over_inference(self):
        rows = [{"id": "A", "name": "满30元减5元", "threshold": 50.0, "reduce": 9.0}]
        ok, _ = parse_rows(rows, pool=MENU)
        self.assertEqual(ok[0]["threshold"], 50.0)
        self.assertEqual(ok[0]["reduce"], 9.0)

    def test_records_carry_provenance(self):
        rows = [{"id": "A", "name": "满30元减5元"}]
        ok, _ = parse_rows(rows, pool=MENU)
        self.assertIn("_confidence", ok[0])
        self.assertEqual(ok[0]["_pattern"], "threshold_reduce")
        self.assertIn("stack_group（互斥组）", ok[0]["_unstated"])

    def test_generated_ids_are_deterministic(self):
        rows = [{"name": "满30元减5元"}]
        first, _ = parse_rows(rows, pool=MENU)
        second, _ = parse_rows(rows, pool=MENU)
        self.assertEqual(first[0]["id"], second[0]["id"])
        self.assertTrue(first[0]["id"].startswith("RULE-"))

    def test_non_dict_rows_are_skipped(self):
        ok, bad = parse_rows(["不是字典", 42, None], pool=MENU)  # type: ignore[list-item]
        self.assertEqual((ok, bad), ([], []))

    def test_output_feeds_coupon_from_dict(self):
        ok, _ = parse_rows([{"id": "A", "name": "满30元减5元"}], pool=MENU)
        c = Coupon.from_dict(ok[0])
        self.assertEqual((c.threshold_cents, c.reduce_cents), (3000, 500))


class TestCli(unittest.TestCase):
    def test_single_text(self):
        code, out = run_cli("--text", "满30元减5元", "--menu",
                            os.path.join(DATA, "sample_menu.json"))
        self.assertEqual(code, 0)
        self.assertIn("threshold_reduce", out)
        self.assertIn("通过 1", out)

    def test_rejections_are_listed_with_reasons(self):
        code, out = run_cli("--text", "第二杯半价", "--text", "满30元减5元")
        self.assertEqual(code, 1)          # 退出码 = 未通过条数，可用于 CI
        self.assertIn("未通过明细", out)
        self.assertIn("结构不支持", out)

    def test_json_mode(self):
        code, out = run_cli("--text", "圣代免费", "--json",
                            "--menu", os.path.join(DATA, "sample_menu.json"))
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload[0]["status"], "accepted")
        self.assertEqual(payload[0]["coupon"]["item_code"], "SUNDAE")

    def test_explain_lists_every_evidence_line(self):
        _, out = run_cli("--text", "半价周末·巨无霸 5折", "--menu",
                         os.path.join(DATA, "sample_menu.json"), "--explain")
        self.assertIn("命中模式：discount", out)
        self.assertIn("交叉印证", out)
        self.assertIn("文本未给出", out)

    def test_threshold_flag(self):
        code, _ = run_cli("--text", "全场8折", "--threshold", "0.7")
        self.assertEqual(code, 0)

    def test_batch_from_coupon_file(self):
        code, out = run_cli("--coupons", os.path.join(DATA, "sample_channels.json"),
                            "--menu", os.path.join(DATA, "sample_menu.json"))
        self.assertEqual(code, 0)
        self.assertIn("17 条：通过 17", out)

    def test_batch_without_menu(self):
        code, out = run_cli("--coupons", os.path.join(DATA, "sample_channels.json"))
        self.assertIn("菜单 0 项", out)
        self.assertGreater(code, 0)        # 「半价周末·巨无霸 5折」缺菜单判不了适用范围

    def test_missing_input_is_a_usage_error(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            main([])


class TestExplanation(unittest.TestCase):
    def test_every_score_line_is_traceable(self):
        out = parse_rule("麦乐鸡（5块）立减4元", menu=INDEX)
        for e in out.evidence:
            self.assertTrue(e.kind)
            if e.weight != 0.0:
                self.assertNotEqual(e.note, "", f"{e.kind} 有分数却没有说明")

    def test_evidence_spans_point_into_the_original_text(self):
        text = "饿了么整单 88折（封顶12元）"
        out = parse_rule(text, menu=INDEX)
        for e in out.evidence:
            if e.text and e.span != (0, 0):
                self.assertEqual(text[e.span[0]:e.span[1]], e.text, e.kind)

    def test_to_dict_is_json_serialisable(self):
        payload = [parse_rule(t, menu=INDEX).to_dict() for t in REAL_TEXTS]
        json.dumps(payload, ensure_ascii=False)

    def test_outcome_type_is_stable(self):
        self.assertIsInstance(parse_rule("满30元减5元", menu=INDEX), ParseOutcome)


class TestShippedCouponTextFixture(unittest.TestCase):
    """`data/sample_coupon_text.json` 自带 ``_expected`` —— 数据自己声明该通过几条。

    这个文件既是演示输入，也是**验收标准**：改了解析器却忘了改它，测试会红。
    """

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(DATA, "sample_coupon_text.json"), encoding="utf-8") as fh:
            cls.fixture = json.load(fh)

    def test_declared_expectations_hold(self):
        want = self.fixture["_expected"]
        names = [c["name"] for c in self.fixture["coupons"]]
        self.assertEqual(len(names), 19)
        # 前 14 条可解析
        for name in names[:14]:
            with self.subTest(name=name):
                self.assertTrue(parse_rule(name, menu=INDEX).accepted)
        # 4 条结构不支持
        for name in want["结构不支持"]:
            with self.subTest(name=name):
                self.assertEqual(parse_rule(name, menu=INDEX).status, "refused")
        # 1 条置信度不足
        for name in want["置信度不足"]:
            with self.subTest(name=name):
                self.assertEqual(parse_rule(name, menu=INDEX).status, "ambiguous")

    def test_frequency_note_is_documented_as_passing(self):
        out = parse_rule("买中薯条送中杯可乐 每单限用一张", menu=INDEX)
        self.assertTrue(out.accepted)
        self.assertEqual(out.fields.get("_stack_hint"), "single")

    def test_fixture_is_valid_json_with_a_reason_for_every_rejection(self):
        code, out = run_cli("--coupons", os.path.join(DATA, "sample_coupon_text.json"),
                            "--menu", os.path.join(DATA, "sample_menu.json"))
        self.assertEqual(code, 5)
        for name in self.fixture["_expected"]["结构不支持"]:
            self.assertIn(name, out)
        self.assertIn("结构不支持", out)

    def test_coupon_list_shape_is_accepted(self):
        # 扁平 {"coupons": [...]} 与渠道表 {"channels": [...]} 都要能读
        from rule_parser import _load_coupon_texts
        flat = _load_coupon_texts(os.path.join(DATA, "sample_coupon_text.json"))
        nested = _load_coupon_texts(os.path.join(DATA, "sample_channels.json"))
        self.assertEqual(len(flat), 19)
        self.assertEqual(len(nested), 17)


if __name__ == "__main__":
    unittest.main(verbosity=2)
