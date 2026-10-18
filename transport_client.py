from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, AsyncIterator, Dict

import httpx


class STS2TransportError(RuntimeError):
    pass


class STS2ActionRejected(STS2TransportError):
    """mod 明确拒收了这个动作（HTTP 4xx/5xx 且带 error 包）。

    与"连不上 mod"是两回事：连不上要退避重连，而被拒多半意味着**游戏状态已经变了**——
    例如缓存里的战斗计划指向的敌人已经死了，于是 target_index 越界。这类拒绝的正确反应是
    "丢掉旧计划、按现场重来"，而不是把自动玩打成终止态。

    ``retryable`` 由 mod 的 ``error.retryable`` 或本地按 ``code`` 推断得出。
    """

    # 这些 code 表示"你问的时候已经不是那个状态了"，重算即可。
    RETRYABLE_CODES = frozenset({
        "invalid_target",
        "invalid_action",
        "state_unavailable",
        "invalid_state",
        "action_unavailable",
    })

    def __init__(
        self,
        message: str,
        *,
        code: str = "",
        action_name: str = "",
        screen: str = "",
        details: Mapping[str, Any] | None = None,
        retryable: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.code = str(code or "")
        self.action_name = str(action_name or "")
        self.screen = str(screen or "")
        self.details = dict(details or {})
        if retryable is None:
            retryable = self.code in self.RETRYABLE_CODES
        self.retryable = bool(retryable)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "action": self.action_name,
            "screen": self.screen,
            "retryable": self.retryable,
            "message": str(self),
        }


