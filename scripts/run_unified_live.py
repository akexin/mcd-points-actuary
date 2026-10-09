#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""麦麦积分精算师 v3 · 统一端到端联调（全渠道到手成本最优化）。

这个脚本把前三代能力**叠在一起**跑一遍真实链路：

::

    v1  点餐组合优化（怎么买最省）
    v2  积分资产估值（积分怎么花最值 → 影子价格 ypp）
    v3  渠道 × 券叠加（在哪里买最省，且积分与现金同一刻度）

统一目标函数
------------

    到手成本 = 现金支出 + 积分消耗 × 积分影子价格（元/积分）

其中影子价格由 :mod:`points_engine` 的兑换基准给出，**不是拍脑袋设的常数** ——
它是"这些积分拿去做最优兑换能保证换回多少现金"的实测值。有了它，
"花 500 积分省 24 元"这类决策第一次可以被判定为**亏损**，
并且可以直接算出「打平影子价格」告诉用户这券在什么条件下才值得换。

真实数据来源（全部经 MCP 实查，无硬编码价格）
--------------------------------------------
::

    ① query-nearby-stores     → 门店（默认选营业中、距离最近的一家）
    ② query-meals ×2          → 到店菜单 + 外送菜单（外送需 beCode）
    ③ query-store-coupons     → 门店可用券（到店 / 外送）
       query-my-coupons       → 我的券
       available-coupons      → 可领取券（接口只给名称，列为"未建模"）
    ④ mall-points-products    → 积分商城 → 价值矩阵 → 影子价格
       query-my-account       → 积分账户（到期提醒用）

本地数据（MCP 拿不到，必须由用户提供）
-------------------------------------
::

    --third-party  第三方平台（美团 / 饿了么）的渠道费率与券面
    --points-map   积分商城标的 → 菜单商品 的**人工核验映射**
                   （两套 SKU，官方无对应关系，本项目拒绝模糊推断）

用法
----
    export MCD_MCP_TOKEN=xxxxx
    python scripts/run_unified_live.py --city 南京市 --keyword 新街口
    python scripts/run_unified_live.py --store 1990346 --max-items 3 --budget 40
    python scripts/run_unified_live.py --store 1990346 --items 1100 4810 9900008751
    python scripts/run_unified_live.py --store 1990346 --shadow-ypp 0   # 积分将过期
    python scripts/run_unified_live.py --offline                        # 全离线复现
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import unicodedata
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from channel_planner import (  # noqa: E402
    DISCLAIMER, Channel, Quote, breakeven_ypp, channel_savings, compare_channels,
    optimize_across_channels, point_bundle_coupon,
)
from mcd_mcp import MCDMcpClient, MCPError  # noqa: E402
from optimize_deals import (  # noqa: E402
    Constraints, Coupon, Item, fmt_yuan, single_coupon_discount, to_cents,
)
from points_engine import (  # noqa: E402
    WINDOW_ACTIVE, ValueMatrix, build_value_matrix, fmt_ypp, offer_from_raw,
    window_state,
)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEFAULT_THIRD_PARTY = os.path.join(ROOT, "data", "live_third_party.json")
DEFAULT_POINTS_MAP = os.path.join(ROOT, "data", "sample_points_mapping.json")
OFFLINE_CHANNELS = os.path.join(ROOT, "data", "sample_channels.json")
OFFLINE_MENU = os.path.join(ROOT, "data", "sample_menu.json")
OFFLINE_OFFERS = os.path.join(ROOT, "data", "sample_points_products.json")

MCP_CALLS_NOTE = ("本次联调共 {n} 次 MCP 调用（全部只读）；"
                  "组合枚举、券叠加、积分折算均在本地完成，未占用 MCP 计价配额。")


# --------------------------------------------------------------------------- #
# 终端列宽
# --------------------------------------------------------------------------- #
def dwidth(s: str) -> int:
    """字符串在等宽终端里占多少**列**（汉字/全角 2 列，其余 1 列）。

    为什么不直接用 ``len()``：``f"{'巨无霸':<20}"`` 按**字符数**补空格，
    而汉字占 2 列 —— 于是"巨无霸"只补了 17 个空格，看起来比 20 列短一截，
    整张表的列就会逐行错位。本文件所有表格都改用 :func:`dpad` 按列宽补齐，
    这样输出在真实终端里是**目视对齐**的，截图也就跟着对齐。
    """
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in s)


