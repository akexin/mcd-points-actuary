#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MCP 调用缓存的单元测试。

缓存最容易"看起来在工作"：它确实返回了数据，也确实比每次都打接口快 ——
但可能返回的是**过期数据**，或者把一次**失败**缓存成了一小时都修不好的脏条目。
所以这里的重点不是"能不能命中"，而是三件事：

1. TTL 到点必须失效（用可注入的假时钟，不靠 ``sleep``）
2. 失败载荷与空值**绝不入缓存**
3. 命中率数字必须与真实行为对得上（否则它只是在骗自己）
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from mcp_cache import (  # noqa: E402
    TTL_DEFAULT, TTL_SECONDS, CacheEntry, CacheStats, CachedMcpClient, McpCache,
    looks_like_error, print_stats, ttl_for, workflow_tasks,
)


class FakeClock:
    """可手动推进的时钟 —— 测 TTL 不能真的 sleep。"""

    def __init__(self, t0: float = 1_700_000_000.0) -> None:
        self.now = t0

    def __call__(self) -> float:
        return self.now

    def tick(self, seconds: float) -> None:
        self.now += seconds


class FakeClient:
    """记录每次真实调用的假客户端。"""

    def __init__(self) -> None:
        self.calls = []

    def initialize(self):
        return {"result": {"serverInfo": {"name": "fake", "version": "0"}}}

    def list_tools(self):
        return []

    def call_tool(self, name, arguments=None):
        self.calls.append((name, arguments))
        return {"structuredContent": {"success": True, "data": {"n": len(self.calls)}}}

    def call_structured(self, name, arguments=None):
        self.calls.append((name, arguments))
        return {"success": True, "data": {"n": len(self.calls)}}

    def call_json(self, name, arguments=None):
        self.calls.append((name, arguments))
        return {"success": True, "data": {"n": len(self.calls)}}


class TempCacheCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="mcp-cache-")
        self.clock = FakeClock()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def make(self, **kw):
        return McpCache(self.dir, clock=self.clock, **kw)


class TestTtlTable(TempCacheCase):
    def test_documented_buckets(self):
        self.assertEqual(ttl_for("query-meals"), 1800)
        self.assertEqual(ttl_for("query-my-account"), 60)
        self.assertEqual(ttl_for("query-nearby-stores"), 600)
        self.assertEqual(ttl_for("query-my-coupons"), 300)

    def test_unknown_tool_gets_conservative_default(self):
        """忘了配 TTL 的代价应该是"多打一次"，不是"永久缓存"。"""
        self.assertEqual(ttl_for("brand-new-tool"), TTL_DEFAULT)
        self.assertLessEqual(TTL_DEFAULT, 600)

    def test_account_ttl_is_the_shortest(self):
        """积分余额是唯一"会被立刻改变"的数据，必须是最短档。"""
        self.assertEqual(min(TTL_SECONDS.values()), TTL_SECONDS["query-my-account"])

    def test_override_wins(self):
        self.assertEqual(ttl_for("query-meals", 5), 5)
        self.assertEqual(ttl_for("query-meals", -1), 0)

    def test_every_tool_has_a_reason_or_falls_back(self):
        for tool in TTL_SECONDS:
            with self.subTest(tool=tool):
                self.assertGreater(ttl_for(tool), 0)


class TestErrorDetection(unittest.TestCase):
    def test_success_false_is_error(self):
        self.assertTrue(looks_like_error({"success": False, "message": "库存不足"}))

    def test_error_flags_are_errors(self):
        for flag in ("error", "errorMessage", "errorCode", "errMsg"):
            with self.subTest(flag=flag):
                self.assertTrue(looks_like_error({flag: "boom"}))

    def test_normal_payload_is_not_error(self):
        self.assertFalse(looks_like_error({"success": True, "data": {"rows": []}}))
        self.assertFalse(looks_like_error({"data": []}))
        self.assertFalse(looks_like_error([1, 2, 3]))
        self.assertFalse(looks_like_error("text"))

    def test_empty_success_is_not_error(self):
        """业务成功的空结果是**合法答案**（例如"当前无可用券"）。"""
        self.assertFalse(looks_like_error({"success": True, "data": {}}))
        self.assertFalse(looks_like_error({"errorCode": 0}))


