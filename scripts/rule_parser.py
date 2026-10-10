#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""麦麦积分精算师 · v3.3 券面文本解析层（轨道③：数据与鲁棒性）。

问题
----
v1~v3.2 的券都是**人手抄进 JSON 的结构化行**（``{"type": "threshold_reduce",
"threshold": 30.0, "reduce": 5.0}``）。这在演示里没问题，一碰到真实数据就崩：

- 券面长这样 —— 「满30元减5元」「麦乐鸡（5块）立减4元」「半价周末·巨无霸 5折」
  「必点榜专属无门槛券 减5」「饿了么整单 88折（封顶12元）」。**它们都是文本**，
  字段是人读出来的，不是程序读出来的。
- 让人来抄，就有抄错、抄漏、抄过期的空间。而**抄错一个门槛**（把"满39"写成"满29"）
  会让整个决策链算出一个错的"最便宜"，比不做还糟。

本模块做的事
------------
把券面文本 → 结构化约束。但它**不是**一个"能解析就解析"的流水线，
而是一条**带闸门**的流水线。三条设计原则：

1. **宁可不算，不可猜算。** 置信度低于 ``DEFAULT_THRESHOLD``（0.75）的解析结果
   不产出 ``Coupon``，只产出一句「以 App 下单页为准」。少算一张券顶多错失一次优惠；
   猜错一张券会给出一个买不到的价格。
2. **置信度是**算出来的**，不是拍出来的。** 每个加分/减分都对应一条 ``Evidence``，
   带原文位置与原因，可逐条复核。没有黑盒分数。
3. **绝不推断文本没写的东西。** 封顶、互斥组、叠加策略这些文本里根本不存在的字段，
   一律记进 ``unstated`` 并交给调用方 —— 本项目不替平台"补全"规则。

解析不了的两类，要分开报
------------------------
- **结构不支持**（``refused``）：文本说得清清楚楚，但**购物车模型装不下**。
  例：「第二杯半价」「买一送一」需要同一商品两份，而购物车是**集合**，
  表达不了"2 份某单品"。这类必须**大声拒绝**，不能当成"没听懂"悄悄丢掉 ——
  静默忽略会让人以为"这个项目不支持券"，实际是模型边界。
- **不敢确定**（``ambiguous``）：文本有歧义或信息不全，比如「满30减5，限部分门店」。
  这类按低置信度拒绝，并在报告里说明是哪几个字让它不敢下手。

七种券型的文本形态（都在 ``data/`` 的真实样例里出现过）
-------------------------------------------------------
=================  ==========================  ================================
文本                解析为                       说明
=================  ==========================  ================================
满30元减5元          ``threshold_reduce``        门槛 + 立减额
无门槛减6            ``threshold_reduce``        门槛为 0
麦乐鸡（5块）立减4元   ``item_reduce``             单品立减
圣代免费             ``item_free``               单品免单
买中薯条送中杯可乐      ``buy_get``                 买赠
半价周末·巨无霸 5折     ``item_discount``           指定商品折扣
饿了么整单 88折（封顶12元） ``discount``               整单折扣 + 封顶
21.9元买巨无霸+中杯可乐  ``bundle_price``           组合打包价
积分兑换·巨无霸（500积分） ``item_free`` + 积分成本    积分商城券
=================  ==========================  ================================

金额一律落到**分**（int），与 :mod:`optimize_deals` 的其他部分一致；
输出的 ``fields`` 字典可直接喂给 ``Coupon.from_dict``。

用法
----
    python rule_parser.py --text "满30元减5元"
    python rule_parser.py --text "半价周末·巨无霸 5折" --menu data/sample_menu.json
    python rule_parser.py --coupons data/sample_channels.json --menu data/sample_menu.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from optimize_deals import Coupon, Item, fmt_yuan, to_cents
from termtext import columns, rule, table_width

#: 低于此置信度**不产出券**。0.75 的选择不是调参调出来的 ——
#: 见 ``_score`` 里各档的构成：一个"结构完整 + 金额带单位"的阈值券刚好落在 0.86，
#: 而"有一个词拿不准"的案例会掉到 0.70 以下。阈值就卡在这条缝里。
DEFAULT_THRESHOLD = 0.75

#: 置信度上限（分数是加出来的，必须夹住）
MAX_CONFIDENCE = 1.0

#: 文本里出现这些词，说明券面**故意留了模糊**。每命中一个扣 0.15，最多扣 0.30。
#: 这份词表是"宁可漏"的：只要出现就不敢按完整优惠算。
FUZZY_WORDS: Tuple[str, ...] = (
    "部分", "指定", "随机", "限时", "不定期", "特定", "个别",
    "以门店", "视库存", "可能", "酌情", "最高可", "有机会",
)

#: 「只在这几家店能用」类限定 —— 比一般模糊词更致命：它不改变金额，它决定
#: 这张券在**本店**存不存在。文本里读不出"本店在不在名单内"，所以一律按不可用处理。
SCOPE_WORDS: Tuple[str, ...] = (
    "部分门店", "指定门店", "限门店", "以下门店", "仅限门店", "部分餐厅", "指定餐厅",
)

#: 这些词出现在文本里，说明是**平台/渠道**发的券，而不是门店通用券。
IDENTITY_WORDS: Tuple[str, ...] = (
    "平台券", "专属", "红包", "会员", "卡券", "吃货卡", "商家券", "店铺",
)

#: 明确写"整单"的字样。折扣券没写出适用商品时，这两种情况要**分开对待**：
#: 写了「整单」说明它就是整单券（按整单估算是**对的**，不该扣分）；
#: 什么都没写才叫"适用对象不明"（按整单估算会**高估**）。
ORDER_WORDS: Tuple[str, ...] = ("整单", "全单", "订单", "通用", "全店", "全场")

#: **结构不支持**的形态：文本明确，但购物车模型（商品集合）装不下。
#: 命中即 ``refused``，不给分数、不给字段。
STRUCTURAL_REFUSALS: Tuple[Tuple[str, str], ...] = (
    (r"第\s*[一二两三四五六七八九十\d]+\s*(?:杯|件|份|个)\s*(?:半价|免单|免费|[\d.]+\s*折)",
     "「第 N 杯 / 件」需要**同一商品多份**，而购物车模型是集合，表达不了份数"),
    (r"买\s*[一二两半\d]+\s*(?:送|赠)\s*[一二两半\d]+",
     "「买一送一」类需要同一商品两份，而购物车模型是集合，表达不了份数"),
    (r"满\s*[\d.]+\s*(?:件|个|杯|份)",
     "「满 N 件」是**件数门槛**，v3 的券门槛只有金额口径（``threshold_cents``）"),
    (r"随机\s*(?:立减|减|优惠|红包|金额)",
     "「随机立减」的优惠额是随机变量，本引擎只做确定性报价。"
     "若要纳入，应取**保守分位**而不是期望值（与 ``lottery_mc`` 同一口径），"
     "否则会算出一个一半概率拿不到的价格"),
)

