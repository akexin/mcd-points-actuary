# MCP 接入说明（MCP_INTEGRATION.md）

> 本文件说明 **麦麦省钱精算师** 实际使用的麦当劳中国 MCP Server、Tool、调用流程与业务价值。

## 1. 接入的 MCP Server

| 项 | 值 |
|---|---|
| 名称 | 麦当劳中国 MCP Server（`mcd-mcp`） |
| 接入地址 | `https://mcp.mcd.cn` |
| 传输协议 | Streamable HTTP（JSON-RPC 2.0，兼容 `application/json` 与 `text/event-stream` 响应） |
| 鉴权方式 | 请求头 `Authorization: Bearer <MCD_MCP_TOKEN>` |
| 协议版本 | `2025-06-18` |
| 开放平台 | <https://open.mcd.cn/mcp> |
| 限流 | 600 请求 / 分钟 / Token |

客户端实现见 [`scripts/mcd_mcp.py`](scripts/mcd_mcp.py)：完成 `initialize` → `tools/list` → `tools/call` 三段式调用，并对 `429` 做指数退避。

## 2. 使用的 Tool 清单

| Tool | 名称 | 在本项目中的用途 |
|---|---|---|
| `query-nearby-stores` | 查询附近可用门店 | 到店场景定位门店，拿到 `storeCode` |
| `delivery-query-addresses` | 获取用户可配送地址列表 | 外送场景选择收货地址 |
| `delivery-query-stores` | 查询可配送的门店列表 | 外送场景确定出餐门店 |
| `query-meals` | 查询当前可售卖餐品列表 | 获取菜单、价格、分类、标签 —— **剪枝引擎的输入** |
| `query-meal-detail` | 查询餐品详情 | 获取套餐组成与可替换项，展开为可比较的原子商品 |
| `query-store-coupons` | 查询当前门店可用优惠券 | 构造券集合 |
| `available-coupons` | 麦麦省券列表查询 | 拿到可领取的券 |
| `auto-bind-coupons` | 麦麦省一键领券 | 自动领取用户可领的全部券，扩大可行优惠空间 |
| `query-my-coupons` | 我的优惠券查询 | 补充用户已持有的券 |
| `calculate-price` | 商品价格计算 | **Stage 2 精确计价**：对 Top-K 组合算出真实金额、配送费、优惠与应付总价 |
| `list-nutrition-foods` | 餐品营养信息列表 | 支撑热量上限约束 |
| `query-my-account` | 我的积分查询 | 评估积分可抵扣/可兑换空间 |
| `mall-points-products` | 查询麦麦商城商品列表 | 积分兑换商品的省钱潜力评估 |
| `campaign-calendar` | 活动日历查询工具 | 提示当月活动，避免错过更优方案 |

> 下单相关工具（`create-order` / `cancel-order` / `query-order` 等）**不在自动流程中**调用；
> 本 Skill 只输出建议，实际下单必须由用户确认后手动发起。

## 3. 调用流程

```text
用户约束（预算 / 件数 / 热量 / 必点 / 忌口）
        │
        ▼
[1] 门店解析     query-nearby-stores  ·  delivery-query-*  ───► storeCode
        │
        ▼
[2] 数据采集     query-meals → query-meal-detail              ───► 原子商品 + 价格
                 query-store-coupons / available-coupons
                 / auto-bind-coupons / query-my-coupons       ───► 券集合
                 list-nutrition-foods                         ───► 热量
        │
        ▼
[3] 本地剪枝     scripts/optimize_deals.py                    ───► 按估算到手价排序的 Top-K
   （零网络）     约束过滤 → 组合枚举 → 券估算 → 排序
        │
        ▼
[4] 精确计价     calculate-price × K（K ≤ 8）                 ───► 真实到手价
        │
        ▼
[5] 结论输出     最省方案 + 省钱明细 + 备选方案
                 （query-my-account / mall-points-products / campaign-calendar 作为加分建议）
```

**为什么要把流程拆成两段？** 若对 10 个商品的全部 4 件组合直接调用 `calculate-price`，将产生数千次请求，瞬间触发 600 次/分钟的限流（返回 `429`）。本地剪枝把请求量压缩到个位数，是本项目能稳定运行的关键工程决策。

## 4. 业务价值

- **把"省钱"从经验变成可复算的计算**：用户给出约束，Skill 给出确定可行的最优组合与省钱金额，而不是模糊的"建议用券"。
- **限流友好**：两阶段架构把 MCP 调用从数千次压缩到不超过 8 次，单次精算耗时从分钟级降到秒级。
- **结果以官方计价为准**：本地估算只用于排序，最终金额一律由 `calculate-price` 实算，避免"看起来便宜"的误导。
- **可扩展到积分与活动**：同一框架可直接接入积分账户与活动日历，从"单笔最省"扩展到"长期最省"。

## 5. 错误与限流处理

| code | 原因 | 处理 |
|---|---|---|
| `401` | Token 无效 / 过期 / 未提供 | 提示重新配置 `MCD_MCP_TOKEN`，不重试 |
| `429` | 超过 600 次/分钟 | 指数退避重试（`2^n` 秒），并缩减 Top-K |
| 网络异常 | DNS / 连接超时 | 线性退避重试，最多 3 次后降级为"仅输出估算结果"并明确告知用户 |