def dpad(s: str, width: int, align: str = "<") -> str:
    """按显示列宽补齐到 ``width`` 列；超宽则原样返回（不截断，宁可错位也不丢信息）。"""
    gap = width - dwidth(s)
    if gap <= 0:
        return s
    if align == ">":
        return " " * gap + s
    if align == "^":
        left = gap // 2
        return " " * left + s + " " * (gap - left)
    return s + " " * gap


# --------------------------------------------------------------------------- #
# 数据归一化
# --------------------------------------------------------------------------- #
def _load(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


_RE_PRICE_PREFIX = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*元")


def menu_items(menu_data: Dict[str, Any]) -> List[Item]:
    """把 ``query-meals`` 的 data 段归一化为 ``Item`` 列表（价格为字符串，需转分）。"""
    meals = (menu_data or {}).get("meals") or {}
    cat_of: Dict[str, str] = {}
    for cat in (menu_data or {}).get("categories") or []:
        for m in cat.get("meals") or []:
            cat_of.setdefault(str(m.get("code")), str(cat.get("name", "")))
    items: List[Item] = []
    for code, info in meals.items():
        raw = info.get("currentPrice")
        if raw is None:
            raw = info.get("originalPrice")
        try:
            cents = to_cents(raw)
        except (TypeError, ValueError):
            continue
        if cents <= 0:
            continue
        items.append(Item(code=str(code), name=str(info.get("name", code)),
                          price_cents=cents, category=cat_of.get(str(code), "")))
    return items


def map_coupons(raw: Any) -> List[Coupon]:
    """把 MCP 券结构尽力映射为引擎券；语义不明的一律跳过，不做猜测量化。"""
    out: List[Coupon] = []
    if not isinstance(raw, list):
        return out
    for c in raw:
        if not isinstance(c, dict):
            continue
        cid = c.get("couponId") or c.get("id") or c.get("code")
        if not cid:
            continue
        threshold = c.get("threshold") or c.get("conditionAmount") or c.get("limitAmount")
        # 字段名各接口不统一，且必须两者齐全才敢量化 —— 猜错门槛会算出错误结论
        reduce = (c.get("discount") or c.get("reduceAmount") or c.get("amount")
                  or c.get("reduce"))
        try:
            if threshold is not None and reduce is not None:
                out.append(Coupon(id=str(cid), name=str(c.get("name") or c.get("title") or cid),
                                  type="threshold_reduce",
                                  threshold_cents=to_cents(threshold),
                                  reduce_cents=to_cents(reduce)))
        except (TypeError, ValueError):
            continue
    return out


def resolve_coupon_rows(rows: Sequence[Any], by_code: Dict[str, Item],
                        by_name: Dict[str, Item]) -> Tuple[List[Dict[str, Any]], List[str]]:
    """把券表里的 ``item_name`` / ``item_code`` 解析为**真实菜单编码**。

    第三方平台券面是用户手抄的，记的多半是商品名（"半价周末·巨无霸"）而不是菜单编码。
    这里只接受**精确命中**（编码或全名）；命中不了就丢弃并把原因报回来，
    由人工补充 —— 与本项目"宁可漏，不可猜"的取舍一致。
    """
    ok_rows: List[Dict[str, Any]] = []
    dropped: List[str] = []
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        row = dict(raw)
        label = str(row.get("name") or row.get("id") or "?")
        bad: Optional[str] = None

        for key in ("item_code", "buy_code", "get_code"):
            val = row.get(key)
            if val and str(val) not in by_code:
                bad = f"{label}（{key}={val} 不在本店菜单）"
                break
        if bad is None:
            named = [k for k in ("item_name", "buy_name", "get_name") if row.get(k)]
            for key in named:
                hit = by_name.get(str(row[key]))
                if hit is None:
                    bad = f"{label}（{key}={row[key]} 不在本店菜单）"
                    break
                row[key[:-5] + "_code"] = hit.code     # item_name → item_code
                row.pop(key, None)
        if bad is None and row.get("item_names"):
            codes: List[str] = []
            for nm in row["item_names"]:
                hit = by_code.get(str(nm)) or by_name.get(str(nm))
                if hit is None:
                    bad = f"{label}（组合含未知名 {nm}）"
                    break
                codes.append(hit.code)
            if bad is None:
                row["item_codes"] = codes
            row.pop("item_names", None)

        if bad:
            dropped.append(bad)
        else:
            ok_rows.append(row)
    return ok_rows, dropped


def load_channels(path: str, by_code: Dict[str, Item], by_name: Dict[str, Item],
                  skip_codes: Sequence[str] = ()) -> Tuple[List[Channel], List[str]]:
    """读渠道表并解析券里的商品名；``skip_codes`` 用于跳过由 MCP 实时构建的自有渠道。"""
    data = _load(path)
    rows = data.get("channels", data) if isinstance(data, dict) else data
    channels: List[Channel] = []
    dropped: List[str] = []
    for d in rows:
        if str(d.get("code")) in skip_codes:
            continue
        cs, bad = resolve_coupon_rows(d.get("coupons") or [], by_code, by_name)
        dropped.extend(bad)
        d = dict(d)
        d["coupons"] = cs
        channels.append(Channel.from_dict(d))
    return channels, dropped


# --------------------------------------------------------------------------- #
# MCP 取数（每步都可降级，绝不因单点失败中断整条链路）
# --------------------------------------------------------------------------- #
def fetch_store(client: MCDMcpClient, args: argparse.Namespace) -> Dict[str, Any]:
    if args.store:
        return {"storeCode": args.store, "storeName": f"(指定) {args.store}"}
    data = client.call_structured("query-nearby-stores", {
        "beType": 1, "searchType": 2, "city": args.city, "keyword": args.keyword,
    }).get("data") or []
    if not data:
        raise MCPError(f"未找到门店：--city {args.city} --keyword {args.keyword}")
    rows = [s for s in data if s.get("businessStatus")] or data
    rows.sort(key=lambda s: s.get("distance") or 10 ** 9)
    return rows[0]


def fetch_menu(client: MCDMcpClient, store_code: str, order_type: int, be_type: int,
               be_code: Optional[str] = None) -> List[Item]:
    payload: Dict[str, Any] = {"storeCode": store_code, "orderType": order_type,
                               "beType": be_type}
    if be_code:
        payload["beCode"] = be_code
    return menu_items(client.call_structured("query-meals", payload).get("data") or {})


def delivery_be_code(client: MCDMcpClient) -> Optional[str]:
    """拿麦乐送所需的 beCode；账号没有配送地址时返回 None —— 这是正常状态，不是错误。"""
    try:
        addrs = client.call_structured("delivery-query-addresses", {}).get("data") or {}
        rows = addrs.get("addresses") or []
        if not rows:
            return None
        aid = rows[0].get("addressId")
        if not aid:
            return None
        stores = client.call_structured("delivery-query-stores",
                                        {"addressId": aid, "beType": 2}).get("data") or []
        return str(stores[0].get("beCode")) if stores and stores[0].get("beCode") else None
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------- #
# 积分 → 影子价格 + 积分券
# --------------------------------------------------------------------------- #
def build_shadow(offers_raw: Sequence[Dict[str, Any]]) -> Tuple[List[Any], ValueMatrix]:
    offers = [offer_from_raw(r) for r in offers_raw]
    return offers, build_value_matrix(offers)


def points_coupons(offers_raw: Sequence[Dict[str, Any]], mapping_path: str,
                   by_code: Dict[str, Item], by_name: Dict[str, Item]
                   ) -> Tuple[List[Coupon], List[Dict[str, Any]], List[str]]:
    """由「人工核验映射 + 精确同名」两条路径生成带积分成本的积分券。

    每条明细里的抵扣额都用 :func:`single_coupon_discount` 现算 ——
    券语义只有一处实现，报告数字与求解器用的绝对是同一套算法。
    """
    by_offer = {str(o.get("spuName") or ""): o for o in offers_raw}
    seen: set = set()
    coupons: List[Coupon] = []
    details: List[Dict[str, Any]] = []
    notes: List[str] = []

    def _add(offer_name: str, item_codes: Sequence[str], fixed_cents: int, source: str) -> None:
        if not offer_name or offer_name in seen:
            return
        live = by_offer.get(offer_name)
        if live is None:
            notes.append(f"「{offer_name}」已不在当前积分商城，跳过")
            seen.add(offer_name)
            return
        point = int(str(live.get("point") or 0) or 0)
        if point <= 0:
            notes.append(f"「{offer_name}」当前不消耗积分，跳过")
            seen.add(offer_name)
            return
        combo = [by_code[c] for c in item_codes if c in by_code]
        if len(combo) != len(set(item_codes)):
            # 不记入 seen：这条映射不适用于本店菜单，但下一条更独立的匹配路径仍可尝试
            notes.append(f"「{offer_name}」映射的商品不在本店菜单，跳过")
            return
        spu = live.get("spuId") or ""
        coupon = point_bundle_coupon(f"PTS-{spu or offer_name}", offer_name,
                                     item_codes, fixed_cents, point)
        discount = single_coupon_discount(combo, coupon)
        if discount <= 0:
            notes.append(f"「{offer_name}」打包价不低于菜单单点价，无优惠，跳过")
            seen.add(offer_name)
            return
        seen.add(offer_name)
        coupons.append(coupon)
        details.append({
            "name": offer_name, "point": point, "fixed_cents": fixed_cents,
            "item_codes": list(item_codes),
            "items": [i.name for i in combo],
            "menu_cents": sum(i.price_cents for i in combo),
            "discount_cents": discount, "source": source,
            "spu_id": spu, "down_time": live.get("downTime"),
            "breakeven_ypp": round(breakeven_ypp(discount, point), 6),
        })

    # ① 人工核验映射（首选）：组合成分已被逐条确认
    if mapping_path and os.path.exists(mapping_path):
        for m in _load(mapping_path).get("mappings", []):
            _add(str(m.get("offer") or ""), [str(c) for c in m.get("item_codes") or []],
                 to_cents(m.get("fixed", 0)), "人工核验映射")

    # ② 精确同名兜底：去价格前缀后与菜单商品名**完全一致**才接受
    for o in offers_raw:
        if int(o.get("shopId") or 0) != 2:
            continue
        if int(str(o.get("point") or 0) or 0) <= 0:
            continue
        nm = str(o.get("spuName") or "")
        m = _RE_PRICE_PREFIX.match(nm)
        if not m:
            continue
        hit = by_name.get(_RE_PRICE_PREFIX.sub("", nm).strip())
        if hit is not None:
            _add(nm, [hit.code], to_cents(float(m.group(1))), "精确同名")

    return coupons, details, notes


# --------------------------------------------------------------------------- #
# 组装渠道
# --------------------------------------------------------------------------- #
def own_channels(own_pickup: List[Coupon], own_delivery: List[Coupon],
                 points: List[Coupon], delivery_factor: float,
                 delivery_available: bool) -> List[Channel]:
    """麦当劳自有两条渠道：到店自取 / 麦乐送。费率与券均来自实查。"""
    pickup = Channel(
        code="mcd-pickup", name="麦当劳 · 到店自取", source="mcp",
        delivery_fee_cents=0, packaging_fee_cents=0, min_order_cents=0,
        coupons=tuple(own_pickup + points), allow_stack=False, max_stack=1,
        notes="官方渠道；无配送费与打包费；单笔仅 1 张券 —— 积分券在同一池内与现金券竞争",
    )
    delivery = Channel(
        code="mcd-delivery", name="麦当劳 · 麦乐送", source="mcp",
        delivery_fee_cents=900, packaging_fee_cents=0, min_order_cents=0,
        free_delivery_threshold_cents=3900, price_factor=delivery_factor,
        coupons=tuple(own_delivery), allow_stack=False, max_stack=1,
        notes=("官方外送；配送费 9 元、满 39 元免；"
               + ("菜单价取自外送菜单实查" if delivery_available
                  else "外送菜单不可得（账号无配送地址），按到店价估算，待复核")),
    )
    return [pickup, delivery]


def candidate_pool(items: Sequence[Item], channels: Sequence[Channel],
                   must_include: Sequence[str] = ()) -> List[Item]:
    """候选池 = 所有券引用到的商品 ∪ 必点商品。

    渠道间的价差**几乎全部来自券**；把池子限定在"券能生效的商品"上，
    既保证每张券都有机会发挥作用，又把组合数从十几万压到几十个，
    在有限配额与算力下给出可复核的结论。需要全菜单视角时用 ``--pool menu``。
    """
    codes: set = set(must_include)
    for ch in channels:
        for c in ch.coupons:
            codes.update(c.item_codes)
            for one in (c.item_code, c.buy_code, c.get_code):
                if one:
                    codes.add(one)
    pool = [i for i in items if i.code in codes]
    if len(pool) < 2:
        pool = sorted(items, key=lambda i: i.price_cents)[:12]
    return pool


# --------------------------------------------------------------------------- #
# 输出
# --------------------------------------------------------------------------- #
def print_matrix(m: ValueMatrix) -> None:
    print(f"  可定价 {len(m.ranked)} 项（周期内 {len(m.active)} · 已过周期 {len(m.stale)}"
          f" · 无法定价 {len(m.unpriced)}）")
    print(f"  周期内效率差 {m.active_spread:.1f} 倍 —— 同样的积分，选错标的就差这么多")
    print("  ── 周期内最优兑换（这是积分的「现实机会价值」）──")
    for o in (m.active or m.ranked)[:5]:
        print(f"    {fmt_ypp(o.yuan_per_point):>14}  {o.point:>5} 积分  {o.name}")
    if m.traps:
        print(f"  ── 积分黑洞（低于中位效率一半，共 {len(m.traps)} 项）例：")
        for o in m.traps[:3]:
            mark = "" if window_state(o, m.now) == WINDOW_ACTIVE else "（已过周期·待复核）"
            print(f"    {o.name}{mark}：{o.point} 积分只换回 {fmt_yuan(o.face_value_cents)}")


def print_points_table(details: Sequence[Dict[str, Any]], shadow_ypp: float) -> None:
    if not details:
        print("  无可用积分餐品券（映射为空，或映射的标的已不在当前商城）")
        return
    print(dpad("积分餐品券", 28) + dpad("积分", 8, ">") + dpad("菜单价", 10, ">")
          + dpad("打包价", 10, ">") + dpad("抵扣", 9, ">")
          + dpad("打平ypp", 10, ">") + dpad("判定", 13, ">"))
    print("-" * 88)
    for d in sorted(details, key=lambda x: -x["discount_cents"]):
        be = d["breakeven_ypp"]
        if shadow_ypp <= 0:
            verdict = "划算(积分免费)"
        else:
            verdict = "划算" if be >= shadow_ypp else "不划算"
        print(dpad(d["name"], 28) + dpad(str(d["point"]), 8, ">")
              + dpad(fmt_yuan(d["menu_cents"]), 10, ">")
              + dpad(fmt_yuan(d["fixed_cents"]), 10, ">")
              + dpad(fmt_yuan(d["discount_cents"]), 9, ">")
              + dpad(f"{be:.4f}", 10, ">")
              + dpad(verdict, 13, ">"))
    if shadow_ypp > 0:
        print(f"\n  判定口径：**打平 ypp = 抵扣额 ÷ 积分消耗**。只要它低于影子价格 "
              f"{fmt_ypp(shadow_ypp)}，")
        print("  就说明「拿这些积分换这张券」不如「把积分用在最优兑换上」—— 应当放弃，")
        print("  而不是因为「券看起来抵得多」就用掉。这正是 v2 的积分定价接入 v3 的意义。")
    else:
        print("\n  判定口径：影子价格取 0，等于把这些积分视为**沉没成本**（即将过期、反正作废）。")
        print("  此时只有「抵多少」才重要 —— 所以上表全部划算。两种口径的差异本身，")
        print("  就是「积分到期前该不该突击兑换」这个问题的量化答案。")


def print_quotes(combo: Sequence[Item], quotes: Sequence[Quote]) -> None:
    print(f"\n购物车：{' + '.join(i.name for i in combo)}")
    print(dpad("渠道", 22) + dpad("券后", 10, ">") + dpad("配送", 8, ">")
          + dpad("打包", 8, ">") + dpad("现金", 10, ">") + dpad("积分", 7, ">")
          + dpad("积分成本", 10, ">") + dpad("到手成本", 11, ">") + "  券")
    print("-" * 110)
    for q in quotes:
        if not q.feasible:
            print(dpad(q.channel.name, 22) + dpad("—", 10, ">") + dpad("—", 8, ">")
                  + dpad("—", 8, ">") + dpad("—", 10, ">") + dpad("—", 7, ">")
                  + dpad("—", 10, ">") + dpad("不可行", 11, ">") + f"  {q.reason}")
            continue
        ct = " + ".join(c.name for c in q.coupons) or "无"
        print(dpad(q.channel.name, 22)
              + dpad(fmt_yuan(q.goods_cents - q.discount_cents), 10, ">")
              + dpad(fmt_yuan(q.delivery_cents), 8, ">")
              + dpad(fmt_yuan(q.packaging_cents), 8, ">")
              + dpad(fmt_yuan(q.cash_cents), 10, ">")
              + dpad(str(q.point_cost), 7, ">")
              + dpad(fmt_yuan(q.point_value_cents), 10, ">")
              + dpad(fmt_yuan(q.landed_cents), 11, ">") + f"  {ct}")


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="v3 · 全渠道到手成本统一联调")
    ap.add_argument("--store", default=None, help="门店编码；不传则按 --city/--keyword 定位")
    ap.add_argument("--city", default="南京市")
    ap.add_argument("--keyword", default="新街口")
    ap.add_argument("--third-party", default=DEFAULT_THIRD_PARTY, help="第三方平台渠道与券")
    ap.add_argument("--points-map", default=DEFAULT_POINTS_MAP, help="积分券人工核验映射")
    ap.add_argument("--shadow-ypp", type=float, default=None,
                    help="积分影子价格（元/积分）；缺省用积分商城实测基准。"
                         "积分即将过期时可传 0（视为沉没成本）")
    ap.add_argument("--objective", choices=("landed", "cash"), default="landed")
    ap.add_argument("--items", nargs="+", default=None, help="指定购物车（商品编码）→ 对比模式")
    ap.add_argument("--min-items", type=int, default=1)
    ap.add_argument("--max-items", type=int, default=3)
    ap.add_argument("--budget", default=None, help="到手成本上限（元）")
    ap.add_argument("--top", type=int, default=3)
    ap.add_argument("--pool", choices=("coupon", "menu"), default="coupon",
                    help="候选池：coupon=仅券涉及商品（默认，快）；menu=全菜单")
    ap.add_argument("--offline", action="store_true", help="全离线复现（不连 MCP）")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    report: Dict[str, Any] = {"source": "offline" if args.offline else "mcp", "calls": []}

    pickup_items: List[Item] = []
    delivery_items: List[Item] = []
    own_pickup: List[Coupon] = []
    own_delivery: List[Coupon] = []
    offers_raw: List[Dict[str, Any]] = []
    account: Dict[str, Any] = {}
    claimable: Any = None          # (原始名称列表, 去重后名称列表)
    store: Dict[str, Any] = {}
    delivery_available = False

    # ── 取数 ──────────────────────────────────────────────────────────── #
    if args.offline:
        print("[离线] 用 data/ 下的脱敏样例复现全流程（不连 MCP）\n")
        pickup_items = [Item.from_dict(d) for d in _load(OFFLINE_MENU)]
        tmp = _load(OFFLINE_OFFERS)
        offers_raw = (tmp.get("data") or tmp.get("offers") or []) if isinstance(tmp, dict) else tmp
        store = {"storeCode": "OFFLINE", "storeName": "离线样例门店"}
        own_pickup = [Coupon.from_dict(c)
                      for c in _load(OFFLINE_CHANNELS)["channels"][0]["coupons"]]
    else:
        client = MCDMcpClient()
        info = client.initialize()
        srv = info.get("result", {}).get("serverInfo", {})
        print(f"[1/6] 已连接 {srv.get('name', 'MCP')} v{srv.get('version', '?')}")

        store = fetch_store(client, args)
        print(f"[2/6] 门店：{store.get('storeName')}（storeCode={store['storeCode']}"
              f"{'，距离 ' + str(store.get('distance')) + 'm' if store.get('distance') else ''}）")
        report["calls"].append("query-nearby-stores")

        pickup_items = fetch_menu(client, store["storeCode"], 1, 1)
        report["calls"].append("query-meals(orderType=1)")
        print(f"      到店菜单 {len(pickup_items)} 项")

        be_code = delivery_be_code(client)
        if be_code:
            try:
                delivery_items = fetch_menu(client, store["storeCode"], 2, 2, be_code)
                report["calls"].append("query-meals(orderType=2)")
                delivery_available = bool(delivery_items)
                print(f"      外送菜单 {len(delivery_items)} 项（beCode={be_code}）")
            except Exception as exc:  # noqa: BLE001
                print(f"      外送菜单读取失败：{exc}")
        else:
            print("      外送菜单不可得（账号无配送地址 → 无 beCode）；"
                  "麦乐送按到店价 + 配送费估算")

        print("[3/6] 自有券（来自 MCP 实查）")
        for tool, payload, bucket in (
            ("query-store-coupons",
             {"storeCode": store["storeCode"], "orderType": 1, "beType": 1}, own_pickup),
            ("query-my-coupons", {}, own_pickup),
        ):
            try:
                data = client.call_structured(tool, payload).get("data")
                rows = (data.get("coupons") if isinstance(data, dict) else data) or []
                got = map_coupons(rows)
                bucket.extend(got)
                print(f"      {tool}：原始 {len(rows)} 条 → 可量化 {len(got)} 张")
            except MCPError:
                print(f"      {tool}：当前无可用券（接口仅返回说明文字，属正常）")
            except Exception as exc:  # noqa: BLE001
                print(f"      {tool} 跳过（{exc}）")
            report["calls"].append(tool)
        try:
            rows = client.call_structured("available-coupons", {}).get("data") or []
            names = [str(r.get("couponName")) for r in rows if isinstance(r, dict)]
            # 接口对同一张券会按不同活动实例重复返回；按名称去重后再展示，
            # 但**同时保留原始条数**，避免把"去重"伪装成"只有这么多"
            seen_names: Dict[str, None] = {}
            for n in names:
                seen_names.setdefault(n, None)
            claimable = (names, list(seen_names))
            report["calls"].append("available-coupons")
        except Exception:  # noqa: BLE001
            pass

        print("[4/6] 积分资产与积分商城")
        try:
            account = client.call_structured("query-my-account", {}).get("data") or {}
            report["calls"].append("query-my-account")
        except Exception:  # noqa: BLE001
            account = {}
        try:
            offers_raw = client.call_structured("mall-points-products", {}).get("data") or []
            report["calls"].append("mall-points-products")
        except Exception as exc:  # noqa: BLE001
            print(f"      积分商城读取失败（{exc}），积分券将不可用")

    # ── 影子价格 ──────────────────────────────────────────────────────── #
    _, matrix = build_shadow(offers_raw)
    measured_ypp = matrix.benchmark_ypp or matrix.median_ypp
    shadow_ypp = args.shadow_ypp if args.shadow_ypp is not None else measured_ypp

    print()
    print("=" * 74)
    print("  ① 积分定价：先给「积分」标一个价，才谈得上跟现金比")
    print("=" * 74)
    if account:
        print(f"  可用 {account.get('availablePoint', 0)} · 累计 {account.get('accumulativePoint', 0)}"
              f" · 本月将过期 {account.get('currentMouthExpirePoint', 0)}"
              f" · 下月将过期 {account.get('nextMouthExpirePoint', 0)}")
    print_matrix(matrix)
    if args.shadow_ypp is None:
        print(f"\n  ➜ 影子价格 = 实测基准 {fmt_ypp(shadow_ypp)}（取周期内最优兑换效率）")
    else:
        print(f"\n  ➜ 影子价格由 --shadow-ypp 指定为 {fmt_ypp(shadow_ypp)}"
              f"（实测基准 {fmt_ypp(measured_ypp)}）")

    # ── 积分券 ────────────────────────────────────────────────────────── #
    by_code = {i.code: i for i in pickup_items}
    by_name: Dict[str, Item] = {}
    for i in pickup_items:
        by_name.setdefault(i.name, i)

    points_map = args.points_map
    if args.offline and points_map == DEFAULT_POINTS_MAP:
        # 映射是**针对特定门店菜单**人工核验的，样例菜单编码不同，套用没有意义
        print("\n  注：离线样例菜单与实查菜单编码不同，live 版积分券映射不适用（已跳过）；"
              "\n      样例渠道里自带的积分券（PTS-*）仍会参与竞争。")
        points_map = ""
    pts_coupons, pts_details, pts_notes = points_coupons(
        offers_raw, points_map, by_code, by_name)

    print()
    print("=" * 74)
    print("  ② 积分餐品券：把「积分」变成一张能与现金券同台竞争的券")
    print("=" * 74)
    print(f"  生成 {len(pts_coupons)} 张带积分成本的券"
          f"（映射表 + 精确同名两条路径；后者要求在名称上完全一致）")
    for n in pts_notes:
        print(f"    ! {n}")
    print()
    print_points_table(pts_details, shadow_ypp)

    delivery_factor = 1.0
    if delivery_available and delivery_items:
        dic = {i.code: i.price_cents for i in delivery_items}
        ratios = sorted(dic[i.code] / i.price_cents
                        for i in pickup_items if i.code in dic and i.price_cents > 0)
        if ratios:
            delivery_factor = round(ratios[len(ratios) // 2], 4)
            print(f"\n  外送/到店菜单价中位比 {delivery_factor:.4f}（{len(ratios)} 项可比）")

    channels = own_channels(own_pickup, own_delivery, pts_coupons,
                            delivery_factor, delivery_available)

    tp_path = args.third_party
    if args.offline and tp_path == DEFAULT_THIRD_PARTY:
        tp_path = OFFLINE_CHANNELS
    if os.path.exists(tp_path):
        tp_channels, tp_dropped = load_channels(
            tp_path, by_code, by_name, skip_codes=("mcd-pickup", "mcd-delivery"))
    else:
        tp_channels, tp_dropped = [], [f"第三方渠道表不存在：{tp_path}"]
    channels += tp_channels

    print()
    print("=" * 74)
    print("  ③ 渠道与券池（自有渠道来自 MCP 实查，第三方由用户提供）")
    print("=" * 74)
    print(dpad("渠道", 22) + dpad("来源", 20) + dpad("配送", 8, ">")
          + dpad("打包", 8, ">") + dpad("起送", 8, ">") + dpad("叠加", 11, ">")
          + dpad("券数", 7, ">"))
    print("-" * 84)
    for ch in channels:
        src = "第三方(用户提供)" if ch.is_external else "麦当劳官方(MCP)"
        stk = f"最多 {ch.max_stack} 张" if ch.allow_stack else "单张"
        print(dpad(ch.name, 22) + dpad(src, 20)
              + dpad(fmt_yuan(ch.delivery_fee_cents), 8, ">")
              + dpad(fmt_yuan(ch.packaging_fee_cents), 8, ">")
              + dpad(fmt_yuan(ch.min_order_cents), 8, ">")
              + dpad(stk, 11, ">") + dpad(str(len(ch.coupons)), 7, ">"))
    for name in tp_dropped:
        print(f"  ! 未采用：{name}")
    if claimable:
        raw_names, uniq = claimable
        print(f"\n  可领取 {len(raw_names)} 条（去重后 {len(uniq)} 个名称）—— "
              f"接口只给名称不给面额，**无法量化**，故不参与计算：")
        print(f"    {'、'.join(uniq)}")

    # ── 求解 ──────────────────────────────────────────────────────────── #
    print()
    print("=" * 74)
    print("  ④ 全网最低到手价")
    print("=" * 74)

    if args.items:
        combo = [by_code[c] for c in args.items if c in by_code]
        missing = [c for c in args.items if c not in by_code]
        if missing:
            print(f"  菜单中找不到：{missing}")
        if not combo:
            print("  购物车为空，无法报价。")
            return 1
        quotes = compare_channels(combo, channels, shadow_ypp=shadow_ypp)
        print_quotes(combo, quotes)
        ok = [q for q in quotes if q.feasible]
        if len(ok) > 1:
            print(f"\n  选对渠道比最贵的省 "
                  f"{fmt_yuan(ok[-1].landed_cents - ok[0].landed_cents)}")
        report["quotes"] = [q.to_dict() for q in quotes]
    else:
        pool = (pickup_items if args.pool == "menu"
                else candidate_pool(pickup_items, channels))
        print(f"  候选池 {len(pool)} 项（{'全菜单' if args.pool == 'menu' else '券涉及商品'}）："
              f"{'、'.join(i.name for i in pool[:14])}{' …' if len(pool) > 14 else ''}")
        cons = Constraints(
            budget_cents=to_cents(args.budget) if args.budget is not None else None,
            min_items=args.min_items, max_items=args.max_items,
        )
        plans, pruned = optimize_across_channels(
            pool, channels, cons, top=args.top, shadow_ypp=shadow_ypp,
            objective=args.objective)
        if not plans:
            print("  无满足约束的方案，请放宽 --budget / --max-items。")
            return 1
        print(f"  枚举口径：{'启发式剪枝' if pruned else '全量枚举'}"
              f" · 目标 = "
              f"{'到手成本（现金 + 积分机会成本）' if args.objective == 'landed' else '现金'}")
        for i, p in enumerate(plans, 1):
            extra = f" + {p.best.point_cost} 积分" if p.best.point_cost else ""
            print(f"\n  {i}. 到手 {fmt_yuan(p.landed_cents)}"
                  f"（现金 {fmt_yuan(p.cash_cents)}{extra}）@ {p.best.channel.name}")
            print(f"     {' + '.join(it.name for it in p.combo)}")
            if p.best.coupons:
                print(f"     券：{' + '.join(c.name for c in p.best.coupons)}")
        best = plans[0]
        print()
        print_quotes(best.combo, best.quotes)
        s = channel_savings(plans, channels)
        if s:
            print(f"\n  渠道差价：最优 {s['best_channel']} {fmt_yuan(s['best_landed'])}"
                  f" vs 最贵 {s['worst_channel']} {fmt_yuan(s['worst_landed'])}"
                  f" → 选对渠道省 {fmt_yuan(s['channel_gap'])}")
        report["plans"] = [p.to_dict() for p in plans]
        report["savings"] = s

    report["shadow_yuan_per_point"] = round(shadow_ypp, 6)
    report["measured_yuan_per_point"] = round(measured_ypp, 6)
    report["points_coupons"] = pts_details

    print(f"\n{MCP_CALLS_NOTE.format(n=len(report['calls']))}")
    print(DISCLAIMER)
    if args.json:
        print("\n" + json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
