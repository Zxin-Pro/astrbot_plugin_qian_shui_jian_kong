# -*- coding: utf-8 -*-
"""config.py —— 插件配置读取封装

职责：
    1. 统一读取 WebUI 配置（_conf_schema.json 生成的 AstrBotConfig）；
    2. 实现「全局配置 + 群独立覆盖」的合并逻辑（多群独立配置）；
    3. 对配置值做类型清洗与合法性校验（用户在 WebUI 里填什么都尽量兜住）。

说明：
    AstrBot v4 在插件目录存在 _conf_schema.json 时，会自动构造 AstrBotConfig
    并作为构造函数第二个参数注入插件（见 astrbot/core/star/star_manager.py），
    本模块不直接保存配置文件 —— 插件配置的持久化由 AstrBot 负责
    （保存于 data/config/<插件目录名>_config.json），本插件只读。
    群独立覆盖值持久化在本插件的 KV 存储中（见 storage.py）。
"""

DEFAULT_WARN_TEMPLATE = (
    "⚠️ 潜水预警：你已连续 {days} 未在本群发言（预警线 {warn_line} 天，阈值 {threshold} 天）。"
    "再潜水约 {remain} 将进入移出评估，快来冒个泡吧～"
)

# 与 _conf_schema.json 保持一致的兜底默认值。
# 即使 AstrBotConfig 里缺失某个键（例如旧版本配置文件），也能安全取值。
DEFAULTS = {
    "threshold_days": 7,        # 潜水天数阈值
    "warning_days": 2,          # 提前预警天数
    "check_interval": 3600,     # 检查间隔（秒）
    "daily_report_time": "08:00",  # 每日报告时间（HH:MM）
    "enable_llm_decision": True,   # 是否启用 LLM 智能决策
    "warn_before_kick": True,      # 踢人前是否发送最终警告
    "groups_to_monitor": [],    # 要监控的群号列表（空 = 全部）
    "whitelist": [],            # 全局白名单用户 ID
    "report_top_n": 10,         # 排行榜显示人数
    "max_warns_per_round": 0,       # 每轮警告人数上限（0 = 不限制）
    "max_kick_evals_per_round": 0,  # 每轮踢人评估人数上限（0 = 不限制）
    "warn_template": DEFAULT_WARN_TEMPLATE,  # 预警文案模板（可含占位符）
}

# 允许被「群独立配置」覆盖的键（多群独立配置能力的核心）。
# whitelist 不在此列：它由 get_whitelist() 做全局 + 群级合并，语义不同。
GROUP_OVERRIDABLE = frozenset({
    "threshold_days",
    "warning_days",
    "enable_llm_decision",
    "warn_before_kick",
    "max_warns_per_round",
    "max_kick_evals_per_round",
    "warn_template",
})

_INT_KEYS = frozenset({"threshold_days", "warning_days", "check_interval", "report_top_n",
                       "max_warns_per_round", "max_kick_evals_per_round"})
_BOOL_KEYS = frozenset({"enable_llm_decision", "warn_before_kick"})
_LIST_KEYS = frozenset({"groups_to_monitor", "whitelist"})
_STR_KEYS = frozenset({"daily_report_time"})


import re


def normalize_hhmm(value, fallback=DEFAULTS["daily_report_time"]):
    """把任意输入规整为合法的 "HH:MM" 字符串；失败时返回 fallback。

    支持 "8:00"、"08：00"（全角冒号）等常见写法。
    """
    if not isinstance(value, str):
        return fallback
    text = value.strip().replace("：", ":")
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", text)
    if not m:
        return fallback
    hh, mm = int(m.group(1)), int(m.group(2))
    if 0 <= hh <= 23 and 0 <= mm <= 59:
        return f"{hh:02d}:{mm:02d}"
    return fallback


class PluginConfig:
    """插件配置访问器：全局值 + 群级覆盖值的统一读取入口。

    用法：
        cfg = PluginConfig(self.config, self.storage)
        threshold = cfg.get_group("threshold_days", group_id)   # 群值优先
        whitelist = cfg.get_whitelist(group_id)                 # 全局 + 群级合并
    """

    def __init__(self, raw_config, storage=None):
        # raw_config 即 AstrBotConfig（dict 子类），可直接 .get()
        self._raw = raw_config if raw_config is not None else {}
        self._storage = storage

    # ------------------------------------------------------------------
    # 全局配置
    # ------------------------------------------------------------------
    def get_global(self, key):
        """读取全局配置项（带类型清洗与默认值兜底）。"""
        if key not in DEFAULTS:
            raise KeyError(f"未知配置项: {key}")
        return self._sanitize(key, self._raw.get(key, DEFAULTS[key]))

    def get_check_interval(self):
        """检查间隔（秒），最小 60 秒，防止误配置导致死循环式扫描。"""
        return max(60, self.get_global("check_interval"))

    def get_monitor_groups(self):
        """要监控的群号列表（字符串），空列表表示监控所有群。"""
        return self.get_global("groups_to_monitor")

    # ------------------------------------------------------------------
    # 群级配置（全局 + 群独立覆盖）
    # ------------------------------------------------------------------
    def get_group(self, key, group_id=None):
        """读取对某个群生效的配置值：群独立覆盖 > 全局默认。"""
        if key not in DEFAULTS:
            raise KeyError(f"未知配置项: {key}")
        gid = str(group_id) if group_id else ""
        if gid and key in GROUP_OVERRIDABLE and self._storage is not None:
            override = self._storage.get_group_config(gid) or {}
            if key in override:
                return self._sanitize(key, override[key])
        return self.get_global(key)

    def get_whitelist(self, group_id=None):
        """返回对某个群生效的白名单集合（全局白名单 ∪ 群级白名单），元素为字符串 QQ 号。"""
        merged = {str(x) for x in self.get_global("whitelist")}
        gid = str(group_id) if group_id else ""
        if gid and self._storage is not None:
            override = self._storage.get_group_config(gid) or {}
            for item in override.get("whitelist", []) or []:
                merged.add(str(item))
        return merged

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    @staticmethod
    def _sanitize(key, value):
        """按配置项类型清洗原始值，尽力兼容 WebUI / 手改配置文件的各种脏输入。"""
        try:
            if key == "warn_template":
                # 文案模板：保持原样（可为空，空则使用内置默认文案）
                return str(value) if value is not None else ""
            if key in _INT_KEYS:
                return int(str(value).strip())
            if key in _BOOL_KEYS:
                if isinstance(value, bool):
                    return value
                return str(value).strip().lower() in ("1", "true", "yes", "on", "是")
            if key in _STR_KEYS:
                return normalize_hhmm(value)
            if key in _LIST_KEYS:
                if value is None:
                    return []
                if isinstance(value, (list, tuple, set)):
                    return [str(item).strip() for item in value if str(item).strip()]
                # 单个值容忍为单元素列表
                text = str(value).strip()
                return [text] if text else []
        except (TypeError, ValueError):
            # 清洗失败时退回默认值，保证插件永远能拿到可用配置
            return DEFAULTS[key]
        return DEFAULTS[key]