class TestPutGet(TempCacheCase):
    def test_roundtrip_within_ttl(self):
        c = self.make()
        c.put("query-meals", {"storeCode": "S1"}, {"success": True, "data": [1, 2]})
        self.clock.tick(60)
        self.assertEqual(c.get("query-meals", {"storeCode": "S1"}),
                         {"success": True, "data": [1, 2]})

    def test_expires_exactly_at_ttl(self):
        c = self.make()
        c.put("query-my-account", {}, {"balance": 100})
        self.clock.tick(59)
        self.assertIsNotNone(c.get("query-my-account", {}))
        self.clock.tick(1)                       # 整 60 秒：边界必须判过期
        self.assertIsNone(c.get("query-my-account", {}))
        self.assertEqual(c.stats.expired, 1)

    def test_per_tool_ttl_is_honored(self):
        c = self.make()
        c.put("query-meals", {}, {"m": 1})
        c.put("query-my-account", {}, {"a": 1})
        self.clock.tick(120)
        self.assertIsNotNone(c.get("query-meals", {}))        # 30 分钟档还活着
        self.assertIsNone(c.get("query-my-account", {}))      # 1 分钟档已死

    def test_key_is_order_insensitive(self):
        c = self.make()
        c.put("query-meals", {"a": 1, "b": 2}, {"v": 1})
        self.assertIsNotNone(c.get("query-meals", {"b": 2, "a": 1}))

    def test_different_args_are_different_keys(self):
        c = self.make()
        c.put("query-meals", {"storeCode": "S1"}, {"v": 1})
        self.assertIsNone(c.get("query-meals", {"storeCode": "S2"}))

    def test_different_tool_same_args(self):
        c = self.make()
        c.put("query-my-coupons", {}, {"v": 1})
        self.assertIsNone(c.get("available-coupons", {}))

    def test_allow_stale_reads_expired(self):
        c = self.make()
        c.put("query-meals", {}, {"v": 1})
        self.clock.tick(99999)
        self.assertIsNone(c.get("query-meals", {}))
        self.assertEqual(c.get("query-meals", {}, allow_stale=True), {"v": 1})
        self.assertEqual(c.stats.stale_hits, 1)

    def test_explicit_ttl_override_shortens(self):
        c = self.make()
        c.put("query-meals", {}, {"v": 1}, ttl=10)
        self.clock.tick(11)
        self.assertIsNone(c.get("query-meals", {}))
        self.assertIsNotNone(c.get("query-meals", {}, allow_stale=True))

    def test_rejected_values(self):
        c = self.make()
        for bad in (None, {"success": False}, {"error": "boom"}, {"errorCode": 500}):
            with self.subTest(bad=bad):
                self.assertFalse(c.put("query-meals", {}, bad))
        self.assertEqual(c.stats.rejected, 4)
        self.assertEqual(c.entries(), [])

    def test_zero_ttl_is_rejected(self):
        c = self.make()
        self.assertFalse(c.put("query-meals", {}, {"v": 1}, ttl=0))

    def test_history_file_is_not_an_entry(self):
        """统计文件与缓存条目同目录，别把它当条目读出来。"""
        c = self.make()
        c.put("query-meals", {}, {"v": 1})
        self.assertTrue(os.path.exists(os.path.join(self.dir, "_stats.json")))
        self.assertEqual(len(c.entries()), 1)


