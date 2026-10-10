#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MCP 调用缓存 —— 按 Tool 的**变质速度**分档 TTL，把结构性重复取数留在本地。

为什么值得做
------------
本项目一次完整联调要 7 次只读 MCP 调用，官方限流 600 次/分钟，所以"打不满"。
真正的问题是**同一个人一天要跑很多次**：改个参数、换个购物车、只想看看券池，
每次都要重新拉门店、菜单、券、积分商城 —— 而这些数据的变质速度差了两个数量级：

======================  ========  ==============================================
Tool                     TTL       为什么是这个量级
======================  ========  ==============================================
``query-meals``          30 分钟   菜单位价与 SKU 极少变
``mall-points-products``  5 分钟   商城上下架 / 限购轮换比菜单勤
``query-store-coupons``   5 分钟   门店券池随活动走
``query-my-coupons``      5 分钟   自己的券：领了、用了都会变
``available-coupons``     5 分钟   可领券池
``query-nearby-stores``  10 分钟   门店列表基本不动
``query-my-account``      1 分钟   **积分余额**：下单立刻扣，缓存久了会算错
======================  ========  ==============================================

所以**不该用一个 TTL 管全部**。最后一行是这个模块的风险点，所以积分账户 TTL 最短
（60 秒），并且 ``--no-cache`` 可以整体旁路。

缓存是为了省调用，不是为了给你一个更旧的答案。

三条硬规矩
----------
1. **错误绝不入缓存。** 网络抖动、退避重试耗尽、业务 ``success: false`` 都不是"答案"；
   把失败缓存起来，会让一次抖动变成一小时都修不好。
2. **命中率必须能被看见。** ``stats()`` 给出本次运行与累计的命中率 ——
   没有命中率数字的缓存等于没有缓存，你没法判断它到底有没有在工作。
3. **过期就是过期，不做静默降级。** 过期条目可以被显式读出（``allow_stale=True``），
   但默认**不返回**：算错比算慢严重得多。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEFAULT_CACHE_DIR = os.path.join(ROOT, ".cache", "mcp")


# --------------------------------------------------------------------------- #
# TTL 表
# --------------------------------------------------------------------------- #
#: 每个 Tool 的保鲜期（秒）。**没列到的 Tool 用保守默认值**，而不是"永久缓存" ——
#: 新增 Tool 时忘了配 TTL 的代价，应该是多打一次接口，不是拿旧数据算错。
TTL_SECONDS: Dict[str, int] = {
    "query-meals": 1800,
    "query-lottery-info": 1800,
    "query-nearby-stores": 600,
    "delivery-query-stores": 600,
    "delivery-query-addresses": 600,
    "mall-points-products": 300,
    "query-store-coupons": 300,
    "query-my-coupons": 300,
    "available-coupons": 300,
    "query-my-account": 60,
}
TTL_DEFAULT = 300

#: 分档的**人话说明**，CLI 直接打印，避免"这个 300 是哪来的"没人知道。
TTL_REASON: Dict[str, str] = {
    "query-meals": "菜单位价与 SKU 极少变",
    "query-lottery-info": "奖池与消耗积分随活动走，活动期内稳定",
    "query-nearby-stores": "门店列表基本不动",
    "delivery-query-stores": "配送门店列表基本不动",
    "delivery-query-addresses": "收货地址列表基本不动",
    "mall-points-products": "商城上下架 / 限购轮换比菜单勤",
    "query-store-coupons": "门店券池随活动走",
    "query-my-coupons": "自己的券：领了、用了都会变",
    "available-coupons": "可领券池随活动走",
    "query-my-account": "积分余额：下单立刻扣，缓存久了会算错",
}

#: 业务失败标记。命中任意一个即视为"这不是答案"，不入缓存。
_ERROR_FLAGS = ("error", "errorMessage", "errorMsg", "errorCode", "errMsg")


def ttl_for(tool: str, override: Optional[int] = None) -> int:
    """取某 Tool 的 TTL（秒）。``override`` 优先，其次查表，最后是保守默认。"""
    if override is not None:
        return max(0, int(override))
    return int(TTL_SECONDS.get(tool, TTL_DEFAULT))


