#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""麦麦积分精算师 —— 端到端联调脚本（真实 MCP）。

流程
----
::

    Stage 1（本地，0 次网络）
      解析积分商城标的名称，算出「元/积分」价值矩阵

    Stage 2（MCP，按需最小化调用）
      ① query-my-account      → 积分资产（可用 / 累计 / 本月与下月将过期）
      ② mall-points-products  → 兑换标的池
      ③ query-lottery-info    → 抽奖活动规则 + 奖品池
      ④ query-meals（仅 1 次） → 一次性取回菜单，用于给「N折券」类奖品补齐原价

设计要点
--------
**为什么只调 1 次 query-meals。** 奖品池里有多张折扣券（麦辣三件套5折券、板烧三件套5折券…），
直觉做法是每张券查一次原价 —— 但菜单接口一次就能返回全量商品，因此改为
「一次拉全量 + 本地做名称模糊匹配」。这与整个项目"用本地算力换 MCP 调用配额"的取舍一致。

**Token 只从环境变量读取**，绝不写入代码或提交仓库。

用法
----
    export MCD_MCP_TOKEN=xxxxx
    python scripts/run_points_live.py --city 南京市 --keyword 新街口
    python scripts/run_points_live.py --store 1990346 --expiring 800 --points 1500
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcd_mcp import MCDMcpClient  # noqa: E402
from points_engine import (  # noqa: E402
    DISCLAIMER, Prize, RedemptionOffer, analyze_lottery, build_value_matrix,
    fmt_yuan, fmt_ypp, offer_from_raw, opportunity_cost_cents, plan_bundles,
    plan_expiry, prize_from_raw, window_state, WINDOW_ACTIVE, WINDOW_STALE,
)


# --------------------------------------------------------------------------- #
# Stage 2 · 菜单原价匹配
# --------------------------------------------------------------------------- #
def _chars(s: str) -> set:
    return {c for c in s if "\u4e00" <= c <= "\u9fff"}


#: 线下活动类商品：名称里出现这些词说明它卖的是"场次"而非"餐品"，
#: 不能用菜单里同名餐饮单品的价格去给它定价（实测中「麦咖啡™奶铁品鉴会」
#: 曾被错配到饮品「麦咖啡™奶铁」，把活动价虚高成一瓶饮料的钱）。
_ACTIVITY_KEYWORDS = ("派对", "品鉴会", "体验营", "读书会", "生日", "营", "活动", "课")


def match_menu_price(name: str, meals: Dict[str, Dict[str, Any]],
                     min_score: float = 0.85) -> Optional[Tuple[str, int]]:
    """把券名模糊匹配到菜单单品，返回 (菜单名, 原价分)。

    三重护栏（缺一不可，都是实测中被误配打出来的）：
      1. **名称重合度 ≥ min_score**（分母取券名，短名不会因长菜单名占便宜）。
         实测有效匹配全部是 100% 重合（"麦辣三件套" ↔ "麦辣鸡腿汉堡三件套"），
         因此阈值可以定得很高；0.6 会把"小食四件套"错配到"BFF四宫格小食盘"。
      2. **券名首字必须出现在候选名中**（品类词头，如"麦/板/双"）。
      3. **活动类商品一律跳过** —— 卖的是场次，不是餐品。
    """
    if any(kw in name for kw in _ACTIVITY_KEYWORDS):
        return None

    base = name
    for suffix in ("券", "折", "元"):
        base = base.replace(suffix, "")
    base = "".join(ch for ch in base if not ch.isdigit() and ch != ".")
    base_chars = _chars(base)
    if not base_chars:
        return None
    head = base[0] if base else ""

    best: Optional[Tuple[str, int]] = None
    best_score = 0.0
    for meal in meals.values():
        mname = str(meal.get("name") or "")
        price = meal.get("currentPrice")
        if not mname or price is None:
            continue
        try:
            cents = int(round(float(price) * 100))
        except (TypeError, ValueError):
            continue
        if cents <= 0:
            continue
        mchars = _chars(mname)
        if head and head not in mchars:
            continue
        score = len(base_chars & mchars) / len(base_chars)
        if score > best_score:
            best_score, best = score, (mname, cents)
    return best if best_score >= min_score else None


def enrich_reference_prices(
    prizes: List[Prize],
    offers: List[RedemptionOffer],
    meals: Dict[str, Dict[str, Any]],
) -> Dict[str, int]:
    """为无法定价的奖品与标的补齐原价（原地更新 face_value_cents）。"""
    refs: Dict[str, int] = {}
    for p in prizes:
        if p.face_value_cents > 0 or p.kind == "points":
            continue
        hit = match_menu_price(p.name, meals)
        if hit:
            refs[p.name] = hit[1]
            print(f"    · 奖品「{p.name}」↔ 菜单「{hit[0]}」原价 {fmt_yuan(hit[1])}")
    for o in offers:
        if o.face_value_cents > 0:
            continue
        hit = match_menu_price(o.name, meals)
        if hit:
            refs[o.name] = hit[1]
            print(f"    · 标的「{o.name}」↔ 菜单「{hit[0]}」原价 {fmt_yuan(hit[1])}")
    return refs


