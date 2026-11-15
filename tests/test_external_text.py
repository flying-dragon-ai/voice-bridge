"""外部 ASR 文本入口（B-37）纯逻辑测试。

覆盖 `core/services/external_text.py`（不依赖 aiohttp / 设备 / 会话实现）：
  - 开关判定：默认关 / config 开 / env 覆盖 / 非法 env 回落（同 resolve_inject_enabled 口径）
  - 限源判定：未配 refused / 非法 refused（fail closed）/ 合法出网段
  - 后端解析：默认 openai / config 覆盖 / 非法回落
  - 载荷校验：text 缺失/非串/空/超长、source 截断、silent 严格解析
  - 限流：同 key 间隔内拒、间隔外放、不同 key 独立、min_interval<=0 直通
"""

import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.services.external_text import (  # noqa: E402
    ENV_EXTERNAL_TEXT_ALLOW_CIDR,
    ENV_EXTERNAL_TEXT_ENABLE,
    MAX_SOURCE_CHARS,
    MAX_TEXT_CHARS,
    ExternalTextThrottle,
    resolve_external_text_backend,
    resolve_external_text_cidrs,
    resolve_external_text_enabled,
    validate_external_text_payload,
)


class ResolveEnabledTest(unittest.TestCase):
    def test_default_is_disabled(self):
        self.assertFalse(resolve_external_text_enabled(None, env={}))

    def test_dict_config_enabled(self):
        self.assertTrue(resolve_external_text_enabled({"external_text": {"enabled": True}}, env={}))

    def test_config_manager_like_object(self):
        class _Cfg:
            def get_app_config(self, path, default=None):
                return True if path == "external_text.enabled" else default

        self.assertTrue(resolve_external_text_enabled(_Cfg(), env={}))

    def test_env_overrides_config(self):
        cfg = {"external_text": {"enabled": False}}
        self.assertTrue(resolve_external_text_enabled(cfg, env={ENV_EXTERNAL_TEXT_ENABLE: "1"}))
        cfg_on = {"external_text": {"enabled": True}}
        self.assertFalse(resolve_external_text_enabled(cfg_on, env={ENV_EXTERNAL_TEXT_ENABLE: "0"}))

    def test_invalid_env_falls_back_to_config(self):
        cfg = {"external_text": {"enabled": True}}
        self.assertTrue(resolve_external_text_enabled(cfg, env={ENV_EXTERNAL_TEXT_ENABLE: "bogus"}))
        self.assertFalse(resolve_external_text_enabled(None, env={ENV_EXTERNAL_TEXT_ENABLE: "bogus"}))

    def test_string_config_value(self):
        self.assertTrue(resolve_external_text_enabled({"external_text": {"enabled": "yes"}}, env={}))


class ResolveCidrsTest(unittest.TestCase):
    def test_empty_is_refused(self):
        result = resolve_external_text_cidrs(env={})
        self.assertEqual(result.networks, ())
        self.assertTrue(result.refused)
        self.assertTrue(result.refused_reason)

    def test_invalid_cidr_is_refused(self):
        result = resolve_external_text_cidrs(env={ENV_EXTERNAL_TEXT_ALLOW_CIDR: "10.0.0.0/99"})
        self.assertEqual(result.networks, ())
        self.assertTrue(result.refused)

    def test_valid_cidrs(self):
        result = resolve_external_text_cidrs(
            env={ENV_EXTERNAL_TEXT_ALLOW_CIDR: "10.10.10.20/32,192.168.1.0/24"}
        )
        self.assertFalse(result.refused)
        self.assertEqual(len(result.networks), 2)


class ResolveBackendTest(unittest.TestCase):
    def test_default_openai(self):
        self.assertEqual(resolve_external_text_backend(None), "openai")
        self.assertEqual(resolve_external_text_backend({}), "openai")

    def test_config_override(self):
        cfg = {"external_text": {"backend": "openclaw"}}
        self.assertEqual(resolve_external_text_backend(cfg), "openclaw")

    def test_invalid_falls_back_to_default(self):
        for bad in ({"external_text": {"backend": ""}}, {"external_text": {"backend": 123}}):
            self.assertEqual(resolve_external_text_backend(bad), "openai")


