# -*- coding: utf-8 -*-
"""notifier.py —— 消息构建与发送

所有发到群里的内容（每日报告、@ 警告、最终通牒、移出通知）统一在这里
拼装，保证文案风格一致、也方便单独调整。

报告/排行等纯文本走 MessageChain().message()；需要 @ 的场景用
MessageChain().at(name, qq) 把 At 组件插入消息链头部（注意 v4 中
at() 的参数顺序是 name 在前、qq 在后）。
"""

import logging
import re
import time
from datetime import datetime

from astrbot.api.event import MessageChain

try:
    from .config import DEFAULT_WARN_TEMPLATE
except ImportError:  # 兜底：平铺目录导入
    from config import DEFAULT_WARN_TEMPLATE

logger = logging.getLogger("astrbot")

DAY_SECONDS = 86400


def render_template(template: str, variables: dict) -> str:
    """安全的模板渲染：只替换 {变量名} 形式的占位符。

    - 变量表里没有的占位符原样保留（如用户写了 {qq}）；
    - 不平衡的花括号不会引发异常（不使用 str.format）。
    """
    if not template:
        return ""

    def _repl(m: "re.Match") -> str:
        key = m.group(1)
        return str(variables[key]) if key in variables else m.group(0)

    return re.sub(r"\{(\w+)\}", _repl, template)


def fmt_days(days: float) -> str:
    """把潜水时长格式化成人类可读文本：
    <1 小时显示分钟，<1 天显示小时，≥10 天取整，其余保留 1 位小数。
    """
    if days < 0:
        days = 0
    if days < 1 / 24:
        return f"{max(1, int(days * 24 * 60))} 分钟"
    if days < 1:
        return f"{int(days * 24)} 小时"
    if days >= 10:
        return f"{int(days)} 天"
    return f"{days:.1f} 天"


class Notifier:
    """消息构建 + 群发送封装。"""

    def __init__(self, context, fetcher):
        self._ctx = context
        self._fetcher = fetcher

    # ------------------------------------------------------------------
    # 发送
    # ------------------------------------------------------------------
    async def send_group_text(self, platform_id: str, gid, text: str) -> bool:
        """发送纯文本到群。"""
        return await self._fetcher.send_group_message(
            platform_id, gid, MessageChain().message(text)
        )

    async def send_group_chain(self, platform_id: str, gid, chain: MessageChain) -> bool:
        """发送消息链到群（用于 @ 消息等富文本场景）。"""
        return await self._fetcher.send_group_message(platform_id, gid, chain)

    # ------------------------------------------------------------------
    # 每日报告 / 排行榜
    # ------------------------------------------------------------------
    def build_report(
        self,
        *,
        title: str,
        group_name: str,
        members: dict,
        whitelist: set,
        threshold: int,
        warning_days: int,
        top_n: int,
    ) -> str:
        """构建潜水监测报告（排行榜）文本。

        members: {uid: 成员记录}；whitelist: 白名单 uid 集合（带 ⭐、不参与警告/踢出计数）。
        """
        now = time.time()
        warn_line = max(1, threshold - warning_days)

        rows = []  # (days, uid, rec)
        for uid, rec in list(members.items()):
            days = (now - float(rec.get("last_message_time") or now)) / DAY_SECONDS
            rows.append((days, str(uid), rec))
        # 潜水最久的排最前
        rows.sort(key=lambda item: item[0], reverse=True)

        total = len(rows)
        active_24h = sum(1 for d, _, _ in rows if d < 1)
        warn_n = sum(
            1 for d, uid, _ in rows
            if warn_line <= d < threshold and uid not in whitelist
        )
        kick_n = sum(1 for d, uid, _ in rows if d >= threshold and uid not in whitelist)
        wl_members = [(d, uid, rec) for d, uid, rec in rows if uid in whitelist]

        lines = [
            f"📊 {title} · {group_name or '未命名群'}",
            "━━━━━━━━━━━━━━━━━━",
            f"👥 群成员 {total} 人 ｜ 💬 24h 活跃 {active_24h} 人",
            f"🔔 预警区（≥{warn_line} 天）{warn_n} 人 ｜ ⚠️ 待处置（≥{threshold} 天）{kick_n} 人",
            "",
            f"🐢 潜水排行 TOP{top_n}：",
        ]

        medals = ["🥇", "🥈", "🥉"]
        shown = 0
        for d, uid, rec in rows:
            if shown >= top_n:
                break
            star = " ⭐" if uid in whitelist else ""
            if not star and d >= threshold:
                mark = " ⚠️已达阈值"
            elif not star and d >= warn_line:
                mark = " 🔔预警区"
            else:
                mark = ""
            medal = medals[shown] if shown < len(medals) else f"{shown + 1}."
            name = rec.get("username") or uid
            lines.append(f"{medal} {name}（{uid}）· 潜水 {fmt_days(d)}{mark}{star}")
            shown += 1
        if shown == 0:
            lines.append("（暂无成员数据，请先用 /lurker init 初始化）")

        if total > shown:
            lines.append(f"……其余 {total - shown} 人未展示")

        if wl_members:
            names = "、".join(
                f"{rec.get('username') or uid}({fmt_days(d)})"
                for d, uid, rec in wl_members[:10]
            )
            more = f" 等 {len(wl_members)} 人" if len(wl_members) > 10 else ""
            lines.append(f"⭐ 白名单：{names}{more}（不参与警告/踢出）")

        lines.append(
            f"🤖 阈值 {threshold} 天 ｜ 提前 {warning_days} 天预警 ｜ {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        )
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # @ 警告 / 最终通牒 / 移出通知
    # ------------------------------------------------------------------
    def build_warn_chain(
        self,
        uid,
        username: str,
        days: float,
        threshold: int,
        warning_days: int,
        template: str = "",
        group_id="",
    ) -> MessageChain:
        """预警期 @ 警告消息链。

        template 为用户自定义文案模板（支持 {name} {days} {remain} {warn_line}
        {threshold} {group} 占位符）；为空时使用内置默认文案。
        消息会自动 @ 目标成员，模板中无需写 @。
        """
        warn_line = max(1, threshold - warning_days)
        remain = max(0.1, threshold - days)
        text = render_template(
            template or DEFAULT_WARN_TEMPLATE,
            {
                "name": username or uid,
                "days": fmt_days(days),
                "remain": fmt_days(remain),
                "warn_line": warn_line,
                "threshold": threshold,
                "group": str(group_id or ""),
            },
        )
        # 注意 v4 MessageChain.at() 签名：at(name, qq)
        return MessageChain().at(username or "", uid).message(f" {text}")

    def build_final_warning_chain(self, uid, username: str, days: float, threshold: int, reason: str) -> MessageChain:
        """踢人前的最终警告消息链。"""
        text = (
            f" 🚨 最后通牒：你已连续潜水 {fmt_days(days)}，达到 {threshold} 天阈值，"
            f"即将被移出本群。\n判定理由：{reason}"
        )
        return MessageChain().at(username or "", uid).message(text)

    def build_kick_notice_chain(
        self, uid, username: str, days: float, reason: str, decision_desc: str
    ) -> MessageChain:
        """移出群聊后的群通知消息链。"""
        text = (
            f"\n🚪 移出通知：{username or uid}（{uid}）已连续 {fmt_days(days)} 未发言，"
            f"经{decision_desc}被移出群聊。\n理由：{reason}\n"
            f"如为误判请联系管理员，欢迎重新入群后保持活跃～"
        )
        return MessageChain().message(text)
