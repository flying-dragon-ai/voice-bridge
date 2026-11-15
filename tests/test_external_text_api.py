"""外部文本入口（B-37）HTTP 端点测试。

桩掉重依赖后加载真实 `core/services/api_server.py`（同 test_injection_long_term
的加载器思路：setUpClass 桩态加载、tearDownClass 恢复），鉴权用**真实**
`core/services/auth`（本端点是写类——验证 401/403/503 与放行链）。覆盖：
  - 路由已注册
  - enabled=false → 503 code=external_text_disabled
  - CIDR 未配 / 源不在网段 / XFF 默认不信任 → 503 code=external_text_refused
  - 正常路径（非 silent）→ 202 accepted + begin/finish 调用链
  - busy → 409 code=external_text_busy；silent → 200 带 reply/outcome/turn
  - 载荷校验 400；后端未登记 503 backend_unknown；同调用方限流 429
  - 鉴权中间件：缺 token 401 / 错 token 403 / 未配置 503 / 合法放行（label 进 request）
"""

import asyncio
import importlib.util
import json
import os
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from aiohttp import web  # noqa: E402
from core.services.external_text import ENV_EXTERNAL_TEXT_ALLOW_CIDR  # noqa: E402

_VALID_TOKEN = "a" * 40


# ---------------------------------------------------------------- 桩与加载器


class FakeEvents:
    """EventManager 替身：外部文本会话的 begin/finish 调用可观测可控制。"""

    INJECTION_GATE_TTL = 30.0
    gate_until = 0.0

    begin_calls: list = []
    finish_calls: list = []
    begin_result: object = object()  # 默认占位成功（返回控制器替身）
    finish_result: dict = {"turn_id": 7, "outcome": "played", "reply": "好的"}

    @classmethod
    def reset(cls):
        cls.begin_calls = []
        cls.finish_calls = []
        cls.begin_result = object()
        cls.finish_result = {"turn_id": 7, "outcome": "played", "reply": "好的"}
        cls.gate_until = 0.0

    # ---- 外部文本会话 ----

    @classmethod
    def begin_external_text_session(cls, name, *, text, source="", silent=False):
        cls.begin_calls.append(
            {"name": name, "text": text, "source": source, "silent": silent}
        )
        return cls.begin_result

    @classmethod
    async def finish_external_text_session(cls, name, controller):
        cls.finish_calls.append((name, controller))
        return dict(cls.finish_result)

    # ---- 注入闸门（APIServer 其余端点需要的最小面）----

    @classmethod
    def open_injection_gate(cls, duration=None):
        return 30.0

    @classmethod
    def close_injection_gate(cls):
        cls.gate_until = 0.0

    @classmethod
    def injection_gate_active(cls):
        return False

    @classmethod
    def injection_gate_remaining(cls):
        return 0.0


class FakeBackendRegistry:
    names = ("openai", "openclaw", "qwenpaw")

    @classmethod
    def get(cls, name):
        return object() if name in cls.names else None


class FakeConfig:
    """ConfigManager 替身：嵌套 dict 按 dotted path 查询。"""

    DATA: dict = {}

    @classmethod
    def instance(cls):
        return cls()

    def get_app_config(self, path, default=None):
        node = self.DATA
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node


class FakeRequest:
    """最小 aiohttp Request 替身：headers/remote/json + 映射存储。"""

    def __init__(self, payload=None, headers=None, remote="10.10.10.20"):
        self._payload = payload
        self.can_read_body = payload is not None
        self.path = "/api/external/text"
        self.method = "POST"
        self.headers = headers or {}
        self.remote = remote
        self.transport = None
        self._storage: dict = {}

    async def json(self):
        if self._payload is None:
            raise ValueError("no body")
        return self._payload

    def __getitem__(self, key):
        return self._storage[key]

    def __setitem__(self, key, value):
        self._storage[key] = value

    def get(self, key, default=None):
        return self._storage.get(key, default)


