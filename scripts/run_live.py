#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端联调：真实门店 → 真实菜单/券 → 本地剪枝（Stage 1）→ MCP 精确计价（Stage 2）。

这是对 `SKILL.md` 五步工作流的可执行实现，也是本项目的真实使用示例。

    export MCD_MCP_TOKEN=xxxxx
    python scripts/run_live.py --city 南京市 --keyword 新街口 \
        --budget 40 --max-items 3 --top 3
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcd_mcp import MCDMcpClient, MCPError  # noqa: E402
from optimize_deals import (  # noqa: E402
    Constraints,
    Coupon,
    Item,
    Plan,
    fmt_yuan,
    optimize,
    to_cents,
)


# --------------------------------------------------------------------------- #
# 数据归一化
# --------------------------------------------------------------------------- #
def build_items(menu_json: Dict[str, Any]) -> List[Item]:
    """把 query-meals 的响应归一化为引擎的 Item 列表。"""
    data = menu_json.get("data") or {}
    meals = data.get("meals") or {}
    cat_of: Dict[str, str] = {}
    for cat in data.get("categories") or []:
        for m in cat.get("meals") or []:
            cat_of.setdefault(str(m.get("code")), str(cat.get("name", "")))

    items: List[Item] = []
    for code, info in meals.items():
        price = info.get("currentPrice") or info.get("originalPrice") or 0
        items.append(Item(
            code=str(code),
            name=str(info.get("name", code)),
            price_cents=to_cents(price),
            category=cat_of.get(str(code), ""),
        ))
    return items