#: 使用频次限制（「每单限用 1 张」「每天限 1 次」）。
#: 这**不是**拒绝理由：它不改变"这一单能省多少"，只限制能用几次。
#: 所以只记成 note + 结构提示，让 :mod:`expiry_policy` 那样的长期策略去消费。
RE_FREQ = re.compile(
    r"每\s*(?P<unit>单|天|周|月|日|笔)\s*(?:限|可)\s*(?:用|享|领)?\s*(?P<n>[\d一二两三四五六七八九十]+)\s*(?:张|次|份|笔)?")
_CN_TO_INT = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
              "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}

#: 尾部附加说明的**引导词**。券面常见「…满30减5（限部分门店）」「…买A送B 每单限用一张」——
#: 这些说明不影响金额结构，留在正文里只会让锚点失配。但它们**必须保留**：
#: 它们是"能用几次 / 在哪能用"的约束。
TAIL_KEYS = ("每", "仅限", "限", "不得", "不可", "不", "有效期", "适用于",
             "适用", "活动", "以上金额", "以门店")
RE_TAIL_BRACKET = re.compile(r"[（(][^（()）]*[)）]")
RE_TAIL_CLAUSE = re.compile(r"[，,。；;\s]+(?=" + "|".join(map(re.escape, TAIL_KEYS)) + r")")


def split_tail(text: str) -> Tuple[str, str]:
    """把券面切成 ``(主体, 尾部附加说明)``。

    只从**末尾**切（主体永远是原文前缀），这样 ``Evidence.span`` 在原文里依然对得上号。

    刻意**不**把「封顶」当尾部说明处理 —— 它是金额结构的一部分，
    切掉会让折扣券失去封顶，估算立刻偏乐观。
    """
    for m in RE_TAIL_BRACKET.finditer(text):
        if any(k in m.group(0)[1:-1] for k in TAIL_KEYS):
            return text[:m.start()].strip(), text[m.start():].strip()
    for m in RE_TAIL_CLAUSE.finditer(text):
        return text[:m.start()].strip(), text[m.end():].strip()
    return text.strip(), ""

# --------------------------------------------------------------------------- #
# 数字与金额
# --------------------------------------------------------------------------- #
_CN_DIGIT = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
             "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}

#: 数字片段：阿拉伯数字**或**中文数字。金额一律用阿拉伯数字，
#: 但折扣率（"八八折"）在券面上几乎总是中文，所以两边都要认。
_NUM = r"(?:\d+(?:\.\d+)?|[零〇一二两三四五六七八九十]+)"
_CN_RUN = r"[零〇一二两三四五六七八九十]+"


def cn_number(raw: str) -> float:
    """解析中文数字 / 阿拉伯数字 / "半"。无法解析时抛 ``ValueError``。

    支持：``88`` / ``8.8`` / ``八`` / ``八八`` / ``十五`` / ``二十`` / ``二十五`` / ``半``。
    **不支持**「一百二十三」这类带单位的数 —— 券面金额不会这么写，真出现就拒绝，
    不硬凑（凑错的代价是把"满100减20"读成"满3减20"）。
    """
    s = raw.strip()
    if not s:
        raise ValueError("空数字")
    if s in ("半", "一半"):
        return 0.5
    if re.fullmatch(r"\d+(?:\.\d+)?", s):
        return float(s)
    if "十" in s:
        left, _, right = s.partition("十")
        if left and left not in _CN_DIGIT:
            raise ValueError(f"无法解析的中文数字: {raw!r}")
        tens = 10 * (_CN_DIGIT[left] if left else 1)
        if not right:
            return float(tens)
        if right not in _CN_DIGIT:
            raise ValueError(f"无法解析的中文数字: {raw!r}")
        return float(tens + _CN_DIGIT[right])
    if all(ch in _CN_DIGIT for ch in s):
        # 「八八」→ 88（逐位拼），不是 8+8
        return float("".join(str(_CN_DIGIT[ch]) for ch in s))
    raise ValueError(f"无法解析的中文数字: {raw!r}")


def cn_rate(raw: str) -> float:
    """把「8折」「八折」「八八折」「半价」统一成折后比例（0.88 / 0.5）。

    规则：数值 ≥ 10 按百分数（``88`` → 0.88），否则按十分数（``8`` → 0.8）。
    这条规则同时覆盖阿拉伯与中文两种写法，且对 ``8.8`` / ``八八`` 都给出 0.88。
    """
    s = raw.strip()
    if s in ("半价", "半", "一半"):
        return 0.5
    s = s.rstrip("折")
    n = cn_number(s)
    return round(n / 100.0, 4) if n >= 10 else round(n / 10.0, 4)


_UNIT_AFTER = (("块钱", "块钱"), ("元", "元"), ("块", "块"))


def unit_after(text: str, idx: int) -> str:
    """看 ``idx`` 位置之后紧跟着哪个货币单位（没有则返回空串）。"""
    for token, label in _UNIT_AFTER:
        if text.startswith(token, idx):
            return label
    return ""


# --------------------------------------------------------------------------- #
# 商品名 → 菜单编码
# --------------------------------------------------------------------------- #
#: 券面上常见的简称。**只放精确的、能唯一定位的映射** ——
#: 放"鸡"→"麦乐鸡"这种模糊规则就等于把猜测写进代码。
ALIASES: Dict[str, str] = {
    "中薯": "FRIES-M",
    "大薯": "FRIES-L",
    "中可": "COLA-M",
    "大可": "COLA-L",
    "可乐": "COLA-M",
    "巨无霸汉堡": "BURGER-BIGMAC",
    "麦辣鸡腿堡": "BURGER-MCFISH",
}


def _norm(s: str) -> str:
    """归一化商品名：去空白、全角括号转半角、去掉分隔点。"""
    s = re.sub(r"\s+", "", s)
    s = s.replace("（", "(").replace("）", ")")
    s = s.replace("·", "").replace("・", "").replace("•", "")
    return s


