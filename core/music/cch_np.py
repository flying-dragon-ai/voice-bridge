"""CCH now-playing 编排器（票 03 换代编排 / 票 04 TTS 恢复，2026-11-20）。

数据面：cch_music_worker.py 采集 `CchBridgeMeta` META/STATE 事件 → 双写
proxy `/cch/np-ingest`（面板面）+ bridge `/api/external/np`（本编排面）。

编排语义（D+kwqq，facts §2.32/§2.34）：
- STATE `finish`/`stop` 且非我方发起 → **换代**：cast 队列下一首 kwqq 曲目；
  不编排则原厂推荐队列自动连播接管（真机实证）。
- STATE `suspend`（TTS 抢占）→ 记录恢复点；恢复动作 = resume 或重新 cast
  （试听档/外部 renderData 实例可能被语音链杀，facts §2.30）——恢复触发由
  `/speak` 编排侧调用 `resume_after_tts()`，双条件不靠 /speak 返回值。
- STATE `play` 且非我方 cast → 方案 b 接管观测点（是否接管待用户裁决，
  本版只记录不动作）。

META 事件更新内存态（Title/IconUri/isFavorite），供 bridge 工具层读回。
队列与索引借用 song_request 的 `_cch_state`（单会话语义不变）。
"""
from __future__ import annotations

import asyncio

from core.utils.logger import logger

_np_state: dict = {"meta": {}, "state": {}}
_advance_lock = asyncio.Lock()


def np_enabled() -> bool:
    from core.tools import song_request as sr

    from core.music.cch_cast import np_enabled as cast_np_enabled

    return cast_np_enabled() and bool(sr._cfg("np_enabled", False))


def snapshot() -> dict:
    """当前内存态（META/STATE 最新 extras）。"""
    return {"meta": dict(_np_state["meta"]), "state": dict(_np_state["state"])}


async def on_np_event(kind: str, extras: dict, device_ts: str = "") -> dict:
    """worker 上行事件入口。返回编排动作（供日志/审计）。"""
    _np_state[kind] = {"extras": extras or {}, "device_ts": device_ts}
    if kind != "STATE":
        return {"action": "recorded", "kind": kind}
    state_str = str((extras or {}).get("statusStr") or "").lower()
    code = str((extras or {}).get("audioStatus") or "")
    if state_str in ("finish", "stop") and code in ("1", "8"):
        return await _advance_if_finished()
    if state_str == "suspend":
        # 票 04：恢复点由 /speak 编排侧触发（resume_after_tts），此处只记录
        return {"action": "suspend_recorded"}
    if state_str == "play":
        return {"action": "play_recorded"}
    return {"action": "recorded", "state": state_str or code}


async def _advance_if_finished() -> dict:
    """kwqq 曲目播完 → cast 队列下一首。队列耗尽 → 回退原厂链（记录，不动作）。"""
    async with _advance_lock:
        from core.tools import song_request as sr

        items = sr._cch_state.get("items") or []
        idx = int(sr._cch_state.get("entry", 0))
        nxt = idx + 1
        if nxt >= len(items):
            logger.info("[CchNp] queue exhausted (played %d/%d) — fallback to native chain",
                        idx + 1, len(items), module="CchNp")
            return {"action": "queue_exhausted"}
        item = items[nxt]
        sr._cch_state["entry"] = nxt
        try:
            from core.music.cch_cast import (
                build_render_data,
                cast_native,
                kwqq_meta,
                resolve_ticket_url,
            )

            ref = {"source": item.get("source") or "", "id": str(item.get("id") or ""),
                   "extra": item.get("extra") or {}}
            ticket_url = await resolve_ticket_url(
                ref, session=sr._CCH_SESSION, epoch=sr._CCH_EPOCH, entry=str(item.get("id") or f"cch-{nxt}"))
            meta = await kwqq_meta(ref["id"])
            render_data = build_render_data(ticket_url, item, meta)
            ok, detail = await cast_native(render_data)
            logger.info(f"[CchNp] advance {idx}->{nxt} ok={ok} detail={detail}", module="CchNp")
            return {"action": "advanced", "index": nxt, "ok": ok}
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[CchNp] advance err: {type(exc).__name__}: {exc}", module="CchNp")
            return {"action": "advance_failed", "error": type(exc).__name__}


async def resume_after_tts() -> dict:
    """TTS 结束后恢复 kwqq 播放（票 04）：先 resume，STATE 8（实例死）→ 重新 cast。

    恢复点判定（字数估算 + KWS 静默超时）由 /speak 编排侧负责，本函数只执行动作。
    """
    from core.music.cch_cast import cast_native_simple

    state = (_np_state.get("state") or {}).get("extras") or {}
    ok, detail = await cast_native_simple(2)
    if ok:
        return {"action": "resumed"}
    # resume 打在死实例上 → 重新 cast 当前曲目（票据 30min 内）
    return await _recast_current()


async def _recast_current() -> dict:
    from core.tools import song_request as sr

    items = sr._cch_state.get("items") or []
    idx = int(sr._cch_state.get("entry", 0))
    if idx >= len(items):
        return {"action": "recast_skipped", "reason": "no current item"}
    item = items[idx]
    try:
        from core.music.cch_cast import (
            build_render_data,
            cast_native,
            kwqq_meta,
            resolve_ticket_url,
        )

        ref = {"source": item.get("source") or "", "id": str(item.get("id") or ""),
               "extra": item.get("extra") or {}}
        ticket_url = await resolve_ticket_url(
            ref, session=sr._CCH_SESSION, epoch=sr._CCH_EPOCH, entry=str(item.get("id") or f"cch-{idx}"))
        meta = await kwqq_meta(ref["id"])
        render_data = build_render_data(ticket_url, item, meta)
        ok, detail = await cast_native(render_data)
        return {"action": "recast", "ok": ok, "detail": detail}
    except Exception as exc:  # noqa: BLE001
        return {"action": "recast_failed", "error": type(exc).__name__}
