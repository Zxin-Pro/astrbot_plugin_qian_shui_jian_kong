# -*- coding: utf-8 -*-
"""member_fetcher.py —— 平台 API 封装

把「拉取群列表 / 拉取群成员列表 / 踢人 / 主动发群消息」等平台能力封装成
与具体协议端无关的方法（需求文档中的 get_group_list、get_group_member_list、
kick_group_member、send_message 均在本类落地上）。

平台支持说明：
    群成员管理与踢人是 OneBot v11 的扩展能力，AstrBot 体系中对应
    aiocqhttp 平台适配器（NapCat、Lagrange.OneBot、go-cqhttp、LLOneBot 等
    均以该协议接入）。本类自动发现所有 aiocqhttp 适配器实例并调用其
    CQHttp 客户端；其他平台（QQ 官方接口等不支持群管理 API）会被跳过并
    记录日志，不影响插件其余功能。
"""

import logging

from astrbot.api.event import MessageChain

logger = logging.getLogger("astrbot")

# 延迟/防御式导入 aiocqhttp 相关内部类：即使未来 AstrBot 内部路径调整，
# 也只降级为「不支持群管理功能」，绝不阻断插件加载。
try:
    from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_platform_adapter import (
        AiocqhttpAdapter,
    )
except Exception:  # pragma: no cover - 仅在内部结构变化时触发
    AiocqhttpAdapter = None

try:
    from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
        AiocqhttpMessageEvent,
    )
except Exception:  # pragma: no cover
    AiocqhttpMessageEvent = None


class MemberFetcher:
    """平台成员/管理接口封装（基于 aiocqhttp / OneBot v11）。"""

    def __init__(self, context):
        self._ctx = context

    # ------------------------------------------------------------------
    # 适配器发现
    # ------------------------------------------------------------------
    def list_adapters(self) -> list:
        """返回当前所有 aiocqhttp 平台适配器实例（支持多机器人）。"""
        try:
            platforms = self._ctx.platform_manager.get_insts()
        except Exception:
            platforms = getattr(self._ctx.platform_manager, "platform_insts", [])

        adapters = []
        for p in platforms or []:
            if AiocqhttpAdapter is not None and isinstance(p, AiocqhttpAdapter):
                adapters.append(p)
            elif hasattr(p, "bot") and hasattr(getattr(p, "bot"), "call_action"):
                # 兜底：内部类路径变化导致 isinstance 失败时，
                # 以「持有可调用 call_action 的 bot 客户端」为特征识别适配器，
                # 该特征目前仅 aiocqhttp 适配器具备
                adapters.append(p)
        return adapters

    def _platform_id(self, adapter) -> str:
        """取适配器的平台实例 ID（WebUI 平台配置里的 id，用于构造 unified_msg_origin）。"""
        try:
            return str(adapter.meta().id)
        except Exception:
            return str(getattr(getattr(adapter, "metadata", None), "id", ""))

    def get_adapter(self, platform_id: str):
        """按平台实例 ID 查找适配器。"""
        for adapter in self.list_adapters():
            if self._platform_id(adapter) == str(platform_id):
                return adapter
        return None

    async def get_bot_self_ids(self) -> set:
        """获取所有机器人的自身 QQ 号（OneBot get_login_info），用于从监控中排除自己。"""
        self_ids = set()
        for adapter in self.list_adapters():
            try:
                info = await adapter.bot.call_action("get_login_info")
                uid = str((info or {}).get("user_id", "")).strip()
                if uid:
                    self_ids.add(uid)
            except Exception as e:
                logger.debug(f"[lurker_watcher] 获取机器人自身账号失败: {e}")
        return self_ids

    def has_adapter(self) -> bool:
        """是否存在可用的 aiocqhttp 适配器。"""
        return bool(self.list_adapters())

    # ------------------------------------------------------------------
    # 群信息
    # ------------------------------------------------------------------
    async def get_group_list(self) -> dict:
        """获取机器人加入的全部群。

        返回 {群号(字符串): {"group_name":..., "platform_id":..., "member_count":...}}，
        多个机器人共同在的群以先发现的为准。失败时返回空 dict（不抛异常）。
        """
        result = {}
        for adapter in self.list_adapters():
            pid = self._platform_id(adapter)
            try:
                groups = await adapter.bot.call_action("get_group_list")
            except Exception as e:
                logger.warning(f"[lurker_watcher] 平台 {pid} 获取群列表失败: {e}")
                continue
            for g in groups or []:
                gid = str(g.get("group_id", "")).strip()
                if not gid or gid in result:
                    continue
                result[gid] = {
                    "group_name": str(g.get("group_name") or ""),
                    "platform_id": pid,
                    "member_count": int(g.get("member_count") or 0),
                }
        return result

    async def get_group_member_list(self, platform_id: str, gid):
        """拉取群成员全量列表。

        返回 OneBot v11 的成员数组（含 user_id / nickname / card / role /
        last_sent_time 等字段）；失败返回 None。
        """
        adapter = self.get_adapter(platform_id)
        if adapter is None:
            logger.warning(f"[lurker_watcher] 未找到平台实例 {platform_id}，无法拉取群 {gid} 成员")
            return None
        try:
            members = await adapter.bot.call_action(
                "get_group_member_list", group_id=int(str(gid))
            )
            return members or []
        except Exception as e:
            logger.error(f"[lurker_watcher] 拉取群 {gid} 成员列表失败: {e}")
            return None

    # ------------------------------------------------------------------
    # 群管理
    # ------------------------------------------------------------------
    async def kick_group_member(self, platform_id: str, gid, uid):
        """把成员移出群聊（对应 OneBot v11 set_group_kick）。

        返回 (成功与否, 失败原因)。常见失败原因：机器人不是群管理员、
        目标已是群主/管理员、成员已退群等。
        """
        adapter = self.get_adapter(platform_id)
        if adapter is None:
            return False, f"未找到平台实例 {platform_id}"
        try:
            await adapter.bot.call_action(
                "set_group_kick",
                group_id=int(str(gid)),
                user_id=int(str(uid)),
                reject_add_request=False,  # 被踢后仍允许重新入群
            )
            return True, ""
        except Exception as e:
            # ActionFailed 等异常统一归类为失败并返回协议端报错信息
            return False, str(e)

    # ------------------------------------------------------------------
    # 主动消息
    # ------------------------------------------------------------------
    async def send_group_message(self, platform_id: str, gid, chain: MessageChain) -> bool:
        """向指定群主动发送消息链（不依赖事件上下文）。失败返回 False。"""
        adapter = self.get_adapter(platform_id)
        if adapter is not None and AiocqhttpMessageEvent is not None:
            try:
                await AiocqhttpMessageEvent.send_message(
                    bot=adapter.bot,
                    message_chain=chain,
                    is_group=True,
                    session_id=str(gid),
                )
                return True
            except Exception as e:
                logger.warning(f"[lurker_watcher] 直连协议端发送群 {gid} 消息失败: {e}，尝试 unified_msg_origin 兜底")
        # 兜底：走 Context.send_message（unified_msg_origin 形如 "平台id:GroupMessage:群号"）
        try:
            return await self._ctx.send_message(
                f"{platform_id}:GroupMessage:{gid}", chain
            )
        except Exception as e:
            logger.error(f"[lurker_watcher] 向群 {gid} 发送消息失败: {e}")
            return False
