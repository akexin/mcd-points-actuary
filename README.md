# 麦麦省钱精算师 · MCD Saver Actuary

> 基于麦当劳中国 MCP 的点餐**精算** Skill：把"菜单价 → 优惠券 → 麦麦省 → 积分 → 活动"这层叠的优惠体系，
> 变成一次可复算的最优解搜索。

[![MCP](https://img.shields.io/badge/MCP-麦当劳中国-red)](https://open.mcd.cn/mcp)
[![Python](https://img.shields.io/badge/python-3.9%2B-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

---

## 项目介绍

麦当劳的到手价由多层优惠共同决定，人工穷举组合几乎不可能。本项目把这个过程工程化：

**两阶段求解**

| 阶段 | 做什么 | 在哪运行 | 为什么 |
|---|---|---|---|
| Stage 1 · 剪枝 | 对菜单组合做穷举 + 约束过滤，估算到手价并排序 | **本地**，零网络 | MCP 限流 600 次/分钟，不可能对全部组合计价 |
| Stage 2 · 精算 | 只对 Top-K 组合调用 `calculate-price` | 麦当劳 MCP | 拿到真实金额、配送费与优惠，作为最终结论 |

**核心特性**

- 支持预算上限、件数区间、热量上限、必点/忌口等**多目标约束**
- 优惠券建模覆盖：满减、单品立减、买赠、单品免费
- 金额以「分」为整数计算，**无浮点误差**
- 本地引擎**离线可测**，13 项单元测试全绿
- 自带 MCP Streamable HTTP 客户端，`429` 自动退避重试

## 目标用户

| 人群 | 痛点 | 本 Skill 的价值 |
|---|---|---|
| 学生 / 上班族 | 想省钱但没精力研究券怎么叠 | 直接给最优组合与省钱明细 |
| 健身 / 控糖人群 | 想吃饱又想控热量 | 热量上限约束下的最省组合 |
| 多人拼单发起者 | 众口难调，凑单凑不明白 | 必点项 + 预算约束的自动搭配 |

## 安装方法

### 1. 申请麦当劳 MCP Token

访问 <https://open.mcd.cn/mcp> → 右上角「登录」（手机号验证）→「控制台」→「激活」→ 复制 Token。

### 2. 配置 MCP 连接器

将 `mcp-config.example.json` 中的 `${MCD_MCP_TOKEN}` 替换为实际 Token，填入你的 MCP Client（WorkBuddy / Cursor / Cherry Studio / Trae 等）：

```json
{
  "mcpServers": {
    "mcd-mcp": {
      "type": "streamablehttp",
      "url": "https://mcp.mcd.cn",
      "headers": { "Authorization": "Bearer YOUR_MCP_TOKEN" }
    }
  }
}
```

### 3. 设置环境变量（脚本层需要）

```bash
export MCD_MCP_TOKEN=你的Token        # Linux / macOS / Git Bash
setx MCD_MCP_TOKEN 你的Token          # Windows（需重开终端）
```

### 4. 运行

```bash
git clone https://github.com/akexin/mcd-saver-actuary.git
cd mcd-saver-actuary
python -m unittest discover -s tests        # 离线自检
```

> 无需 `pip install`：本项目只依赖 Python 标准库。

## 使用示例

### 示例 A · 离线体验（不需要 Token）

```bash
python scripts/optimize_deals.py \
  --menu data/sample_menu.json \
  --coupons data/sample_coupons.json \
  --max-items 4 --budget 35 --top 5
```

输出（节选）：

```text
共 5 个候选方案（全量枚举）：

 1. 到手 ¥0.00  |  原价 ¥8.00  省 ¥8.00 (100.0%)
    圣代
    券：圣代免费   热量：200 kcal
 3. 到手 ¥9.50  |  原价 ¥17.50  省 ¥8.00 (45.7%)
    中杯可乐 + 圣代
    券：圣代免费   热量：350 kcal

估算结果仅供排序参考，最终优惠与应付金额以麦当劳 MCP calculate-price 为准。
```

### 示例 B · 连接真实 MCP

```bash
python scripts/mcd_mcp.py list-tools
python scripts/mcd_mcp.py call query-nearby-stores '{"address":"上海人民广场"}'
```

拿到菜单与券后，按 `SKILL.md` 的五步工作流让 Agent 完成精算并输出结论。

## 目录结构

```text
mcd-saver-actuary/
├── SKILL.md                       # WorkBuddy Skill 定义与工作流
├── README.md                      # 本文件
├── CONTEST_DECLARATION.md         # 参赛声明（官方原文，未改动）
├── MCP_INTEGRATION.md             # MCP 接入说明
├── mcp-config.example.json        # 脱敏 MCP 配置（环境变量占位符）
├── workbuddy.md                   # WorkBuddy 开发对话上下文
├── scripts/
│   ├── optimize_deals.py          # 本地组合剪枝引擎
│   └── mcd_mcp.py                 # MCP Streamable HTTP 客户端
├── data/
│   ├── sample_menu.json           # 脱敏示例菜单
│   └── sample_coupons.json        # 脱敏示例优惠券
├── tests/
│   └── test_optimizer.py          # 单元测试
└── LICENSE
```

## 数据说明

`data/` 下的示例菜单与优惠券为**脱敏演示数据**，仅用于离线测试与文档演示，**不代表麦当劳真实价格或券规则**。真实数据一律通过 MCP 实时获取。

## 声明

本项目为**麦当劳程序员节创意开发大赛**参赛作品，由参赛者独立开发，**非麦当劳官方产品**。
项目输出仅供参考，不构成医疗、营养或其他专业建议；餐品信息、价格及供应状态以麦当劳官方渠道的实时结果为准。

## 许可证

[MIT](LICENSE) © 2026 akeXin
