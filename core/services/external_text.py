"""外部 ASR 文本入口（B-37）的开关判定 / 限源 / 限流 / 载荷校验（纯逻辑）。

纯逻辑模块：只依赖 stdlib 与 `inject_control` 的 CIDR 工具，不 import aiohttp /
设备 / 会话实现，便于单测。HTTP 适配（路由、鉴权中间件、json_response）留在
`core/services/api_server.py`。

## 开关语义（对齐 inject_control 的 `resolve_inject_enabled` 模式，ticket 07）

- 配置键 `external_text.enabled`（config.py 的 APP_CONFIG），**默认 false**；
  环境变量 `EXTERNAL_TEXT_ENABLE` 可强制覆盖（1/true/yes/on 开，0/false/no/off 关），
  非法值忽略、回落到配置值。优先级：env > config.py > 默认 false。
- 关闭时端点返回 **503 + code="external_text_disabled"**（唯一例外无：本端点
  只进不出，没有 inject 那样的只读状态探针；运维观测走启动日志行）。

## 限源语义（fail closed，对齐注入数据面的 refused 口径）

- `EXTERNAL_TEXT_ALLOW_CIDR`（逗号分隔 CIDR，建议调用方 /32）：未配置 /
  配置非法 / 源不在网段，一律 **503 + code="external_text_refused"**。
  与注入线不同，本端点**不提供 anon 裸奔逃生门**——调用方本就是发指令的，
  token + CIDR 双闸缺一不可（spec 风险节口径）。

## 会话占用与限流（双麦互听回环的缓解）

- **busy 拒绝**：已有活跃（含启动中）连续对话会话时，二次 POST 一律
  **409 + code="external_text_busy"**（串行拒绝、不顶替）。外部轮自身播报
  期间拾音回灌的再触发被这一跳拦下。
- **per-caller 限流**：`ExternalTextThrottle` 按「token label + TCP peer」
  限最小间隔（config `external_text.min_interval`，默认 10s；<=0 关闭）。
  body `source` 只进审计（TurnTrace caller + 日志），**不做限流键**——
  客户端可伪造；token label（不可伪造）经鉴权中间件写进 request["auth_label"]。

## 错误码总表（端点与测试共用）

| code | HTTP | 语义 |
|---|---|---|
| external_text_disabled | 503 | 模块关闭 |
| external_text_refused | 503 | 限源拒绝（未配/非法/未命中） |
| external_text_busy | 409 | 已有活跃会话（串行拒绝） |
| external_text_rate_limited | 429 | 同调用方间隔内重复请求 |
| external_text_backend_unknown | 503 | 配置的后端不在注册表 |
| external_text_start_failed | 503 | 会话占位异常（内部错误，fail closed） |

另有 400（载荷校验失败：text 缺失/非串/空/超长 ≤500、非法 JSON）。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

from core.services.inject_control import parse_flag, parse_inject_cidrs

# 环境变量覆盖名（与配置键 `external_text.enabled` / `.backend` 并存）
ENV_EXTERNAL_TEXT_ENABLE = "EXTERNAL_TEXT_ENABLE"
ENV_EXTERNAL_TEXT_ALLOW_CIDR = "EXTERNAL_TEXT_ALLOW_CIDR"

# 关闭/拒绝语义的固定错误码（端点与测试共用，避免各处自造字符串）
EXTERNAL_TEXT_DISABLED_CODE = "external_text_disabled"
EXTERNAL_TEXT_REFUSED_CODE = "external_text_refused"
EXTERNAL_TEXT_BUSY_CODE = "external_text_busy"
EXTERNAL_TEXT_RATE_LIMITED_CODE = "external_text_rate_limited"
EXTERNAL_TEXT_BACKEND_UNKNOWN_CODE = "external_text_backend_unknown"
EXTERNAL_TEXT_START_FAILED_CODE = "external_text_start_failed"

# 载荷上限：ASR 文本远短于此；超长说明调用方失控，直接拒绝
MAX_TEXT_CHARS = 500
MAX_SOURCE_CHARS = 64
# 后端默认：生产主对话后端（deploy/config.py 口径）；改配置即可切换
DEFAULT_BACKEND = "openai"


def _config_enabled(config, path: str) -> bool:
    """从 ConfigManager / 嵌套 dict 读取 `path` 下的 enabled；异常一律按关闭处理。"""
    if config is None:
        return False
    try:
        if hasattr(config, "get_app_config"):
            value = config.get_app_config(path, False)
        elif isinstance(config, Mapping):
            node = config
            for part in path.split("."):
                node = node.get(part) if isinstance(node, Mapping) else None
                if node is None:
                    return False
            value = node
        else:
            return False
    except Exception:
        return False
    if isinstance(value, str):
        return parse_flag(value) is True
    return bool(value)


def resolve_external_text_enabled(config=None, env: Mapping[str, str] | None = None) -> bool:
    """判定外部文本模块是否启用（唯一判定点）。

    Args:
        config: ConfigManager 实例或 `APP_CONFIG` 形式的嵌套 dict。
        env: 环境映射（默认 `os.environ`），用于 `EXTERNAL_TEXT_ENABLE` 覆盖。
    """
    environ = os.environ if env is None else env
    override = parse_flag(environ.get(ENV_EXTERNAL_TEXT_ENABLE))
    if override is not None:
        return override
    return _config_enabled(config, "external_text.enabled")


@dataclass(frozen=True)
class ExternalTextCidrs:
    """限源判定结果：`refused_reason` 非空 = 拒绝（networks 为空）。"""

    networks: tuple[tuple, ...] = ()
    refused_reason: str = ""

    @property
    def refused(self) -> bool:
        return bool(self.refused_reason)


def resolve_external_text_cidrs(
    env: Mapping[str, str] | None = None,
) -> ExternalTextCidrs:
    """判定限源网段（唯一判定点）。默认空 = refused；非法 CIDR 同样 refused。"""
    environ = os.environ if env is None else env
    raw_cidr = str(environ.get(ENV_EXTERNAL_TEXT_ALLOW_CIDR, "") or "").strip()
    if not raw_cidr:
        return ExternalTextCidrs(
            refused_reason=(
                "未配置 EXTERNAL_TEXT_ALLOW_CIDR：外部文本端点拒绝监听"
                "（调用方本就是发指令的，必须显式限源）"
            )
        )
    networks, errors = parse_inject_cidrs(raw_cidr)
    if errors:
        return ExternalTextCidrs(
            refused_reason=f"EXTERNAL_TEXT_ALLOW_CIDR 非法（{'; '.join(errors)}），fail closed"
        )
    return ExternalTextCidrs(networks=networks)


def resolve_external_text_backend(config=None) -> str:
    """解析外部文本轮承载的后端（backend_registry 名）。

    配置键 `external_text.backend`，默认 `openai`（生产主后端）；非串/空回落默认。
    """
    value = None
    if config is not None:
        try:
            if hasattr(config, "get_app_config"):
                value = config.get_app_config("external_text.backend", DEFAULT_BACKEND)
            elif isinstance(config, Mapping):
                section = config.get("external_text") or {}
                value = section.get("backend", DEFAULT_BACKEND) if isinstance(section, Mapping) else None
        except Exception:
            value = None
    if isinstance(value, str) and value.strip():
        return value.strip()
    return DEFAULT_BACKEND


@dataclass(frozen=True)
class ExternalTextPayload:
    """校验后的外部文本轮载荷。"""

    text: str
    source: str = ""
    silent: bool = False


def validate_external_text_payload(
    data: Mapping | None,
) -> tuple[ExternalTextPayload | None, str | None]:
    """校验请求体 → (payload, error)。error 非 None 时 payload 为 None。

    - text：必填、非空串、strip 后 ≤ MAX_TEXT_CHARS；
    - source：可选调用方 id（审计用，客户端可伪造——限流/鉴权不依赖它），
      非串忽略，超长截断；
    - silent：可选，严格解析（"false"/"0" 等不算真值）。
    """
    if not isinstance(data, Mapping):
        return None, "请求体必须是 JSON 对象"
    raw_text = data.get("text")
    if raw_text is None:
        return None, "缺少必填字段：text"
    if not isinstance(raw_text, str):
        return None, "text 必须是字符串"
    text = raw_text.strip()
    if not text:
        return None, "text 不能为空"
    if len(text) > MAX_TEXT_CHARS:
        return None, f"text 超长（上限 {MAX_TEXT_CHARS} 字符）"

    raw_source = data.get("source")
    source = raw_source.strip() if isinstance(raw_source, str) else ""
    source = source[:MAX_SOURCE_CHARS]

    silent = parse_flag(_silent_raw(data.get("silent"))) is True
    return ExternalTextPayload(text=text, source=source, silent=silent), None


def _silent_raw(value) -> str | None:
    """把 silent 字段归一成 flag 字符串（无法识别 → None = False）。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return "1" if value != 0 else "0"
    if isinstance(value, str):
        return value.strip().lower() or None
    return None


class ExternalTextThrottle:
    """per-caller 最小间隔限流（双麦互听回环缓解；非访问闸门）。

    键由调用方拼装（token label + TCP peer，均不可伪造）；被拒尝试**不
    顺延**窗口（窗口自首次放行起算，重试惩罚会造成拒绝风暴放大）。
    """

    def __init__(self) -> None:
        self._last_allowed: dict[str, float] = {}

    def try_acquire(self, key: str, *, now: float, min_interval: float) -> bool:
        """间隔内返回 False；min_interval<=0 直通（限流关闭）。"""
        try:
            interval = float(min_interval)
        except (TypeError, ValueError):
            interval = 0.0
        if interval <= 0:
            return True
        last = self._last_allowed.get(key)
        if last is not None and (now - last) < interval:
            return False
        self._last_allowed[key] = now
        return True