@dataclass(slots=True)
class MenuIndex:
    """菜单索引：券面文本里的商品名 → ``Item``。

    解析顺序（**从最确定到最不确定**，一命中就停）：
    编码 → 全名 → 别名 → 归一化全名 → 单侧包含（**要求唯一**）。

    最后一步的"要求唯一"是关键：``巨无霸`` 同时是 ``BURGER-BIGMAC`` 的全名、
    又是 ``COMBO-BIGMAC``（巨无霸套餐（含中薯+中可））的前缀。包含匹配会给出 2 个候选 ——
    这时**必须判为不确定**，而不是随便挑一个。全名精确命中排在包含之前，
    所以正常路径根本走不到这一步。
    """

    items: Tuple[Item, ...] = ()
    by_code: Dict[str, Item] = field(default_factory=dict)
    by_name: Dict[str, Item] = field(default_factory=dict)

    @classmethod
    def build(cls, pool: Optional[Iterable[Item]]) -> "MenuIndex":
        items = tuple(pool or ())
        return cls(
            items=items,
            by_code={i.code: i for i in items},
            by_name={_norm(i.name): i for i in items},
        )

    @property
    def enabled(self) -> bool:
        return bool(self.items)

    def resolve(self, name: str) -> Tuple[Optional[Item], str]:
        """返回 ``(item, 命中方式)``；定位不了时 ``item`` 为 ``None``。"""
        if not name or not self.enabled:
            return None, ""
        key = _norm(name)
        if key in self.by_code:
            return self.by_code[key], "编码"
        if key in self.by_name:
            return self.by_name[key], "全名"
        code = ALIASES.get(key)
        if code and code in self.by_code:
            return self.by_code[code], "别名"
        hits = [i for i in self.items if key and (key in _norm(i.name) or _norm(i.name) in key)]
        if len(hits) == 1:
            return hits[0], "包含"
        if len(hits) > 1:
            return None, f"歧义({len(hits)}个候选)"
        return None, "未命中"


# --------------------------------------------------------------------------- #
# 证据与结果
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class Evidence:
    """一条加分/减分，带**原文位置**与原因 —— 置信度必须能逐条复核。"""

    kind: str
    weight: float
    text: str = ""
    span: Tuple[int, int] = (0, 0)
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind, "weight": round(self.weight, 3),
                "text": self.text, "span": list(self.span), "note": self.note}


@dataclass(slots=True)
class ParseOutcome:
    """一次解析的完整结果。

    ``status`` 三态：
      - ``accepted``：置信度达标，``fields`` 可直接构造 ``Coupon``
      - ``ambiguous``：不敢确定，``fields`` 为空，原因在 ``evidence`` / ``notes``
      - ``refused``：结构不支持，``fields`` 为空，原因在 ``notes[0]``
    """

    text: str
    status: str = "ambiguous"
    pattern: str = ""
    fields: Dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0
    gate: float = DEFAULT_THRESHOLD
    evidence: List[Evidence] = field(default_factory=list)
    unstated: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    @property
    def accepted(self) -> bool:
        return self.status == "accepted"

    @property
    def reason(self) -> str:
        """给人看的一句话。拒绝时必须是**可执行**的，而不是"解析失败"。"""
        if self.status == "accepted":
            return f"已解析为 {self.fields.get('type')}（置信度 {self.confidence:.2f}）"
        if self.status == "refused":
            return self.notes[0] if self.notes else "结构不支持"
        parts = [f"置信度 {self.confidence:.2f} < 阈值 {self.gate:.2f}，**不参与计算**，以 App 下单页为准"]
        parts.extend(self.notes)
        low = [e for e in self.evidence if e.weight < 0]
        if low:
            parts.append("扣分项：" + "、".join(f"{e.kind}（{e.note or e.text}）" for e in low))
        if self.unstated and not any(e.weight < 0 for e in self.evidence):
            # 只有"形状对、单纯缺字段"时，这句"补上即可纳入"才成立。
            # 一旦有实质扣分项，真正的拦路虎在扣分项里（且各自的 note 会说怎么解），
            # 再补一句"补上 stack_group 即可"就是把人往错的方向引。
            parts.append(f"无实质扣分项，补上「{'、'.join(self.unstated)}」后即可纳入计算")
        return "；".join(parts)

    def explanation(self) -> str:
        """逐条列出置信度是怎么来的 —— 用于 CLI 与人工复核。"""
        lines = [f"原文：{self.text}",
                 f"结论：{self.reason}"]
        if self.pattern:
            lines.append(f"命中模式：{self.pattern}")
        for e in self.evidence:
            sign = "+" if e.weight >= 0 else ""
            span = f"@{e.span[0]}:{e.span[1]}" if e.span[0] != e.span[1] else ""
            lines.append(f"  {sign}{e.weight:.2f}  {e.kind:<12}{e.text!r}{span}"
                         + (f"  —— {e.note}" if e.note else ""))
        for n in self.notes:
            lines.append(f"  注：{n}")
        if self.unstated:
            lines.append(f"文本未给出（不推断，交调用方）：{'、'.join(self.unstated)}")
        return "\n".join(lines)

    def to_coupon(self, cid: str, *, name: Optional[str] = None,
                  channel: str = "", stack_group: str = "") -> Coupon:
        """构造 ``Coupon``。只接受 ``accepted`` 的结果 —— 这是闸门的最后一道。"""
        if not self.accepted:
            raise ValueError(f"未通过置信度闸门，不能构造 Coupon：{self.reason}")
        row = dict(self.fields)
        row["id"] = cid
        row["name"] = name or self.text
        if channel:
            row["channel"] = channel
        if stack_group:
            row["stack_group"] = stack_group
        return Coupon.from_dict(row)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "text": self.text, "status": self.status, "pattern": self.pattern,
            "confidence": round(self.confidence, 3),
            "coupon": dict(self.fields) if self.fields else None,
            "unstated": list(self.unstated), "notes": list(self.notes),
            "evidence": [e.to_dict() for e in self.evidence],
        }


# --------------------------------------------------------------------------- #
# 打分
# --------------------------------------------------------------------------- #
#: 结构命中就给的基础分。不给高分，是因为"认得形状"离"算得对"还有距离。
_BASE = 0.60
#: 该券型能算所必需的**关键量**全部拿到
_W_COMPLETE = 0.20
#: 关键金额带了货币单位（元/块）—— 没单位时必须靠上下文猜，扣掉这分
_W_UNIT = 0.06
#: 商品名在菜单里**精确命中**（编码/全名/别名）
_W_ITEM = 0.14
#: 多商品券（买赠/组合）每多定位一个商品加的边际分
_W_ITEM_EXTRA = 0.06
#: 商品名定位上限（再多也不加，避免"名字多"冒充"更可信"）
_W_ITEM_CAP = 0.20
#: 渠道/身份词（平台券/红包/专属…）—— 说明它是一张**真实存在**的券，不是随手写的
_W_IDENTITY = 0.04
#: 折扣券明确写出封顶
_W_CAP = 0.06
#: 两处独立陈述互相印证（如「半价」与「5折」同时出现）
_W_CORROBORATE = 0.04
#: 券面自己写明「整单 / 全场」⇒ 判成整单折扣是**对的**，给一点分
_W_ORDER_DECLARED = 0.02
#: 折扣券判不出适用对象（没写适用商品、也没写「整单」）⇒ 只能按整单估，偏乐观
_W_SCOPE_UNKNOWN = -0.10
#: 模糊词每个扣的分与总上限
_W_FUZZY = -0.15
_FUZZY_CAP = 0.30
#: 折扣类券没写封顶：按不封顶估算会**高估**优惠，必须体现为扣分
_W_NO_CAP = -0.12
#: 券面限定适用门店（"限部分门店"）—— 直接影响"这张券在本店能不能用"
_W_SCOPE = -0.25
#: 两处陈述互相**矛盾**（如「半价」却是「8折」）—— 直接压到阈值以下
_W_CONFLICT = -0.35
#: 商品名定位失败（且提供了菜单）时的**硬上限**：这类结果一律不算
_HARD_CAP_UNRESOLVED = 0.40


