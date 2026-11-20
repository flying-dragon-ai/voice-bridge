"""A3-4 起播前编排离线契约（facts §2.48：预清残留 + 条件重注册 + 9102 白名单模板）。"""
import importlib.util
import unittest
from pathlib import Path
from unittest import mock

from core.music import cch_cast


def _fake_musicctl(responses):
    calls = []

    async def fake(op):
        calls.append(op)
        return responses.get(op, (False, {}))

    return fake, calls


class PreparePlaybackTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        cch_cast._last_core_pid = 0

    async def test_first_call_baseline_no_reinit(self):
        fake, calls = _fake_musicctl({
            "prestop": (True, {"rc": 0}),
            "corepid": (True, {"core_pid": 111}),
        })
        with mock.patch.object(cch_cast, "musicctl", fake):
            r = await cch_cast.prepare_playback()
        self.assertEqual(calls, ["prestop", "corepid"])
        self.assertTrue(r["prestop"])
        self.assertFalse(r["reinit"])
        self.assertEqual(r["core_pid"], 111)
        self.assertEqual(cch_cast._last_core_pid, 111)

    async def test_pid_change_triggers_reinit(self):
        cch_cast._last_core_pid = 111
        fake, calls = _fake_musicctl({
            "prestop": (True, {}),
            "corepid": (True, {"core_pid": 222}),
            "reinit": (True, {"old_pid": 7549, "new_pid": 14296}),
        })
        with mock.patch.object(cch_cast, "musicctl", fake):
            r = await cch_cast.prepare_playback()
        self.assertEqual(calls, ["prestop", "corepid", "reinit"])
        self.assertTrue(r["reinit"])
        self.assertEqual(cch_cast._last_core_pid, 222)

    async def test_pid_same_no_reinit(self):
        cch_cast._last_core_pid = 222
        fake, calls = _fake_musicctl({
            "prestop": (True, {}),
            "corepid": (True, {"core_pid": 222}),
        })
        with mock.patch.object(cch_cast, "musicctl", fake):
            r = await cch_cast.prepare_playback()
        self.assertEqual(calls, ["prestop", "corepid"])
        self.assertFalse(r["reinit"])

    async def test_prestop_fail_not_blocking(self):
        fake, calls = _fake_musicctl({
            "prestop": (False, {"error": "http 502"}),
            "corepid": (True, {"core_pid": 333}),
        })
        with mock.patch.object(cch_cast, "musicctl", fake):
            r = await cch_cast.prepare_playback()
        self.assertFalse(r["prestop"])
        self.assertEqual(r["core_pid"], 333)
        self.assertEqual(cch_cast._last_core_pid, 333)

    async def test_corepid_fail_no_cache_no_reinit(self):
        cch_cast._last_core_pid = 111
        fake, calls = _fake_musicctl({
            "prestop": (True, {}),
            "corepid": (False, {}),
        })
        with mock.patch.object(cch_cast, "musicctl", fake):
            r = await cch_cast.prepare_playback()
        self.assertEqual(calls, ["prestop", "corepid"])
        self.assertFalse(r["reinit"])
        self.assertEqual(cch_cast._last_core_pid, 111)


PS_OUT = (
    "system        7456   285 1268876 136760                     0 S com.alibaba.ailabs.genie.smartapp\n"
    "system       14296   285 1146768 102244                     0 S com.alibaba.ailabs.genie.smartapp:business\n"
    "system       22347   285 1240964 135240                     0 S com.alibaba.ailabs.genie.smartapp:core\n"
)


def _load_tts():
    path = Path(__file__).resolve().parents[2] / "deploy" / "cch-tts" / "tts_http.py"
    spec = importlib.util.spec_from_file_location("tts_http_a3test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TtsMusicctlTest(unittest.TestCase):
    def setUp(self):
        self.mod = _load_tts()
        self.cmds = []

        def fake_run(args, timeout=25):
            self.cmds.append(args[-1])
            if args[-1] == "ps":
                return 0, PS_OUT, ""
            return 0, "", ""

        self.run_patch = mock.patch.object(self.mod, "_run", fake_run)
        self.conn_patch = mock.patch.object(self.mod, "_connect", lambda: None)
        self.sleep_patch = mock.patch.object(self.mod.time, "sleep", lambda s: None)
        self.run_patch.start()
        self.conn_patch.start()
        self.sleep_patch.start()
        self.addCleanup(self.run_patch.stop)
        self.addCleanup(self.conn_patch.stop)
        self.addCleanup(self.sleep_patch.stop)

    def test_op_whitelist(self):
        ok, payload = self.mod.musicctl("rm -rf")
        self.assertFalse(ok)
        self.assertIn("error", payload)
        self.assertEqual(self.cmds, [])

    def test_corepid_parse(self):
        ok, payload = self.mod.musicctl("corepid")
        self.assertTrue(ok)
        self.assertEqual(payload["core_pid"], 22347)

    def test_prestop_template(self):
        ok, payload = self.mod.musicctl("prestop")
        self.assertTrue(ok)
        self.assertEqual(len(self.cmds), 1)
        self.assertIn("am startservice -a com.alibaba.ailabs.genie.musicplayer.cmd", self.cmds[0])
        self.assertIn("--ei Control 22", self.cmds[0])
        self.assertIn(self.mod.MUSIC_SERVICE_CMP, self.cmds[0])

    def test_reinit_kill_template_and_new_pid(self):
        ok, payload = self.mod.musicctl("reinit")
        self.assertTrue(ok)
        self.assertEqual(payload["old_pid"], 14296)
        self.assertEqual(payload["new_pid"], 14296)
        self.assertIn("su -c 'kill 14296'", self.cmds)

    def test_reinit_no_business_fails(self):
        with mock.patch.object(self.mod, "_smartapp_pids", lambda: {}):
            ok, payload = self.mod.musicctl("reinit")
        self.assertFalse(ok)
        self.assertIn("error", payload)


if __name__ == "__main__":
    unittest.main()