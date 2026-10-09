#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""麦麦积分精算师 —— 麦当劳 MCP 轻量客户端（Streamable HTTP）。

用途
----
让本 Skill 的脚本层可以**直接**调用麦当劳中国 MCP Server：
先 ``initialize`` 握手，再 ``tools/list`` / ``tools/call`` 调用具体工具。
三个引擎共用本客户端 —— ``optimize_deals.py``（点餐组合）、
``points_engine.py``（积分价值）、``channel_planner.py``（跨渠道到手成本）。

取数有两条路径，**优先走结构化那条**：

- :meth:`MCDMcpClient.call_structured` —— 优先读响应的 ``structuredContent``
  （服务端预解析好的 JSON），缺失时回退到文本提取。**日常取数用这个。**
- :meth:`MCDMcpClient.call_json` —— 从「说明文字 + JSON + 尾部指令」混合文本里
  枚举 ``{`` 起点取跨度最大的对象。保留用于兼容不返回 ``structuredContent`` 的 Tool。

约束
----
- 传输协议：Streamable HTTP（同时兼容 JSON 与 SSE 两种响应体）。
- 鉴权：请求头 ``Authorization: Bearer <MCD_MCP_TOKEN>``。
- 限流：每 Token 每分钟 600 次；本客户端对 ``429`` 自动退避重试。

安全
----
Token **只从环境变量读取**，绝不写入代码或提交进仓库。

用法
----
    export MCD_MCP_TOKEN=xxxxx            # bash
    setx MCD_MCP_TOKEN xxxxx              # Windows（需重开终端）

    python mcd_mcp.py list-tools
    python mcd_mcp.py call query-my-account '{}'
    python mcd_mcp.py call mall-points-products '{}'
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

DEFAULT_URL = "https://mcp.mcd.cn"
PROTOCOL_VERSION = "2025-06-18"


class MCPError(RuntimeError):
    pass


class MCDMcpClient:
    """极简 MCP Streamable HTTP 客户端。"""

    def __init__(self, url: str = DEFAULT_URL, token: Optional[str] = None, timeout: int = 30, max_retries: int = 3):
        self.url = url.rstrip("/")
        self.token = token or os.environ.get("MCD_MCP_TOKEN", "").strip()
        if not self.token:
            raise MCPError("未找到 MCD_MCP_TOKEN，请先设置环境变量（Token 请勿写入代码或提交仓库）。")
        self.timeout = timeout
        self.max_retries = max_retries
        self._session_id: Optional[str] = None
        self._id = 0

    # -- 传输层 ------------------------------------------------------------ #
    def _next_id(self) -> int:
        self._id += 1
        return self._id

    def _post(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {self.token}",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id

        body = json.dumps(payload).encode("utf-8")
        last_err: Optional[Exception] = None

        for attempt in range(self.max_retries):
            req = urllib.request.Request(self.url, data=body, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    sid = resp.headers.get("Mcp-Session-Id")
                    if sid:
                        self._session_id = sid
                    raw = resp.read().decode("utf-8", errors="replace")
                    ctype = resp.headers.get("Content-Type", "")
                result = self._parse_body(raw, ctype)
                if "error" in result:
                    raise MCPError(f"MCP 返回错误: {result['error']}")
                return result
            except urllib.error.HTTPError as exc:
                if exc.code == 429:  # 限流：指数退避
                    time.sleep(2 ** attempt)
                    last_err = exc
                    continue
                detail = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
                raise MCPError(f"HTTP {exc.code}: {detail}") from exc
            except urllib.error.URLError as exc:
                last_err = exc
                time.sleep(1 + attempt)

        raise MCPError(f"请求失败（已重试 {self.max_retries} 次）: {last_err}")

    @staticmethod
    def _parse_body(raw: str, ctype: str) -> Dict[str, Any]:
        if "text/event-stream" in ctype or raw.lstrip().startswith("event:") or "\ndata:" in raw:
            for line in raw.splitlines():
                line = line.strip()
                if line.startswith("data:"):
                    chunk = line[5:].strip()
                    if chunk and chunk != "[DONE]":
                        try:
                            return json.loads(chunk)
                        except json.JSONDecodeError:
                            continue
            raise MCPError(f"无法从 SSE 响应中解析 JSON: {raw[:200]!r}")
        return json.loads(raw)

    # -- MCP 方法 ---------------------------------------------------------- #
    def initialize(self) -> Dict[str, Any]:
        return self._post({
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "mcd-points-actuary", "version": "3.0.0"},
            },
        })

    def list_tools(self) -> List[Dict[str, Any]]:
        res = self._post({"jsonrpc": "2.0", "id": self._next_id(), "method": "tools/list", "params": {}})
        return res.get("result", {}).get("tools", [])

    def call_tool(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> Any:
        res = self._post({
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        })
        return res.get("result")

    def call_json(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> Any:
        """调用 Tool 并直接返回解析后的业务 JSON。

        麦当劳 MCP 的多数 Tool 返回的是**「说明文字 + JSON」混合文本**，
        本方法负责把其中的顶层 JSON 对象提取出来。
        """
        result = self.call_tool(name, arguments)
        return extract_json(_text_of(result))

    def call_structured(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> Any:
        """调用 Tool 并返回**结构化业务数据**（首选路径）。

        麦当劳 MCP 的 ``tools/call`` 结果里通常已含 ``structuredContent`` ——
        服务端预解析好的 JSON，比从说明文字里正则捞 JSON 稳健得多。
        旧版本或不返回该字段的 Tool 自动回退到 :meth:`call_json` 的文本提取。

        注意：``structuredContent`` 存在时其内容就**是**业务 JSON（顶层即
        ``{"success":…, "data":…}``），无需再取 ``.data``。
        """
        result = self.call_tool(name, arguments)
        if isinstance(result, dict) and result.get("structuredContent") is not None:
            return result["structuredContent"]
        return extract_json(_text_of(result))


def _text_of(result: Any) -> str:
    """从 MCP tools/call 结果中取出文本内容。"""
    if isinstance(result, dict) and isinstance(result.get("content"), list):
        parts = [c.get("text", "") for c in result["content"] if isinstance(c, dict)]
        return "\n".join(p for p in parts if p)
    if isinstance(result, str):
        return result
    return json.dumps(result, ensure_ascii=False)


def extract_json(text: str) -> Any:
    """从「说明文字 + JSON + 尾部指令」混合文本中提取顶层 JSON。

    麦当劳 MCP 的响应格式并不固定：JSON 可能位于文本中部，前后都有说明性文字。
    策略：枚举每个 ``{`` 起点做 ``raw_decode``，取**解析跨度最长**的对象 —— 顶层对象
    必然包含所有子对象，因此跨度最大。
    """
    text = text.strip()
    decoder = json.JSONDecoder()
    best_obj: Any = None
    best_span = -1
    for match in re.finditer(r"\{", text):
        start = match.start()
        try:
            obj, end = decoder.raw_decode(text[start:])
        except (json.JSONDecodeError, ValueError):
            continue
        if end > best_span:
            best_span, best_obj = end, obj
    if best_span < 0:
        raise MCPError(f"响应文本中未找到合法 JSON（前 200 字符：{text[:200]!r}）")
    return best_obj


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="麦当劳 MCP 轻量客户端")
    p.add_argument("--url", default=DEFAULT_URL)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list-tools", help="列出 MCP Server 支持的全部 Tool")
    c = sub.add_parser("call", help="调用指定 Tool")
    c.add_argument("name")
    c.add_argument("args", nargs="?", default="{}", help="JSON 格式的 arguments")

    args = p.parse_args(argv)
    client = MCDMcpClient(url=args.url)
    info = client.initialize()
    print(f"[connected] {json.dumps(info.get('result', {}).get('serverInfo', {}), ensure_ascii=False)}", file=sys.stderr)

    if args.cmd == "list-tools":
        for t in client.list_tools():
            print(f"- {t.get('name')}: {t.get('description', '')}")
        return 0

    if args.cmd == "call":
        try:
            payload = json.loads(args.args)
        except json.JSONDecodeError as exc:
            print(f"arguments 不是合法 JSON: {exc}", file=sys.stderr)
            return 2
        print(json.dumps(client.call_tool(args.name, payload), ensure_ascii=False, indent=2))
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
