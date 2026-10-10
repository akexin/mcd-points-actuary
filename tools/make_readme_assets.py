#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从**真实运行输出**生成 README 配图（架构图 / 运行截图 / 测试截图）。

为什么要写生成脚本，而不是贴一张裁剪的屏幕截图？
--------------------------------------------------
README 里的图最怕"和代码对不上"。本脚本的输入是 ``docs/captures/*.txt`` ——
由真机运行**原样重定向**得到的文本（见文件头的 ``$`` 命令行）。
任何人可以：

1. 重新跑一遍那条命令，``diff`` 一下 capture 文件，确认图里的每个字都来自真实输出；
2. 重新执行本脚本，得到与仓库里**逐字节一致**的图片。

也就是说：图不是"画"出来的，而是**渲染**出来的。改一个数字，图就会跟着变。

依赖 Pillow（仅生成图片时需要，运行本 Skill 本身不需要任何第三方库）。
字体使用 Windows 自带的等宽黑体 ``simhei.ttf`` 与雅黑 ``msyh.ttc``。

排版上踩过两个坑，都写在这里免得下次再犯：

1. **字号必须是偶数。** SimHei 的汉字步进 = 半角的 **恰好 2 倍**，但只在偶数号
   （12/14/16/18/20…）成立；15 号会得到 8 : 15（1.875），表格列就会逐行漂移。
2. **有 4 个字形 SimHei 里没有**：``¥ ➜ ⚠ −``。缺字时 FreeType 会画 .notdef（豆腐块）。
   这里逐字检测（把字形画出来和 U+E000 的 .notdef 位图比对，而不是比步进宽度 ——
   比宽度会误判），命中缺字就换 ``seguisym.ttf`` 兜底。

用法
----
    pip install pillow
    python tools/make_readme_assets.py
"""
from __future__ import annotations

import os
import sys
import unicodedata

from PIL import Image, ImageDraw, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CAPTURES = os.path.join(ROOT, "docs", "captures")
ASSETS = os.path.join(ROOT, "assets")

#: 渲染倍率：先按 2 倍画再缩小，边缘才干净（Pillow 不做几何抗锯齿）
S = 2

FONT_MONO = "C:/Windows/Fonts/simhei.ttf"     # 等宽：汉字步进 = 半角的 2 倍（偶数号）
FONT_SYM = "C:/Windows/Fonts/seguisym.ttf"    # 仅用于 simhei 缺失的 ¥ ➜ ⚠ −
FONT_UI = "C:/Windows/Fonts/msyh.ttc"         # 架构图用（比例字体更耐看）

# ---- 终端配色 -------------------------------------------------------------- #
BG = "#0B0E14"
BAR = "#161B22"
BORDER = "#30363D"
TEXT = "#C9D1D9"
DIM = "#7D8590"
OK = "#3FB950"
ACCENT = "#58A6FF"

# ---- 架构图配色（浅色，双主题下都能看清） ---------------------------------- #
A_BG = "#FFFFFF"
A_LINE = "#E2E8F0"
A_TITLE = "#0F172A"
A_MUTED = "#64748B"
A_DATA_BG, A_DATA_ED, A_DATA_TX = "#EEF2FF", "#C7D2FE", "#312E81"
A_ENG_BG, A_ENG_ED, A_ENG_TX = "#ECFDF5", "#A7F3D0", "#065F46"
A_OUT_BG, A_OUT_ED, A_OUT_TX = "#F1F5F9", "#CBD5E1", "#0F172A"
A_FORM_BG, A_FORM_TX = "#0F172A", "#FFFFFF"
A_ARROW = "#94A3B8"


def font(path: str, size: int, bold: bool = False):
    """bold 走 msyhbd.ttc（雅黑粗体）；其余用给定的 .ttf/.ttc index=0。"""
    if bold and path == FONT_UI:
        path = "C:/Windows/Fonts/msyhbd.ttc"
    return ImageFont.truetype(path, size * S)


# --------------------------------------------------------------------------- #
# 终端截图
# --------------------------------------------------------------------------- #
def dw(s: str) -> int:
    """显示列宽：汉字/全角 2 列，其余 1 列。与 ``run_unified_live.dwidth`` 同一口径。"""
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in s)


_coverage: dict = {}


def glyph_missing(path: str, ch: str) -> bool:
    """判断字体里到底有没有这个字形。

    做法是**位图比对**：分别把 ``ch`` 和私用区 ``U+E000``（必然落到 .notdef）
    画到同一张小图上，像素一模一样就说明 ``ch`` 也是豆腐块。
    早期版本用「字形步进宽度 == PUA 的步进」来判断，会误判（雅黑的 ``⚠``
    被误报为缺字），所以改成了现在这个直接看笔画的方式。
    """
    state = _coverage.get(path)
    if state is None:
        f = ImageFont.truetype(path, 20)

        def sig(c: str) -> bytes:
            img = Image.new("L", (80, 80), 0)
            ImageDraw.Draw(img).text((8, 8), c, font=f, fill=255)
            return img.tobytes()

        state = {"notdef": sig("\ue000"), "sig": sig, "cache": {}}
        _coverage[path] = state
    if ch not in state["cache"]:
        state["cache"][ch] = state["sig"](ch) == state["notdef"]
    return state["cache"][ch]


def _line_color(line: str) -> str:
    """按行首特征给一点颜色区分，**不改变任何字符**。"""
    s = line.strip()
    if s == "OK" or s.startswith("Ran ") or s.endswith("... ok"):
        return OK
    if s.startswith("$") or s.startswith("["):
        return ACCENT
    if set(s) <= set("=- ") and s:
        return DIM
    if s.startswith("本次联调") or s.startswith("到手价由"):
        return DIM
    return TEXT


def render_terminal(lines, out_path: str, title: str, size: int = 16,
                    pad: int = 18, lh: int = 26) -> tuple:
    """把文本行渲染成一张终端风格的 PNG。

    逐**字符**摆放到固定列网格上，而不是整行丢给 ``draw.text`` ——
    这样每个字符的落点由「第几列」决定，与字体自身的步进无关，
    表格列永远不会漂移。缺字（``¥ ➜ ⚠ −``）自动走 Segoe UI Symbol 兜底。
    """
    if size % 2:
        raise ValueError("字号必须是偶数：SimHei 的汉字步进只在偶数号下等于半角的 2 倍")

    f = ImageFont.truetype(FONT_MONO, size * S)     # 绘制（2 倍）
    fsym = ImageFont.truetype(FONT_SYM, size * S)   # 缺字兜底（2 倍）
    fm = ImageFont.truetype(FONT_MONO, size)        # 1 倍，仅用于量宽
    cell = fm.getlength("中") / 2.0                 # 一个半角列的宽度
    if abs(fm.getlength("A") - cell) > 1e-6:
        raise ValueError(f"{FONT_MONO} 在 {size} 号下不是 2:1 的等宽（半角 "
                         f"{fm.getlength('A')} vs {cell}）")

    cols = max((dw(ln) for ln in lines), default=0)
    bar_h, line_h = 36, lh
    cw = int(round(cols * cell)) + pad * 2          # 以下均为「1 倍」坐标
    ch_ = bar_h + pad + len(lines) * line_h + pad

    img = Image.new("RGB", (cw * S, ch_ * S), BG)
    d = ImageDraw.Draw(img)

    # 标题栏
    d.rectangle([0, 0, cw * S, bar_h * S], fill=BAR)
    d.line([0, bar_h * S, cw * S, bar_h * S], fill=BORDER, width=S)
    for i, col in enumerate(("#FF5F57", "#FEBC2E", "#28C840")):
        cx = (pad + 6 + i * 18) * S
        d.ellipse([cx - 6 * S, (bar_h // 2 - 6) * S, cx + 6 * S, (bar_h // 2 + 6) * S], fill=col)
    ft = font(FONT_MONO, 13)
    d.text((pad + 76 * S, bar_h * S // 2), title, font=ft, fill=DIM, anchor="lm")

    ascent, descent = fm.getmetrics()
    top = bar_h + pad
    for ln in lines:
        colour = _line_color(ln)
        base = top + (line_h - (ascent + descent)) / 2.0 + ascent   # 基线
        col = 0
        for c in ln:
            if c == " ":
                col += 1
                continue
            wcell = 2 if unicodedata.east_asian_width(c) in ("W", "F") else 1
            x = pad + col * cell
            if glyph_missing(FONT_MONO, c):
                # 兜底字体步进不同 → 让它居中占满自己的格子，后面的列不受影响
                d.text(((x + wcell * cell / 2) * S, base * S), c, font=fsym,
                       fill=colour, anchor="ms")
            else:
                d.text((x * S, base * S), c, font=f, fill=colour, anchor="ls")
            col += wcell
        top += line_h

    d.rectangle([0, 0, cw * S - 1, ch_ * S - 1], outline=BORDER, width=S)
    img.resize((cw, ch_), Image.LANCZOS).save(out_path)
    return cw, ch_


def render_tests_tail(path: str, out_path: str, tail: int = 30) -> tuple:
    lines = open(path, encoding="utf-8").read().splitlines()
    lines = [ln for ln in lines]
    # 去掉尾部的空行后再取尾部连续片段（保持与真实输出逐字一致）
    while lines and not lines[-1].strip():
        lines.pop()
    return render_terminal(lines[-tail:], out_path,
                           "MINGW64:/c/Users/Administrator/WorkBuddy/2026-10-09-14-55-25"
                           "/mcd-points-actuary   python -m unittest discover -s tests -v",
                           size=16, lh=25)


def render_live(path: str, out_path: str, title: str, start: int, end=None) -> tuple:
    """渲染运行输出的一段**连续原文**（不做任何裁剪或改写）。"""
    lines = open(path, encoding="utf-8").read().splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    got = lines[start:end]
    while got and not got[-1].strip():
        got.pop()
    return render_terminal(got, out_path, title, size=16, lh=26)


LIVE_CMD = ("MINGW64:/c/Users/Administrator/WorkBuddy/2026-10-09-14-55-25/mcd-points-actuary"
            "   python scripts/run_unified_live.py --store 1990346 --items 1100 4810 9900008751")


def render_rule_parse(path: str, out_path: str, title: str) -> tuple:
    """渲染券面解析的表（到「19 条：通过 14」为止）。

    只截到汇总行，**不截「未通过明细」**：那些拒绝理由单行就有 180+ 显示列，
    放进图里会把整张图拉宽三倍，反而看不清表格本身。
    """
    lines = open(path, encoding="utf-8").read().splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    end = next((i for i, ln in enumerate(lines) if ln.startswith("19 条：")), len(lines) - 1)
    return render_terminal(lines[:end + 1], out_path, title, size=16, lh=24)


def live_sections(path: str):
    """按 '====' 三行块切分运行输出，返回 {序号: (起, 止)}。

    这样"取哪一段"由**输出自身的结构**决定，而不是写死行号 ——
    脚本输出变了，切片也会跟着走，不会悄悄错位。
    """
    lines = open(path, encoding="utf-8").read().splitlines()
    sep = [i for i, l in enumerate(lines) if l.startswith("===")]
    bounds = {}
    for k in range(0, len(sep) - 1, 2):
        title = lines[sep[k] + 1].strip()
        end = sep[k + 2] if k + 2 < len(sep) else len(lines)
        bounds[title[0]] = (sep[k], end)
    return lines, bounds


# --------------------------------------------------------------------------- #
# 架构图
# --------------------------------------------------------------------------- #
class Canvas:
    def __init__(self, w: int, h: int, radius: int = 14):
        self.w, self.h = w, h
        self.img = Image.new("RGB", (w * S, h * S), A_BG)
        self.d = ImageDraw.Draw(self.img)
        self.d.rounded_rectangle([0, 0, w * S - 1, h * S - 1], radius=radius * S,
                                 fill=A_BG, outline=A_LINE, width=S)

    def text(self, x, y, s, size=14, color=A_TITLE, anchor="la", bold=False):
        f = font(FONT_UI, size, bold)
        self.d.text((x * S, y * S), s, font=f, fill=color, anchor=anchor)

    def box(self, x, y, w, h, title, lines, bg, edge, tx, r=10):
        self.d.rounded_rectangle([x * S, y * S, (x + w) * S, (y + h) * S], radius=r * S,
                                 fill=bg, outline=edge, width=S)
        self.text(x + 14, y + h / 2 - (0 if not lines else 11), title, 15, tx)
        for i, ln in enumerate(lines):
            self.text(x + 14, y + h / 2 + 9 + i * 17, ln, 12, A_MUTED)

    def band(self, x, y, w, h, text_, size=19, bg=A_FORM_BG, tx=A_FORM_TX, r=10):
        self.d.rounded_rectangle([x * S, y * S, (x + w) * S, (y + h) * S], radius=r * S,
                                 fill=bg, outline=bg, width=S)
        self.text(x + w / 2, y + h / 2, text_, size, tx, anchor="mm")

    def _head(self, x, y, color=A_ARROW):
        self.d.polygon([((x - 5) * S, (y - 8) * S), ((x + 5) * S, (y - 8) * S),
                        (x * S, y * S)], fill=color)

    def varrow(self, x, y0, y1, color=A_ARROW):
        self.d.line([x * S, y0 * S, x * S, (y1 - 7) * S], fill=color, width=int(1.6 * S))
        self._head(x, y1, color)

    def elbow(self, x, y0, xt, ym, y_tip, color=A_ARROW):
        """竖 → 横到目标列 → 竖，末端加箭头。"""
        w = int(1.6 * S)
        self.d.line([x * S, y0 * S, x * S, ym * S], fill=color, width=w)
        self.d.line([x * S, ym * S, xt * S, ym * S], fill=color, width=w)
        self.d.line([xt * S, ym * S, xt * S, (y_tip - 7) * S], fill=color, width=w)
        self._head(xt, y_tip, color)

    def save(self, path):
        self.img.resize((self.w, self.h), Image.LANCZOS).save(path)


def render_architecture(out_path: str) -> tuple:
    W, M = 1212, 32
    inner = W - M * 2
    g = 24
    cw = (inner - g * 2) // 3

    c = Canvas(W, 768)
    c.text(M, 26, "麦麦积分精算师 v3.3 · 统一到手成本引擎", 24, A_TITLE, bold=True)
    c.text(M, 62, "把「点餐组合 / 第三方平台券 / 积分机会成本」三层压到同一个目标函数上",
           14, A_MUTED)

    y1 = 108
    c.text(M, y1 - 24, "① 数据来源", 13, A_MUTED)
    c.box(M, y1, cw, 78, "麦当劳 MCP（7 次只读）",
          ["门店 · 到店/外送菜单 · 自有券", "积分账户 · 积分商城"], A_DATA_BG, A_DATA_ED, A_DATA_TX)
    c.box(M + cw + g, y1, cw, 78, "第三方平台券面",
          ["美团 / 饿了么 费率与券", "用户提供，MCP 拿不到"], A_DATA_BG, A_DATA_ED, A_DATA_TX)
    c.box(M + (cw + g) * 2, y1, cw, 78, "积分标的映射表",
          ["积分商城 SKU ≠ 菜单 SKU", "人工逐条核验，不猜"], A_DATA_BG, A_DATA_ED, A_DATA_TX)

    # ② L-1 接入层：券面文本 → 结构化约束（评审项⑦）。它夹在"数据来源"与"本地求解"之间，
    # 因为它的职责就是**卡在入口** —— 转化不了的券不放行，而不是带着猜测往下走。
    mid = M + inner / 2
    yp = 240
    for i in range(3):
        c.elbow(M + cw / 2 + i * (cw + g), y1 + 78, mid, yp - 34, yp - 18)
    c.text(M, yp - 24, "② L-1 接入层（本地 · 零网络）", 13, A_MUTED)
    c.band(M, yp, inner, 62,
           "rule_parser.py：券面文本 → 结构化约束 ｜ 置信度 < 0.75 ⇒ 不产出券", 17)

    y2 = 372
    c.text(M, y2 - 24, "③ 本地求解（零网络 · 不占 MCP 配额）", 13, A_MUTED)
    c.varrow(mid, yp + 62, y2 - 18)
    c.box(M, y2, cw, 78, "optimize_deals.py",
          ["统一券模型 · 7 种券型", "券语义的唯一实现"], A_ENG_BG, A_ENG_ED, A_ENG_TX)
    c.box(M + cw + g, y2, cw, 78, "points_engine.py",
          ["价值矩阵 → 影子价格 ypp", "抽奖 EV · 背包 · 止损"], A_ENG_BG, A_ENG_ED, A_ENG_TX)
    c.box(M + (cw + g) * 2, y2, cw, 78, "channel_planner.py",
          ["券叠加 + 渠道比价", "按净收益择优"], A_ENG_BG, A_ENG_ED, A_ENG_TX)

    y3 = 510
    c.text(M, y3 - 24, "④ 统一目标函数", 13, A_MUTED)
    c.varrow(mid, y2 + 78, y3 - 18)
    c.band(M, y3, inner, 62, "到手成本 = 现金支出 + 积分消耗 × 影子价格（元/积分）", 20)

    y4 = 616
    c.text(M, y4 - 24, "⑤ 输出", 13, A_MUTED)
    c.varrow(mid, y3 + 62, y4 - 18)
    c.box(M, y4, cw, 78, "渠道到手价对比",
          ["券后 / 配送 / 打包 / 现金", "积分成本 / 到手成本"], A_OUT_BG, A_OUT_ED, A_OUT_TX)
    c.box(M + cw + g, y4, cw, 78, "积分券打平 ypp",
          ["抵扣 ÷ 积分消耗", "对比影子价格 → 换不换"], A_OUT_BG, A_OUT_ED, A_OUT_TX)
    c.box(M + (cw + g) * 2, y4, cw, 78, "最优组合与券",
          ["全网最低到手价", "含跨组叠加明细"], A_OUT_BG, A_OUT_ED, A_OUT_TX)

    c.text(M, 726, "实测（2026-10-09，storeCode 1990346，124 项菜单）："
                   "影子价格 0.1380 元/积分；同一份订单 美团 ¥27.75 / 饿了么 ¥39.94 / 到店 ¥50.50",
           12, A_MUTED)
    c.save(out_path)
    return c.w, c.h


# --------------------------------------------------------------------------- #
def main() -> int:
    os.makedirs(ASSETS, exist_ok=True)
    cap_live = os.path.join(CAPTURES, "unified-live.txt")
    lines, sec = live_sections(cap_live)

    jobs = [
        ("architecture.png",
         lambda p: render_architecture(p)),
        ("screenshot-points.png",                 # ① 取数日志 + ①积分定价 + ②积分券打平线
         lambda p: render_live(cap_live, p, LIVE_CMD, 0, sec["③"][0])),
        ("screenshot-channels.png",               # ③ 渠道与券池 + ④ 全网最低到手价
         lambda p: render_live(cap_live, p, LIVE_CMD, sec["③"][0])),
        ("screenshot-tests.png",
         lambda p: render_tests_tail(os.path.join(CAPTURES, "tests-verbose.txt"), p)),
        ("screenshot-rule-parse.png",
         lambda p: render_rule_parse(
             os.path.join(CAPTURES, "rule-parse.txt"), p,
             "python scripts/rule_parser.py --coupons data/sample_coupon_text.json"
             " --menu data/sample_menu.json")),
    ]

    for name, fn in jobs:
        out = os.path.join(ASSETS, name)
        w, h = fn(out)
        print(f"  assets/{name:<24} {w:>5}×{h:<5} {os.path.getsize(out)/1024:>7.1f} KB")
    print(f"\n  切片依据：unified-live.txt 共 {len(lines)} 行，"
          f"分节边界 {[(k, v[0]) for k, v in sec.items()]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