def _clamp(x: float) -> float:
    return max(0.0, min(MAX_CONFIDENCE, x))


def _ev(kind: str, weight: float, text: str = "", span: Tuple[int, int] = (0, 0),
        note: str = "") -> Evidence:
    return Evidence(kind=kind, weight=weight, text=text, span=span, note=note)


# --------------------------------------------------------------------------- #
# 模式：每个函数返回 ``ParseOutcome``，没命中则返回 ``None``
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class _Ctx:
    """一次解析的上下文。

    ``body`` / ``full`` 分开是**故意的**：
    - 金额模式跑 ``body``（切掉「（限部分门店）」这类尾注，否则锚点对不上）
    - 结构不支持的判定与模糊词扣分跑 ``full``（尾注里藏的约束照样得拦住）
    """

    body: str
    full: str
    menu: MenuIndex


_RE_THRESHOLD = re.compile(
    r"满\s*(?P<th>" + _NUM + r")\s*(?:元|块钱|块)?\s*"
    r"(?:减|立减|直减|优惠|返)\s*(?P<red>" + _NUM + r")")
_RE_FREE_ORDER = re.compile(
    r"(?:无门槛|不限|任意金额)\s*(?:券|红包|立减金)?\s*(?:立减|减|优惠|抵扣)?\s*(?P<red>" + _NUM + r")")
_RE_ITEM_REDUCE = re.compile(
    r"(?P<name>[^，,、；;·・•\s]+?)\s*(?:单品)?\s*(?:立减|直降|直减|减)\s*(?P<red>" + _NUM + r")")
_RE_BUY_GET = re.compile(
    r"买\s*(?P<buy>[^，,、；;\s]+?)\s*(?:送|赠|赠品)\s*(?P<get>[^，,、；;\s]+?)"
    r"\s*(?:券|券一张|一张)?\s*[。.！!]?$")
_RE_ITEM_FREE = re.compile(
    r"^(?P<name>[^，,、；;\s]+?)\s*(?:免费|免单|0\s*元购|不要钱)"
    r"\s*(?:券|券一张|一张)?\s*[。.！!]?$")
_RE_REDEEM_A = re.compile(
    r"(?:积分)?(?:兑换|换购|换)\s*[·・•\s]*(?P<name>[^（(，,、；;·・•\s]+?)\s*"
    r"[（(]\s*(?P<pts>\d+)\s*积分")
_RE_REDEEM_B = re.compile(
    r"^(?P<name>[^（(，,、；;\s]+?)\s*[（(]\s*(?P<pts>\d+)\s*积分\s*[)）]")
_RE_BUNDLE = re.compile(
    r"(?P<amt>" + _NUM + r")\s*(?:元|块钱|块)\s*(?:买|购|换|享)\s*(?P<names>.+?)"
    r"\s*(?:券|套餐券)?\s*[。.！!]?$")
_RE_CAP = re.compile(
    r"(?:封顶|最高|上限|最多)\s*(?:可)?\s*(?:优惠|抵扣|减|立减)?\s*(?P<cap>" + _NUM + r")\s*(?:元|块钱|块)")
_RE_RATE = re.compile(r"(?P<rate>" + _NUM + r")\s*折")
_RE_HALF = re.compile(r"半价")
_RE_SIGN = re.compile(r"[，,、；;·・•\s]+")


def _money(raw: str, text: str, end: int) -> Tuple[int, bool]:
    """``(分值, 是否写了货币单位)``。"""
    unit = unit_after(text, end)
    yuan = cn_number(raw) if not re.fullmatch(r"\d+(?:\.\d+)?", raw) else float(raw)
    return to_cents(yuan), bool(unit)


def _split_names(blob: str) -> List[str]:
    """把券面里的一段商品名切成候选token（按中英分隔符 + 加号）。"""
    return [t for t in re.split(r"[+＋，,、；;·・•\s]+", blob) if t]


def _resolve_names(tokens: Sequence[str], menu: MenuIndex) -> Tuple[List[str], List[str], str]:
    """把候选 token 逐个定位为菜单编码。

    返回 ``(codes, unresolved, ambiguous_note)``。**长 token 优先**：
    「巨无霸套餐（含中薯+中可）」比「巨无霸」更具体，先试长的能避免把套餐券
    当成单品券。定位不出来的 token 不算错 —— 它可能只是营销词（"半价周末"）。
    """
    codes: List[str] = []
    unresolved: List[str] = []
    ambiguous: List[str] = []
    if not menu.enabled:
        return list(tokens), [], ""
    order = {it.code: i for i, it in enumerate(menu.items)}
    for tok in sorted(tokens, key=len, reverse=True):
        item, how = menu.resolve(tok)
        if item is not None:
            if item.code not in codes:
                codes.append(item.code)
        elif how.startswith("歧义"):
            ambiguous.append(f"{tok}{how}")
        else:
            unresolved.append(tok)
    # 按菜单顺序输出，保证同一券面每次得到**同一个元组**（否则长度排序会让
    # ``item_codes`` 的顺序随文案微调而变，回归对拍就没法做了）。
    codes.sort(key=lambda c: order.get(c, 1 << 30))
    return codes, unresolved, ("；".join(ambiguous))


def _identity_score(text: str) -> float:
    return _W_IDENTITY if any(w in text for w in IDENTITY_WORDS) else 0.0


def _fuzzy_evidence(full: str) -> List[Evidence]:
    """模糊限定词的扣分。**跑在券面全文上**，不跑在切掉尾部后的主体上。

    ``split_tail`` 会切掉「（限部分门店）」这类尾注，但这类限定恰恰是最致命的：
    它决定的不是"省多少"，而是"这张券在本店到底能不能用"。算错了不会得到一个
    偏差 5% 的结论，会得到一个**根本不存在**的券。所以切是切给**金额模式**用的，
    扣分必须看全文。
    """
    ev: List[Evidence] = []
    hits = [w for w in FUZZY_WORDS if w in full]
    if hits:
        ev.append(_ev("模糊词", max(_W_FUZZY * len(hits), -_FUZZY_CAP), "、".join(hits),
                      note=f"券面出现 {len(hits)} 个模糊限定词，完整优惠无法确定"))
    scope = [w for w in SCOPE_WORDS if w in full]
    if scope:
        ev.append(_ev("门店范围限定", _W_SCOPE, "、".join(scope),
                      note="券面限定适用门店 —— 本店是否在名单内无法从文本判断，"
                           "一律按**不可用**处理；确认本店在名单内可人工标注绕过本闸门"))
    return ev


