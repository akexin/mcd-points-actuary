# WorkBuddy 开发对话上下文（workbuddy.md）

> 本文件记录使用**腾讯 WorkBuddy** 开发本项目的真实对话上下文，用于核验 WorkBuddy 专项奖励资格。
> 开发日期：2026-10-09。

## 会话 1 · 活动信息核实与参赛方案

- **用户**：提供麦当劳程序员节创意开发大赛的微信文章链接，要求"根据文章帮我参加活动"。
- **WorkBuddy**：抓取文章并定位到官方组织 `M-China`，读取
  `mcd-developer-innovation-challenge`（活动规则、排行榜）与 `mcd-mcp-server`（MCP 使用指南、工具清单），
  归纳出报名条件、必备文件、Issue 报名格式、Star 排名机制与奖励规则，产出《参赛行动清单》与文件包模板。

## 会话 2 · 项目方向与仓库决策

- **WorkBuddy**：就"项目方向 / GitHub 仓库 / MCP Token"三项向用户确认。
- **用户**：方向选定「麦麦省钱精算师」；同意在 akeXin 名下新建公开仓库；MCP Token 已有、稍后提供。
- **WorkBuddy**：据此确定仓库名 `mcd-saver-actuary`，并设计两阶段（本地剪枝 + MCP 精算）架构。

## 会话 3 · 核心代码实现与调试

- **WorkBuddy**：实现 `scripts/optimize_deals.py`（组合剪枝引擎）与 `scripts/mcd_mcp.py`（Streamable HTTP 客户端），
  编写 13 项离线单元测试。
- **调试过程**：
  1. `Plan` 缺少 `saved_pct` 属性导致 CLI 报 `AttributeError` → 抽为 `@property`。
  2. `buy_get` 类优惠券语义错误（把"买的商品"当作抵扣额）→ 修正为"需购物车同时含 buy/get 两件商品，get 件免单"。
  3. 测试用例 `test_infeasible_returns_empty` 以"极小预算"断言无解，但存在"圣代免费"券导致仍有解 → 改用"必点商品不存在"构造真正的无解场景。
- **结果**：`python -m unittest discover -s tests` → **13 项全部通过**。

---

## 会话 4 · 真实 MCP 联调（Token 到手后）

- **用户**：提供麦当劳 MCP Token，要求继续推进。
- **WorkBuddy**：将 Token 仅写入本地环境（`~/.workbuddy/mcp.json` 与 gitignore 覆盖的 `.env`），
  确认未进入版本库；随后在南京新百餐厅（`1990346`）做端到端联调。
- **联调中定位并修复的 4 个真实问题**：
  1. MCP 响应是「说明文字 + JSON + 尾部指令」混合文本、JSON 不在末尾
     → 提取策略改为"枚举 `{` 起点、取 `raw_decode` 跨度最大者"；
  2. `calculate-price` 金额字段单位为**分**，误按"元"换算会放大 100 倍 → 修正取值；
  3. 无券时 `query-my-coupons` 只返回说明文字、不含 JSON → 判定为 0 张券而非解析失败；
  4. 组合剪枝按"最低价优先"会把候选池缩成**全是调味酱** → 改为按分类轮转取样。
- **结果**：Stage 1 剪枝出 4 个候选，Stage 2 `calculate-price` 实算值与本地估算**偏差均为 ¥0.00**。

---

## 使用了 WorkBuddy 的哪些能力

| 能力 | 用途 |
|---|---|
| 文档抓取与信息核实 | 读取官方规则仓库，避免误信二手信息 |
| 代码生成与调试 | 优化引擎、MCP 客户端的编写与缺陷修复 |
| 单元测试编排 | 生成并运行 13 项离线测试，验证引擎正确性 |
| 项目脚手架 | 生成符合赛事要求的文件结构（README / MCP_INTEGRATION / CONTEST_DECLARATION 等） |
| GitHub 操作 | 创建公开仓库并推送代码 |