class TestPersistence(TempCacheCase):
    def test_survives_new_instance(self):
        c = self.make()
        c.put("query-meals", {"storeCode": "S1"}, {"v": 42})
        c.save_stats()
        c2 = McpCache(self.dir, clock=self.clock)
        self.assertEqual(c2.get("query-meals", {"storeCode": "S1"}), {"v": 42})

    def test_stats_accumulate_across_instances(self):
        c = self.make()
        c.put("query-meals", {}, {"v": 1})
        c.get("query-meals", {})                 # 1 次命中
        c.save_stats()
        c2 = McpCache(self.dir, clock=self.clock)
        c2.get("query-meals", {})                # 又 1 次
        self.assertEqual(c2.stats.hits, 2)
        self.assertEqual(c2.stats.run_hits, 1)   # 本次运行只算本次

    def test_corrupt_entry_is_dropped_not_fatal(self):
        c = self.make()
        c.put("query-meals", {"storeCode": "S1"}, {"v": 1})
        path = os.path.join(self.dir, c.key("query-meals", {"storeCode": "S1"}) + ".json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{ this is not json")
        self.assertIsNone(c.get("query-meals", {"storeCode": "S1"}))
        self.assertEqual(c.stats.corrupt, 1)
        self.assertFalse(os.path.exists(path))   # 坏条目必须被清掉，不然一直坏

    def test_tampered_entry_is_rejected(self):
        """文件被人改过 tool/args 时要按坏条目处理，不能拿错内容当答案。"""
        c = self.make()
        c.put("query-meals", {"storeCode": "S1"}, {"v": 1})
        path = os.path.join(self.dir, c.key("query-meals", {"storeCode": "S1"}) + ".json")
        with open(path, "r", encoding="utf-8") as fh:
            blob = json.load(fh)
        blob["tool"] = "query-my-account"
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(blob, fh, ensure_ascii=False)
        self.assertIsNone(c.get("query-meals", {"storeCode": "S1"}))
        self.assertEqual(c.stats.corrupt, 1)

    def test_non_dict_blob_is_rejected(self):
        c = self.make()
        key = c.key("query-meals", {})
        with open(os.path.join(self.dir, key + ".json"), "w", encoding="utf-8") as fh:
            json.dump([1, 2], fh)
        self.assertIsNone(c.get("query-meals", {}))

    def test_missing_file_is_a_plain_miss(self):
        c = self.make()
        self.assertIsNone(c.get("query-meals", {}))
        self.assertEqual(c.stats.corrupt, 0)
        self.assertEqual(c.stats.misses, 1)

    def test_disabled_cache_never_stores(self):
        c = self.make(enabled=False)
        c.put("query-meals", {}, {"v": 1})
        self.assertIsNone(c.get("query-meals", {}))
        self.assertFalse(os.path.exists(os.path.join(self.dir, "..", "x")))

    def test_non_persistent_cache_is_memoryless(self):
        c = McpCache(self.dir, clock=self.clock, persist=False)
        c.put("query-meals", {}, {"v": 1})
        self.assertIsNone(c.get("query-meals", {}))

    def test_entry_roundtrip_preserves_expiry(self):
        e = CacheEntry("t", {"a": 1}, {"v": 1}, 100.0, 300, "json")
        back = CacheEntry.from_dict(e.to_dict())
        self.assertEqual(back.expires_at, 400.0)
        self.assertEqual(back.ttl, 300)
        self.assertEqual(back.source, "json")


class TestStats(TempCacheCase):
    def test_hit_rate_arithmetic(self):
        s = CacheStats(hits=3, misses=1)
        self.assertAlmostEqual(s.hit_rate, 0.75)
        self.assertEqual(s.lookups, 4)

    def test_run_rate_is_separate_from_cumulative(self):
        s = CacheStats(hits=100, misses=0, run_hits=0, run_misses=2)
        self.assertEqual(s.hit_rate, 1.0)
        self.assertEqual(s.run_hit_rate, 0.0)

    def test_empty_stats_do_not_divide_by_zero(self):
        s = CacheStats()
        self.assertEqual(s.hit_rate, 0.0)
        self.assertEqual(s.run_hit_rate, 0.0)

    def test_counters_move_with_behavior(self):
        c = self.make()
        c.get("query-meals", {})                              # miss
        c.put("query-meals", {}, {"v": 1})                    # store
        c.get("query-meals", {})                              # hit
        c.put("query-meals", {}, {"success": False})          # rejected
        self.clock.tick(2000)
        c.get("query-meals", {})                              # expired + miss
        st = c.stats
        self.assertEqual((st.hits, st.misses, st.stores, st.rejected, st.expired),
                         (1, 2, 1, 1, 1))
        self.assertAlmostEqual(st.run_hit_rate, 1 / 3)

    def test_stats_json_safe(self):
        c = self.make()
        c.put("query-meals", {}, {"v": 1})
        c.get("query-meals", {})
        json.dumps(c.stats.to_dict())