def _load_api_server_module():
    """桩掉重依赖后加载 api_server（鉴权/caller/inject_control 保持真实）。"""
    stub_names = [
        "open_xiaoai_server",
        "core.ref",
        "core.wakeup_session",
        "core.services.admin_api",
        "core.services.tts.doubao",
        "core.services.speaker",
        "core.services.health",
        "core.device",
        "core.utils.config",
        "core.utils.logger",
        "core.backend_registry",
        "core.music",
    ]
    saved = {n: sys.modules.get(n) for n in stub_names}
    for name in stub_names:
        sys.modules[name] = types.ModuleType(name)

    sys.modules["open_xiaoai_server"].__getattr__ = lambda n: (lambda *a, **k: None)

    ref = sys.modules["core.ref"]
    ref.get_speaker = lambda: None
    ref.get_xiaoai = lambda: None
    ref.get_kws = lambda: None

    sys.modules["core.wakeup_session"].EventManager = FakeEvents
    FakeEvents.reset()

    admin = sys.modules["core.services.admin_api"]

    class _AdminAPI:
        async def auth_middleware(self, request, handler):
            return await handler(request)

        def register(self, app):
            pass

    admin.AdminAPI = _AdminAPI

    sys.modules["core.services.tts.doubao"].DoubaoTTS = type("DoubaoTTS", (), {})
    sys.modules["core.services.speaker"].AnnounceRejected = type(
        "AnnounceRejected", (Exception,), {}
    )
    sys.modules["core.services.health"].build_health_payload = lambda *a, **k: {}
    sys.modules["core.device"].get_playback_events = lambda *a, **k: []
    sys.modules["core.utils.config"].ConfigManager = FakeConfig
    sys.modules["core.backend_registry"].backend_registry = FakeBackendRegistry

    class _Logger:
        def __getattr__(self, _n):
            return lambda *a, **k: None

    sys.modules["core.utils.logger"].logger = _Logger()

    spec = importlib.util.spec_from_file_location(
        "api_server_under_test_b37",
        ROOT / "core/services/api_server.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, saved


def _restore_modules(saved):
    for name, original in saved.items():
        if original is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = original


def _json(resp):
    return json.loads(resp.text)


_MODULE = None
_SAVED = None


class _ApiServerFixture:
    """setUpClass 桩态加载 + tearDownClass 恢复（同 test_injection_long_term）。"""

    @classmethod
    def _setup(cls):
        global _MODULE
        cls.mod, cls._saved = _load_api_server_module()
        _MODULE = cls.mod

    @classmethod
    def _teardown(cls):
        _restore_modules(cls._saved)


def _make_server(config_extra=None):
    FakeConfig.DATA = {
        "external_text": {"enabled": True, "backend": "openai", "min_interval": 10},
    }
    if config_extra:
        for key, value in config_extra.items():
            FakeConfig.DATA[key] = value
    return _MODULE.APIServer(host="127.0.0.1", port=0)


def _cidr_env(cidr):
    """返回一个把 EXTERNAL_TEXT_ALLOW_CIDR 置为给定值的 environ patch。"""
    return mock.patch.dict(os.environ, {ENV_EXTERNAL_TEXT_ALLOW_CIDR: cidr})


# ---------------------------------------------------------------- 路由与关闭态


class RouteRegistrationTest(_ApiServerFixture, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._setup()

    @classmethod
    def tearDownClass(cls):
        cls._teardown()

    def test_route_registered(self):
        server = _make_server()
        canonicals = [res.canonical for res in server.app.router.resources()]
        self.assertIn("/api/external/text", canonicals)


class DisabledModuleTest(_ApiServerFixture, unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls._setup()

    @classmethod
    def tearDownClass(cls):
        cls._teardown()

    def setUp(self):
        FakeEvents.reset()

    async def test_post_disabled_returns_503(self):
        server = _make_server(config_extra={"external_text": {"enabled": False}})
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(ENV_EXTERNAL_TEXT_ALLOW_CIDR, None)
            resp = await server.handle_external_text(FakeRequest({"text": "hi"}))
        self.assertEqual(resp.status, 503)
        self.assertEqual(_json(resp)["code"], "external_text_disabled")
        self.assertEqual(FakeEvents.begin_calls, [])


class CidrGateTest(_ApiServerFixture, unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls._setup()

    @classmethod
    def tearDownClass(cls):
        cls._teardown()

    def setUp(self):
        FakeEvents.reset()

    async def test_no_cidr_configured_is_refused(self):
        server = _make_server()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(ENV_EXTERNAL_TEXT_ALLOW_CIDR, None)
            resp = await server.handle_external_text(FakeRequest({"text": "hi"}))
        self.assertEqual(resp.status, 503)
        self.assertEqual(_json(resp)["code"], "external_text_refused")
        self.assertEqual(FakeEvents.begin_calls, [])

    async def test_peer_outside_cidr_is_refused(self):
        server = _make_server()
        with _cidr_env("10.10.10.0/24"):
            resp = await server.handle_external_text(
                FakeRequest({"text": "hi"}, remote="192.168.1.5")
            )
        self.assertEqual(resp.status, 503)
        self.assertEqual(_json(resp)["code"], "external_text_refused")

    async def test_xff_header_not_trusted_by_default(self):
        server = _make_server()
        with _cidr_env("10.10.10.0/24"):
            resp = await server.handle_external_text(
                FakeRequest(
                    {"text": "hi"},
                    headers={"X-Forwarded-For": "10.10.10.20"},
                    remote="192.168.1.5",
                )
            )
        self.assertEqual(resp.status, 503)
        self.assertEqual(_json(resp)["code"], "external_text_refused")


class AcceptFlowTest(_ApiServerFixture, unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls._setup()

    @classmethod
    def tearDownClass(cls):
        cls._teardown()

    def setUp(self):
        FakeEvents.reset()

    async def test_non_silent_accepts_and_finishes_in_background(self):
        server = _make_server()
        with _cidr_env("10.10.10.0/24"):
            resp = await server.handle_external_text(
                FakeRequest({"text": "把客厅灯关掉", "source": "cch-screen"})
            )
        self.assertEqual(resp.status, 202)
        body = _json(resp)
        self.assertTrue(body["success"])
        self.assertTrue(body["accepted"])
        self.assertEqual(
            FakeEvents.begin_calls,
            [
                {
                    "name": "openai",
                    "text": "把客厅灯关掉",
                    "source": "cch-screen",
                    "silent": False,
                }
            ],
        )
        await asyncio.sleep(0)
        self.assertEqual(len(FakeEvents.finish_calls), 1)

    async def test_silent_awaits_and_returns_reply(self):
        server = _make_server()
        with _cidr_env("10.10.10.0/24"):
            resp = await server.handle_external_text(
                FakeRequest({"text": "现在几点", "source": "cch-screen", "silent": True})
            )
        self.assertEqual(resp.status, 200)
        body = _json(resp)
        self.assertTrue(body["success"])
        self.assertTrue(body["completed"])
        self.assertEqual(body["reply"], "好的")
        self.assertEqual(body["outcome"], "played")
        self.assertEqual(body["turn"], 7)
        self.assertEqual(len(FakeEvents.finish_calls), 1)

    async def test_busy_returns_409(self):
        server = _make_server()
        FakeEvents.begin_result = None
        with _cidr_env("10.10.10.0/24"):
            resp = await server.handle_external_text(FakeRequest({"text": "hi"}))
        self.assertEqual(resp.status, 409)
        self.assertEqual(_json(resp)["code"], "external_text_busy")
        self.assertEqual(FakeEvents.finish_calls, [])

    async def test_unknown_backend_returns_503(self):
        server = _make_server(
            config_extra={"external_text": {"enabled": True, "backend": "nope"}}
        )
        with _cidr_env("10.10.10.0/24"):
            resp = await server.handle_external_text(FakeRequest({"text": "hi"}))
        self.assertEqual(resp.status, 503)
        self.assertEqual(_json(resp)["code"], "external_text_backend_unknown")
        self.assertEqual(FakeEvents.begin_calls, [])

    async def test_begin_exception_returns_503_start_failed(self):
        """占位段异常（如控制器构造失败）→ 503 code=external_text_start_failed，不裸 500。"""
        server = _make_server()

        @classmethod
        def boom(cls, name, *, text, source="", silent=False):
            raise RuntimeError("controller construct failed")

        with mock.patch.object(FakeEvents, "begin_external_text_session", boom):
            with _cidr_env("10.10.10.0/24"):
                resp = await server.handle_external_text(FakeRequest({"text": "hi"}))
        self.assertEqual(resp.status, 503)
        self.assertEqual(_json(resp)["code"], "external_text_start_failed")
        self.assertEqual(FakeEvents.finish_calls, [])


class PayloadAndThrottleTest(_ApiServerFixture, unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls._setup()

    @classmethod
    def tearDownClass(cls):
        cls._teardown()

    def setUp(self):
        FakeEvents.reset()

    async def test_missing_text_returns_400(self):
        server = _make_server()
        with _cidr_env("10.10.10.0/24"):
            resp = await server.handle_external_text(FakeRequest({}))
        self.assertEqual(resp.status, 400)
        self.assertEqual(FakeEvents.begin_calls, [])

    async def test_too_long_text_returns_400(self):
        server = _make_server()
        with _cidr_env("10.10.10.0/24"):
            resp = await server.handle_external_text(FakeRequest({"text": "好" * 501}))
        self.assertEqual(resp.status, 400)
        self.assertEqual(FakeEvents.begin_calls, [])

    async def test_same_caller_within_interval_is_rate_limited(self):
        server = _make_server()
        with _cidr_env("10.10.10.0/24"):
            first = await server.handle_external_text(FakeRequest({"text": "hi"}))
            second = await server.handle_external_text(FakeRequest({"text": "hi"}))
        self.assertEqual(first.status, 202)
        self.assertEqual(second.status, 429)
        self.assertEqual(_json(second)["code"], "external_text_rate_limited")
        self.assertEqual(len(FakeEvents.begin_calls), 1)

    async def test_rate_limit_disabled_with_zero_interval(self):
        server = _make_server(
            config_extra={"external_text": {"enabled": True, "min_interval": 0}}
        )
        with _cidr_env("10.10.10.0/24"):
            first = await server.handle_external_text(FakeRequest({"text": "hi"}))
            second = await server.handle_external_text(FakeRequest({"text": "hi"}))
        self.assertEqual(first.status, 202)
        self.assertEqual(second.status, 202)


class AuthMiddlewareTest(_ApiServerFixture, unittest.IsolatedAsyncioTestCase):
    """真实 auth（classify 默认 write）下的中间件判定。"""

    @classmethod
    def setUpClass(cls):
        cls._setup()

    @classmethod
    def tearDownClass(cls):
        cls._teardown()

    def setUp(self):
        FakeEvents.reset()

    async def _through_middleware(self, server, headers):
        request = FakeRequest({"text": "hi"}, headers=headers)
        passed = []

        async def handler(req):
            passed.append(True)
            return web.json_response({"ok": True})

        return await server.api_auth_middleware(request, handler), passed

    async def test_missing_token_is_401(self):
        server = _make_server()
        with mock.patch.dict(os.environ, {"BRIDGE_API_TOKENS": f"cch:{_VALID_TOKEN}"}):
            resp, passed = await self._through_middleware(server, {})
        self.assertEqual(resp.status, 401)
        self.assertEqual(passed, [])

    async def test_wrong_token_is_403(self):
        server = _make_server()
        with mock.patch.dict(os.environ, {"BRIDGE_API_TOKENS": f"cch:{_VALID_TOKEN}"}):
            resp, passed = await self._through_middleware(
                server, {"Authorization": "Bearer " + "b" * 40}
            )
        self.assertEqual(resp.status, 403)
        self.assertEqual(passed, [])

    async def test_valid_token_passes_and_stashes_label(self):
        server = _make_server()
        request = FakeRequest(
            {"text": "hi"}, headers={"Authorization": f"Bearer {_VALID_TOKEN}"}
        )

        async def handler(req):
            return web.json_response({"label": req.get("auth_label", "")})

        with mock.patch.dict(os.environ, {"BRIDGE_API_TOKENS": f"cch:{_VALID_TOKEN}"}):
            resp = await server.api_auth_middleware(request, handler)
        self.assertEqual(resp.status, 200)
        self.assertEqual(json.loads(resp.text)["label"], "cch")

    async def test_unconfigured_tokens_fail_closed_503(self):
        server = _make_server()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BRIDGE_API_TOKENS", None)
            os.environ.pop("BRIDGE_API_TOKEN", None)
            resp, passed = await self._through_middleware(server, {})
        self.assertEqual(resp.status, 503)
        self.assertEqual(passed, [])


if __name__ == "__main__":
    unittest.main()
