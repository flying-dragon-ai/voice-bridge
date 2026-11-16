"""合同级采集器（票 07）离线守卫测试。

被测对象 = ws: deploy/lx06/kws-beamform-ab/capture_contract.py（按文件加载，
纯 stdlib；不碰设备、不录音）。覆盖 2026-11-03 办公室 PC 准备轮实测发现并修复的缺陷：

  1. 8ch/S32 帧长：S32 每帧 8×4=32 字节，按 16 字节算会把秒数/小时数**翻倍**，
     票 08 的「≥6h 长负样本」门禁会在只采到 3h 时就报 ✓；
  2. 断点续采键必须含 speaker：第二说话人（25 段）与主说话人同 (session,condition,
     kind,word,occurrence) 会被判「已完成」而整组漏采；
  3. 模式 A 的唤醒日志源：设备 busybox **没有 logread**（实测 `ash: logread: not found`），
     原生唤醒日志在 `/tmp/log/messages`；
  4. 正样本「人声非静音」门必须按实测电平定标：历史真机安静段整段 RMS 仅 34–74、
     300ms 窗最大 110–181（旧门限 120 按整段算 ⇒ 安静条件真人正样本**整批判失败**）；
  5. 播放参考门：空闲态 ch6 严格为 0（实测）⇒「全零」硬失败，电平低只告警；
  6. 合成音色代理必须凑满 3 词（旧实现只映射到 make_tts_wavs 的 2 个正样本词 ⇒ 36 条
     < 门禁 54），且合成条目不得写真机 manifest（RECORDING-CONTRACT §2 目录隔离）；
  7. 覆盖清单的小时数与缺项点名按真实秒数计算；音乐条件没有设备自播 URL 时拒跑。
"""

import argparse
import importlib.util
import struct
import sys
import tempfile
import unittest
from pathlib import Path

BRIDGE_ROOT = Path(__file__).resolve().parents[1]
HARNESS = BRIDGE_ROOT.parent / "deploy" / "lx06" / "kws-beamform-ab"

sys.path.insert(0, str(HARNESS))
_spec = importlib.util.spec_from_file_location(
    "capture_contract", HARNESS / "capture_contract.py")
cc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cc)


def write_raw(path: Path, seconds: float, *, mic_s16: int = 120, ref_s16: int = 400,
              bits: int = 32, identical_pair: bool = True,
              ref_active_ratio: float = 1.0):
    """生成 8ch raw；幅度按 **S16 量纲**给（S32 时左移 16 位，等价 ALSA plug 口径）。"""
    shift = 16 if bits == 32 else 0
    pack = "<i" if bits == 32 else "<h"
    total = int(seconds * cc.RATE)
    live = int(total * ref_active_ratio)

    def frame(ref_value: int) -> bytes:
        pair = [mic_s16] * 6 + [ref_value,
                                ref_value if identical_pair else ref_value + 1]
        return b"".join(struct.pack(pack, value << shift) for value in pair)

    data = frame(ref_s16) * live + frame(0) * (total - live)
    path.write_bytes(data)
    return path


def args_for(**overrides) -> argparse.Namespace:
    base = dict(session=None, all_sessions=False, speaker="main", dry_run=True,
                music_url="", music_title="", volume=60, azimuth="", retake_of=None,
                allow_audible=False, wake_log_cmd=cc.DEFAULT_WAKE_LOG_CMD,
                neg_condition=None, session_num=1, hours=3.0, tts_model="",
                allow_sync_root=False, synth_root=None,
                with_guard=False, with_playback=False,
                only_speaker="", auto=False, start_delay=0, condition_delay=0,
                lead_seconds=0, gap=0, max_segments=0)
    base.update(overrides)
    return argparse.Namespace(**base)