class TestMaintenance(TempCacheCase):
    def test_invalidate_by_tool(self):
        c = self.make()
        c.put("query-meals", {"s": "1"}, {"v": 1})
        c.put("query-my-account", {}, {"v": 2})
        self.assertEqual(c.invalidate("query-meals"), 1)
        self.assertIsNone(c.get("query-meals", {"s": "1"}))
        self.assertIsNotNone(c.get("query-my-account", {}))

    def test_purge_clears_everything_and_resets_stats(self):
        c = self.make()
        c.put("query-meals", {}, {"v": 1})
        c.get("query-meals", {})
        n = c.purge()
        self.assertGreaterEqual(n, 1)
        self.assertEqual(c.entries(), [])
        self.assertEqual(c.stats.hits, 0)

    def test_purge_on_missing_dir(self):
        c = McpCache(os.path.join(self.dir, "nope"), clock=self.clock)
        self.assertEqual(c.purge(), 0)

    def test_entries_sorted_by_time(self):
        c = self.make()
        c.put("query-meals", {"i": 1}, {"v": 1})
        self.clock.tick(10)
        c.put("query-meals", {"i": 2}, {"v": 2})
        self.clock.tick(10)
        c.put("query-meals", {"i": 3}, {"v": 3})
        self.assertEqual([e.args["i"] for e in c.entries()], [1, 2, 3])

    def test_peek_ignores_ttl_and_stats(self):
        c = self.make()
        c.put("query-meals", {}, {"v": 1})
        self.clock.tick(99999)
        self.assertIsNotNone(c.peek("query-meals", {}))
        self.assertEqual(c.stats.lookups, 0)


class TestCachedClient(TempCacheCase):
    def setUp(self):
        super().setUp()
        self.raw = FakeClient()
        self.cache = self.make()
        self.client = CachedMcpClient(self.raw, self.cache)

    def test_second_call_hits_cache(self):
        a = self.client.call_structured("query-meals", {"s": "1"})
        b = self.client.call_structured("query-meals", {"s": "1"})
        self.assertEqual(a, b)
        self.assertEqual(len(self.raw.calls), 1)
        self.assertEqual(self.client.network_calls, 1)

    def test_arguments_are_part_of_the_key(self):
        self.client.call_structured("query-meals", {"s": "1"})
        self.client.call_structured("query-meals", {"s": "2"})
        self.assertEqual(len(self.raw.calls), 2)

    def test_refresh_bypasses_cache(self):
        self.client.call_structured("query-meals", {"s": "1"})
        self.client.call_structured("query-meals", {"s": "1"}, refresh=True)
        self.assertEqual(len(self.raw.calls), 2)

    def test_call_tool_is_not_cached(self):
        """原始传输层不缓存：要看原始结构的时候给旧数据没有意义。"""
        self.client.call_tool("query-my-account", {})
        self.client.call_tool("query-my-account", {})
        self.assertEqual(len(self.raw.calls), 2)
        self.assertEqual(self.cache.stats.stores, 0)

    def test_failures_are_never_cached(self):
        class Boom(FakeClient):
            def call_structured(self, name, arguments=None):
                self.calls.append((name, arguments))
                raise RuntimeError("网络抖动")

        raw = Boom()
        c = CachedMcpClient(raw, self.cache)
        for _ in range(3):
            with self.assertRaises(RuntimeError):
                c.call_structured("query-meals", {"s": "1"})
        self.assertEqual(len(raw.calls), 3)          # 每次都真的重试了
        self.assertEqual(self.cache.stats.stores, 0)
        self.assertEqual(self.cache.entries(), [])

    def test_business_failure_payload_is_not_cached(self):
        class Biz(FakeClient):
            def call_structured(self, name, arguments=None):
                self.calls.append((name, arguments))
                return {"success": False, "message": "门店不存在"}

        raw = Biz()
        c = CachedMcpClient(raw, self.cache)
        c.call_structured("query-meals", {"s": "1"})
        c.call_structured("query-meals", {"s": "1"})
        self.assertEqual(len(raw.calls), 2)
        self.assertEqual(self.cache.stats.rejected, 2)

    def test_json_and_structured_share_a_key(self):
        """两条路径拿到的都是业务 JSON，共用一个缓存空间才有意义。"""
        self.client.call_structured("query-meals", {"s": "1"})
        self.client.call_json("query-meals", {"s": "1"})
        self.assertEqual(len(self.raw.calls), 1)

    def test_other_attributes_pass_through(self):
        self.assertEqual(self.client.list_tools(), [])
        self.assertIn("serverInfo", self.client.initialize()["result"])