def _unstated_of(fields: Dict[str, Any], *, text: str) -> List[str]:
    """列出**文本根本没写**、因此本模块拒绝推断的字段。

    只列**真会改变结论**的那几个。``discount`` 的 ``threshold_cents`` 就是典型：
    :func:`optimize_deals.single_coupon_discount` 会拿它做门槛判定，留 0 等于
    "无条件可用"，估算立刻从"打折"变成"必打折" —— 这个方向是**乐观**的。
    """
    out: List[str] = []
    if fields.get("type") in ("discount", "item_discount"):
        if not fields.get("cap"):
            out.append("封顶额（cap）")
        if "满" not in text:
            out.append("门槛（threshold）")
    out.append("stack_group（互斥组）")
    return out


def _finish(text: str, pattern: str, fields: Dict[str, Any], ev: List[Evidence],
            notes: List[str], unstated: List[str], *,
            item_unresolved: bool = False, gate: float = DEFAULT_THRESHOLD) -> ParseOutcome:
    # 把"权重为 0 又没说明"的条目清掉：留着只会让置信度构成看起来更"满"。
    # 但**带 note 的 0 分条目要留** —— 「商品定位 0.00：未在菜单命中」正是最该被看见的一条。
    ev = [e for e in ev if e.weight != 0.0 or (e.note and e.kind != "加分")]
    score = _clamp(sum(e.weight for e in ev))
    if item_unresolved and score > _HARD_CAP_UNRESOLVED:
        ev.append(_ev("硬上限", _HARD_CAP_UNRESOLVED - score,
                      note="指定商品定位不了 ⇒ 就算形状对，也不敢按这张券报价"))
        score = _HARD_CAP_UNRESOLVED
    return ParseOutcome(text=text, status="accepted", pattern=pattern, fields=fields,
                        confidence=score, gate=gate, evidence=ev, unstated=unstated,
                        notes=notes)


def _freq_note(text: str, fields: Dict[str, Any]) -> List[str]:
    """把「每单限用 1 张」这类频次限制记成 note（**不拒绝**）。

    理由：它不改变"这一单能省多少"，只限制"能用几次"。拒绝它等于扔掉一张
    本来算得对的券。真正需要它的地方是长期策略（可用次数是序贯决策的约束之一），
    所以额外给一个 ``_stack_hint`` 结构提示，让下游能用上。
    """
    m = RE_FREQ.search(text)
    if not m:
        return []
    raw, unit = m.group("n"), m.group("unit")
    times = _CN_TO_INT.get(raw, None)
    if times is None:
        times = int(raw) if raw.isdigit() else None
    notes = [f"券面注明使用频次限制（{m.group(0)}）—— 只限制能用几次，"
             "不影响本单金额；长期策略需把它算成可用次数上限"]
    if unit == "单" and times == 1:
        fields["_stack_hint"] = "single"
        notes.append("每单限用 1 张 ⇒ 下游应视该券为**不可叠加**")
    return notes


# -- 各模式 ---------------------------------------------------------------- #

def _p_refusal(ctx: "_Ctx") -> Optional[ParseOutcome]:
    text = ctx.full          # 结构不支持的判定要看**全文**：
    for rx, why in STRUCTURAL_REFUSALS:   # 尾部说明里也可能藏着"满2件""第二件半价"
        m = re.search(rx, text)
        if m:
            return ParseOutcome(text=text, status="refused", pattern="structural_refusal",
                                notes=[f"**结构不支持**：{why}"],
                                evidence=[_ev("拒绝", 0.0, m.group(0), m.span())])
    return None


def _p_buy_get(ctx: "_Ctx") -> Optional[ParseOutcome]:
    text, menu = ctx.body, ctx.menu
    m = _RE_BUY_GET.search(text)
    if not m:
        return None
    buy, get = m.group("buy"), m.group("get")
    ev = [_ev("结构", _BASE, "买…送…", m.span(), "买赠结构完整"),
          _ev("关键量齐全", _W_COMPLETE, note="买什么、送什么都写着")]
    b_item, b_how = menu.resolve(buy)
    g_item, g_how = menu.resolve(get)
    fields: Dict[str, Any] = {"type": "buy_get", "buy_code": b_item.code if b_item else buy,
                              "get_code": g_item.code if g_item else get}
    if b_item and g_item:
        ev.append(_ev("商品定位", min(_W_ITEM * 2, _W_ITEM_CAP),
                      f"{buy}→{b_item.code}；{get}→{g_item.code}",
                      note="两个商品都在本店菜单里"))
    else:
        n = int(bool(b_item)) + int(bool(g_item))
        if menu.enabled:
            ev.append(_ev("商品定位", -0.0, f"{buy}({b_how or '未提供'}) / {get}({g_how or '未提供'})",
                          note=f"仅 {n}/2 命中菜单"))
    notes = _fuzzy_evidence(ctx.full)
    ev.extend(notes)
    bad = menu.enabled and not (b_item and g_item)
    return _finish(text, "buy_get", fields, ev, [], _unstated_of(fields, text=text),
                   item_unresolved=bad)


def _p_redeem(ctx: "_Ctx") -> Optional[ParseOutcome]:
    text, menu = ctx.body, ctx.menu
    m = _RE_REDEEM_A.search(text) or _RE_REDEEM_B.search(text)
    if not m:
        return None
    name, pts = m.group("name"), int(m.group("pts"))
    item, how = menu.resolve(name)
    ev = [_ev("结构", _BASE, "积分兑换", m.span(), "积分商城券"),
          _ev("关键量齐全", _W_COMPLETE, note=f"兑换成本 {pts} 积分已写明")]
    fields: Dict[str, Any] = {"type": "item_free", "point_cost": pts}
    if item:
        fields["item_code"] = item.code
        ev.append(_ev("商品定位", _W_ITEM, f"{name}→{item.code}", note=f"命中方式：{how}"))
    else:
        fields["item_name"] = name
        ev.append(_ev("商品定位", 0.0, name, note=f"未在菜单命中（{how or '未提供菜单'}）"))
    ev.append(_ev("加分", _identity_score(text), note="渠道/身份词"))
    ev.extend(_fuzzy_evidence(ctx.full))
    return _finish(text, "redeem", fields, ev, [], _unstated_of(fields, text=text),
                   item_unresolved=menu.enabled and item is None)


def _p_item_free(ctx: "_Ctx") -> Optional[ParseOutcome]:
    text, menu = ctx.body, ctx.menu
    m = _RE_ITEM_FREE.match(text)
    if not m:
        return None
    name = m.group("name")
    item, how = menu.resolve(name)
    ev = [_ev("结构", _BASE, "商品免费", m.span(), "单品免单"),
          _ev("关键量齐全", _W_COMPLETE, note="免单对象就是商品本身，无金额待定项")]
    fields: Dict[str, Any] = {"type": "item_free"}
    if item:
        fields["item_code"] = item.code
        ev.append(_ev("商品定位", _W_ITEM, f"{name}→{item.code}", note=f"命中方式：{how}"))
    else:
        fields["item_name"] = name
        ev.append(_ev("商品定位", 0.0, name, note=f"未在菜单命中（{how or '未提供菜单'}）"))
    ev.append(_ev("加分", _identity_score(text), note="渠道/身份词"))
    ev.extend(_fuzzy_evidence(ctx.full))
    return _finish(text, "item_free", fields, ev, [], _unstated_of(fields, text=text),
                   item_unresolved=menu.enabled and item is None)