class ValidatePayloadTest(unittest.TestCase):
    def test_minimal_payload(self):
        payload, error = validate_external_text_payload({"text": "  把客厅灯关掉  "})
        self.assertIsNone(error)
        self.assertEqual(payload.text, "把客厅灯关掉")
        self.assertEqual(payload.source, "")
        self.assertFalse(payload.silent)

    def test_full_payload(self):
        payload, error = validate_external_text_payload(
            {"text": "今天天气怎么样", "source": "cch-screen", "silent": True}
        )
        self.assertIsNone(error)
        self.assertEqual(payload.source, "cch-screen")
        self.assertTrue(payload.silent)

    def test_missing_text(self):
        _, error = validate_external_text_payload({})
        self.assertTrue(error)

    def test_non_string_text(self):
        _, error = validate_external_text_payload({"text": 123})
        self.assertTrue(error)

    def test_empty_after_strip(self):
        _, error = validate_external_text_payload({"text": "   "})
        self.assertTrue(error)

    def test_too_long_text(self):
        _, error = validate_external_text_payload({"text": "好" * (MAX_TEXT_CHARS + 1)})
        self.assertTrue(error)
        payload, error = validate_external_text_payload({"text": "好" * MAX_TEXT_CHARS})
        self.assertIsNone(error)

    def test_source_truncated(self):
        payload, error = validate_external_text_payload(
            {"text": "hi", "source": "x" * (MAX_SOURCE_CHARS + 10)}
        )
        self.assertIsNone(error)
        self.assertEqual(payload.source, "x" * MAX_SOURCE_CHARS)

    def test_source_non_string_ignored(self):
        payload, error = validate_external_text_payload({"text": "hi", "source": ["a"]})
        self.assertIsNone(error)
        self.assertEqual(payload.source, "")

    def test_silent_truthy_string(self):
        payload, _ = validate_external_text_payload({"text": "hi", "silent": "yes"})
        self.assertTrue(payload.silent)

    def test_silent_false_string_is_false(self):
        payload, _ = validate_external_text_payload({"text": "hi", "silent": "false"})
        self.assertFalse(payload.silent)

    def test_silent_nonzero_int_is_true(self):
        payload, _ = validate_external_text_payload({"text": "hi", "silent": 1})
        self.assertTrue(payload.silent)
        payload, _ = validate_external_text_payload({"text": "hi", "silent": 0})
        self.assertFalse(payload.silent)


class ThrottleTest(unittest.TestCase):
    def test_first_acquire_allowed_then_blocked_within_interval(self):
        throttle = ExternalTextThrottle()
        now = time.monotonic()
        self.assertTrue(throttle.try_acquire("k1", now=now, min_interval=10.0))
        self.assertFalse(throttle.try_acquire("k1", now=now + 5, min_interval=10.0))

    def test_allowed_after_interval(self):
        throttle = ExternalTextThrottle()
        now = time.monotonic()
        self.assertTrue(throttle.try_acquire("k1", now=now, min_interval=10.0))
        self.assertTrue(throttle.try_acquire("k1", now=now + 10.5, min_interval=10.0))

    def test_rejected_attempt_does_not_extend_window(self):
        throttle = ExternalTextThrottle()
        now = time.monotonic()
        self.assertTrue(throttle.try_acquire("k1", now=now, min_interval=10.0))
        for offset in (1, 2, 3):
            self.assertFalse(throttle.try_acquire("k1", now=now + offset, min_interval=10.0))
        # 窗口从首次放行起算，被拒尝试不顺延
        self.assertTrue(throttle.try_acquire("k1", now=now + 10.5, min_interval=10.0))

    def test_keys_independent(self):
        throttle = ExternalTextThrottle()
        now = time.monotonic()
        self.assertTrue(throttle.try_acquire("a", now=now, min_interval=10.0))
        self.assertTrue(throttle.try_acquire("b", now=now, min_interval=10.0))

    def test_zero_or_negative_interval_disables_limit(self):
        throttle = ExternalTextThrottle()
        now = time.monotonic()
        self.assertTrue(throttle.try_acquire("k", now=now, min_interval=0))
        self.assertTrue(throttle.try_acquire("k", now=now, min_interval=0))


if __name__ == "__main__":
    unittest.main()