class STS2TransportClient:
    # 每个请求都带上的"外部控制者"标识头。mod 用它 + 租约判断自己是被插件控制还是独自运行：
    # 插件在（且带这个头）时 mod 走 coop_map_policy（默认彻底等玩家发话）；插件不在时 mod 自己跑，
    # 地图不会永久等待。插件空闲 3 秒 / 活动 1 秒轮询一次 /state，所以这个头天然就是心跳。
    EXTERNAL_CONTROL_HEADERS: Dict[str, str] = {
        "X-Neko-External-Control": "1",
        "X-Neko-Client": "sts2_autoplay",
    }

    def __init__(self, base_url: str, *, connect_timeout: float = 5.0, request_timeout: float = 15.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.connect_timeout = connect_timeout
        self.request_timeout = request_timeout
        self._effect_catalogs: dict[str, dict[str, Any]] = {}

    async def close(self) -> None:
        return None

    async def health(self) -> Dict[str, Any]:
        return await self._request("GET", "/health")

    async def get_state(self) -> Dict[str, Any]:
        state = await self._request("GET", "/state")
        # 旧 Mod 的候选物品缺说明，按游戏自己的稳定 ID 补齐；新版现场说明优先。
        for collection, id_key, items in (
            ("relics", "relic_id", (state.get("chest") or {}).get("relic_options") or []),
            ("relics", "relic_id", (state.get("shop") or {}).get("relics") or []),
            ("potions", "potion_id", (state.get("shop") or {}).get("potions") or []),
        ):
            missing = [item for item in items if item.get(id_key) and not item.get("description")]
            if not missing:
                continue
            if collection not in self._effect_catalogs:
                try:
                    exported = await self._request("GET", "/data/" + collection)
                except STS2TransportError as exc:
                    # 补充说明失败时保留可读现场，并向模型暴露具体缺项。
                    for item in missing:
                        item["effect_error"] = str(exc)
                    continue
                self._effect_catalogs[collection] = {item["id"]: item for item in exported.get("value") or []}
            for item in missing:
                source = self._effect_catalogs[collection].get(item[id_key]) or {}
                if source.get("description"):
                    item["description"] = source["description"]
                if source.get("related_effects") and not item.get("related_effects"):
                    item["related_effects"] = source["related_effects"]
        return state

    async def get_available_actions(self) -> Dict[str, Any]:
        return await self._request("GET", "/actions/available")

    async def get_combat_plan(self, *, companion: bool = False) -> Dict[str, Any]:
        """Read the mod's authoritative combat solver plan (GET /solver/plan).

        Returns the SolverPlanPayload (in_combat/action/card_index/card_id/target_index/line),
        or {"in_combat": false, "reason": "..."} when not in combat.
        """
        if companion:
            return await self._request("GET", "/solver/plan", params={"mode": "companion"}, timeout=8.0)
        return await self._request("GET", "/solver/plan")

    async def execute_action(self, action: str, **kwargs: Any) -> Dict[str, Any]:
        return await self._request("POST", "/action", json=self._build_action_payload(action, **kwargs))

    async def subscribe_events(self) -> AsyncIterator[Dict[str, Any]]:
        """Stream the mod's scene-change events (GET /events/stream, server-sent events).

        Yields one dict per SSE event — the parsed GameEventEnvelope (type / event_id /
        timestamp_utc / data) — as it arrives, so callers react to the scene changing instead of
        polling /state. Raise STS2TransportError on a transport/HTTP failure so the caller can
        reconnect; the stream is long-lived, so browse it with ``async for``.
        """
        url = f"{self.base_url}/events/stream"
        timeout = httpx.Timeout(connect=self.connect_timeout, read=60.0, write=60.0, pool=self.connect_timeout)
        event_name: str | None = None
        data_lines: list[str] = []

        def _flush() -> Dict[str, Any] | None:
            nonlocal event_name, data_lines
            if not data_lines:
                return None
            text = "\n".join(data_lines)
            data_lines = []
            try:
                payload = json.loads(text)
            except ValueError:
                return None
            if not isinstance(payload, dict):
                return None
            if event_name and not payload.get("type"):
                payload["type"] = event_name
            event_name = None
            return payload

        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
                async with client.stream("GET", url, headers=dict(self.EXTERNAL_CONTROL_HEADERS)) as response:
                    if response.status_code >= 400:
                        body = await response.aread()
                        raise STS2TransportError(
                            f"events stream HTTP {response.status_code}: {body[:120]!r}"
                        )
                    async for raw_line in response.aiter_lines():
                        line = raw_line.rstrip("\r")
                        if not line:
                            # Blank line terminates the current SSE event (if any).
                            flushed = _flush()
                            if flushed is not None:
                                yield flushed
                            continue
                        if line.startswith(":"):
                            # Comment / heartbeat; carries no event.
                            continue
                        if line.startswith("id:"):
                            # id is also mirrored inside the JSON envelope; ignore the raw field.
                            continue
                        if line.startswith("event:"):
                            event_name = line[len("event:"):].strip()
                            continue
                        if line.startswith("data:"):
                            data_lines.append(line[len("data:"):].lstrip(" "))
        except STS2TransportError:
            raise
        except httpx.HTTPError as exc:
            raise STS2TransportError(f"events stream failed: {exc}") from exc

    async def push_danmaku(self, text: str, *, style: str = "catgirl", placement: str = "scrolling", avatar: str | None = None) -> Dict[str, Any]:
        """Push a catgirl danmaku line to the in-game overlay (POST /danmaku).

        `avatar` is an optional base64 image (with or without a data:...;base64, prefix) rendered next to
        the catgirl text in-game.
        """
        payload: Dict[str, Any] = {"text": text, "style": style, "placement": placement}
        if avatar:
            payload["avatar"] = avatar
        return await self._request("POST", "/danmaku", json=payload)

    async def clear_danmaku(self) -> Dict[str, Any]:
        """Clear every danmaku line already on screen (POST /danmaku/clear).

        Used when the user switches danmaku off: gating new pushes alone leaves the lines already
        scrolling for up to ~20s, which reads as "关不掉".
        """
        return await self._request("POST", "/danmaku/clear", json={})

    async def set_companion_status(self, payload: Mapping[str, Any]) -> Dict[str, Any]:
        """Refresh the host's fixed status lease; revision is monotonic across plugin restarts."""
        return await self._request("POST", "/companion/status", json=dict(payload))

    async def get_config(self) -> Dict[str, Any]:
        """Read the mod's config (GET /config). The api_key is never exposed by the mod.

        用途之一：从**主机**问出 `coop_client_port`，从而知道 coop 里的猫娘进程在哪个端口。
        """
        return await self._request("GET", "/config")

    async def set_config(self, **fields: Any) -> Dict[str, Any]:
        """Write the mod's config (POST /config). Only the fields provided are applied.

        When the plugin drives the catgirl, call ``set_config(llm_enabled=False)`` so the mod defers its
        MAP/reward/deck/event decision screens to this plugin instead of its own LLM.
        """
        return await self._request("POST", "/config", json=fields)

    def _build_action_payload(self, action_name: str, **kwargs: Any) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"action": action_name}
        for key, value in kwargs.items():
            if value is None or key in {"type", "action"}:
                continue
            payload[key] = value
        return payload

    @staticmethod
    def _describe_error(payload: Dict[str, Any], status_code: int) -> str:
        """把 mod 的 error 包渲染成一句可诊断的文本。

        mod 端多个动作共用同一句 message（例如 "Action is not available in the current state."
        同时来自 open_multiplayer_menu / host_multiplayer_lobby / ...），只取 message 分不出是哪个
        动作挂的；error.details 里的 action / screen / code 才是有用信息，一并带上。
        """
        error = payload.get("error")
        if not isinstance(error, dict):
            return f"STS2-Agent 请求失败: HTTP {status_code}"

        message = str(error.get("message") or error.get("code") or f"HTTP {status_code}")
        details = error.get("details") if isinstance(error.get("details"), dict) else {}
        bits = [f"{key}={details[key]}" for key in ("action", "screen") if details.get(key) not in (None, "")]
        code = str(error.get("code") or "")
        if code and code not in message:
            bits.insert(0, f"code={code}")
        return f"{message} ({', '.join(bits)})" if bits else message

    async def _request(self, method: str, path: str, **kwargs: Any) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"
        # 调用方若自带 headers（例如将来的鉴权），以调用方为准，这里只补默认的标识头。
        headers = dict(self.EXTERNAL_CONTROL_HEADERS)
        extra_headers = kwargs.pop("headers", None)
        if isinstance(extra_headers, Mapping):
            headers.update(extra_headers)
        timeout = httpx.Timeout(
            connect=self.request_timeout if self.connect_timeout <= 0 else self.connect_timeout,
            read=self.request_timeout,
            write=self.request_timeout,
            pool=self.request_timeout,
        )
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
                response = await client.request(method, url, headers=headers, **kwargs)
        except RuntimeError as exc:
            if "Event loop is closed" in str(exc):
                raise STS2TransportError("事件循环已关闭，无法完成 STS2 请求") from exc
            raise STS2TransportError(str(exc)) from exc
        except httpx.HTTPError as exc:
            raise STS2TransportError(f"无法连接 STS2-Agent: {exc}") from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise STS2TransportError(f"STS2-Agent 返回了无效 JSON: {url}") from exc

        if not isinstance(payload, dict):
            raise STS2TransportError(f"STS2-Agent 返回了非对象 JSON: {url}")

        if response.status_code >= 400 or payload.get("ok") is False:
            raise rejection_from_envelope(payload, response.status_code)

        data = payload.get("data")
        return data if isinstance(data, dict) else {"value": data}


def rejection_from_envelope(payload: Dict[str, Any], status_code: int) -> STS2TransportError:
    """把 mod 的错误包变成结构化异常：带 error 包 → ``STS2ActionRejected``（可能是"状态漂移"），
    否则退化成普通 ``STS2TransportError``。

    单独抽出来是为了可测：不用起 HTTP 也能验证"哪些 code 会被判成可重试"。
    """
    error = payload.get("error") if isinstance(payload.get("error"), dict) else None
    if error is None:
        return STS2TransportError(STS2TransportClient._describe_error(payload, status_code))
    details = error.get("details") if isinstance(error.get("details"), dict) else {}
    raw_retryable = error.get("retryable")
    return STS2ActionRejected(
        STS2TransportClient._describe_error(payload, status_code),
        code=str(error.get("code") or ""),
        action_name=str(details.get("action") or ""),
        screen=str(details.get("screen") or ""),
        details=details,
        retryable=bool(raw_retryable) if isinstance(raw_retryable, bool) else None,
    )


__all__ = ["STS2TransportClient", "STS2TransportError", "STS2ActionRejected", "rejection_from_envelope"]