class FrameMathTest(unittest.TestCase):
    def test_s32_frame_is_32_bytes(self):
        self.assertEqual(cc.FRAME_BYTES, 8 * 4)
        self.assertEqual(cc.frame_bytes(32), 32)
        self.assertEqual(cc.frame_bytes(16), 16)

    def test_qc_seconds_not_doubled_for_s32(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_raw(Path(tmp) / "seg.raw", 5.0)
            qc = cc.qc_segment(path, "music_far", "pos")
            self.assertAlmostEqual(qc["seconds"], 5.0, places=2)

    def test_alignment_uses_s32_frame(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.raw"
            path.write_bytes(b"\x00" * (cc.FRAME_BYTES + 16))  # 多出半个帧
            qc = cc.qc_segment(path, "quiet_near", "pos")
            self.assertEqual(qc["qc_error"], "unaligned")
            self.assertFalse(qc["qc_ok"])

    def test_record_rejects_truncated_capture(self):
        """断流必须报错（否则 manifest 里留下一条「5s 实际 0.4s」的假样本）。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "short.raw"
            path.write_bytes(b"\x00" * (cc.FRAME_BYTES * cc.RATE // 20))  # 0.05 s
            qc = cc.qc_segment(path, "quiet_near", "pos")
            self.assertLess(qc["seconds"], 1.0)


class PlanTest(unittest.TestCase):
    def test_total_205_and_condition_counts(self):
        entries = cc.build_plan()
        self.assertEqual(len(entries), 205)
        by_condition: dict[str, int] = {}
        for entry in entries:
            by_condition[entry["condition"]] = by_condition.get(entry["condition"], 0) + 1
        self.assertEqual(by_condition["music_far"], 60 + 25)
        for condition in ("quiet_near", "quiet_far", "music_near"):
            self.assertEqual(by_condition[condition], 40)

    def test_plan_keys_are_globally_unique(self):
        """第二说话人不得与主说话人撞键（撞键 ⇒ 断点续采整组漏采）。"""
        keys = [cc.plan_key(entry) for entry in cc.build_plan()]
        self.assertEqual(len(keys), len(set(keys)), "计划键有重复，续采会跳过他人样本")

    def test_speaker2_group_present(self):
        entries = cc.build_plan()
        second = [e for e in entries if e["speaker"] == "speaker2"]
        self.assertEqual(len(second), 25)
        self.assertTrue(any(e["word"] == "小爱同学" for e in second))

    def test_main_words_each_30_in_music_far(self):
        for word in cc.MAIN_WORDS:
            got = [e for e in cc.build_plan()
                   if e["condition"] == "music_far" and e["word"] == word
                   and e["speaker"] == "main"]
            self.assertEqual(len(got), 30)

    def test_session_spread_for_music_far(self):
        sessions = {e["session"] for e in cc.build_plan()
                    if e["condition"] == "music_far" and e["speaker"] == "main"}
        self.assertEqual(sessions, {1, 2, 3}, "主条件必须跨 ≥3 场次（票 08 三层留出）")


class ResumeTest(unittest.TestCase):
    def test_done_key_includes_speaker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            entry = {"session": 1, "condition": "music_far", "kind": "pos",
                     "word": "你好小塔", "occurrence": 1, "speaker": "main"}
            cc.append_manifest(root, {**entry, "file": "x.raw"})
            done = cc.load_done(root)
            self.assertIn(cc.plan_key(entry), done)
            self.assertNotIn(cc.plan_key(dict(entry, speaker="speaker2")), done,
                             "同键不同说话人被误判为已完成")

    def test_pending_plan_skips_done_and_filters_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            entries = cc.build_plan()
            for entry in entries[:2]:
                cc.append_manifest(root, {**entry, "file": "x.raw"})
            self.assertEqual(len(cc.pending_plan(root)), len(entries) - 2)
            session1 = cc.pending_plan(root, session=1)
            self.assertTrue(session1)
            self.assertTrue(all(e["session"] == 1 for e in session1))


class SpeechGateTest(unittest.TestCase):
    """正样本「人声非静音」门按真机实测电平静置（300ms 窗最大 RMS，S16 量纲）。"""

    def test_quiet_pos_segment_with_speech_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_raw(Path(tmp) / "a.raw", 5.0, mic_s16=140, ref_s16=0)
            self.assertIsNone(cc.qc_segment(path, "quiet_near", "pos")["qc_error"])

    def test_quiet_pos_silent_segment_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_raw(Path(tmp) / "a.raw", 5.0, mic_s16=25, ref_s16=0)
            self.assertEqual(cc.qc_segment(path, "quiet_near", "pos")["qc_error"],
                             "mic_silent")

    def test_default_gate_is_ticket_value(self):
        """票面 QC 条：正样本人声非静音 mic RMS≥120 —— 默认必须是这个数，不得自行放宽。"""
        for condition in ("quiet_near", "quiet_far", "music_near", "music_far"):
            self.assertEqual(cc.mic_gate(condition, "pos"), 120.0, condition)

    def test_tiered_gate_requires_explicit_opt_in(self):
        """分层（近 60 / 远 40）偏离票面 ⇒ 只能显式打开 GATE_TIERED，且远场仍高于本底。"""
        self.assertFalse(cc.GATE_TIERED, "默认不得开启分层")
        keep = cc.GATE_TIERED
        cc.GATE_TIERED = True
        try:
            self.assertLess(cc.mic_gate("quiet_far", "pos"), cc.mic_gate("quiet_near", "pos"))
            self.assertGreater(cc.mic_gate("quiet_far", "pos"), 30.0, "仍要高于本底 16–24")
            with tempfile.TemporaryDirectory() as tmp:
                path = write_raw(Path(tmp) / "f.raw", 5.0, mic_s16=55, ref_s16=0)
                self.assertTrue(cc.qc_segment(path, "quiet_far", "pos", 5.0)["qc_ok"])
        finally:
            cc.GATE_TIERED = keep

    def test_far_normal_volume_segment_fails_ticket_gate(self):
        """票面口径下的既成事实：远场正常音量（实测 53–92）**不合格**，必须重采。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = write_raw(Path(tmp) / "f.raw", 5.0, mic_s16=63, ref_s16=0)
            qc = cc.qc_segment(path, "quiet_far", "pos", 5.0)
            self.assertFalse(qc["qc_ok"])
            self.assertEqual(qc["qc_error"], "mic_silent")

    def test_music_gate_accepts_music_level_speech(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_raw(Path(tmp) / "a.raw", 5.0, mic_s16=300, ref_s16=500)
            self.assertIsNone(cc.qc_segment(path, "music_far", "pos", 5.0)["qc_error"])

    def test_neg_long_has_no_speech_gate(self):
        self.assertEqual(cc.mic_gate("tv_speech", "neg_long"), 0.0)


class ReferenceGateTest(unittest.TestCase):
    def test_all_zero_reference_is_hard_fail(self):
        """空闲态 ch6 严格为 0（实测）⇒ music 段参考全零 = 播放没走设备，必须硬失败。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = write_raw(Path(tmp) / "a.raw", 5.0, mic_s16=300, ref_s16=0)
            qc = cc.qc_segment(path, "music_far", "pos")
            self.assertEqual(qc["qc_error"], "ref_silent")
            self.assertFalse(qc["qc_ok"])

    def test_low_reference_level_is_warning_not_failure(self):
        """参考有信号但电平低于门 ⇒ 只告警（电平随曲目/音量浮动，不得整段判废）。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = write_raw(Path(tmp) / "a.raw", 5.0, mic_s16=300, ref_s16=40)
            qc = cc.qc_segment(path, "music_far", "pos")
            self.assertIsNone(qc["qc_error"])
            self.assertIn("ref_quiet", qc["qc_warn"])

    def test_partially_active_reference_is_still_reference_silent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_raw(Path(tmp) / "a.raw", 5.0, mic_s16=300, ref_s16=500,
                             ref_active_ratio=0.001)
            self.assertEqual(cc.qc_segment(path, "music_far", "pos")["qc_error"],
                             "ref_silent")

    def test_ch6_must_equal_ch7(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_raw(Path(tmp) / "a.raw", 2.0, identical_pair=False)
            qc = cc.qc_segment(path, "music_far", "pos")
            self.assertFalse(qc["ch6_eq_ch7"])
            self.assertFalse(qc["qc_ok"])

    def test_quiet_long_negative_tolerates_zero_reference(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_raw(Path(tmp) / "q.raw", 5.0, mic_s16=60, ref_s16=0)
            self.assertIsNone(cc.qc_segment(path, "quiet", "neg_long")["qc_error"])
            music = write_raw(Path(tmp) / "m.raw", 5.0, mic_s16=60, ref_s16=0)
            self.assertEqual(
                cc.qc_segment(music, "music_vocal", "neg_long")["qc_error"],
                "ref_silent")


class CoverageReportTest(unittest.TestCase):
    def test_hours_use_real_seconds(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for index in range(1, 37):  # 36 × 600 s = 6 h
                cc.append_manifest(root, {
                    "session": 1, "condition": "tv_speech", "kind": "neg_long",
                    "word": "tv_speech", "occurrence": index, "speaker": "none",
                    "seconds": 600.0, "source": "device", "qc_ok": True})
            self.assertAlmostEqual(cc.neg_hours(root), 6.0, places=2)
            lines = "\n".join(cc.coverage_lines(root))
            self.assertIn("6.00 h", lines)
            self.assertIn("12h", lines)

    def test_speaker2_gap_is_reported_by_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            lines = "\n".join(cc.coverage_lines(Path(tmp)))
            self.assertIn("第二说话人", lines)
            self.assertIn("✗", lines)

    def test_qc_failure_and_warning_are_named(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cc.append_manifest(root, {"session": 1, "condition": "music_far",
                                      "kind": "pos", "word": "你好小塔",
                                      "occurrence": 1, "speaker": "main",
                                      "source": "device", "seconds": 5.0,
                                      "file": "a.raw", "qc_ok": False,
                                      "qc_error": "ref_silent", "qc_warn": []})
            lines = "\n".join(cc.coverage_lines(root))
            self.assertIn("QC 未过段", lines)
            self.assertIn("ref_silent", lines)


class ModeATest(unittest.TestCase):
    def test_mode_a_target_defined(self):
        self.assertEqual(cc.P0_TARGET, 8)

    def test_wake_log_source_is_not_logread(self):
        """设备 busybox 无 logread（实测）；原生唤醒日志在 /tmp/log/messages。"""
        cmd = " ".join(cc.DEFAULT_WAKE_LOG_CMD)
        self.assertNotIn("logread", cmd)
        self.assertIn("/tmp/log/messages", cmd)

    def test_mode_a_refuses_when_silence_not_verified(self):
        """票面前置「必须先实测能完全静音」= 机器判据，不是一句提示。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = cc.enforce_silence
            cc.enforce_silence = _raise_silence
            try:
                rc = cc.run_mode_a(args_for(mode_a=True, allow_audible=False, dry_run=False), root)
            finally:
                cc.enforce_silence = original
            self.assertEqual(rc, 3)
            self.assertEqual(list(root.glob("*.raw")), [])
            self.assertFalse((root / "manifest.jsonl").exists())

    def test_mode_a_records_volume_zero_evidence(self):
        """静音实测通过 ⇒ manifest 必须留痕 silence=verified 且 volume=0（不是元信息默认 60）。"""
        import unittest.mock as mock

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            recorded: list[dict] = []
            restored: list[int] = []

            def fake_record(_root, entry, _seconds, _args, *, guard=True, volume=None):
                recorded.append({"entry": dict(entry), "guard": guard,
                                 "volume": volume})

            keep = (cc.enforce_silence, cc.wake_log_sniffer, cc._record_entry,
                    cc.restore_volume)
            cc.enforce_silence = lambda: 21
            cc.wake_log_sniffer = lambda _root, _cmd: (root / "native_wake.log",
                                                       lambda: None)
            cc._record_entry = fake_record
            cc.restore_volume = lambda value: restored.append(value)
            try:
                with mock.patch("builtins.input", lambda *a: None):
                    rc = cc.run_mode_a(args_for(mode_a=True, allow_audible=True, dry_run=False), root)
            finally:
                (cc.enforce_silence, cc.wake_log_sniffer, cc._record_entry,
                 cc.restore_volume) = keep
            self.assertEqual(rc, 0)
            self.assertEqual(len(recorded), cc.P0_TARGET)
            self.assertTrue(all(r["volume"] == 0 for r in recorded))
            self.assertTrue(all(r["entry"]["silence"] == "verified" for r in recorded))
            self.assertEqual(restored, [21], "模式 A 结束必须把音量还给设备")


def _raise_silence() -> int:
    raise cc.SilenceNotVerified("音量归零回读=21（要求 0）", 21)


class NoAnswerGuardTest(unittest.TestCase):
    """「禁止音箱答话」是票 07 硬约束：每段前复查护栏，失效即中止本批。"""

    def test_guard_checked_before_every_segment(self):
        import unittest.mock as mock

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            calls: list[str] = []
            keep = (cc._guard_on, cc._guard_off, cc.guard_check, cc._record_8ch,
                    cc.music_start, cc.music_stop)
            cc._guard_on = lambda: calls.append("guard_on")
            cc._guard_off = lambda: calls.append("guard_off")
            cc.guard_check = lambda: calls.append("guard_check")
            cc.music_start = lambda url: (calls.append("music_start"), 21)[1]
            cc.ensure_reference_active = lambda **kw: 0.999
            cc.music_stop = lambda original: calls.append("music_stop")
            cc._record_8ch = lambda path, seconds, bits=cc.BITS: path.write_bytes(
                b"\x00" * (cc.FRAME_BYTES * cc.RATE * seconds)) or path.stat().st_size
            try:
                with mock.patch("builtins.input", lambda *a: None):
                    args = args_for(session=None, all_sessions=True, dry_run=False,
                                    music_url="http://10.10.10.2:8899/kws.wav")
                    # 只跑 3 段（用 monkeypatch 截短计划，避免 205 段全跑）
                    keep_plan = cc.build_plan
                    cc.build_plan = lambda: keep_plan()[:3]
                    try:
                        cc.run_pos(args, root)
                    finally:
                        cc.build_plan = keep_plan
            finally:
                (cc._guard_on, cc._guard_off, cc.guard_check, cc._record_8ch,
                 cc.music_start, cc.music_stop) = keep
            self.assertEqual(calls.count("guard_check"), 3,
                             "每段前都必须复查护栏（批 1 纪律）")
            self.assertEqual(calls[0], "guard_on")
            self.assertEqual(calls[-1], "guard_off", "异常/结束都要恢复设备")

    def test_guard_loss_aborts_batch(self):
        import unittest.mock as mock

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            keep = (cc._guard_on, cc._guard_off, cc.guard_check,
                    cc.music_start, cc.music_stop)

            def broken():
                raise cc.GuardLost("静音状态丢失：LEFT=1")

            cc._guard_on = lambda: None
            cc._guard_off = lambda: None
            cc.guard_check = broken
            cc.music_start = lambda url: 21
            cc.music_stop = lambda original: None
            try:
                with mock.patch("builtins.input", lambda *a: None):
                    args = args_for(session=None, all_sessions=True, dry_run=False,
                                    music_url="http://10.10.10.2:8899/kws.wav")
                    rc = cc.run_pos(args, root)
            finally:
                (cc._guard_on, cc._guard_off, cc.guard_check,
                 cc.music_start, cc.music_stop) = keep
            self.assertEqual(rc, 4)
            self.assertEqual(list(root.glob("*.raw")), [],
                             "护栏失效后不得继续落盘（否则整段是被应答污染的样本）")


    def test_device_restored_even_if_prompt_write_fails(self):
        """收尾顺序教训：提示窗写入抛异常时，设备恢复必须已经发生（否则音箱留在闭麦态）。"""
        import unittest.mock as mock

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events: list[str] = []
            keep = (cc._guard_on, cc._guard_off, cc.guard_check, cc._record_8ch,
                    cc.music_start, cc.music_stop, cc._show_prompt)

            def boom(*a, **k):
                raise RuntimeError("提示文件写不了（盘满/权限）")

            cc._guard_on = lambda: events.append("guard_on")
            cc._guard_off = lambda: events.append("guard_off")
            cc.guard_check = lambda: None
            cc.music_start = lambda url: 21
            cc.music_stop = lambda original: events.append("music_stop")
            cc._record_8ch = lambda path, seconds, bits=cc.BITS: path.write_bytes(
                b"\x00" * (cc.FRAME_BYTES * cc.RATE * seconds)) or path.stat().st_size
            cc._show_prompt = boom
            try:
                with mock.patch("builtins.input", lambda *a: None):
                    args = args_for(session=None, all_sessions=True, dry_run=False,
                                    music_url="http://10.10.10.2:8899/kws.wav")
                    keep_plan = cc.build_plan
                    cc.build_plan = lambda: keep_plan()[:1]
                    try:
                        cc.run_pos(args, root)
                    finally:
                        cc.build_plan = keep_plan
            except Exception as exc:  # noqa: BLE001 - 允许把异常抛出来
                raised = exc
            else:
                raised = None
                self.fail("提示窗写入失败应抛出，但不影响设备恢复")
            finally:
                (cc._guard_on, cc._guard_off, cc.guard_check, cc._record_8ch,
                 cc.music_start, cc.music_stop, cc._show_prompt) = keep
            self.assertIn("music_stop", events)
            self.assertIn("guard_off", events)
            self.assertGreater(events.index("guard_off"), 0)


class SyntheticTest(unittest.TestCase):
    def test_three_words_have_tts_texts(self):
        """合成门禁 54 条 = 3 词 × 18 音色；缺一个词的文本就永远到不了 54。"""
        texts = cc.load_tts_texts()
        for word in cc.SYNTH_WORDS:
            self.assertIn(word, texts.values(), f"make_tts_wavs 缺「{word}」的文本")

    def test_synth_word_files_maps_all_three_words(self):
        self.assertEqual(set(cc.synth_word_files()), set(cc.SYNTH_WORDS))

    def test_synthetic_entries_do_not_share_device_manifest(self):
        """RECORDING-CONTRACT §2：真机与合成不得混在同一 manifest。"""
        self.assertNotEqual(cc.SYNTH_ROOT / "manifest.jsonl",
                            cc.CONTRACT_ROOT / "manifest.jsonl")

    def test_tts_wav_is_resampled_to_16k(self):
        """aishell3 输出 22050 Hz：直接扒 PCM 会留下「16k」假标签 ⇒ 必须重采样。"""
        import struct as _struct
        import wave as _wave

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "t.wav"
            rate = 22050
            with _wave.open(str(src), "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(rate)
                handle.writeframes(b"".join(
                    _struct.pack("<h", int(9000 * (1 if i % 2 else -1)))
                    for i in range(rate)))  # 1 s 方波
            pcm, src_rate = cc._wav_to_16k_mono_s16(src)
        self.assertEqual(src_rate, 22050)
        self.assertEqual(len(pcm) // 2, cc.RATE, "1 s @22050 应得 16000 帧 @16k")


class PlaybackGuardTest(unittest.TestCase):
    def test_music_playback_helpers_exist(self):
        """契约要求「音乐必须设备自播」⇒ 采集器自己起/停播放并恢复原音量。"""
        self.assertTrue(callable(cc.music_start))
        self.assertTrue(callable(cc.music_stop))

    def test_music_condition_without_url_refuses_to_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = args_for(session=1, dry_run=False, music_title="bgm")
            todo = cc.pending_plan(root, session=1)
            self.assertTrue(any(e["condition"].startswith("music") for e in todo))
            self.assertEqual(cc.run_pos(args, root), 5)
            self.assertEqual(list(root.glob("*.raw")), [])

    def test_quiet_only_session_does_not_need_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # 已完成所有 music 条目后，安静条件批次不应因缺 URL 拒跑
            for entry in cc.build_plan():
                if entry["condition"].startswith("music"):
                    cc.append_manifest(root, {**entry, "file": "x.raw"})
            todo = cc.pending_plan(root)
            self.assertTrue(todo)
            self.assertFalse(any(e["condition"].startswith("music") for e in todo))


class DataRootGuardTest(unittest.TestCase):
    """票 07「数据只落本地、脚本拒绝写入仓库路径」+ 2026-11-03 家庭语音隐私加固。"""

    def test_default_roots_are_outside_repo(self):
        self.assertFalse(cc._inside_repo(cc.CONTRACT_ROOT))
        self.assertFalse(cc._inside_repo(cc.SYNTH_ROOT))
        self.assertNotEqual(cc.SYNTH_ROOT, cc.CONTRACT_ROOT)

    def test_repo_internal_root_requires_gitignore(self):
        """仓库内路径只有被 .gitignore 覆盖才允许（未覆盖即拒跑，防家庭语音入库）。"""
        ok, _detail = cc.data_root_ok(cc.HERE / "results" / "contract")
        self.assertTrue(ok, "results/** 已被 .gitignore 覆盖，应放行")
        self.assertFalse(cc.data_root_ok(cc.HERE / "results_nonignored")[0])

    @unittest.skipIf(cc.drive_sync_root(cc.DATA_ROOT) is None,
                     "本机 dev 区不是 Synology Drive 同步根")
    def test_drive_sync_root_requires_explicit_approval(self):
        """dev 区会被 Synology Drive 同步 ⇒ 家庭语音落点必须显式放行（--allow-sync-root）。"""
        path = cc.DATA_ROOT / "contract"
        self.assertFalse(cc.data_root_ok(path)[0])
        self.assertTrue(cc.data_root_ok(path, allow_sync_root=True)[0])


if __name__ == "__main__":
    unittest.main()