def map_coupons(raw: Any) -> List[Coupon]:
    """尽力把 MCP 返回的券结构映射为引擎的 Coupon（结构未知时安全跳过）。"""
    out: List[Coupon] = []
    if not isinstance(raw, list):
        return out
    for c in raw:
        if not isinstance(c, dict):
            continue
        cid = c.get("couponId") or c.get("id") or c.get("code")
        if not cid:
            continue
        # 只映射语义明确的"满减"券；其余类型交给 MCP calculate-price 处理
        threshold = c.get("threshold") or c.get("conditionAmount") or c.get("limitAmount")
        reduce = c.get("discount") or c.get("reduceAmount") or c.get("amount")
        try:
            if threshold is not None and reduce is not None:
                out.append(Coupon(
                    id=str(cid),
                    name=str(c.get("name") or c.get("title") or cid),
                    type="threshold_reduce",
                    threshold_cents=to_cents(threshold),
                    reduce_cents=to_cents(reduce),
                ))
        except (TypeError, ValueError):
            continue
    return out


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="v1 能力 · 点餐链路端到端联调")
    p.add_argument("--city", default="南京市")
    p.add_argument("--keyword", default="")
    p.add_argument("--be-type", type=int, default=1, help="1-到店取餐 2-麦乐送 5-得来速 6-团餐")
    p.add_argument("--budget", default=None, help="到手价上限（元）")
    p.add_argument("--min-items", type=int, default=1)
    p.add_argument("--max-items", type=int, default=3)
    p.add_argument("--must-include", nargs="*", default=None, help="必点商品 productCode")
    p.add_argument("--max-calories", type=float, default=None)
    p.add_argument("--top", type=int, default=3, help="交给 MCP 精确计价的候选数")
    args = p.parse_args(argv)

    client = MCDMcpClient()
    info = client.initialize()
    print(f"[1] 已连接 MCP：{info.get('result', {}).get('serverInfo', {})}", file=sys.stderr)

    # --- 门店 ---
    stores = client.call_json("query-nearby-stores",
                              {"beType": args.be_type, "searchType": 2, "city": args.city, "keyword": args.keyword})
    slist = (stores.get("data") or []) if isinstance(stores, dict) else []
    if not slist:
        print("未找到门店，请调整 --city / --keyword", file=sys.stderr)
        return 1
    store = next((s for s in slist if s.get("businessStatus")), slist[0])
    store_code = store["storeCode"]
    order_type = 1 if args.be_type in (1, 5) else 2
    print(f"[2] 门店：{store['storeName']}（{store.get('address','')}）storeCode={store_code}")

    # --- 菜单 ---
    menu = client.call_json("query-meals",
                            {"storeCode": store_code, "orderType": order_type, "beType": args.be_type})
    items = build_items(menu)
    print(f"[3] 菜单：{len(items)} 个商品")

    # --- 券（尽力而为） ---
    coupons: List[Coupon] = []
    for tool, params in (
        ("query-store-coupons", {"storeCode": store_code, "orderType": order_type, "beType": args.be_type}),
        ("query-my-coupons", {}),
    ):
        try:
            raw = client.call_json(tool, params)
            raw_list = (raw.get("data") or []) if isinstance(raw, dict) else raw
            got = map_coupons(raw_list)
            coupons.extend(got)
            print(f"[4] {tool}：原始 {len(raw_list) if isinstance(raw_list, list) else '?'} 条，映射 {len(got)} 条")
        except MCPError as exc:
            # 无券时 MCP 只返回一句说明文字，不含 JSON —— 属正常情况
            print(f"[4] {tool}：当前无可用券")
        except Exception as exc:  # noqa: BLE001
            print(f"[4] {tool} 跳过（{exc}）", file=sys.stderr)

    # --- Stage 1：本地剪枝 ---
    cons = Constraints(
        budget_cents=to_cents(args.budget) if args.budget is not None else None,
        min_items=args.min_items,
        max_items=args.max_items,
        max_calories=args.max_calories,
        must_include=tuple(args.must_include or ()),
    )
    plans, pruned = optimize(items, coupons, cons, top=args.top)
    if not plans:
        print("Stage 1 无可行组合，请放宽约束。", file=sys.stderr)
        return 1
    print(f"[5] Stage 1 本地剪枝{'（启发式）' if pruned else ''}：得到 {len(plans)} 个候选\n")

    # --- Stage 2：MCP 精确计价 ---
    print("[6] Stage 2 · calculate-price 精确计价：")
    print(f"{'#':>2}  {'估算到手':>9}  {'实算到手':>9}  {'偏差':>7}  组合")
    print("-" * 78)
    for i, plan in enumerate(plans, 1):
        payload = [{"productCode": it.code, "quantity": 1} for it in plan.items]
        actual: Optional[int] = None
        err = "未提取到金额字段"
        try:
            priced = client.call_json("calculate-price", {
                "storeCode": store_code, "orderType": order_type, "beType": args.be_type,
                "items": payload,
            })
            actual = _extract_total(priced)
        except Exception as exc:  # noqa: BLE001
            err = str(exc)[:40]
        est = fmt_yuan(plan.final_cents)
        act = fmt_yuan(actual) if actual is not None else f"失败({err})"
        delta = f"{(actual - plan.final_cents)/100:+.2f}" if actual is not None else "-"
        names = " + ".join(it.name for it in plan.items)
        print(f"{i:>2}  {est:>9}  {act:>9}  {delta:>7}  {names}")

    print("\n说明：估算值仅用于排序；实算值为麦当劳 MCP 官方计价结果。")
    return 0


def _extract_total(priced: Any) -> Optional[int]:
    """从 calculate-price 响应中提取应付总价（**单位：分**）。

    麦当劳 MCP 该接口的金额字段本身以「分」为单位，切勿再乘 100。
    字段说明（摘自 Tool 的响应结构）：``data.price`` = 现价，``data.discount`` = 优惠金额。
    """
    if not isinstance(priced, dict):
        return None
    data = priced.get("data", priced)
    if isinstance(data, dict):
        for key in ("price", "payAmount", "totalAmount", "payPrice"):
            v = data.get(key)
            if v is not None:
                try:
                    return int(v)
                except (TypeError, ValueError):
                    continue
    return None


if __name__ == "__main__":
    sys.exit(main())
