# -*- coding: utf-8 -*-
"""storage.py —— 数据存储层（基于 AstrBot PluginKVStoreMixin）

AstrBot v4 的 Star 基类已经混入了 PluginKVStoreMixin
（astrbot/core/utils/plugin_kv_store.py），提供三个异步接口：

    await self.put_kv_data(key, value)      # 写入
    await self.get_kv_data(key, default)    # 读取（不存在返回 default）
    await self.delete_kv_data(key)          # 删除

数据保存在 AstrBot 内置数据库的 preference 表中，scope="plugin"、
scope_id=plugin_id（即 "作者/插件名"），随插件/机器人重启持久保留。

本插件的 KV 结构设计（与需求文档一致，按 group_id 分桶）：

    lurker:index                -> {群号: {"platform_id", "group_name"}}
    lurker:members:{群号}       -> {用户ID: {"first_seen",       # 首次纳管时间
                                             "last_message_time",# 最后发言时间
                                             "username",         # 昵称/群名片
                                             "warned_at",        # 上次警告时间
                                             "evaluated_at",     # 上次踢人评估时间
                                             "kick_fails"}}      # 连续踢出失败次数
    lurker:group_config:{群号}  -> {"threshold_days" 等群级覆盖值, "whitelist": [...]}
    lurker:group_meta:{群号}    -> {"initialized_at", "last_report_date", "member_count"}

性能设计：
    每条群消息都会更新成员活跃时间，如果每次都写库会产生巨大写放大。
    因此本层采用「内存读写 + 脏标记 + 批量落盘」策略：
      * 读：全部走内存（load() 时一次性载入）；
      * 写：先改内存并标记脏 key，由定时任务每 60 秒 flush 一次，
        并在关键动作（初始化/踢出/改配置）和插件 terminate() 时强制落盘。
"""

import copy
import logging

logger = logging.getLogger("astrbot")

# ---- KV key 常量与生成器 -------------------------------------------------

K_INDEX = "lurker:index"


def k_members(gid: str) -> str:
    return f"lurker:members:{gid}"


def k_group_config(gid: str) -> str:
    return f"lurker:group_config:{gid}"


def k_group_meta(gid: str) -> str:
    return f"lurker:group_meta:{gid}"


def new_member_record(
    first_seen: float,
    last_message_time: float,
    username: str,
    role: str = "member",
) -> dict:
    """构造一条新的成员记录（字段与需求文档定义保持一致，并追加运维字段）。"""
    return {
        "first_seen": first_seen,          # 首次纳入监控的时间戳（秒）
        "last_message_time": last_message_time,  # 最后一次发言的时间戳（秒）
        "username": username or "",        # 群名片 / 昵称
        "role": role or "member",          # 群身份：owner / admin / member（owner、admin 不参与警告/踢出）
        "warned_at": None,                 # 上次被 @ 警告的时间戳；None 表示从未警告
        "evaluated_at": None,              # 上次进入踢人评估的时间戳
        "kick_fails": 0,                   # 连续踢出失败次数（机器人无管理员权限等）
    }