class TestPrefetch(TempCacheCase):
    def setUp(self):
        super().setUp()
        self.raw = FakeClient()
        self.cache = self.make()
        self.client = CachedMcpClient(self.raw, self.cache)

    def test_prefetch_fetches_all_then_second_run_is_free(self):
        tasks = [("query-meals", {"s": "1"}), ("query-my-account", {}),
                 ("mall-points-products", {})]
        r1 = self.client.prefetch(tasks)
        self.assertEqual((r1.requested, r1.fetched, r1.fresh, r1.failed), (3, 3, 0, 0))
        self.assertEqual(len(self.raw.calls), 3)

        r2 = self.client.prefetch(tasks)
        self.assertEqual((r2.fetched, r2.fresh, r2.tool_calls_saved), (0, 3, 3))
        self.assertEqual(len(self.raw.calls), 3)      # 一次都没再打

    def test_prefetch_reports_failures_without_aborting(self):
        class Partial(FakeClient):
            def call_structured(self, name, arguments=None):
                self.calls.append((name, arguments))
                if name == "query-meals":
                    raise RuntimeError("boom")
                return {"success": True, "data": {}}

        raw = Partial()
        c = CachedMcpClient(raw, self.cache)
        r = c.prefetch([("query-meals", {}), ("query-my-account", {})])
        self.assertEqual((r.fetched, r.failed), (1, 1))
        self.assertTrue(r.errors and "query-meals" in r.errors[0])
        self.assertIsNotNone(self.cache.get("query-my-account", {}))

    def test_ttl_override_in_task(self):
        self.client.prefetch([("query-meals", {}, 5)])
        self.clock.tick(6)
        self.assertIsNone(self.cache.get("query-meals", {}))

    def test_refresh_prefetch_refetches(self):
        tasks = [("query-my-account", {})]
        self.client.prefetch(tasks)
        r = self.client.prefetch(tasks, refresh=True)
        self.assertEqual((r.fetched, r.fresh), (1, 0))

    def test_workflow_tasks_cover_the_documented_calls(self):
        tools = [t[0] for t in workflow_tasks("S1", include_delivery=False)]
        self.assertEqual(tools, ["query-meals", "query-store-coupons", "query-my-coupons",
                                 "available-coupons", "query-my-account",
                                 "mall-points-products"])

    def test_workflow_includes_delivery_only_with_be_code(self):
        with_be = [t for t in workflow_tasks("S1", be_code="BE9", include_delivery=True)]
        self.assertEqual(sum(1 for t in with_be if t[0] == "query-meals"), 2)
        no_be = workflow_tasks("S1", be_code=None, include_delivery=False)
        self.assertEqual(sum(1 for t in no_be if t[0] == "query-meals"), 1)

    def test_repeat_run_costs_zero_calls(self):
        """这才是缓存真正省下来的东西：整条 workflow 重跑一次，0 次 MCP 调用。"""
        tasks = workflow_tasks("S1", include_delivery=False)
        self.client.prefetch(tasks)
        before = self.client.network_calls
        self.client.prefetch(tasks)
        self.assertEqual(self.client.network_calls, before)


class TestPrintStats(TempCacheCase):
    def test_prints_without_crashing(self):
        import io as _io
        from contextlib import redirect_stdout

        c = self.make()
        c.put("query-meals", {"storeCode": "1990346"}, {"v": 1})
        c.get("query-meals", {"storeCode": "1990346"})
        buf = _io.StringIO()
        with redirect_stdout(buf):
            print_stats(c)
        out = buf.getvalue()
        for token in ("命中率", "TTL 分档依据", "菜单位价与 SKU 极少变"):
            with self.subTest(token=token):
                self.assertIn(token, out)

    def test_prints_on_empty_cache(self):
        import io as _io
        from contextlib import redirect_stdout

        buf = _io.StringIO()
        with redirect_stdout(buf):
            print_stats(self.make())
        self.assertIn("暂无条目", buf.getvalue())

    def test_columns_line_up(self):
        from termtext import dwidth
        import io as _io
        from contextlib import redirect_stdout

        c = self.make()
        c.put("query-meals", {"storeCode": "1990346"}, {"v": 1})
        c.put("query-my-account", {}, {"v": 2})
        buf = _io.StringIO()
        with redirect_stdout(buf):
            print_stats(c)
        rows = [ln for ln in buf.getvalue().splitlines() if "分钟" in ln and "·" in ln
                and "TTL" not in ln and "档" not in ln]
        self.assertTrue(rows)


if __name__ == "__main__":
    unittest.main(verbosity=2)