def _p_bundle(ctx: "_Ctx") -> Optional[ParseOutcome]:
    text, menu = ctx.body, ctx.menu
    m = _RE_BUNDLE.search(text)
    if not m:
        return None
    amt, blob = m.group("amt"), m.group("names")
    fixed, has_unit = _money(amt, text, m.end("amt"))
    tokens = _split_names(blob)
    codes, unresolved, ambiguous = _resolve_names(tokens, menu)
    ev = [_ev("结构", _BASE, "组合特价", m.span(), "X 元买指定组合"),
          _ev("关键量齐全", _W_COMPLETE, note="打包价已写明")]
    if has_unit:
        ev.append(_ev("金额带单位", _W_UNIT, unit_after(text, m.end("amt")), note="券面写出了单位，不用靠上下文猜"))
    ev.append(_ev("加分", _identity_score(text), note="渠道/身份词"))
    n = 0 if not menu.enabled else len(codes)
    ev.append(_ev("商品定位", min(_W_ITEM + _W_ITEM_EXTRA * max(n - 1, 0), _W_ITEM_CAP),
                  "、".join(codes) or blob,
                  note=f"组合内 {n} 个商品命中菜单" if menu.enabled else "未提供菜单，留待下游解析"))
    notes: List[str] = []
    if ambiguous:
        notes.append(f"商品名有歧义，未采用：{ambiguous}")
    if menu.enabled and n < 2:
        notes.append("组合特价至少要定位到 2 个商品才能算出效果")
    ev.extend(_fuzzy_evidence(ctx.full))
    bad = menu.enabled and n < 2
    fields: Dict[str, Any] = {"type": "bundle_price", "fixed": fixed / 100.0,
                              "item_codes": codes or list(tokens)}
    if not has_unit:
        # 下划线键 = 结构提示，``Coupon.from_dict`` 会忽略、``_brief`` 也不打印，
        # 但 ``--json`` 里看得见 —— "我们默认它是元"这件事不该被藏起来。
        fields["_assumed_unit"] = "元"
    return _finish(text, "bundle_price", fields, ev, notes,
                   _unstated_of(fields, text=text), item_unresolved=bad)


def _p_item_reduce(ctx: "_Ctx") -> Optional[ParseOutcome]:
    text, menu = ctx.body, ctx.menu
    m = _RE_ITEM_REDUCE.search(text)
    if not m:
        return None
    name, red = m.group("name"), m.group("red")
    # 兜底防线：满减由 _p_threshold 处理且优先级更高，走到这里还带"满"字说明
    # 是「满 5 元立减 3 元」这类**没有门槛数字在前**的怪写法，不猜。
    if "满" in name or name in ("无门槛", "不限"):
        return None
    reduce_cents, has_unit = _money(red, text, m.end("red"))
    item, how = menu.resolve(name)
    ev = [_ev("结构", _BASE, "单品立减", m.span(), "指定商品直降"),
          _ev("关键量齐全", _W_COMPLETE, note="商品与立减额都有")]
    if has_unit:
        ev.append(_ev("金额带单位", _W_UNIT, unit_after(text, m.end("red")), note="券面写出了单位，不用靠上下文猜"))
    fields: Dict[str, Any] = {"type": "item_reduce", "reduce": reduce_cents / 100.0}
    if item:
        fields["item_code"] = item.code
        ev.append(_ev("商品定位", _W_ITEM, f"{name}→{item.code}", note=f"命中方式：{how}"))
    else:
        fields["item_name"] = name
        ev.append(_ev("商品定位", 0.0, name, note=f"未在菜单命中（{how or '未提供菜单'}）"))
    ev.append(_ev("加分", _identity_score(text), note="渠道/身份词"))
    ev.extend(_fuzzy_evidence(ctx.full))
    return _finish(text, "item_reduce", fields, ev, [], _unstated_of(fields, text=text),
                   item_unresolved=menu.enabled and item is None)


def _p_threshold(ctx: "_Ctx") -> Optional[ParseOutcome]:
    text, menu = ctx.body, ctx.menu
    m = _RE_THRESHOLD.search(text)
    free = None if m else _RE_FREE_ORDER.search(text)
    if not m and not free:
        return None
    if m:
        th_raw, red_raw = m.group("th"), m.group("red")
        span = m.span()
        th_cents, th_unit = _money(th_raw, text, m.end("th"))
        red_cents, red_unit = _money(red_raw, text, m.end("red"))
        pattern = "threshold_reduce"
    else:
        red_raw = free.group("red")
        span = free.span()
        th_cents, th_unit = 0, True          # 「无门槛」本身就是明确陈述，不算缺单位
        red_cents, red_unit = _money(red_raw, text, free.end("red"))
        pattern = "threshold_reduce"
    ev = [_ev("结构", _BASE, pattern, span, "满减" if m else "无门槛减")]
    ev.append(_ev("关键量齐全", _W_COMPLETE,
                  note=("门槛与立减额都有" if m else "「无门槛」等价于门槛＝0，是明确陈述")))
    for ok, raw in ((th_unit, "门槛"), (red_unit, "立减额")):
        if ok:
            ev.append(_ev("金额带单位", _W_UNIT, raw,
                          note="券面写出了单位，不用靠上下文猜" if raw == "门槛"
                               else "券面写出了单位"))
    ev.append(_ev("加分", _identity_score(text), note="渠道/身份词"))
    ev.extend(_fuzzy_evidence(ctx.full))
    fields = {"type": "threshold_reduce",
              "threshold": th_cents / 100.0, "reduce": red_cents / 100.0}
    notes: List[str] = []
    if th_cents and red_cents >= th_cents:
        notes.append(f"立减额 {fmt_yuan(red_cents)} ≥ 门槛 {fmt_yuan(th_cents)}，"
                     "券面疑似抄错（不否决，但请复核）")
    return _finish(text, pattern, fields, ev, notes,
                   _unstated_of(fields, text=text))