def looks_like_error(value: Any) -> bool:
    """判断业务载荷是否是一次**失败**。

    只认明确的失败标记 —— 宁可漏判（把一次失败缓存 60 秒）也不误判
    （把正常数据当失败、每次都白打接口）。
    """
    if not isinstance(value, dict):
        return False
    if value.get("success") is False:
        return True
    for flag in _ERROR_FLAGS:
        v = value.get(flag)
        if v not in (None, "", 0, False, [], {}):
            return True
    return False


# --------------------------------------------------------------------------- #
# 统计数据
# --------------------------------------------------------------------------- #
@dataclass
class CacheStats:
    """命中率账本。``run_*`` 是本次进程的，其余是累计（落盘）。"""

    hits: int = 0
    stale_hits: int = 0
    misses: int = 0
    stores: int = 0
    expired: int = 0
    rejected: int = 0      # 失败载荷 / 空值，被拒绝入缓存
    corrupt: int = 0       # 磁盘上读不出来的条目

    run_hits: int = field(default=0, repr=False)
    run_misses: int = field(default=0, repr=False)

    @property
    def lookups(self) -> int:
        return self.hits + self.stale_hits + self.misses

    @property
    def hit_rate(self) -> float:
        total = self.lookups
        return self.hits / total if total else 0.0

    @property
    def run_lookups(self) -> int:
        return self.run_hits + self.run_misses

    @property
    def run_hit_rate(self) -> float:
        return self.run_hits / self.run_lookups if self.run_lookups else 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "hits": self.hits, "stale_hits": self.stale_hits, "misses": self.misses,
            "stores": self.stores, "expired": self.expired,
            "rejected": self.rejected, "corrupt": self.corrupt,
            "lookups": self.lookups, "hit_rate": round(self.hit_rate, 4),
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "CacheStats":
        return cls(
            hits=int(d.get("hits", 0)), stale_hits=int(d.get("stale_hits", 0)),
            misses=int(d.get("misses", 0)), stores=int(d.get("stores", 0)),
            expired=int(d.get("expired", 0)), rejected=int(d.get("rejected", 0)),
            corrupt=int(d.get("corrupt", 0)),
        )


# --------------------------------------------------------------------------- #
# 缓存本体
# --------------------------------------------------------------------------- #
class CacheEntry:
    """一条缓存记录。用 ``__slots__`` —— 命中路径上会有很多条，别让 ``__dict__`` 拖后腿。"""

    __slots__ = ("tool", "args", "value", "stored_at", "expires_at", "source")

    def __init__(self, tool: str, args: Dict[str, Any], value: Any,
                 stored_at: float, ttl: int, source: str = "structured") -> None:
        self.tool = tool
        self.args = args
        self.value = value
        self.stored_at = stored_at
        self.expires_at = stored_at + ttl
        self.source = source

    @property
    def ttl(self) -> int:
        return int(round(self.expires_at - self.stored_at))

    def is_fresh(self, now: float) -> bool:
        return now < self.expires_at

    def remaining(self, now: float) -> float:
        return max(0.0, self.expires_at - now)

    def to_dict(self) -> Dict[str, Any]:
        return {"tool": self.tool, "args": self.args, "value": self.value,
                "stored_at": self.stored_at, "expires_at": self.expires_at,
                "source": self.source}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "CacheEntry":
        e = cls(str(d["tool"]), dict(d.get("args") or {}), d.get("value"),
                float(d["stored_at"]),
                int(float(d["expires_at"]) - float(d["stored_at"])),
                str(d.get("source", "structured")))
        e.expires_at = float(d["expires_at"])   # 以落盘值为准，避免重算引入偏差
        return e