class LurkerStorage:
    """潜水监测插件的存储门面：内存缓存 + PluginKVStoreMixin 持久化。"""

    def __init__(self, star):
        # star：插件主类实例（Star 子类），借其继承的 PluginKVStoreMixin 读写 KV。
        # 注意：plugin_id 由 AstrBot 在实例化之后、initialize() 之前赋值，
        # 因此 load() 必须在 initialize() 中调用，不能提前到 __init__。
        self._star = star
        self._loaded = False
        self._index: dict = {}          # gid -> {"platform_id","group_name"}
        self._members: dict = {}        # gid -> {uid -> record}
        self._group_configs: dict = {}  # gid -> {key: value}
        self._group_metas: dict = {}    # gid -> {key: value}
        self._dirty: set = set()        # 待落盘的 key 集合

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def load(self):
        """从 KV 存储载入全部历史数据到内存（幂等，可重复调用）。"""
        index = await self._star.get_kv_data(K_INDEX, {}) or {}
        self._index = dict(index)
        self._members, self._group_configs, self._group_metas = {}, {}, {}
        for gid in list(self._index.keys()):
            self._members[gid] = await self._star.get_kv_data(k_members(gid), {}) or {}
            self._group_configs[gid] = await self._star.get_kv_data(k_group_config(gid), {}) or {}
            self._group_metas[gid] = await self._star.get_kv_data(k_group_meta(gid), {}) or {}
        self._dirty.clear()
        self._loaded = True
        total = sum(len(m) for m in self._members.values())
        logger.info(f"[lurker_watcher] 存储载入完成：{len(self._index)} 个群，{total} 名成员")

    async def flush(self):
        """把所有脏 key 批量写回 KV 存储。

        并发安全：先把所有待写数据做一次统一的深拷贝快照，再逐个异步写入。
        这样即使落盘期间有新消息并发修改内存，本次落盘的也是一组互相一致的数据。
        """
        if not self._dirty:
            return
        pending = []
        for key in list(self._dirty):
            try:
                if key == K_INDEX:
                    value = self._index
                elif key.startswith("lurker:members:"):
                    gid = key.split(":", 2)[2]
                    value = self._members.get(gid, {})
                elif key.startswith("lurker:group_config:"):
                    gid = key.split(":", 2)[2]
                    value = self._group_configs.get(gid, {})
                elif key.startswith("lurker:group_meta:"):
                    gid = key.split(":", 2)[2]
                    value = self._group_metas.get(gid, {})
                else:
                    self._dirty.discard(key)
                    continue
                pending.append((key, copy.deepcopy(value)))
            except Exception as e:
                logger.error(f"[lurker_watcher] 快照失败 key={key}: {e}")
        for key, value in pending:
            try:
                await self._star.put_kv_data(key, value)
                self._dirty.discard(key)
            except Exception as e:  # 单 key 失败不影响其他 key
                logger.error(f"[lurker_watcher] 落盘失败 key={key}: {e}")
        logger.debug("[lurker_watcher] 存储已落盘")

    # ------------------------------------------------------------------
    # 群索引
    # ------------------------------------------------------------------
    def has_group(self, gid) -> bool:
        return str(gid) in self._index

    def list_groups(self) -> dict:
        """返回 {群号: {"platform_id","group_name"}} 的浅拷贝。"""
        return dict(self._index)

    def get_group_platform(self, gid) -> str:
        """返回该群绑定的平台实例 ID（用于主动发消息/调用管理接口）。"""
        info = self._index.get(str(gid)) or {}
        return info.get("platform_id", "")

    def get_group_name(self, gid) -> str:
        info = self._index.get(str(gid)) or {}
        return info.get("group_name", "")

    def upsert_group(self, gid, platform_id: str, group_name: str):
        """登记/更新一个受监控群，并标记索引为脏。"""
        self._index[str(gid)] = {
            "platform_id": str(platform_id or ""),
            "group_name": str(group_name or ""),
        }
        self._dirty.add(K_INDEX)

    async def drop_group(self, gid):
        """彻底移除一个群的所有数据（索引、成员、群配置、元数据）。"""
        gid = str(gid)
        self._index.pop(gid, None)
        self._members.pop(gid, None)
        self._group_configs.pop(gid, None)
        self._group_metas.pop(gid, None)
        self._dirty.discard(K_INDEX)
        for key in (k_members(gid), k_group_config(gid), k_group_meta(gid)):
            try:
                await self._star.delete_kv_data(key)
            except Exception as e:
                logger.warning(f"[lurker_watcher] 删除 KV 失败 key={key}: {e}")
        self._dirty.add(K_INDEX)

    # ------------------------------------------------------------------
    # 成员数据
    # ------------------------------------------------------------------
    def get_members(self, gid) -> dict:
        """返回 {用户ID: 成员记录}（内部 dict 引用，只读遍历请自行 list() 拷贝）。"""
        return self._members.setdefault(str(gid), {})

    def get_member(self, gid, uid):
        return self.get_members(gid).get(str(uid))

    def init_members(self, gid, mapping: dict):
        """用拉取到的全量成员表整体替换某群的成员记录。"""
        self._members[str(gid)] = mapping
        self._dirty.add(k_members(str(gid)))

    def touch_member(self, gid, uid, username: str, now: float) -> bool:
        """成员发言：刷新最后发言时间，并清除警告/评估状态。

        返回 True 表示该成员是首次出现（例如新入群成员在两次全量拉取之间入群）。
        """
        members = self.get_members(gid)
        uid = str(uid)
        rec = members.get(uid)
        if rec is None:
            members[uid] = new_member_record(now, now, username)
            self._dirty.add(k_members(gid))
            return True
        rec["last_message_time"] = now
        if username and username != rec.get("username"):
            rec["username"] = username  # 群名片可能变更
        # 一旦发言即脱离潜水状态，清除此前的警告/评估标记
        rec["warned_at"] = None
        rec["evaluated_at"] = None
        rec["kick_fails"] = 0
        self._dirty.add(k_members(gid))
        return False

    def set_member_fields(self, gid, uid, **fields):
        """局部更新成员记录字段（如 warned_at / evaluated_at / kick_fails）。"""
        rec = self.get_members(gid).get(str(uid))
        if rec is None:
            return
        rec.update(fields)
        self._dirty.add(k_members(str(gid)))

    def remove_member(self, gid, uid):
        """把成员从监控表中移除（被踢出/退群/连续踢出失败时调用）。"""
        members = self.get_members(gid)
        if members.pop(str(uid), None) is not None:
            self._dirty.add(k_members(str(gid)))

    # ------------------------------------------------------------------
    # 群独立配置（多群独立配置能力）
    # ------------------------------------------------------------------
    def get_group_config(self, gid) -> dict:
        return self._group_configs.setdefault(str(gid), {})

    async def set_group_config(self, gid, key, value):
        """写入一条群级覆盖配置并立即落盘（指令修改配置时调用）。"""
        cfg = self.get_group_config(gid)
        cfg[key] = value
        self._dirty.add(k_group_config(str(gid)))
        await self.flush()

    async def remove_group_config(self, gid, key):
        """删除一条群级覆盖配置（恢复使用全局默认值），立即落盘。"""
        cfg = self.get_group_config(gid)
        if key in cfg:
            cfg.pop(key)
            self._dirty.add(k_group_config(str(gid)))
            await self.flush()

    # ------------------------------------------------------------------
    # 群元数据
    # ------------------------------------------------------------------
    def get_group_meta(self, gid) -> dict:
        return self._group_metas.setdefault(str(gid), {})

    def set_group_meta(self, gid, key, value):
        """写群元数据（仅标脏，由周期 flush 落盘即可）。"""
        self.get_group_meta(gid)[key] = value
        self._dirty.add(k_group_meta(str(gid)))