def _p_discount(ctx: "_Ctx") -> Optional[ParseOutcome]:
    text, menu = ctx.body, ctx.menu
    mr = _RE_RATE.search(text)
    mh = _RE_HALF.search(text)
    if not mr and not mh:
        return None
    if mr:
        rate = cn_rate(mr.group("rate"))
        rate_txt = mr.group(0)
        rate_span = mr.span()
        prefix = text[:mr.start()]
    else:
        rate = 0.5
        rate_txt = "半价"
        rate_span = mh.span()
        prefix = text[:mh.start()]
    ev = [_ev("结构", _BASE, rate_txt, rate_span, "折扣")]
    ev.append(_ev("关键量齐全", _W_COMPLETE, note=f"折后比例 {rate:g}"))
    # 交叉印证：券面同时写「半价」和「5折」
    if mr and mh:
        if abs(cn_rate(mr.group("rate")) - 0.5) < 1e-9:
            ev.append(_ev("交叉印证", _W_CORROBORATE, "半价 + 5折",
                          note="两处独立陈述一致"))
        else:
            ev.append(_ev("交叉矛盾", _W_CONFLICT, f"半价 vs {rate_txt}",
                          note="两处陈述不一致，任何一侧都可能是抄错"))
    cap: Optional[int] = None
    mc = _RE_CAP.search(text)
    if mc:
        cap, _ = _money(mc.group("cap"), text, mc.end("cap"))
        ev.append(_ev("封顶明确", _W_CAP, mc.group(0), mc.span(),
                      note="封顶写明了 ⇒ 不需要假设"))
    ev.append(_ev("加分", _identity_score(text), note="渠道/身份词"))

    # 适用对象：在折扣标记之前找可定位的商品名
    tokens = _split_names(prefix)
    codes, _unresolved, ambiguous = _resolve_names(tokens, menu)
    declared_order = any(w in text for w in ORDER_WORDS)
    notes: List[str] = []
    # ``rate`` 是折扣券的**唯一必需量**，先落地再谈适用对象 —— 漏了它
    # ``Coupon.from_dict`` 会静默取默认值 0.0，等于"这张券没优惠"。
    fields: Dict[str, Any] = {"rate": rate}
    if menu.enabled and len(codes) == 1:
        fields["type"] = "item_discount"
        fields["item_code"] = codes[0]
        ev.append(_ev("商品定位", _W_ITEM, f"{tokens} → {codes[0]}",
                      note="唯一命中，判为**单品折扣**"))
    else:
        # 判成整单折扣是**乐观**方向（整单基数比单品大），所以要么券面自己写明了
        # 「整单」，要么就得扣分。没有菜单时**绝不能**把 token 当成商品编码 ——
        # 那会凭一个"全场"造出一张绑定商品的券。
        fields["type"] = "discount"
        if menu.enabled and len(codes) > 1:
            notes.append(f"折扣前出现多个可定位商品，无法确定适用哪一个：{codes}")
        if ambiguous:
            notes.append(f"商品名有歧义：{ambiguous}")
        if declared_order:
            ev.append(_ev("适用对象明确", _W_ORDER_DECLARED, "整单/全场",
                          note="券面写明是整单折扣 ⇒ 按整单估算是对的"))
        else:
            why = ("没有菜单可比对，无法判断是单品还是整单" if not menu.enabled
                   else "折扣前找不到可定位的商品")
            ev.append(_ev("适用对象不明", _W_SCOPE_UNKNOWN, prefix.strip() or text,
                          note=f"{why}；按整单估算会**高估**优惠"
                               + ("。提供 --menu 后即可按商品名判定" if not menu.enabled
                                  else "，或把券面里的商品名写完整")))
            if menu.enabled:
                notes.append(f"{why} ⇒ 只能按**整单折扣**估，若实际是单品则偏乐观")
            else:
                notes.append(f"{why} ⇒ 需要 --menu 才能判定适用范围")
    if cap:
        fields["cap"] = cap / 100.0
    else:
        ev.append(_ev("无封顶", _W_NO_CAP,
                      note="文本没写封顶：按不封顶估算会高估优惠。"
                           "补上封顶额即可重新纳入（例：「…5折（封顶15元）」）"))
    ev.extend(_fuzzy_evidence(ctx.full))
    return _finish(text, "discount", fields, ev, notes, _unstated_of(fields, text=text))


#: 顺序即优先级：先拒绝结构不支持的，再按**特异性从强到弱**。
#: ``_p_threshold`` 必须排在 ``_p_item_reduce`` 前面 —— 否则「无门槛减6」
#: 会被「X 减 Y」的单品模式抢走，把一张整单券读成"某商品的立减"。
_PATTERNS = (
    _p_refusal,
    _p_buy_get,
    _p_redeem,
    _p_item_free,
    _p_bundle,
    _p_threshold,
    _p_item_reduce,
    _p_discount,
)


# --------------------------------------------------------------------------- #
# 对外 API
# --------------------------------------------------------------------------- #
def parse_rule(text: str, *, pool: Optional[Iterable[Item]] = None,
               threshold: float = DEFAULT_THRESHOLD,
               menu: Optional[MenuIndex] = None) -> ParseOutcome:
    """解析一条券面文本。

    ``pool`` 给出本店菜单（``Item`` 序列）时，才能把商品名落成编码；
    不提供也能解析，只是商品名会留在 ``item_name`` 里等下游
    :func:`run_unified_live.resolve_coupon_rows` 处理 —— 并且**不会因此扣分**
    （因为无法判断对错时，扣分等于把"没给菜单"混进"名字不认得"）。
    """
    full = str(text or "").strip()
    idx = menu if menu is not None else MenuIndex.build(pool)
    if not full:
        return ParseOutcome(text=full, status="refused", pattern="empty", gate=threshold,
                            notes=["空白券面"])
    body, tail = split_tail(full)
    ctx = _Ctx(body=body, full=full, menu=idx)
    for fn in _PATTERNS:
        out = fn(ctx)
        if out is None:
            continue
        out.text = full
        if tail:
            out.notes.append(f"券面尾部附加说明：「{tail}」—— 不属于金额结构，"
                             "但属于适用范围/频次约束，已保留")
        if out.accepted:
            out.notes.extend(_freq_note(full, out.fields))
            if out.confidence < threshold:
                # 闸门：**不是**把分数改成低分，而是把结果降级为"不产出券"。
                # 保留 fields 会让调用方有机会"顺手用一下"，那这个闸门就白设了。
                out.status = "ambiguous"
                out.fields = {}
        out.gate = threshold
        return out
    return ParseOutcome(text=full, status="ambiguous", pattern="", gate=threshold,
                        notes=["没有命中任何已知券型。**不猜测** —— "
                               "请补充结构化字段，或直接以 App 下单页为准"],
                        evidence=[_ev("未命中", 0.0, full)])


def _slug_id(text: str) -> str:
    """从券面文本生成**确定性**的 id（同一文本在任何进程里都得到同一个 id）。"""
    slug = re.sub(r"[^\w\u4e00-\u9fff]+", "-", str(text)).strip("-")[:24]
    return f"RULE-{slug}" if slug else "RULE-?"


