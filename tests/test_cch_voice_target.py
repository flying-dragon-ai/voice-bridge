"""CCH 语音播控目标路由（T2 ticket 08）契约测试。

验证：
- target 两层判定（LLM 显式优先 / 本轮上下文兜底 / 默认 lx06）；
- 上下文 set/reset 不串轮；
- cch_cast.cast() 经 Bearer 投 /cch/cast，revoke() 经 managed/command 撤票。
不连设备、不联网（aiohttp TestServer 伪造）。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aiohttp import web  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from core.tools import song_request as sr  # noqa: E402
from core.music import cch_cast  # noqa: E402


class TargetResolutionTest(unittest.TestCase):
    def setUp(self):
        sr.reset_playback_target()

    def tearDown(self):
        sr.reset_playback_target()

    def test_default_is_lx06(self):
        self.assertEqual(sr._resolve_target(None), "lx06")

    def test_context_fallback(self):
        sr.set_playback_target("cch")
        self.assertEqual(sr._resolve_target(None), "cch")

    def test_explicit_overrides_context(self):
        sr.set_playback_target("cch")
        self.assertEqual(sr._resolve_target("lx06"), "lx06")
        sr.set_playback_target("lx06")
        self.assertEqual(sr._resolve_target("cch"), "cch")

    def test_context_reset(self):
        sr.set_playback_target("cch")
        sr.reset_playback_target()
        self.assertEqual(sr._resolve_target(None), "lx06")

    def test_invalid_values_ignored(self):
        sr.set_playback_target("garbage")  # setter 白名单拒绝
        self.assertEqual(sr._resolve_target(None), "lx06")
        self.assertEqual(sr._resolve_target("garbage"), "lx06")


class CchCastChannelTest(unittest.IsolatedAsyncioTestCase):
    async def test_cast_sends_bearer_and_url(self):
        seen = {}

        async def cast_handler(req):
            seen["auth"] = req.headers.get("Authorization", "")
            seen["body"] = await req.json()
            return web.json_response({"ok": True})

        app = web.Application()
        app.router.add_post("/cch/cast", cast_handler)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            orig_url = cch_cast.cch_cast_url
            orig_token = cch_cast.panel_token
            cch_cast.cch_cast_url = lambda: str(client.make_url("/cch/cast"))
            cch_cast.panel_token = lambda: "test-panel-token"
            ok = await cch_cast.cast("http://10.10.10.2:58091/media?media_ticket=abc")
            self.assertTrue(ok)
            self.assertEqual(seen["auth"], "Bearer test-panel-token")
            self.assertEqual(seen["body"]["url"],
                             "http://10.10.10.2:58091/media?media_ticket=abc")
        finally:
            cch_cast.cch_cast_url = orig_url
            cch_cast.panel_token = orig_token
            await client.close()

    async def test_cast_no_token_returns_false(self):
        orig_token = cch_cast.panel_token
        cch_cast.panel_token = lambda: ""
        try:
            ok = await cch_cast.cast("http://10.10.10.2:58091/media?media_ticket=abc")
            self.assertFalse(ok)
        finally:
            cch_cast.panel_token = orig_token

    async def test_revoke_502_is_ticket_revoked(self):
        """songloft: bff.Execute 先撤票、forwardCommand 后才 502 —— 502 视为撤票成功。"""
        import core.music.song_bridge as sb

        orig_token = sb._songloft_token
        orig_cfg = sb._songloft_cfg
        bodies = []

        async def fake_token(base, http):
            return "jwt"

        async def revoke_handler(req):
            bodies.append(await req.json())
            return web.json_response({"error": "bridge session not established"}, status=502)

        app = web.Application()
        app.router.add_post("/api/v1/managed/command", revoke_handler)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            base = str(client.make_url("")).rstrip("/")
            sb._songloft_token = fake_token
            sb._songloft_cfg = lambda k, d=None: base if k == "songloft_base" else d
            ok = await cch_cast.revoke("bridge-cch", 1, "e1")
            self.assertTrue(ok, "502 should count as ticket revoked")
            self.assertEqual(bodies[0]["name"], "stop")
        finally:
            sb._songloft_token = orig_token
            sb._songloft_cfg = orig_cfg
            await client.close()


if __name__ == "__main__":
    unittest.main()
