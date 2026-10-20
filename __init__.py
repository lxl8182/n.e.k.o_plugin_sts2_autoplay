from __future__ import annotations

import asyncio
import sys
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

# Windows 上 asyncio 默认 Proactor 事件循环与 ZeroMQ 不兼容（zmq 无法 add_reader，回退到 tornado
# selector 线程，可能让上行 recv 读到截断的 pickle -> "unpickling stack underflow"）。
# 尽早把循环策略切成 Selector，让 NEKO 插件进程的 ZMQ 上行更稳。
if sys.platform == "win32":
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    except AttributeError:
        pass

from plugin.sdk.plugin import Err, NekoPluginBase, Ok, SdkError, lifecycle, llm_tool, neko_plugin, plugin_entry, tr
from plugin.sdk.plugin import ui as _ui

from .dispatcher import STS2Dispatcher
from .service import STS2AutoplayService

JsonObject = dict[str, Any]
AsyncPayloadFactory = Callable[[], Awaitable[JsonObject]]


def _as_mapping(value: Any) -> JsonObject:
    return dict(value) if isinstance(value, Mapping) else {}


def _summary_from(payload: Mapping[str, Any]) -> str:
    return str(payload.get("summary") or payload.get("message") or payload.get("content") or "")


@neko_plugin
class STS2AutoplayPlugin(NekoPluginBase):
    def __init__(self, ctx: Any) -> None:
        super().__init__(ctx)
        self.file_logger = self.enable_file_logging(log_level="INFO")
        self.logger = self.file_logger
        self._cfg: JsonObject = {}
        self._dispatcher = STS2Dispatcher(self)
        self._service = STS2AutoplayService(
            self.logger,
            self.report_status,
            self._push_frontend_notification,
            sdk_bus=self.bus,
            sdk_ctx=self.ctx,
            i18n=self.i18n,
        )

    @lifecycle(id="startup")
    async def startup(self, **_: Any):
        cfg = _as_mapping(await self.config.dump(timeout=5.0))
        self._cfg = _as_mapping(cfg.get("sts2"))
        # 注册插件静态 UI（sts2 面板），服务地址：/plugin/sts2_autoplay/ui/
        registered = self.register_static_ui("static")
        if not registered:
            self.logger.warning("[sts2_static_ui] static UI 注册失败：插件 static/ 目录缺失")
        startup_result = _as_mapping(await self._service.startup(self._cfg))
        return Ok({"status": "ready", "result": await self._service.get_status(), "startup": startup_result})

    @lifecycle(id="shutdown")
    async def shutdown(self, **_: Any):
        await self._service.shutdown()
        return Ok({"status": "shutdown"})

    # ------------------------------------------------------------------
    # Hosted surface UI context
    # ------------------------------------------------------------------
    #
    # ★★ 为什么必须有这个 provider（踩了很久才定位，记下来）：
    #
    #   hosted surface 的按钮走 `call_surface_action` → `host.get_ui_context(context_id)`。
    #   而 `host._collect_ui_actions()`（`core/host.py:1253-1261`）只收集**同时**带
    #   `@ui.action` 的 entry：
    #
    #       ui_meta = _get_ui_action_meta(handler)
    #       if not ui_meta:
    #           continue          # ← 没有 @ui.action 的 entry 直接被跳过
    #
    #   于是 actions 白名单为空，面板上**每个按钮都会 403**
    #   （"is not exposed by surface"）。
    #
    #   provider 缺失还有第二重后果：`get_ui_context` 直接报
    #   "UI context not found"，连 context 里的 state 都拿不到。
    #
    #   这条路径的对错只在 `warnings` 里留一行，UI 上看不出区别，
    #   所以它长期没被发现。参照实现见 `plugins/game_agent_minecraft/__init__.py:628`。
    #
    # ★ 返回**轻量快照**即可：surface 自己会通过 api.call 拉实时状态，
    #   这里不做 lazy-start，避免每次取 context 都阻塞等连接握手。
    @_ui.context(id="main")
    async def main_ui_context(self, **_: Any) -> JsonObject:
        """main 面板的只读上下文 provider（与 `plugin.toml` 的 panel context 对应）。"""
        try:
            status = await self._service.get_status()
        except Exception:
            self.logger.exception("[sts2_ui_context] get_status 失败，降级返回空快照")
            status = {}
        return {"status": status}

    @llm_tool(
        name="sts2_get_status",
        description=tr("tools.sts2_get_status.description", default="只读获取杀戮尖塔连接状态、当前界面和基础分类信息。"),
        parameters={"type": "object", "properties": {}},
        timeout=10.0,
    )
    async def llm_get_status(self, **_: Any) -> JsonObject:
        return await self._service.get_status()

    @llm_tool(
        name="sts2_query_combat",
        description=tr(
            "tools.sts2_query_combat.description",
            default=(
                "只读刷新你自己游戏进程的战斗事实。回答自己和敌人的血量、格挡、意图、powers 层数，"
                "以及手牌的完整效果、能量、合法目标；明确区分本地玩家和另一位玩家。"
                "认知固定属于自己的进程，不随面板活动实例切换。不调用其他进程查询，不拼接全知视图。"
                "powers 或 hand 为 null 表示未知，不代表没有。不要因为查询而出牌。"
                "回答每个战斗问题都要重新调用本工具，旧结果不能代表这一回合。"
                "session 包含实际行动和能量观测历史，解释之前为什么没打某张牌必须读历史；当前零能量不代表一直零能量。"
            ),
        ),
        parameters={
            "type": "object",
            "properties": {"question": {"type": "string", "description": "玩家正在问的战斗问题，可留空。"}},
            "required": [],
        },
        timeout=20.0,
    )
    async def llm_query_combat(self, question: str = "", **_: Any) -> JsonObject:
        return await self._service.neko.query_combat(question)

    @llm_tool(
        name="sts2_companion_context",
        description=tr("tools.sts2_companion_context.description", default="读取最新局面、计划和行动历史来回应玩家。询问用 query；玩家提出改变打法的要求用 guidance 并传原话，会让后续行动重新判断。"),
        parameters={"type": "object", "properties": {"message": {"type": "string"}, "message_kind": {"type": "string", "enum": ["query", "guidance"]}}, "required": []},
        timeout=20.0,
    )
    async def llm_companion_context(self, message: str = "", message_kind: str = "query", **_: Any) -> JsonObject:
        if message_kind == "guidance":
            return await self._service.apply_user_override_safely(message)
        return await self._service.chat_companion(message)

    @llm_tool(
        name="sts2_start_discussion",
        description=tr("tools.sts2_start_discussion.description", default="暂停自己的游戏动作继续讨论；仍可聊天和给建议，不需要逐步批准动作。"),
        parameters={"type": "object", "properties": {"reason": {"type": "string"}}, "required": []},
        timeout=20.0,
    )
    async def llm_start_discussion(self, reason: str = "user", **_: Any) -> JsonObject:
        return await self._service.neko.start_discussion(reason)

    @llm_tool(
        name="sts2_end_discussion",
        description=tr("tools.sts2_end_discussion.description", default="结束讨论并明确恢复自动运行。"),
        parameters={"type": "object", "properties": {}},
        timeout=20.0,
    )
    async def llm_end_discussion(self, **_: Any) -> JsonObject:
        return await self._service.neko.end_discussion()

    async def _run_entry(self, action: AsyncPayloadFactory, *, finish: bool = False):
        try:
            payload = await action()
            if finish:
                payload = dict(payload) if isinstance(payload, Mapping) else {"value": payload}
                payload.setdefault("summary", _summary_from(payload))
                return await self.finish(data=payload, delivery="passive", message=_summary_from(payload))
            return Ok(payload)
        except SdkError as error:
            self.logger.warning(f"STS2 plugin entry failed: {error}")
            return Err(str(error))
        except Exception as error:
            self.logger.exception("Unexpected STS2 plugin entry failure")
            return Err(self.i18n.t("errors.internal", default="尖塔插件内部错误: {error}", error=error))

    def _get_dispatcher(self) -> STS2Dispatcher:
        """懒加载 dispatcher：测试桩（NotificationPlugin/LiveEntryPlugin）不调用
        super().__init__()，不会设置 self._dispatcher。"""
        dispatcher = getattr(self, "_dispatcher", None)
        if dispatcher is None:
            dispatcher = STS2Dispatcher(self)
            object.__setattr__(self, "_dispatcher", dispatcher)
        return dispatcher

    def _push_silent_mode_change(self, entry_id: str, payload: JsonObject) -> None:
        """模式切换的内部留痕：只记录，不让 NEKO 看见，更不触发它说话。

        `push_status_feedback` 默认 `ai_behavior="respond"`，那会让 AI **立刻开口**一轮
        （"好，我认真打"）——玩家要的是猫娘**偷偷**换档，不是每次汇报。`visibility=[]` 已经
        让它不进聊天窗，这里再把 `ai_behavior` 放到 `"blind"`：连喂给 LLM 都不喂。
        留痕仍然有用——它是插件面板与排障能看到的那份记录。
        """
        label = str(
            payload.get("play_style") or payload.get("style") or payload.get("mode") or entry_id
        )
        self._get_dispatcher().push_status_feedback(label, entry_id=entry_id, ai_behavior="blind")

    def _push_frontend_notification(
        self,
        *,
        content: str,
        description: str,
        metadata: JsonObject,
        priority: int = 5,
        message_type: str = "sts2_status",
        visibility: list[str] | None = None,
        ai_behavior: str | None = None,
    ) -> None:
        self._get_dispatcher().push_frontend_notification(
            content=content,
            description=description,
            metadata=metadata,
            priority=priority,
            message_type=message_type,
            visibility=visibility,
            ai_behavior=ai_behavior,
        )

    @plugin_entry(
        id="sts2_health_check",
        name=tr("entries.sts2_health_check.name", default="看看尖塔连上没"),
        description=tr("entries.sts2_health_check.description", default="看看本地尖塔 Agent 服务现在能不能正常连上。"),
        llm_result_fields=["summary"],
        input_schema={"type": "object", "properties": {}},
        metadata={"agent_auto": False},
    )
    @_ui.action(id="sts2_health_check", label=tr("entries.sts2_health_check.name", default="sts2_health_check"), tone="default", group="status", order=10)
    async def sts2_health_check(self, **_: Any):
        async def action() -> JsonObject:
            payload = await self._service.health_check()
            self._get_dispatcher().push_status_feedback(
                str(payload.get("summary") or payload.get("message") or self.i18n.t("messages.health_check.done", default="尖塔服务检查完成。")),
                entry_id="sts2_health_check",
                ai_behavior="respond",
            )
            return payload

        return await self._run_entry(action)

    @plugin_entry(
        id="sts2_get_status",
        name=tr("entries.sts2_get_status.name", default="看看现在是什么情况"),
        description=tr("entries.sts2_get_status.description", default="看看尖塔连接状态、当前界面和基础局面信息。"),
        llm_result_fields=["summary"],
        input_schema={"type": "object", "properties": {}},
        metadata={"agent_auto": False},
    )
    @_ui.action(id="sts2_get_status", label=tr("entries.sts2_get_status.name", default="sts2_get_status"), tone="default", group="status", order=20)
    async def sts2_get_status(self, **_: Any):
        return await self._run_entry(self._service.get_status)

    @plugin_entry(
        id="sts2_read_state",
        name=tr("entries.sts2_read_state.name", default="看看当前局面"),
        description=tr("entries.sts2_read_state.description", default="顺手刷新一下，并把当前快照、局势摘要和猫娘同步包一起读出来。"),
        llm_result_fields=["summary"],
        input_schema={"type": "object", "properties": {}},
        metadata={"agent_auto": False},
    )
    @_ui.action(id="sts2_read_state", label=tr("entries.sts2_read_state.name", default="sts2_read_state"), tone="default", group="status", order=30)
    async def sts2_read_state(self, **_: Any):
        async def action() -> JsonObject:
            payload = await self._service.neko.get_readout()
            self._get_dispatcher().push_status_feedback(
                str(payload.get("summary") or payload.get("message") or self.i18n.t("messages.read_state.done", default="已读取尖塔局面。")),
                entry_id="sts2_read_state",
                ai_behavior="read",
            )
            return payload

        return await self._run_entry(action)

    @plugin_entry(
        id="sts2_set_standby",
        name=tr("entries.sts2_set_standby.name", default="设置尖塔待机"),
        description=tr("entries.sts2_set_standby.description", default="切换尖塔待机模式。待机模式下停止动作执行，但保留状态整理与猫娘同步准备。"),
        llm_result_fields=["summary"],
        input_schema={
            "type": "object",
            "properties": {
                "standby": {"type": "boolean"},
            },
            "required": ["standby"],
        },
        metadata={"agent_auto": False},
    )
    @_ui.action(id="sts2_set_standby", label=tr("entries.sts2_set_standby.name", default="sts2_set_standby"), tone="default", group="mode", order=10)
    async def sts2_set_standby(self, standby: bool, **_: Any):
        return await self._run_entry(lambda: self._service.neko.set_standby(standby))

    @plugin_entry(
        id="sts2_start_autoplay",
        name=tr("entries.sts2_start_autoplay.name", default="让它自己玩起来"),
        description=tr("entries.sts2_start_autoplay.description", default="启动后台自动运行，让尖塔自己继续往下打。"),
        llm_result_fields=["summary"],
        input_schema={"type": "object", "properties": {}},
        metadata={"agent_auto": False},
    )
    @_ui.action(id="sts2_start_autoplay", label=tr("entries.sts2_start_autoplay.name", default="让它自己玩起来"), tone="primary", group="autoplay", order=10)
    async def sts2_start_autoplay(self, **_: Any):
        return await self._run_entry(self._service.neko.start_autoplay)

    @plugin_entry(
        id="sts2_pause_autoplay",
        name=tr("entries.sts2_pause_autoplay.name", default="先停一下自动玩"),
        description=tr("entries.sts2_pause_autoplay.description", default="先暂停后台自动运行，等你决定下一步。"),
        llm_result_fields=["summary"],
        input_schema={"type": "object", "properties": {}},
        metadata={"agent_auto": False},
    )
    @_ui.action(id="sts2_pause_autoplay", label=tr("entries.sts2_pause_autoplay.name", default="先停一下自动玩"), group="autoplay", order=20)
    async def sts2_pause_autoplay(self, **_: Any):
        return await self._run_entry(self._service.neko.pause_autoplay)

    @plugin_entry(
        id="sts2_resume_autoplay",
        name=tr("entries.sts2_resume_autoplay.name", default="继续让它自己玩"),
        description=tr("entries.sts2_resume_autoplay.description", default="从暂停处接着自动运行。"),
        llm_result_fields=["summary"],
        input_schema={"type": "object", "properties": {}},
        metadata={"agent_auto": False},
    )
    @_ui.action(id="sts2_resume_autoplay", label=tr("entries.sts2_resume_autoplay.name", default="继续让它自己玩"), group="autoplay", order=30)
    async def sts2_resume_autoplay(self, **_: Any):
        return await self._run_entry(self._service.neko.resume_autoplay)

    @plugin_entry(
        id="sts2_stop_autoplay",
        name=tr("entries.sts2_stop_autoplay.name", default="别让它自己玩了"),
        description=tr("entries.sts2_stop_autoplay.description", default="停止后台自动运行，把控制权收回来。"),
        llm_result_fields=["summary"],
        input_schema={"type": "object", "properties": {}},
        metadata={"agent_auto": False},
    )
    @_ui.action(id="sts2_stop_autoplay", label=tr("entries.sts2_stop_autoplay.name", default="别让它自己玩了"), tone="danger", group="autoplay", order=40)
    async def sts2_stop_autoplay(self, **_: Any):
        return await self._run_entry(self._service.neko.stop_autoplay, finish=True)

    @plugin_entry(
        id="sts2_open_coop_room",
        name=tr("entries.sts2_open_coop_room.name", default="打开 co-op 房间"),
        description=tr("entries.sts2_open_coop_room.description", default="打开联机房间（host 建房 / 非 host 加入本机 127.0.0.1:33771），猫娘选角 + ready 由自动游玩处理。"),
        llm_result_fields=["summary"],
        input_schema={
            "type": "object",
            "properties": {"host": {"type": "boolean"}},
            "required": [],
        },
        metadata={"agent_auto": False},
    )
    @_ui.action(id="sts2_open_coop_room", label=tr("entries.sts2_open_coop_room.name", default="sts2_open_coop_room"), tone="default", group="coop", order=10)
    async def sts2_open_coop_room(self, host: bool = True, **_: Any):
        return await self._run_entry(lambda: self._service.open_coop_room(host=host))

    @plugin_entry(
        id="sts2_enable_companion_mode",
        name=tr("entries.sts2_enable_companion_mode.name", default="打开陪玩模式"),
        description=tr("entries.sts2_enable_companion_mode.description", default="让它开始陪你看局面，并适时给点评和提醒。"),
        llm_result_fields=["summary"],
        input_schema={"type": "object", "properties": {}},
        metadata={"agent_auto": False},
    )
    @_ui.action(id="sts2_enable_companion_mode", label=tr("entries.sts2_enable_companion_mode.name", default="sts2_enable_companion_mode"), tone="default", group="companion", order=10)
    async def sts2_enable_companion_mode(self, **_: Any):
        return await self._run_entry(lambda: self._service.neko.set_companion_mode(True))

    @plugin_entry(
        id="sts2_disable_companion_mode",
        name=tr("entries.sts2_disable_companion_mode.name", default="关掉陪玩模式"),
        description=tr("entries.sts2_disable_companion_mode.description", default="先别继续陪玩点评，只保留基础运行。"),
        llm_result_fields=["summary"],
        input_schema={"type": "object", "properties": {}},
        metadata={"agent_auto": False},
    )
    @_ui.action(id="sts2_disable_companion_mode", label=tr("entries.sts2_disable_companion_mode.name", default="sts2_disable_companion_mode"), tone="default", group="companion", order=20)
    async def sts2_disable_companion_mode(self, **_: Any):
        return await self._run_entry(lambda: self._service.neko.set_companion_mode(False))

    @plugin_entry(
        id="sts2_set_danmaku",
        name=tr("entries.sts2_set_danmaku.name", default="开关游戏内弹幕"),
        description=tr(
            "entries.sts2_set_danmaku.description",
            default="打开/关闭游戏内猫娘弹幕。关闭后插件不再向游戏推弹幕（也不生成点评、不清屏），自动游玩与状态读取不受影响。",
        ),
        llm_result_fields=["summary"],
        input_schema={
            "type": "object",
            "properties": {
                "enabled": {"type": "boolean"},
            },
            "required": ["enabled"],
        },
        metadata={"agent_auto": False},
    )
    @_ui.action(id="sts2_set_danmaku", label=tr("entries.sts2_set_danmaku.name", default="sts2_set_danmaku"), tone="default", group="companion", order=30)
    async def sts2_set_danmaku(self, enabled: bool = True, **_: Any):
        return await self._run_entry(lambda: self._service.neko.set_danmaku(enabled))

    @plugin_entry(
        id="sts2_set_catgirl_name",
        name=tr("entries.sts2_set_catgirl_name.name", default="改联机里猫娘的名字"),
        description=tr(
            "entries.sts2_set_catgirl_name.description",
            default=(
                "设置联机（co-op）里猫娘那个玩家的显示名。真人主机和猫娘跑在同一个 Steam 账号上，"
                "名牌默认同一个名字、分不清谁是谁；设了这个名字，两个窗口里猫娘都显示它、真人仍显示 Steam 名。"
                "留空则改回跟 Steam 一样。对单人局无影响。"
            ),
        ),
        llm_result_fields=["summary"],
        input_schema={
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
        metadata={"agent_auto": False},
    )
    async def sts2_set_catgirl_name(self, name: str = "", **_: Any):
        return await self._run_entry(lambda: self._service.neko.set_catgirl_name(name))

    @plugin_entry(
        id="sts2_set_host_digest",
        name=tr("entries.sts2_set_host_digest.name", default="开关尖塔速报"),
        description=tr(
            "entries.sts2_set_host_digest.description",
            default=(
                "打开/关闭「尖塔速报」：开着时插件会把局势悄悄交给你（不可见、不进记忆），"
                "由你自己按当前人格与玩家交流。关掉只影响这一路，游戏内弹幕与自动游玩不受影响。"
            ),
        ),
        llm_result_fields=["summary"],
        input_schema={
            "type": "object",
            "properties": {"enabled": {"type": "boolean"}},
            "required": ["enabled"],
        },
        metadata={"agent_auto": False},
    )
    @_ui.action(id="sts2_set_host_digest", label=tr("entries.sts2_set_host_digest.name", default="sts2_set_host_digest"), tone="default", group="companion", order=40)
    async def sts2_set_host_digest(self, enabled: bool = True, **_: Any):
        async def action() -> JsonObject:
            payload = self._service.set_host_digest_enabled(bool(enabled))
            self._push_silent_mode_change("sts2_set_host_digest", payload)
            return payload

        return await self._run_entry(action)

    @plugin_entry(
        id="sts2_set_run_reflection",
        name=tr("entries.sts2_set_run_reflection.name", default="开关尖塔复盘"),
        description=tr(
            "entries.sts2_set_run_reflection.description",
            default=(
                "打开/关闭「尖塔复盘」：开着时每局结束会生成一次复盘并交给你；关掉后只记数据、不反思，"
                "损血归因/路线图/历史页照旧可看。"
            ),
        ),
        llm_result_fields=["summary"],
        input_schema={
            "type": "object",
            "properties": {"enabled": {"type": "boolean"}},
            "required": ["enabled"],
        },
        metadata={"agent_auto": False},
    )
    @_ui.action(id="sts2_set_run_reflection", label=tr("entries.sts2_set_run_reflection.name", default="sts2_set_run_reflection"), tone="default", group="companion", order=50)
    async def sts2_set_run_reflection(self, enabled: bool = True, **_: Any):
        async def action() -> JsonObject:
            payload = self._service.set_run_reflection_enabled(bool(enabled))
            self._push_silent_mode_change("sts2_set_run_reflection", payload)
            return payload

        return await self._run_entry(action)

    @plugin_entry(
        id="sts2_set_wait_for_turn",
        name=tr("entries.sts2_set_wait_for_turn.name", default="每回合等不等我说话"),
        description=tr(
            "entries.sts2_set_wait_for_turn.description",
            default=(
                "切换出牌节奏：true = 每个战斗回合都等你先说一句话才出牌（陪玩节奏）；"
                "false = 自己连着打，不等发言（全自动）。两种下你随时插话都会被立即采纳。"
            ),
        ),
        llm_result_fields=["summary"],
        input_schema={
            "type": "object",
            "properties": {"enabled": {"type": "boolean"}},
            "required": ["enabled"],
        },
        metadata={"agent_auto": False},
    )
    async def sts2_set_wait_for_turn(self, enabled: bool = True, **_: Any):
        async def action() -> JsonObject:
            payload = self._service.set_wait_for_turn_message(bool(enabled))
            self._push_silent_mode_change("sts2_set_wait_for_turn", payload)
            return payload

        return await self._run_entry(action)

    @plugin_entry(
        id="sts2_continue_run",
        name=tr("entries.sts2_continue_run.name", default="回到存档继续玩"),
        description=tr(
            "entries.sts2_continue_run.description",
            default="在主菜单点「继续」，回到当前存档（co-op 中途退出后回不到存档时用它）。不可用时会报出当前界面与可用动作。",
        ),
        llm_result_fields=["summary"],
        input_schema={"type": "object", "properties": {}},
        metadata={"agent_auto": False},
    )
    @_ui.action(id="sts2_continue_run", label=tr("entries.sts2_continue_run.name", default="sts2_continue_run"), tone="primary", group="run", order=10)
    async def sts2_continue_run(self, **_: Any):
        return await self._run_entry(self._service.neko.continue_saved_run, finish=True)

    @llm_tool(
        name="sts2_set_route",
        description=tr(
            "tools.sts2_set_route.description",
            default="读取自己的地图局面并讨论路线。工具不直接投票，当前角色的自主决策循环会决定行动。",
        ),
        parameters={
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": ["safe", "mirror", "pick"],
                    "description": "路线讨论倾向：safe=稳妥；mirror=讨论跟票；pick=讨论指定位置。工具只读，由自主模型选择投票。",
                },
                "pick": {
                    "type": "string",
                    "description": "mode=pick 时玩家说的位置原话，例如“中间”“第三个”“最左边”“最右”。",
                },
            },
            "required": ["mode"],
        },
        timeout=20.0,
    )
    async def llm_set_route(self, mode: str = "safe", pick: Any = None, **_: Any) -> JsonObject:
        return await self._service.neko.set_route(mode=mode, pick=pick)

    @plugin_entry(
        id="sts2_set_route",
        name=tr("entries.sts2_set_route.name", default="讨论路线"),
        description=tr(
            "entries.sts2_set_route.description",
            default="读取自己的路线选择和本局对话，继续讨论；不以启发式直接投票。",
        ),
        llm_result_fields=["summary"],
        input_schema={
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["safe", "mirror", "pick"]},
                "pick": {"type": "string"},
                "index": {"type": "integer"},
            },
            "required": ["mode"],
        },
        metadata={"agent_auto": False},
    )
    @_ui.action(id="sts2_set_route", label=tr("entries.sts2_set_route.name", default="sts2_set_route"), tone="default", group="route", order=10)
    async def sts2_set_route(self, mode: str = "safe", pick: Any = None, index: Any = None, **_: Any):
        async def action() -> JsonObject:
            payload = await self._service.neko.set_route(mode=mode, pick=pick, index=index)
            self._get_dispatcher().push_status_feedback(
                str(payload.get("summary") or payload.get("message") or "已处理地图路线指令。"),
                entry_id="sts2_set_route",
                ai_behavior="respond",
            )
            return payload

        return await self._run_entry(action)

    @llm_tool(
        name="sts2_set_control_mode",
        description=tr(
            "tools.sts2_set_control_mode.description",
            default=(
                "切换猫娘在游戏里的控制档位。按玩家原话选 mode："
                "玩家让你自己玩、不用问了（“你自己玩”“你自己看着办”“随便打”）-> mode=program；"
                "玩家要自己拿主意、让你听着（“听我安排”“你先别动”“等我说”“这步我来定”）-> mode=advise；"
                "玩家要你完全停手（“停手”“别动”“待机”）-> mode=standby。"
                "advise 下你仍然会算，玩家问“你打算怎么走”时要答得出来；"
                "他说“就这个”时用 sts2_execute_planned_operation 走一步。"
                "advise 在所有界面只思考不自动执行，包括战斗；聊天始终继续。"
            ),
        ),
        parameters={
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": ["program", "advise", "standby"],
                    "description": "program=自己玩；advise=听玩家安排，一步步等发话；standby=完全停手。",
                },
            },
            "required": ["mode"],
        },
        timeout=20.0,
    )
    async def llm_set_control_mode(self, mode: str = "program", **_: Any) -> JsonObject:
        result = await self._service.neko.set_control_mode(mode)
        # 工具返回值会原样回给 LLM，所以这里只给机器可读的最小结果。
        # 若带上"好，我自己玩"这类句子，猫娘多半会把它说出口——那就不是"偷偷换档"了。
        return {"ok": result.get("status") == "ok", "mode": result.get("mode") or mode}

    @plugin_entry(
        id="sts2_set_control_mode",
        name=tr("entries.sts2_set_control_mode.name", default="你自己玩/听我安排"),
        description=tr(
            "entries.sts2_set_control_mode.description",
            default=(
                "切换控制档位：program 自己玩（默认）；advise 所有界面只判断不自动执行，"
                "等玩家发话才走一步；standby 完全停手。档位会同步给 mod。"
            ),
        ),
        llm_result_fields=["summary"],
        input_schema={
            "type": "object",
            "properties": {"mode": {"type": "string", "enum": ["program", "advise", "standby"]}},
            "required": ["mode"],
        },
        metadata={"agent_auto": False},
    )
    @_ui.action(id="sts2_set_control_mode", label=tr("entries.sts2_set_control_mode.name", default="sts2_set_control_mode"), tone="default", group="mode", order=20)
    async def sts2_set_control_mode(self, mode: str = "program", **_: Any):
        async def action() -> JsonObject:
            payload = await self._service.neko.set_control_mode(mode)
            self._push_silent_mode_change("sts2_set_control_mode", payload)
            return payload

        return await self._run_entry(action)

    @llm_tool(
        name="sts2_set_play_style",
        description=tr(
            "tools.sts2_set_play_style.description",
            default=(
                "切换猫娘的游玩模式（打得认不认真）。按玩家原话选 style："
                "玩家要认真打、想通关（“认真打”“好好打”“我要通关”“别乱来”）-> style=serious；"
                "玩家要你偶尔闹一下（“捣蛋”“闹着玩”“别太认真”“调皮点”）-> style=mischievous；"
                "玩家要你摆烂（“笨蛋”“你傻啦”“随便打打”“犯傻”）-> style=silly。"
                "风格交给模型自行理解，不按概率或评分替换模型动作；"
                "serious 是默认档，行为与平常完全一致。这是「打得多不认真」，不是「谁来指挥」——"
                "后者用 sts2_set_control_mode。玩家亲口说「就这个」时执行的那一步不受本模式影响。"
            ),
        ),
        parameters={
            "type": "object",
            "properties": {
                "style": {
                    "type": "string",
                    "enum": ["serious", "mischievous", "silly"],
                    "description": "serious=认真（默认）；mischievous=捣蛋；silly=笨蛋。",
                },
            },
            "required": ["style"],
        },
        timeout=20.0,
    )
    async def llm_set_play_style(self, style: str = "serious", **_: Any) -> JsonObject:
        result = await self._service.neko.set_play_style(style)
        # 同上：只回机器可读的最小结果，别给 LLM 一句可以被复述的话。
        return {"ok": result.get("status") == "ok", "style": result.get("play_style") or style}

    @llm_tool(
        name="sts2_set_host_digest",
        description=tr(
            "tools.sts2_set_host_digest.description",
            default=(
                "开关「尖塔速报」：是否让插件把对局局势悄悄交给你、由你按当前人格与玩家交流。"
                "玩家说「别自己报局势了」「安静点看就行」「不用主动说话」-> enabled=false；"
                "玩家说「你多讲讲局势」「直播感一点」「主动点」-> enabled=true。"
                "只影响这一路：游戏内弹幕、自动游玩、历史记录都不受影响。"
            ),
        ),
        parameters={
            "type": "object",
            "properties": {
                "enabled": {"type": "boolean", "description": "true=开着速报；false=不主动报局势。"},
            },
            "required": ["enabled"],
        },
        timeout=20.0,
    )
    async def llm_set_host_digest(self, enabled: bool = True, **_: Any) -> JsonObject:
        result = self._service.set_host_digest_enabled(bool(enabled))
        return {"ok": result.get("status") == "ok", "enabled": bool(result.get("enabled"))}

    @llm_tool(
        name="sts2_set_run_reflection",
        description=tr(
            "tools.sts2_set_run_reflection.description",
            default=(
                "开关「尖塔复盘」：每局结束时是否生成一次复盘（指出哪几处损血最要命、下次怎么改）。"
                "玩家说「别复盘了」「打完直接开下一局」「快点重开」-> enabled=false；"
                "玩家说「打完讲讲问题」「复盘一下」-> enabled=true。"
                "关掉后只记数据不反思，损血归因/路线图/历史页照旧可看。"
            ),
        ),
        parameters={
            "type": "object",
            "properties": {
                "enabled": {"type": "boolean", "description": "true=局末复盘；false=只记数据。"},
            },
            "required": ["enabled"],
        },
        timeout=20.0,
    )
    async def llm_set_run_reflection(self, enabled: bool = True, **_: Any) -> JsonObject:
        result = self._service.set_run_reflection_enabled(bool(enabled))
        return {"ok": result.get("status") == "ok", "enabled": bool(result.get("enabled"))}

    @llm_tool(
        name="sts2_set_wait_for_turn",
        description=tr(
            "tools.sts2_set_wait_for_turn.description",
            default=(
                "切换尖塔出牌节奏：是否每个战斗回合都等玩家先说一句话再出牌。"
                "玩家嫌慢、说「你自己打就行」「别每回合都等我」「自动打」-> enabled=false；"
                "玩家想掌控每回合、说「每回合等我」「我说了你再打」-> enabled=true。"
                "注意：这不影响玩家中途指挥——两种节奏下玩家插话都会被立即采纳。"
            ),
        ),
        parameters={
            "type": "object",
            "properties": {
                "enabled": {"type": "boolean", "description": "true=每回合等玩家发言；false=自己连续打。"},
            },
            "required": ["enabled"],
        },
        timeout=20.0,
    )
    async def llm_set_wait_for_turn(self, enabled: bool = True, **_: Any) -> JsonObject:
        result = self._service.set_wait_for_turn_message(bool(enabled))
        return {"ok": result.get("status") == "ok", "enabled": bool(result.get("enabled"))}

    @plugin_entry(
        id="sts2_set_play_style",
        name=tr("entries.sts2_set_play_style.name", default="认真打/捣蛋/摆烂"),
        description=tr(
            "entries.sts2_set_play_style.description",
            default=(
                "切换游玩风格：serious 认真（默认）；mischievous 捣蛋；"
                "silly 笨蛋。作为风格输入交给模型判断，不在程序中替换模型选择。"
            ),
        ),
        llm_result_fields=["summary"],
        input_schema={
            "type": "object",
            "properties": {"style": {"type": "string", "enum": ["serious", "mischievous", "silly"]}},
            "required": ["style"],
        },
        metadata={"agent_auto": False},
    )
    @_ui.action(id="sts2_set_play_style", label=tr("entries.sts2_set_play_style.name", default="sts2_set_play_style"), tone="default", group="mode", order=30)
    async def sts2_set_play_style(self, style: str = "serious", **_: Any):
        async def action() -> JsonObject:
            payload = await self._service.neko.set_play_style(style)
            self._push_silent_mode_change("sts2_set_play_style", payload)
            return payload

        return await self._run_entry(action)

    @plugin_entry(
        id="sts2_set_instance",
        name=tr("entries.sts2_set_instance.name", default="对着主机还是猫娘"),
        description=tr(
            "entries.sts2_set_instance.description",
            default=(
                "切换插件当前驱动的游戏进程：host（配置里的 base_url；单人局就是它，coop 里是主机）"
                "或 catgirl（coop 里被拉起的猫娘进程，地址由主机的 coop_client_port 推导）。"
                "一般是自动的——打开 co-op 房间成功后插件会自己切到猫娘；这个入口用于查看与强制覆盖。"
            ),
        ),
        llm_result_fields=["summary"],
        input_schema={
            "type": "object",
            "properties": {"instance": {"type": "string", "enum": ["host", "catgirl"]}},
            "required": ["instance"],
        },
        metadata={"agent_auto": False},
    )
    @_ui.action(id="sts2_set_instance", label=tr("entries.sts2_set_instance.name", default="sts2_set_instance"), tone="default", group="mode", order=40)
    async def sts2_set_instance(self, instance: str = "host", **_: Any):
        return await self._run_entry(lambda: self._service.neko.set_instance(instance))

    @plugin_entry(
        id="sts2_apply_user_override",
        name=tr("entries.sts2_apply_user_override.name", default="聊聊当前对局"),
        description=tr("entries.sts2_apply_user_override.description", default="把当前谈话作为本局游戏上下文，并读取自己的状态；由 N.E.K.O 本体继续回应，不执行动作、不生成批准提议。"),
        llm_result_fields=["summary", "agent_view", "session", "question"],
        input_schema={
            "type": "object",
            "properties": {
                "instruction": {"type": "string"},
                "source": {"type": "string"},
            },
            "required": ["instruction"],
        },
        metadata={"agent_auto": False},
    )
    async def sts2_apply_user_override(self, instruction: str, source: str = "user", **_: Any):
        return await self._run_entry(lambda: self._service.apply_user_override_safely(instruction, source=source))

    @plugin_entry(
        id="sts2_get_planned_operation",
        name=tr("entries.sts2_get_planned_operation.name", default="看看它准备怎么走"),
        description=tr("entries.sts2_get_planned_operation.description", default="看看当前局面下，它下一步打算怎么操作。"),
        llm_result_fields=["summary"],
        input_schema={"type": "object", "properties": {}},
        metadata={"agent_auto": False},
    )
    @_ui.action(id="sts2_get_planned_operation", label=tr("entries.sts2_get_planned_operation.name", default="sts2_get_planned_operation"), tone="default", group="plan", order=10)
    async def sts2_get_planned_operation(self, **_: Any):
        async def action() -> JsonObject:
            payload = await self._service.neko.get_planned_operation()
            self._get_dispatcher().push_status_feedback(
                str(payload.get("summary") or payload.get("message") or self.i18n.t("messages.get_planned_operation.done", default="已获取尖塔规划动作。")),
                entry_id="sts2_get_planned_operation",
                ai_behavior="respond",
            )
            return payload

        return await self._run_entry(action)

    @plugin_entry(
        id="sts2_execute_planned_operation",
        name=tr("entries.sts2_execute_planned_operation.name", default="按建议走一步"),
        description=tr("entries.sts2_execute_planned_operation.description", default="直接执行它当前建议的下一步动作。"),
        llm_result_fields=["summary"],
        input_schema={"type": "object", "properties": {}},
        metadata={"agent_auto": False},
    )
    @_ui.action(id="sts2_execute_planned_operation", label=tr("entries.sts2_execute_planned_operation.name", default="sts2_execute_planned_operation"), tone="default", group="plan", order=20)
    async def sts2_execute_planned_operation(self, **_: Any):
        return await self._run_entry(self._service.neko.execute_planned_operation, finish=True)