def _canonical(args: Optional[Dict[str, Any]]) -> str:
    """把 arguments 规范化成**稳定**的字符串 —— 键序不同必须是同一个 key。"""
    return json.dumps(args or {}, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


class McpCache:
    """按 ``(tool, arguments)`` 落盘的 TTL 缓存。"""

    def __init__(self, root: str = DEFAULT_CACHE_DIR, *,
                 clock: Callable[[], float] = time.time,
                 enabled: bool = True, persist: bool = True) -> None:
        self.root = root
        self.enabled = enabled
        self.persist = persist
        self._clock = clock
        self.stats = CacheStats()
        self._stats_path = os.path.join(root, "_stats.json")
        if enabled and persist:
            self._load_stats()

    # -- 路径与键 ---------------------------------------------------------- #
    def key(self, tool: str, args: Optional[Dict[str, Any]]) -> str:
        raw = f"{tool}\x00{_canonical(args)}".encode("utf-8")
        return hashlib.sha1(raw).hexdigest()

    def _path(self, key: str) -> str:
        return os.path.join(self.root, key + ".json")

    # -- 统计落盘 ---------------------------------------------------------- #
    def _load_stats(self) -> None:
        try:
            with open(self._stats_path, "r", encoding="utf-8") as fh:
                self.stats = CacheStats.from_dict(json.load(fh))
        except (OSError, ValueError):
            self.stats = CacheStats()

    def save_stats(self) -> None:
        if not (self.enabled and self.persist):
            return
        try:
            os.makedirs(self.root, exist_ok=True)
            tmp = self._stats_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.stats.to_dict(), fh, ensure_ascii=False, indent=2)
            os.replace(tmp, self._stats_path)     # 原子替换：别留半截文件
        except OSError:
            pass

    # -- 读写 -------------------------------------------------------------- #
    def get(self, tool: str, args: Optional[Dict[str, Any]] = None, *,
            ttl: Optional[int] = None, allow_stale: bool = False) -> Optional[Any]:
        """取一条缓存。过期返回 ``None``（除非 ``allow_stale``）。"""
        entry = self.get_entry(tool, args, ttl=ttl, allow_stale=allow_stale)
        return entry.value if entry is not None else None

    def get_entry(self, tool: str, args: Optional[Dict[str, Any]] = None, *,
                  ttl: Optional[int] = None, allow_stale: bool = False) -> Optional[CacheEntry]:
        if not self.enabled:
            self._miss()
            return None
        entry = self._read(self.key(tool, args), tool, args)
        if entry is None:
            self._miss()
            return None
        now = self._clock()
        # 显式 TTL 覆盖：调用方说"这个场景我要更短/更长"，以调用方为准。
        if ttl is not None and now >= entry.stored_at + ttl:
            entry.expires_at = entry.stored_at + ttl
        if entry.is_fresh(now):
            self.stats.hits += 1
            self.stats.run_hits += 1
            return entry
        self.stats.expired += 1
        if allow_stale:
            self.stats.stale_hits += 1
            self.stats.run_misses += 1
            return entry
        self._miss()
        return None

    def _miss(self) -> None:
        self.stats.misses += 1
        self.stats.run_misses += 1

    def peek(self, tool: str, args: Optional[Dict[str, Any]] = None) -> Optional[CacheEntry]:
        """不看 TTL，直接看磁盘上有什么 —— CLI / 调试用，不计入命中率。"""
        return self._read(self.key(tool, args), tool, args)

    def _read(self, key: str, tool: str, args: Optional[Dict[str, Any]]) -> Optional[CacheEntry]:
        if not self.persist:
            return None
        path = self._path(key)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                blob = json.load(fh)
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            self.stats.corrupt += 1
            self._unlink(path)          # 坏条目留着只会一直坏
            return None
        if not isinstance(blob, dict):
            self.stats.corrupt += 1
            self._unlink(path)
            return None
        try:
            entry = CacheEntry.from_dict(blob)
        except (KeyError, TypeError, ValueError):
            self.stats.corrupt += 1
            self._unlink(path)
            return None
        # 校验：条目里的 tool/args 必须与请求一致 —— 防 sha1 撞车，也防文件被改过。
        if entry.tool != tool or entry.args != (args or {}):
            self.stats.corrupt += 1
            self._unlink(path)
            return None
        return entry

    def put(self, tool: str, args: Optional[Dict[str, Any]], value: Any, *,
            ttl: Optional[int] = None, source: str = "structured") -> bool:
        """写入一条缓存。**失败载荷与空值一律拒绝**，返回是否真的写进去了。"""
        if not self.enabled:
            return False
        if value is None or looks_like_error(value):
            self.stats.rejected += 1
            return False
        ttl_s = ttl_for(tool, ttl)
        if ttl_s <= 0:
            self.stats.rejected += 1
            return False
        entry = CacheEntry(tool, args or {}, value, self._clock(), ttl_s, source)
        if self.persist:
            try:
                os.makedirs(self.root, exist_ok=True)
                path = self._path(self.key(tool, args))
                tmp = path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(entry.to_dict(), fh, ensure_ascii=False)
                os.replace(tmp, path)
            except OSError:
                return False
        self.stats.stores += 1
        self.save_stats()
        return True

    # -- 维护 -------------------------------------------------------------- #
    def _unlink(self, path: str) -> None:
        try:
            os.remove(path)
        except OSError:
            pass

    def entries(self) -> List[CacheEntry]:
        """列出磁盘上全部可解析条目。"""
        if not self.persist or not os.path.isdir(self.root):
            return []
        out: List[CacheEntry] = []
        for name in sorted(os.listdir(self.root)):
            if not name.endswith(".json") or name.startswith("_"):
                continue
            path = os.path.join(self.root, name)
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    out.append(CacheEntry.from_dict(json.load(fh)))
            except (OSError, ValueError, KeyError, TypeError):
                self.stats.corrupt += 1
                self._unlink(path)
        out.sort(key=lambda e: e.stored_at)
        return out

    def invalidate(self, tool: Optional[str] = None) -> int:
        """按 Tool 精确失效；``tool=None`` 等价于 ``purge()``。返回删掉的条数。"""
        if tool is None:
            return self.purge()
        n = 0
        for e in self.entries():
            if e.tool == tool:
                self._unlink(self._path(self.key(e.tool, e.args)))
                n += 1
        return n

    def purge(self) -> int:
        """清空缓存目录（含统计）。"""
        if not os.path.isdir(self.root):
            return 0
        n = 0
        for name in os.listdir(self.root):
            path = os.path.join(self.root, name)
            if os.path.isfile(path):
                self._unlink(path)
                n += 1
        self.stats = CacheStats()
        return n