def rebuild(items, refs: Dict[str, int], factory):
    """按补齐后的原价重建对象（dataclass 为 frozen，必须重建）。"""
    return [factory(item, refs) for item in items]


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="麦麦积分精算师 · 端到端联调")
    ap.add_argument("--store", default=None, help="门店编码；不传则用 --city/--keyword 自动定位")
    ap.add_argument("--city", default="南京市")
    ap.add_argument("--keyword", default="新街口")
    ap.add_argument("--order-type", type=int, default=1, help="1=到店 2=外送")
    ap.add_argument("--be-type", type=int, default=1)
    ap.add_argument("--points", type=int, default=None, help="模拟积分预算（覆盖账户可用积分）")
    ap.add_argument("--expiring", type=int, default=None, help="模拟将过期积分（覆盖账户字段）")
    ap.add_argument("--top", type=int, default=3)
    args = ap.parse_args(argv)

    client = MCDMcpClient()
    info = client.initialize()
    server = info.get("result", {}).get("serverInfo", {})
    print(f"[连接] {server.get('name', 'MCP')} v{server.get('version', '?')}\n")

    # ── Stage 2-① 积分资产 ────────────────────────────────────────────── #
    print("[1/4] 读取积分账户（query-my-account）")
    try:
        account = client.call_json("query-my-account", {}).get("data", {}) or {}
    except Exception as exc:  # noqa: BLE001
        print(f"    读取失败：{exc}")
        account = {}
    available = int(account.get("availablePoint") or 0)
    expiring = int(account.get("currentMouthExpirePoint") or 0)
    next_expire = int(account.get("nextMouthExpirePoint") or 0)
    print(f"    可用 {available} · 累计 {account.get('accumulativePoint', 0)}"
          f" · 已用 {account.get('usedPoint', 0)}"
          f" · 本月将过期 {expiring} · 下月将过期 {next_expire}\n")

    # ── Stage 2-② 兑换标的 ────────────────────────────────────────────── #
    print("[2/4] 读取积分商城（mall-points-products）")
    raw_offers = client.call_json("mall-points-products", {}).get("data", []) or []
    offers = [offer_from_raw(r) for r in raw_offers]
    print(f"    标的 {len(offers)} 项，其中餐品券 "
          f"{sum(1 for o in offers if o.point > 0 and o.shop_id == 2)} 项\n")

    # ── Stage 2-③ 抽奖活动 ────────────────────────────────────────────── #
    print("[3/4] 读取抽奖活动（query-lottery-info）")
    lottery_raw = client.call_json("query-lottery-info", {}).get("data", {}) or {}
    draw_point = int(str(lottery_raw.get("drawPoint") or 0) or 0)
    raw_prizes = lottery_raw.get("prizes", []) or []
    prizes = [prize_from_raw(p) for p in raw_prizes]
    print(f"    {lottery_raw.get('activityName', '抽奖')}"
          f"（{lottery_raw.get('activityStatusText', '?')}）· 单次 {draw_point} 积分"
          f" · 奖品 {len(prizes)} 项\n")

    # ── Stage 2-④ 补原价（仅 1 次菜单调用） ───────────────────────────── #
    store_code = args.store
    if args.order_type == 1 and not store_code:
        print(f"[4/4] 定位门店（query-nearby-stores）")
        try:
            stores = client.call_json("query-nearby-stores", {
                "beType": args.be_type, "searchType": 2,
                "city": args.city, "keyword": args.keyword,
            }).get("data", []) or []
            if stores:
                store_code = str(stores[0].get("storeCode") or "")
                print(f"    {stores[0].get('storeName')}（{store_code}）")
        except Exception as exc:  # noqa: BLE001
            print(f"    定位失败：{exc}")

    if store_code:
        print(f"[4/4] 一次取回全量菜单，为折扣券本地匹配原价（query-meals）")
        try:
            menu = client.call_json("query-meals", {
                "storeCode": store_code, "orderType": args.order_type, "beType": args.be_type,
            }).get("data", {}) or {}
            meals = menu.get("meals", {}) or {}
            print(f"    菜单 {len(meals)} 项")
            refs = enrich_reference_prices(prizes, offers, meals)
            if refs:
                # frozen dataclass 需重建
                prizes = [prize_from_raw(
                    {"name": p.name, "typeText": "积分奖" if p.kind == "points" else "优惠券"},
                    refs) for p in prizes]
                offers = [offer_from_raw(
                    {"spuName": o.name, "spuId": o.spu_id, "point": o.point,
                     "shopId": o.shop_id, "catName": o.category, "status": o.status,
                     "upTime": o.up_time, "downTime": o.down_time}, refs) for o in offers]
            print()
        except Exception as exc:  # noqa: BLE001
            print(f"    菜单读取失败，折扣券将保持未定价：{exc}\n")
    else:
        print("[4/4] 未提供门店，跳过原价补齐（折扣券将标记为未定价）\n")

    # ── 分析 ─────────────────────────────────────────────────────────── #
    m = build_value_matrix(offers)

    print("=" * 74)
    print("  积分资产诊断报告")
    print("=" * 74)

    print("\n【积分资产】")
    print(f"  可用 {available} · 本月将过期 {expiring} · 下月将过期 {next_expire}")
    if expiring > 0:
        print(f"  ⚠ 本月有 {expiring} 积分即将归零，按基准 {fmt_ypp(m.benchmark_ypp)} "
              f"折算价值 {fmt_yuan(int(expiring * m.benchmark_ypp * 100))}")

    print(f"\n【兑换价值矩阵】可定价 {len(m.ranked)} 项"
          f"（周期内 {len(m.active)} · 已过周期 {len(m.stale)} · 无法定价 {len(m.unpriced)}）")
    print(f"  中位效率 {fmt_ypp(m.median_ypp)}")
    print(f"  效率差：全库 {m.spread:.1f} 倍 | 周期内 {m.active_spread:.1f} 倍")
    print("\n  ── 周期内性价比排行 ──")
    for i, o in enumerate(m.active or m.ranked, 1):
        print(f"  {i:>2}. {fmt_ypp(o.yuan_per_point):>14}  {o.point:>5} 积分  {o.name}")

    if m.traps:
        print("\n【积分黑洞预警】低于中位效率一半的标的")
        for o in m.traps:
            mark = "" if window_state(o, m.now) == WINDOW_ACTIVE else "（已过周期·待复核）"
            print(f"  · {o.name}{mark}")
            print(f"    {o.point} 积分只换回 {fmt_yuan(o.face_value_cents)}，"
                  f"按最优用法应值 {fmt_yuan(int(o.point * m.benchmark_ypp * 100))}，"
                  f"机会成本 {fmt_yuan(opportunity_cost_cents(o, m.benchmark_ypp))}")

    print("\n【抽奖 vs 直接兑换】")
    lottery = analyze_lottery(draw_point, prizes, m.benchmark_ypp)
    print(f"  单次 {draw_point} 积分 · 打平线 {fmt_yuan(lottery.required_ev_cents)}"
          f" · 奖池最高 {fmt_yuan(lottery.max_prize_cents)}")
    print(f"  判定 {lottery.verdict.upper()}：{lottery.reason}")

    # ── 方案 ─────────────────────────────────────────────────────────── #
    budget = args.points if args.points is not None else available
    if budget > 0:
        plans = plan_bundles(budget, offers, top=args.top)
        print(f"\n【{budget} 积分怎么花最值】")
        if plans:
            for i, p in enumerate(plans, 1):
                names = " + ".join(f"{o.name}×{q}" if q > 1 else o.name for o, q in p.picks)
                print(f"  {i}. 面值 {fmt_yuan(p.total_value_cents)} | 花 {p.spent_point} 积分"
                      f" | 效率 {fmt_ypp(p.efficiency)} | 余 {p.leftover_point}")
                print(f"     {names}")
        else:
            print("  积分不足以兑换任何已定价标的。")

    expiring_point = args.expiring if args.expiring is not None else expiring
    if expiring_point > 0:
        ep = plan_expiry(expiring_point, offers, draw_point=draw_point, lottery=lottery)
        print(f"\n【{expiring_point} 积分即将过期，止损方案】")
        for b in ep.bundles:
            names = " + ".join(f"{o.name}×{q}" if q > 1 else o.name for o, q in b.picks)
            print(f"  ① 兑换挽回 {fmt_yuan(b.total_value_cents)}：{names}")
        if ep.lottery_draws:
            print(f"  ② 余额抽奖 {ep.lottery_draws} 次（{ep.lottery_point} 积分，"
                  f"按基准折价 {fmt_yuan(ep.lottery_value_cents)}）")
        print(f"  合计挽回 {fmt_yuan(ep.salvaged_cents)}")
        if ep.lost_point:
            print(f"  ⚠ 仍有 {ep.lost_point} 积分无法消耗，预计损失 {fmt_yuan(ep.lost_cents)}")

    print(f"\n{DISCLAIMER}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
