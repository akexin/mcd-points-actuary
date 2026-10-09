---
name: mcd-saver-actuary
description: 麦麦省钱精算师。基于麦当劳中国 MCP，在预算 / 热量 / 口味 / 必点项约束下，穷举并精算最优点餐组合，输出"最省钱方案 + 省钱明细 + 备选方案"。当用户询问"麦当劳怎么点最划算""帮我搭配麦麦套餐""优惠券怎么用""外卖/到店怎么省"时触发。
---

# 麦麦省钱精算师（MCD Saver Actuary）

## 这个 Skill 解决什么问题

麦当劳的优惠体系是多层的：**菜单价 → 优惠券 → 麦麦省领券 → 积分兑换 → 活动日历**。
用户看到的"到手价"取决于购物车组合与券的匹配方式，人工很难穷举。
本 Skill 把这件事变成可复算的**精算流程**：

> 先用**本地引擎**对海量组合剪枝排序，再用麦当劳 MCP 的 `calculate-price` 对最有希望的少数组合**精确计价**，最终给出确定可行的最优解。

这样既保证了结果准确（以 MCP 计价为准），又把请求量压在了 **600 次/分钟**的限流线以内。

## 前置条件

1. 已配置麦当劳 MCP 连接器（`mcd-mcp`），配置见 `mcp-config.example.json`。
2. 本地脚本层需环境变量 `MCD_MCP_TOKEN`（**切勿写入代码或提交仓库**）。
3. Python ≥ 3.9（本项目仅用标准库，无第三方依赖）。

## 工作流

### 第 1 步 · 确认约束（必须先向用户澄清）

在动手前确认以下要素，缺失时主动询问：

| 约束 | 示例问法 |
|---|---|
| 就餐方式 | 外送到家、到店取餐，还是堂食？ |
| 位置 / 门店 | 送哪个地址？或哪家餐厅？ |
| 人数 / 件数 | 一人食还是多人？最少/最多几件主食？ |
| 预算 | 到手价不超过多少元？ |
| 忌口 / 必点 | 必须含某商品？不要辣 / 不要冰？ |
| 热量目标 | 单餐热量上限（kcal）？ |

### 第 2 步 · 拉取现场数据（MCP）

按就餐方式选择工具，**用真实数据而非记忆中的价格**：

- 门店：`query-nearby-stores`（到店）/ `delivery-query-addresses` + `delivery-query-stores`（外送）
- 菜单与价格：`query-meals` → 需要套餐组成时 `query-meal-detail`
- 优惠：`query-store-coupons`（当前门店可用）、`available-coupons` + `auto-bind-coupons`（麦麦省一键领券）、`query-my-coupons`（我的券）
- 积分：`query-my-account`、`mall-points-products`
- 活动：`campaign-calendar`
- 营养：`list-nutrition-foods`

### 第 3 步 · 本地剪枝（脚本层，零网络）

把第 2 步的菜单与券归一化成引擎输入格式，运行：

```bash
python scripts/optimize_deals.py \
  --menu   data/current_menu.json \
  --coupons data/current_coupons.json \
  --max-items 4 --budget 35 --max-calories 900 \
  --must-include BURGER-BIGMAC --top 10 --json
```

引擎输出**按估算到手价升序**的候选组合。金额全程以"分"计算，避免浮点误差。

### 第 4 步 · MCP 精确计价（Stage 2）

只对第 3 步的 **Top-K（建议 K ≤ 8）** 组合调用 `calculate-price`，拿到真实金额、配送费与优惠。
对比估算值与实算值，剔除被高估的方案。

```bash
python scripts/mcd_mcp.py call calculate-price '{"storeCode":"...","items":[...]}'
```

### 第 5 步 · 输出结论

按下列结构回复用户，**结论先行**：

```text
【最省方案】到手 ¥28.50（原价 ¥36.50，省 ¥8.00 / 21.9%）
  巨无霸 + 中薯条 + 中杯可乐
  用券：买中薯条送中杯可乐
  热量：约 1040 kcal
【备选方案】到手 ¥31.50 —— 巨无霸 + 中薯条（用满 30 减 5）
【说明】优惠与应付金额以麦当劳 MCP calculate-price 实算为准。
```

## 关键约束与边界

- **单笔仅用 1 张最优券**（保守假设）；真实叠加规则以 MCP 计价结果为准。
- 本地引擎的券抵扣是**估算**，作用是排序，不是最终答案。
- 热量约束下，若某商品缺少营养数据，该组合会被判定为不可行，交由 `list-nutrition-foods` 补齐后再算。
- 请求限流 600 次/分钟，禁止对全部组合暴力调用 `calculate-price`。
- 只输出建议，**下单前必须由用户确认**；涉及支付的操作需用户显式同意。

## 目录结构

```text
mcd-saver-actuary/
├── SKILL.md                       # 本文件
├── scripts/
│   ├── optimize_deals.py          # 本地组合剪枝引擎（离线可测）
│   └── mcd_mcp.py                 # MCP Streamable HTTP 轻量客户端
├── data/
│   ├── sample_menu.json           # 脱敏示例菜单
│   └── sample_coupons.json        # 脱敏示例优惠券
└── tests/test_optimizer.py        # 13 项离线单元测试
```

## 声明

本项目为麦当劳程序员节创意开发大赛参赛作品，由参赛者独立开发，**非麦当劳官方产品**。
输出仅供参考，不构成营养或医疗建议；餐品信息、价格及供应状态以麦当劳官方渠道实时结果为准。
