"""CCH 投送通道（T2 ticket 08）—— Songloft 票据 URL 经 /cch/cast 投到 CCH 屏。

设计（用户裁定 2026-11-19）：
- CCH 播控走语音 + bridge 作中心 + 配合 HA；
- 投送路径 = Songloft prepare 出票据 URL → POST NAS /cch/cast → patched dlnaserver → 桥 APK v0.2.0；
- 端侧只接触短时效票据 URL，不接触管理员凭证；
- 换代显式撤票归 bridge（songloft NewEpoch 只 ++ 不撤旧票）。

本模块职责：
1. `resolve_ticket_url(ref)` —— 经 Songloft managed prepare 拿票据化绝对 URL（复用 song_bridge 的 client）；
2. `cast(url)` —— POST NAS /cch/cast（Bearer PANEL_TOKEN + CAST_ALLOW 白名单约束）；
3. `revoke(session, epoch, entry)` —— stop 撤票 / 换代逐条撤。

凭据纪律：PANEL_TOKEN / SONGLOFT_PASSWORD 从配置/env 读，绝不入日志/文档/URL；
日志只记事件与 http 码，不记 token/ticket 值。成功判据一律设备侧 observable（调用方负责回读）。
"""
from __future__ import annotations

import asyncio
from urllib.parse import urlencode

from core.utils.logger import logger

# Songloft managed prepare 轮询上限（与 song_bridge._songloft_resolver 一致 120s）
_PREPARE_TIMEOUT_S = 120
_PREPARE_POLL_S = 2


def _cfg(key: str, default=None):
    """读 song_request 配置段（与 song_bridge 同源）。"""
    from core.tools import song_request as sr

    return sr._cfg(key, default)


def cch_cast_url() -> str:
    """NAS /cch/cast 端点（cch-proxy，host 网络 :18080）。"""
    return str(_cfg("cch_cast_url", "http://10.10.10.2:18080/cch/cast")).rstrip("/")


def cast_enabled() -> bool:
    """CCH 主动投送总开关，**默认 False**（票 14 → 16/17 的 D 方向裁决）。

    关闭原因（真机实证）：
    - 真人语音下原厂 AGMC 云链与桥链**并发**，桥链 8/8 次在拿到窗口焦点 81–190 ms
      内被交回（`ws: docs/devices/AliGenie/cch-tg-z02-facts.md` §2.27）；
    - 更严重的是桥的 `BridgeReceiver` MediaPlayer 是**独立第二播放器**，原厂暂停后
      它仍在出声（§2.30 实测 `media.audio_flinger` track 表 Client=com.cch.bridge Active=yes），
      且每次起播泄漏一条音频焦点监听器（累积 17 条）。

    ⇒ D 方向 = 原厂负责出声，bridge 只做控制/状态；投送仅保留代码与日志，默认不执行。
    打开此开关即回滚到 v0.2.0 行为（也须同时回装播放版桥 APK，见 `deploy/cch-music-bridge/README.md`）。
    """
    val = _cfg("cch_cast_enabled", False)
    if isinstance(val, str):
        return val.strip().lower() in ("1", "true", "yes", "on")
    return bool(val)


def panel_token() -> str:
    """cch-proxy 的 PANEL_TOKEN（env 回落，不入库）。"""
    import os

    val = str(_cfg("cch_panel_token", "") or "")
    return val or os.environ.get("PANEL_TOKEN", "")


async def _songloft_client():
    """复用 song_bridge 的 songloft 客户端（token + resolver），避免重复实现。"""
    from core.music import song_bridge

    return song_bridge


async def resolve_ticket_url(ref: dict, *, session: str, epoch: int, entry: str) -> str:
    """经 Songloft managed prepare 拿票据化绝对 URL。

    ref = {"source":..., "id":..., "extra":...}；返回 base + /media?media_ticket=...。
    失败抛 ManagedError（由调用方收敛为错误串）。
    """
    from core.music.model import ManagedError, TrackRef

    sb = await _songloft_client()
    base = str(sb._songloft_cfg("songloft_base", "") or "").rstrip("/")
    if not base:
        raise ManagedError("cch_cast: songloft_base not configured")
    import aiohttp

    async with aiohttp.ClientSession() as http:
        token = await sb._songloft_token(base, http)
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        body = {"session": session, "epoch": epoch, "entry": entry, "ref": ref}
        async with http.post(f"{base}/api/v1/managed/prepare", json=body, headers=headers) as resp:
            if resp.status != 202:
                raise ManagedError(f"cch_cast prepare: http {resp.status}")
        loop = asyncio.get_event_loop()
        deadline = loop.time() + _PREPARE_TIMEOUT_S
        while loop.time() < deadline:
            await asyncio.sleep(_PREPARE_POLL_S)
            qs = urlencode({"session": session, "epoch": epoch, "entry": entry})
            async with http.get(f"{base}/api/v1/managed/prepare?{qs}", headers=headers) as resp:
                if resp.status != 200:
                    raise ManagedError(f"cch_cast prepare poll: http {resp.status}")
                data = await resp.json()
            state = str(data.get("state") or "")
            if state == "ready":
                media = str(data.get("media_url") or "")
                if not media.startswith("/media?"):
                    raise ManagedError("cch_cast: bad media_url")
                logger.info("[CchCast] ticket ready", module="CchCast")
                return base + media
            if state == "failed":
                raise ManagedError(f"cch_cast prepare failed: {data.get('error', '')}")
        raise ManagedError("cch_cast prepare timeout")


async def cast(url: str) -> bool:
    """POST /cch/cast {url}（Bearer PANEL_TOKEN）。返回 HTTP 是否 2xx。

    注意：CAST_ALLOW 白名单在 proxy 侧校验——票据 URL host(10.10.10.2:58091)
    必须在白名单内，否则 proxy 返 403（调用方据此判失败，不误报成功）。
    """
    import aiohttp

    token = panel_token()
    if not token:
        logger.warning("[CchCast] PANEL_TOKEN not configured", module="CchCast")
        return False
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    async with aiohttp.ClientSession() as http:
        async with http.post(cch_cast_url(), json={"url": url}, headers=headers) as resp:
            ok = 200 <= resp.status < 300
            logger.info(f"[CchCast] /cch/cast http={resp.status} ok={ok}", module="CchCast")
            return ok


async def revoke(session: str, epoch: int, entry: str) -> bool:
    """显式撤票：stop 命令带票据 epoch+entry（songloft bff 按此撤销）。"""
    from core.music.model import ManagedError

    sb = await _songloft_client()
    base = str(sb._songloft_cfg("songloft_base", "") or "").rstrip("/")
    if not base:
        return False
    import aiohttp

    async with aiohttp.ClientSession() as http:
        token = await sb._songloft_token(base, http)
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        body = {"name": "stop", "session": session, "epoch": epoch, "entry": entry}
        async with http.post(f"{base}/api/v1/managed/command", json=body, headers=headers) as resp:
            # songloft Execute 顺序：bff.Execute(stop→RevokeEntry 撤票) 在前，
            # forwardCommand(通知 bridge 托管会话) 在后。CCH 投送无桥托管会话 ⇒
            # 转发失败返 502，但票据已撤。502 因此视为"撤票已生效"的次生错误。
            ok = (200 <= resp.status < 300) or resp.status == 502
            logger.info(
                f"[CchCast] revoke entry={entry} http={resp.status} ok={ok}"
                + (" (502: ticket revoked, bridge forward failed)" if resp.status == 502 else ""),
                module="CchCast",
            )
            return ok