def parse_rows(rows: Sequence[Any], *, pool: Optional[Iterable[Item]] = None,
               threshold: float = DEFAULT_THRESHOLD
               ) -> Tuple[List[Dict[str, Any]], List[Tuple[str, ParseOutcome]]]:
    """批量解析券表。返回 ``(通过的记录, [(标签, 未通过的完整结果)])``。

    通过的记录 = ``{"id", "name", "_confidence", "_pattern", "_unstated"}`` + 解析字段，
    可直接接 :func:`run_unified_live.resolve_coupon_rows` 做商品名兜底解析，
    再送 ``Coupon.from_dict``。

    行里**已经显式给出的结构化字段一律保留**，解析只补空缺 ——
    人工标注的可信度高于文本推断，这个优先级不能倒。
    """
    menu = MenuIndex.build(pool)
    ok: List[Dict[str, Any]] = []
    bad: List[Tuple[str, ParseOutcome]] = []
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        text = raw.get("name") or raw.get("title") or raw.get("text") or ""
        label = str(raw.get("id") or text or "?")
        out = parse_rule(str(text), menu=menu, threshold=threshold)
        # 显式字段优先：把券表里已有的键盖回解析结果之上
        explicit = {k: v for k, v in raw.items()
                    if k not in ("name", "title", "text") and v not in (None, "", (), [])}
        if not out.accepted:
            bad.append((label, out))
            continue
        rec: Dict[str, Any] = {}
        # id 必须**可复现**：早先版本用 ``hash()`` 生成，跨进程会变
        # （PYTHONHASHSEED 一换，同一份券表就得到不同的 id，回归对拍直接失效）。
        rec["id"] = str(raw.get("id") or _slug_id(text))
        rec["name"] = str(text or rec["id"])
        rec.update(out.fields)
        rec.update(explicit)
        rec["_confidence"] = round(out.confidence, 3)
        rec["_pattern"] = out.pattern
        rec["_unstated"] = list(out.unstated)
        ok.append(rec)
    return ok, bad


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _load_menu(path: Optional[str]) -> List[Item]:
    if not path:
        return []
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    rows = data.get("items", data) if isinstance(data, dict) else data
    return [Item.from_dict(d) for d in rows]


def _load_coupon_texts(path: str) -> List[Dict[str, Any]]:
    """从券表里抽出**只用 ``name``** 的券行 —— 模拟"拿到一堆券面文字"。

    接受两种文件形状：渠道表（``{"channels": [{"coupons": [...]}]}``）与
    扁平券表（``{"coupons": [...]}`` / 裸数组）。
    """
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, dict) and "coupons" in data:
        rows = [data]
    else:
        rows = data.get("channels", data) if isinstance(data, dict) else data
    seen: Dict[str, Dict[str, Any]] = {}
    for ch in rows:
        if not isinstance(ch, dict):
            continue
        for c in ch.get("coupons", []) or []:
            if not isinstance(c, dict):
                continue
            key = str(c.get("name") or c.get("id"))
            if key not in seen:
                seen[key] = {"id": c.get("id"), "name": c.get("name")}
    return list(seen.values())


#: 字段 → 中文标签。表格里宁可写「门槛¥30.00」也不写 ``threshold=3000`` ——
#: 这个表格是给人复核券面的，不是给程序读的。
_FIELD_LABEL = {
    "threshold": "门槛", "reduce": "减", "item_code": "商品", "buy_code": "买",
    "get_code": "送", "rate": "折扣", "cap": "封顶", "point_cost": "积分",
    "fixed": "打包价", "item_codes": "组合", "item_name": "商品名(未落地)",
}
_MONEY_FIELDS = ("threshold", "reduce", "fixed", "cap")


def _brief(out: ParseOutcome) -> str:
    """一行说清解析结果（通过）或拒绝原因（未通过）。"""
    if not out.accepted:
        return out.reason
    parts: List[str] = []
    for key, val in out.fields.items():
        if key == "type" or key.startswith("_"):
            continue
        label = _FIELD_LABEL.get(key, key)
        if key in _MONEY_FIELDS:
            parts.append(f"{label}{fmt_yuan(to_cents(val))}")
        elif key == "rate":
            parts.append(f"{label}{val * 10:g}折")
        elif key == "item_codes":
            parts.append(f"{label}{'+'.join(str(v) for v in val)}")
        else:
            parts.append(f"{label}{val}")
    return " ".join(parts)


def _demo(pool: List[Item], texts: Sequence[str], threshold: float) -> int:
    menu = MenuIndex.build(pool)
    specs = [("状态", 8, "<"), ("", 2, "<"), ("置信", 6, ">"), ("", 2, "<"),
             ("类型", 17, "<"), ("", 2, "<"), ("券面", 34, "<"), ("", 2, "<"),
             ("解析结果", 0, "<")]
    rows: List[Tuple[str, ...]] = []
    rejected: List[ParseOutcome] = []
    for t in texts:
        out = parse_rule(t, menu=menu, threshold=threshold)
        mark = {"accepted": "通过", "ambiguous": "不敢算", "refused": "不支持"}[out.status]
        if not out.accepted:
            rejected.append(out)
        rows.append((mark, "", f"{out.confidence:.2f}", "",
                     out.fields.get("type", "—"), "", out.text, "",
                     _brief(out) if out.accepted else "—"))
    print(f"券面文本解析 · 菜单 {len(pool)} 项 · 置信度阈值 {threshold:.2f}")
    print(rule(table_width(specs)))
    for line in columns(specs, rows):
        print(line.rstrip())
    print(rule(table_width(specs)))
    print(f"{len(texts)} 条：通过 {len(texts) - len(rejected)}，未通过 {len(rejected)}"
          "（未通过的不参与计算，以 App 下单页为准）")
    if rejected:
        # 拒绝理由**不塞进表格** —— 表格单元格放不下，塞进去只会挤坏其他列。
        # 单独列在下面，一条一行，完整显示。
        print()
        print("未通过明细：")
        for out in rejected:
            print(f"  · {out.text}")
            print(f"    {out.reason}")
    return len(rejected)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="券面文本 → 结构化约束（带置信度闸门）")
    ap.add_argument("--text", action="append", default=[], help="单条券面文本，可重复")
    ap.add_argument("--coupons", help="渠道/券表 JSON，抽取其中券面文本批量解析")
    ap.add_argument("--menu", help="菜单 JSON（提供才能把商品名落地成编码）")
    ap.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD,
                    help=f"置信度阈值（默认 {DEFAULT_THRESHOLD}）")
    ap.add_argument("--explain", action="store_true", help="逐条列出置信度构成")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args(argv)

    pool = _load_menu(args.menu)
    menu = MenuIndex.build(pool)
    texts = list(args.text)
    if args.coupons:
        texts.extend(r["name"] for r in _load_coupon_texts(args.coupons))
    if not texts:
        ap.error("请用 --text 给定券面文本，或用 --coupons 指定券表")

    if args.json:
        payload = [parse_rule(t, menu=menu, threshold=args.threshold).to_dict() for t in texts]
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if args.explain:
        for t in texts:
            print(parse_rule(t, menu=menu, threshold=args.threshold).explanation())
            print(rule(72))
        return 0

    return _demo(pool, texts, args.threshold)


if __name__ == "__main__":
    sys.exit(main())