# --------------------------------------------------------------------------- #
# 套在客户端外面
# --------------------------------------------------------------------------- #
@dataclass
class PrefetchReport:
    """一次预取的结果 —— 省了多少次调用，必须看得见。"""

    requested: int = 0
    fetched: int = 0
    fresh: int = 0          # 本来就有新鲜缓存，直接跳过
    failed: int = 0
    tool_calls_saved: int = 0
    errors: List[str] = field(default_factory=list)

    @property
    def calls_made(self) -> int:
        return self.fetched

    def to_dict(self) -> Dict[str, Any]:
        return {"requested": self.requested, "fetched": self.fetched,
                "fresh": self.fresh, "failed": self.failed,
                "tool_calls_saved": self.tool_calls_saved,
                "errors": list(self.errors)}


class CachedMcpClient:
    """给 ``MCDMcpClient`` 套一层 TTL 缓存。**方法名与用法保持一致**，可直接替换。

    ``call_tool``（原始传输层）**不缓存** —— 拿到未解析的 result 一般是要看原始结构，
    那种场景下给旧数据没有意义。缓存只发生在 ``call_structured`` / ``call_json`` 上。
    """

    def __init__(self, client: Any, cache: Optional[McpCache] = None) -> None:
        self._client = client
        self.cache = cache if cache is not None else McpCache()
        #: 真正打到服务端的次数。与"逻辑取数次数"分开记 ——
        #: 只有这个数掉了，缓存才算真的省了东西。
        self.network_calls = 0

    # -- 透传 -------------------------------------------------------------- #
    def __getattr__(self, item: str) -> Any:
        return getattr(self._client, item)

    def initialize(self) -> Any:
        return self._client.initialize()

    def list_tools(self) -> Any:
        return self._client.list_tools()

    def call_tool(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> Any:
        return self._client.call_tool(name, arguments)

    # -- 缓存路径 ---------------------------------------------------------- #
    def call_structured(self, name: str, arguments: Optional[Dict[str, Any]] = None, *,
                        ttl: Optional[int] = None, refresh: bool = False) -> Any:
        return self._cached("call_structured", name, arguments, ttl=ttl, refresh=refresh)

    def call_json(self, name: str, arguments: Optional[Dict[str, Any]] = None, *,
                  ttl: Optional[int] = None, refresh: bool = False) -> Any:
        return self._cached("call_json", name, arguments, ttl=ttl, refresh=refresh)

    def _cached(self, method: str, name: str, arguments: Optional[Dict[str, Any]], *,
                ttl: Optional[int], refresh: bool) -> Any:
        if not refresh:
            hit = self.cache.get(name, arguments, ttl=ttl)
            if hit is not None:
                return hit
        # 未命中 / 强制刷新：真打一次。这里**故意不捕获异常** ——
        # 失败要往上传给调用方处理，绝不能变成一条缓存。
        value = getattr(self._client, method)(name, arguments)
        self.network_calls += 1
        self.cache.put(name, arguments, value, ttl=ttl,
                       source="structured" if method == "call_structured" else "json")
        return value

    # -- 预取 -------------------------------------------------------------- #
    def prefetch(self, tasks: Sequence[Tuple[Any, ...]], *,
                 refresh: bool = False) -> PrefetchReport:
        """按 **workflow 的消费顺序**把接下来要用的数据提前拉好。

        ``tasks`` 是 ``(tool, arguments)`` 或 ``(tool, arguments, ttl)`` 的序列。
        本来就有新鲜缓存的直接跳过（``fresh``）—— 所以**重复跑一次联调通常 0 次 MCP 调用**，
        这才是缓存真正省下来的东西。
        """
        report = PrefetchReport(requested=len(tasks))
        for task in tasks:
            tool, args = task[0], (task[1] if len(task) > 1 else {})
            ttl = task[2] if len(task) > 2 else None
            if not refresh and self.cache.get(tool, args, ttl=ttl) is not None:
                report.fresh += 1
                report.tool_calls_saved += 1
                continue
            try:
                self.call_structured(tool, args, ttl=ttl)
                report.fetched += 1
            except Exception as exc:                       # noqa: BLE001
                report.failed += 1
                report.errors.append(f"{tool}: {type(exc).__name__}: {exc}")
        return report


def workflow_tasks(store_code: str, *, be_code: Optional[str] = None,
                   include_delivery: bool = True) -> List[Tuple[Any, ...]]:
    """统一联调会依次用到的取数清单（**顺序即消费顺序**）。

    这就是"预测性"的来源：门店一确定，后面 5 次调用要什么参数就已经知道了，
    没必要等每一步走到才发请求。
    """
    tasks: List[Tuple[Any, ...]] = [
        ("query-meals", {"storeCode": store_code, "orderType": 1, "beType": 1}),
    ]
    if include_delivery:
        # 外送需要 beCode；没有配送地址时取不到是正常状态，失败也不影响主流程。
        tasks.append(("query-meals",
                      {"storeCode": store_code, "orderType": 2, "beType": 2,
                       "beCode": be_code or ""}))
    tasks += [
        ("query-store-coupons", {"storeCode": store_code}),
        ("query-my-coupons", {}),
        ("available-coupons", {}),
        ("query-my-account", {}),
        ("mall-points-products", {}),
    ]
    return tasks


# --------------------------------------------------------------------------- #
# 输出
# --------------------------------------------------------------------------- #
def print_stats(cache: McpCache, *, limit: int = 12) -> None:
    from termtext import columns, rule, table_width  # noqa: PLC0415

    st = cache.stats
    print(f"  缓存目录：{os.path.relpath(cache.root, ROOT) if cache.root.startswith(ROOT) else cache.root}")
    print(f"  启用：{'是' if cache.enabled else '否（--no-cache）'} · 落盘：{'是' if cache.persist else '否'}")
    print(f"\n  本次运行：命中 {st.run_hits} / 查询 {st.run_lookups}"
          f" → 命中率 {st.run_hit_rate * 100:.1f}%")
    print(f"  累计：命中 {st.hits} / 查询 {st.lookups} → 命中率 {st.hit_rate * 100:.1f}%"
          f" · 写入 {st.stores} · 过期 {st.expired} · 拒收 {st.rejected} · 坏条目 {st.corrupt}")

    entries = cache.entries()
    if not entries:
        print("\n  （磁盘上暂无条目）")
        return
    now = time.time()
    specs = [("Tool", 24, "<"), ("arguments", 40, "<"), ("新鲜度", 12, ">"), ("TTL", 8, ">")]
    rows = []
    for e in entries[:limit]:
        left = e.expires_at - now
        rows.append((e.tool, _short(e.args, 40),
                     "已过期" if left <= 0 else f"{left / 60:.1f} 分钟",
                     f"{ttl_for(e.tool) // 60} 分钟"))
    print()
    print(rule(table_width(specs)))
    for line in columns(specs, rows):
        print(line)
    print(rule(table_width(specs)))
    if len(entries) > limit:
        print(f"  … 另有 {len(entries) - limit} 条")

    print("\n  TTL 分档依据：")
    for tool in sorted({e.tool for e in entries}):
        print(f"    {tool:<24} {ttl_for(tool) // 60:>3} 分钟 · {TTL_REASON.get(tool, '默认档')}")


def _short(args: Dict[str, Any], width: int) -> str:
    text = json.dumps(args, ensure_ascii=False, separators=(",", ":"))
    return text if len(text) <= width else text[:width - 1] + "…"


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="MCP 调用缓存：分档 TTL / 命中率 / 维护")
    ap.add_argument("command", nargs="?", default="stats",
                    choices=("stats", "list", "purge", "invalidate", "ttl"))
    ap.add_argument("--dir", default=DEFAULT_CACHE_DIR)
    ap.add_argument("--tool", default=None, help="invalidate 用：只失效这一个 Tool")
    ap.add_argument("--limit", type=int, default=12)
    return ap


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    cache = McpCache(args.dir)

    if args.command == "purge":
        n = cache.purge()
        print(f"  已清空 {os.path.relpath(args.dir, ROOT) if args.dir.startswith(ROOT) else args.dir}"
              f"（删除 {n} 个文件）")
        return 0

    if args.command == "invalidate":
        n = cache.invalidate(args.tool)
        print(f"  已失效 {n} 条{'（Tool=' + args.tool + '）' if args.tool else '（全部）'}")
        return 0

    if args.command == "ttl":
        from termtext import columns, rule, table_width  # noqa: PLC0415

        # 第三列是 2 列宽的空白分隔列 —— 不然最后一列是自由文本，表头会贴着数字跑。
        specs = [("Tool", 24, "<"), ("TTL", 10, ">"), ("", 2, "<"),
                 ("为什么是这个量级", 0, "<")]
        rows = [(t, f"{s // 60} 分钟", "", TTL_REASON.get(t, ""))
                for t, s in sorted(TTL_SECONDS.items(), key=lambda kv: -kv[1])]
        rows.append(("（未列出的 Tool）", f"{TTL_DEFAULT // 60} 分钟", "",
                     "保守默认 —— 忘了配 TTL 的代价是多打一次，不是算错"))
        print(rule(table_width(specs)))
        for line in columns(specs, rows):
            print(line)
        print(rule(table_width(specs)))
        return 0

    if args.command == "list":
        for e in cache.entries():
            left = e.expires_at - time.time()
            flag = "过期" if left <= 0 else f"{left / 60:.1f}分"
            print(f"  {e.tool:<24} {flag:>7}  {_short(e.args, 60)}")
        return 0

    print_stats(cache, limit=args.limit)
    return 0


if __name__ == "__main__":
    sys.exit(main())
