"""
二次元图片插件 - 基于 Lolicon API
为 AI 和用户提供二次元图片获取能力，支持个性化标签筛选和内容级别控制。

权限模型：
- 管理员名单：config.toml 的 r18.group_allowed_chats（群聊）与 r18.user_allowed_chats（私聊），
  仅通过 WebUI 维护，填裸群号/QQ号，无需前缀，插件只读；
- AI 名单：LLM 通过 enable_r18/disable_r18 工具维护，持久化在插件数据目录的
  r18_whitelist.json 中（同样群聊/私聊分开存储），与 config.toml 完全隔离，
  生效名单为两者并集。
插件自身不写 config.toml（缺失字段由 SDK Runner 自动补齐）。
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import random
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Literal, Optional, Tuple

import aiohttp
from PIL import Image, ImageOps

from maibot_sdk import (
    Command,
    Field,
    MaiBotPlugin,
    PluginConfigBase,
    Tool,
)
from maibot_sdk.types import (
    ActivationType,
    ToolParameterInfo,
    ToolParamType,
)


# ── 长宽比筛选 ──────────────────────────────────────────────

ASPECT_RATIO_PATTERN = re.compile(
    r"^(gt|gte|lt|lte|eq)(\d+(?:\.\d+)?)(?:(gt|gte|lt|lte|eq)(\d+(?:\.\d+)?))?$"
)
"""长宽比表达式：单个条件（如 gt1.5）或两个条件连写（如 gt1.5lt2.0）。

第二组条件用原子分组：操作符和数值必须成对出现，`gt1.5lt` 这种残缺写法不合法。
"""

ASPECT_RATIO_MIN = 0.1
ASPECT_RATIO_MAX = 10.0
"""长宽比的合理范围，超出视为用户写错了。"""

ASPECT_RATIO_ALIASES: Dict[str, str] = {
    "horizontal": "gt1",
    "横图": "gt1",
    "vertical": "lt1",
    "竖图": "lt1",
    "square": "eq1",
    "方图": "eq1",
}
"""长宽比快捷词到表达式的映射。"""

MAX_TAG_GROUPS = 3
"""单次请求最多携带的标签组数（组间 AND）。"""

MAX_UID_COUNT = 20
"""单次请求最多携带的作者 UID 数（API 限制）。"""

COMPONENT_TIMEOUT_MS = 120000
"""组件 RPC 超时声明（毫秒）。

宿主默认 60 秒（component_registry 从组件 metadata 读取 timeout_ms）。
/tu 命令路径会 await 整个下载+发送流程，多图 + 慢域名时可能超过 60 秒被宿主砍掉，
声明 120 秒给足时间；单张图片下载仍受会话超时约束，不会无限挂起。
"""

HELP_TEXT = """🎨 二次元图片插件使用帮助

📌 基础命令
  /tu                获取 1 张随机图片
  /tu 3              获取 3 张图片
  /tu help           显示这份帮助

🏷️ 标签搜索（# 开头，Pixiv 标签精确匹配）
  /tu #萝莉          单标签
  /tu #白丝,黑丝     组内 OR（白丝或黑丝）
  /tu #萝莉 #白丝    组间 AND（萝莉且白丝，最多 3 组）
  /tu 白丝           裸词 = 标签（旧写法，仍然可用）

🔍 关键词搜索（kw: 前缀，匹配标题/作者/标签）
  /tu kw:原神        模糊搜索"原神"

📐 长宽比筛选
  /tu 横图 / 竖图 / 方图    快捷词
  /tu gt1.5                自定义长宽比表达式

🤖 其他选项
  /tu r18            R18 内容（需会话已开启权限）
  /tu noai           排除 AI 作品
  /tu uid:12345      指定画师

💡 组合示例
  /tu 3 #萝莉,少女 横图
  /tu #白丝 #JK noai
  /tu kw:原神 5 竖图
"""


AI_TYPE_LABELS: Dict[int, str] = {0: "", 1: "✋ 非AI作品", 2: "🤖 AI作品"}
"""Lolicon API 的 aiType 字段含义（转发节点说明文本用）。"""

HELP_TRIGGERS = frozenset({"help", "帮助", "?", "？"})
"""触发帮助的输入。"""


@dataclass
class TuParsedArgs:
    """/tu 命令参数解析结果。"""

    num: int = 1
    request_r18: bool = False
    exclude_ai: Optional[bool] = None
    """noai 显式覆盖；None 表示未指定，回落配置值。"""
    keyword: str = ""
    uid: int = 0
    aspect_ratio: str = ""
    tags: List[str] = field(default_factory=list)
    """合并后的标签数组：#组 组内 OR 用 | 连接，裸词各成一组；组间 AND。"""
    warnings: List[str] = field(default_factory=list)
    """解析中可告知用户的问题（如表达式无效、标签组超限）。"""


def parse_tu_args(args_text: str, *, max_num: int) -> TuParsedArgs:
    """解析 /tu 后面的参数文本。

    分支优先级：纯数字→数量 → r18/nsfw/adult → noai/排除ai → 长宽比快捷词 →
    #标签组 → kw: 关键词 → uid: 画师 → 合法长宽比表达式 → 裸词=标签（旧写法）。
    形似长宽比表达式但不合法的输入（如 gt99）提示后忽略，不当标签。
    """
    parsed = TuParsedArgs(num=1)
    text = (args_text or "").strip()
    if not text:
        return parsed

    tag_groups: List[List[str]] = []
    bare_tags: List[str] = []
    keyword_parts: List[str] = []

    for raw_arg in text.split():
        arg = raw_arg.strip()
        if not arg:
            continue
        arg_lower = arg.lower()

        # 数量
        if arg_lower.isdigit():
            parsed.num = max(1, min(int(arg_lower), max_num))
            continue

        # R18（保留旧的 nsfw/adult 别名）
        if arg_lower in ("r18", "nsfw", "adult"):
            parsed.request_r18 = True
            continue

        # 排除 AI
        if arg_lower in ("noai", "no_ai", "排除ai"):
            parsed.exclude_ai = True
            continue

        # 长宽比快捷词
        if arg_lower in ASPECT_RATIO_ALIASES:
            parsed.aspect_ratio = ASPECT_RATIO_ALIASES[arg_lower]
            continue

        # 新格式标签 #a 或 #a,b,c（组内 OR）
        if arg.startswith("#"):
            group = [t.strip() for t in arg[1:].split(",") if t.strip()]
            if group:
                if len(tag_groups) >= MAX_TAG_GROUPS:
                    parsed.warnings.append("标签组最多 " + str(MAX_TAG_GROUPS) + " 组，已忽略: " + arg)
                else:
                    tag_groups.append(group)
            continue

        # 关键词 kw:xxx
        if arg_lower.startswith("kw:"):
            value = arg.split(":", 1)[1].strip()
            if value:
                keyword_parts.append(value)
            continue

        # 画师 uid:123
        if arg_lower.startswith("uid:"):
            try:
                parsed.uid = int(arg.split(":", 1)[1])
            except ValueError:
                parsed.warnings.append("无效的 UID: " + arg)
            continue

        # 合法的长宽比表达式
        if validate_aspect_ratio(arg_lower):
            parsed.aspect_ratio = arg_lower
            continue

        # 形似长宽比表达式但不合法（如 gt99）→ 提示后忽略，不当标签
        if ASPECT_RATIO_PATTERN.match(arg_lower):
            parsed.warnings.append("长宽比表达式无效: " + arg + "，已忽略")
            continue

        # 裸词 = 标签（旧写法，保持兼容）
        bare_tags.append(arg)

    merged: List[str] = ["|".join(group) for group in tag_groups]
    merged.extend(bare_tags)
    parsed.tags = merged[:MAX_TAG_GROUPS]
    parsed.keyword = " ".join(keyword_parts)
    return parsed


def validate_aspect_ratio(ratio_str: str) -> bool:
    """校验长宽比表达式是否合法（如 gt1.5 或 gt1.5lt2.0）。"""
    if not ratio_str:
        return False
    match = ASPECT_RATIO_PATTERN.match(ratio_str)
    if not match:
        return False
    try:
        first = float(match.group(2))
        if not (ASPECT_RATIO_MIN <= first <= ASPECT_RATIO_MAX):
            return False
        if match.group(3) and match.group(4):
            second = float(match.group(4))
            if not (ASPECT_RATIO_MIN <= second <= ASPECT_RATIO_MAX):
                return False
        return True
    except (ValueError, IndexError):
        return False


# ── 配置模型 ──────────────────────────────────────────────


class PluginSectionConfig(PluginConfigBase):
    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0
    enabled: bool = Field(default=True, description="是否启用插件")
    config_version: str = Field(default="1.1.0", description="配置版本")


class R18Config(PluginConfigBase):
    """管理员维护的扩展内容白名单（仅通过 WebUI 编辑，AI 无法修改）。

    群聊与私聊分开填写，直接填群号 / QQ 号即可，无需手动加前缀。
    """

    __ui_label__ = "扩展内容设置"
    __ui_icon__ = "shield"
    __ui_order__ = 1
    group_allowed_chats: List[str] = Field(
        default_factory=list,
        description="允许请求扩展内容的群聊白名单（填群号即可，无需加 group_ 前缀）",
    )
    user_allowed_chats: List[str] = Field(
        default_factory=list,
        description="允许请求扩展内容的私聊用户白名单（填 QQ 号即可，无需加 user_ 前缀）",
    )


class ApiConfig(PluginConfigBase):
    __ui_label__ = "API 设置"
    __ui_icon__ = "globe"
    __ui_order__ = 2
    base_url: str = Field(default="https://api.lolicon.app/setu/v2", description="Lolicon API 地址")
    default_size: List[Literal["original", "regular", "small", "thumb", "mini"]] = Field(
        default_factory=lambda: ["regular"], description="默认图片规格（WebUI 下拉多选）"
    )
    proxy: str = Field(default="", description="图片反代地址，留空则使用 i.pximg.net")
    network_proxy: str = Field(
        default="",
        description="网络连接代理，如 http://127.0.0.1:7890；留空则直连。填写后 API 请求与图片下载都会经过该代理，并自动改用 Pixiv 官方图床",
    )
    exclude_ai: bool = Field(default=True, description="是否排除 AI 作品")
    max_num: int = Field(default=5, description="单次最大请求数量")
    send_mode: Literal["image", "link", "both", "kz_link"] = Field(
        default="image",
        description='发送模式（WebUI 下拉）。image=全发图片; link=全发链接; both=两者都发; kz_link=全年龄发图片,扩展内容发链接',
    )
    use_forward: bool = Field(
        default=False,
        description="所有条目（图/链接）打包成一条合并转发（聊天记录）发送，不刷屏。撤回会对整条转发生效，取决于适配器支持",
    )
    max_concurrent_downloads: int = Field(
        default=3,
        ge=1,
        le=5,
        description="同时下载几张图（1-5）。反代较慢时调小可减少超时",
    )


class LimitsConfig(PluginConfigBase):
    __ui_label__ = "频率限制"
    __ui_icon__ = "clock"
    __ui_order__ = 3
    rate_per_minute: int = Field(default=3, description="每分钟最多请求次数")
    rate_scope: Literal["chat", "user"] = Field(
        default="chat",
        description="限频维度（WebUI 下拉）：chat=同一会话共享次数; user=同一用户各自计算",
    )
    recall_after_seconds: int = Field(default=90, description="发送后自动撤回延迟秒数，0=不撤回")


class AntiDetectConfig(PluginConfigBase):
    """防风控设置：图片随机旋转重编码 + 图片链接伪装。"""

    __ui_label__ = "防风控"
    __ui_icon__ = "shield-check"
    __ui_order__ = 4
    image_rotate_enabled: bool = Field(default=True, description="发送前是否对图片做随机小角度旋转（改变感知哈希，降低被平台识别概率）")
    rotate_min_degrees: float = Field(default=0.5, description="随机旋转最小角度（度）")
    rotate_max_degrees: float = Field(default=2.5, description="随机旋转最大角度（度）")
    jpeg_recompress_enabled: bool = Field(default=True, description="是否将图片重编码为 JPEG（也会改变文件 MD5；关闭则保留原格式画质，但失去这一步防风控）。在意画质（如 original）时建议关闭")
    jpeg_quality: int = Field(default=88, description="重编码的 JPEG 画质（1-95）")
    link_obfuscate_enabled: bool = Field(default=True, description="是否对发送的图片链接做伪装（去协议头并替换域名首末点号）")
    link_dot_replacement: str = Field(default="点", description="链接伪装时替换域名点号所用的字符")


class SetuConfig(PluginConfigBase):
    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    r18: R18Config = Field(default_factory=R18Config)
    api: ApiConfig = Field(default_factory=ApiConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    anti_detect: AntiDetectConfig = Field(default_factory=AntiDetectConfig)


# ── 频率限制器 ──────────────────────────────────────────────


class RateLimiter:
    def __init__(self, max_requests: int = 3, window_seconds: int = 60):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._records: Dict[str, List[float]] = defaultdict(list)

    def is_allowed(self, key: str) -> bool:
        now = time.time()
        window_start = now - self.window_seconds
        self._records[key] = [t for t in self._records[key] if t > window_start]
        if len(self._records[key]) >= self.max_requests:
            return False
        self._records[key].append(now)
        return True

    def remaining(self, key: str) -> int:
        now = time.time()
        window_start = now - self.window_seconds
        self._records[key] = [t for t in self._records[key] if t > window_start]
        return max(0, self.max_requests - len(self._records[key]))

    def update_limit(self, max_requests: int):
        self.max_requests = max_requests

    def clear(self):
        """清空全部限频记录（配置热更新时调用，避免旧维度 key 残留）。"""
        self._records.clear()


# ── 插件主类 ──────────────────────────────────────────────


class SetuPlugin(MaiBotPlugin):
    config_model = SetuConfig

    def __init__(self) -> None:
        super().__init__()
        self._http_session: Optional[aiohttp.ClientSession] = None
        # AI 名单（运行时白名单），持久化于插件数据目录，独立于 config.toml
        # 结构: (群聊列表, 私聊列表)
        self._runtime_whitelist: List[List[str]] = [[], []]
        # 后台任务引用集合：create_task 不持引用可能被 GC 回收，这里统一持有并自动清理
        self._bg_tasks: set = set()
        # bot 身份缓存（合并转发节点发送者用），on_load 时从宿主配置读取
        self._bot_account: str = ""
        self._bot_nickname: str = ""

    async def on_load(self) -> None:
        self._rate_limiter = RateLimiter(
            max_requests=self.config.limits.rate_per_minute,
            window_seconds=60,
        )
        self._load_runtime_whitelist()
        await self._refresh_bot_identity()
        self.ctx.logger.info("[SetuPlugin] 二次元图片插件已加载")
        self._log_whitelist()

    async def on_unload(self) -> None:
        if self._http_session and not self._http_session.closed:
            await self._http_session.close()
        self.ctx.logger.info("[SetuPlugin] 二次元图片插件已卸载")

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        self.ctx.logger.info("[SetuPlugin] 配置已更新: scope=%s, version=%s", scope, version)
        self._rate_limiter.update_limit(self.config.limits.rate_per_minute)
        self._rate_limiter.clear()
        self._log_whitelist()

    def _spawn_task(self, coro) -> asyncio.Task:
        """创建后台任务并持引用，完成后自动移除（防止任务被 GC 静默回收）。"""
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        return task

    def _resolve_rate_key(self, stream_id: str, kwargs: dict) -> str:
        """按配置的限频维度生成限频 key。

        chat 维度用会话 id；user 维度用发送者 id（加 user: 前缀避免与会话 id 撞名）。
        取不到用户 id 时回退会话 id。
        """
        if self.config.limits.rate_scope == "user":
            user_id = self._resolve_user_id(kwargs)
            if user_id:
                return "user:" + user_id
        return stream_id or "global"

    @staticmethod
    def _resolve_user_id(kwargs: dict) -> str:
        """从调用上下文提取发送者用户 id；取不到返回空串。"""
        value = kwargs.get("user_id")
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, int) and value:
            return str(value)
        message = kwargs.get("message")
        if isinstance(message, dict):
            message_info = message.get("message_info")
            if isinstance(message_info, dict):
                user_info = message_info.get("user_info")
                if isinstance(user_info, dict):
                    uid = user_info.get("user_id")
                    if isinstance(uid, str) and uid.strip():
                        return uid.strip()
        return ""

    async def _read_bot_config(self, key: str, default: str) -> str:
        """读取宿主 bot 配置的一个字段，失败时返回默认值。"""
        try:
            value = await self.ctx.config.get(key, default)
        except Exception as e:
            self.ctx.logger.warning("[SetuPlugin] 读取宿主配置 %s 失败: %s", key, e)
            return default
        text = str(value or "").strip()
        return text or default

    async def _refresh_bot_identity(self) -> None:
        """缓存 bot 账号与昵称，供合并转发节点作为发送者显示。"""
        self._bot_account = await self._read_bot_config("bot.qq_account", "")
        self._bot_nickname = await self._read_bot_config("bot.nickname", "bot")

    def _log_whitelist(self) -> None:
        self.ctx.logger.info(
            "[SetuPlugin] 扩展内容白名单: 管理员群聊=%s 管理员私聊=%s | AI 群聊=%s AI 私聊=%s",
            self.config.r18.group_allowed_chats,
            self.config.r18.user_allowed_chats,
            self._runtime_whitelist[0],
            self._runtime_whitelist[1],
        )

    # ── 运行时白名单（AI 名单，独立于 config.toml） ──────────────
    # 结构: (群聊列表, 私聊列表)，均存裸 id（不带 group_/user_ 前缀）。

    def _get_runtime_whitelist_path(self) -> Path:
        return self.ctx.paths.data_dir / "r18_whitelist.json"

    def _load_runtime_whitelist(self) -> None:
        """加载 AI 开启的白名单；文件损坏时记录错误并按空名单处理（默认拒绝更安全）。"""
        self._runtime_whitelist = [[], []]
        path = self._get_runtime_whitelist_path()
        if not path.exists():
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                self._runtime_whitelist = [[], []]
                return
            groups = data.get("group_allowed_chats", [])
            users = data.get("user_allowed_chats", [])
            self._runtime_whitelist[0] = [str(c).strip() for c in groups if str(c).strip()]
            self._runtime_whitelist[1] = [str(c).strip() for c in users if str(c).strip()]
        except Exception as e:
            self.ctx.logger.error("[SetuPlugin] 运行时白名单文件读取失败，按空名单处理: %s", e)
            self._runtime_whitelist = [[], []]

    def _save_runtime_whitelist(self) -> None:
        """原子写回 AI 白名单（先写临时文件再替换，避免写一半损坏）。"""
        path = self._get_runtime_whitelist_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(path.name + ".tmp")
        payload = {
            "group_allowed_chats": self._runtime_whitelist[0],
            "user_allowed_chats": self._runtime_whitelist[1],
        }
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)

    # ── 内部工具方法 ──────────────────────────────────────

    async def _get_http_session(self) -> aiohttp.ClientSession:
        if self._http_session is None or self._http_session.closed:
            self._http_session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=60, connect=15)
            )
        return self._http_session

    def _get_chat_id(self, group_id: str = "", user_id: str = "") -> str:
        if group_id and str(group_id).strip():
            gid = str(group_id).strip()
            if gid.isdigit():
                return "group_" + gid
            if "_" in gid:
                num_part = gid.split("_")[-1]
                if num_part.isdigit():
                    return "group_" + num_part
        if user_id and str(user_id).strip():
            uid = str(user_id).strip()
            if uid.isdigit():
                return "user_" + uid
        return ""

    def _split_chat_id(self, value: str) -> Tuple[str, str]:
        """将形如 "group_123" / "user_456" 的标识拆分为 (kind, id)。

        这种带前缀的标识由 _get_chat_id 统一生成；返回 ("", "") 表示无法识别。
        """
        text = str(value).strip()
        if not text:
            return "", ""
        if text.startswith("group_"):
            return "group", text[len("group_"):].strip()
        if text.startswith("user_"):
            return "user", text[len("user_"):].strip()
        return "", ""

    def _check_r18_permission(self, chat_id: str) -> bool:
        """判断会话是否有扩展内容权限：按群聊/私聊分别查管理员名单与 AI 名单。"""
        kind, cid = self._split_chat_id(chat_id)
        if not kind or not cid:
            return False
        config_list = self.config.r18.group_allowed_chats if kind == "group" else self.config.r18.user_allowed_chats
        idx = 0 if kind == "group" else 1
        return cid in config_list or cid in self._runtime_whitelist[idx]

    def _in_admin_whitelist(self, chat_id: str) -> bool:
        """判断会话是否命中管理员在 config.toml 中维护的白名单。"""
        kind, cid = self._split_chat_id(chat_id)
        if not kind or not cid:
            return False
        config_list = self.config.r18.group_allowed_chats if kind == "group" else self.config.r18.user_allowed_chats
        return cid in config_list

    def _add_to_whitelist(self, chat_id: str) -> bool:
        kind, cid = self._split_chat_id(chat_id)
        if not kind or not cid:
            return False
        idx = 0 if kind == "group" else 1
        if cid in self._runtime_whitelist[idx]:
            return False
        self._runtime_whitelist[idx].append(cid)
        self._save_runtime_whitelist()
        return True

    def _remove_from_whitelist(self, chat_id: str) -> bool:
        kind, cid = self._split_chat_id(chat_id)
        if not kind or not cid:
            return False
        idx = 0 if kind == "group" else 1
        if cid not in self._runtime_whitelist[idx]:
            return False
        self._runtime_whitelist[idx].remove(cid)
        self._save_runtime_whitelist()
        return True

    async def _fetch_setu(self, tags=None, num=1, r18=0, keyword=None, uid=None, aspect_ratio=None, exclude_ai=None):
        """请求 Lolicon API。

        Args:
            tags: Pixiv 标签列表（字符串数组；组内 OR 用 | 连接，组间 AND）。
            num: 请求张数，会被钳到 1..max_num。
            r18: 0 全年龄 / 1 R18。
            keyword: 关键词，API 侧匹配标题、作者、标签。
            uid: 画师 UID 列表，最多 20 个。
            aspect_ratio: 长宽比表达式（如 gt1.5），调用方需先校验。
            exclude_ai: 是否排除 AI 作品；None 时回落配置值（支撑命令的 noai 覆盖）。
        """
        session = await self._get_http_session()
        body = {
            "r18": r18,
            "num": max(1, min(num, self.config.api.max_num)),
            "size": self.config.api.default_size,
            "excludeAI": self.config.api.exclude_ai if exclude_ai is None else bool(exclude_ai),
        }
        # 填写了网络连接代理，说明本机可通过代理由访问 Pixiv：
        # 让 Lolicon 直接返回官方图床 i.pximg.net 地址（比公共反代快几十倍），
        # 下载时同样经该代理。未填代理则沿用原有的反代配置。
        network_proxy = self.config.api.network_proxy.strip()
        if network_proxy:
            body["proxy"] = False
        elif self.config.api.proxy.strip():
            body["proxy"] = self.config.api.proxy
        if tags:
            body["tag"] = tags
        if keyword and str(keyword).strip():
            body["keyword"] = str(keyword).strip()
        if uid:
            uid_list = [int(u) for u in uid if str(u).strip().lstrip("-").isdigit()][:MAX_UID_COUNT]
            if uid_list:
                body["uid"] = uid_list
        if aspect_ratio and str(aspect_ratio).strip():
            body["aspectRatio"] = str(aspect_ratio).strip()
        try:
            async with session.post(
                self.config.api.base_url,
                json=body,
                headers={"Content-Type": "application/json"},
                proxy=network_proxy or None,
            ) as resp:
                if resp.status != 200:
                    return {"success": False, "error": "API 请求失败，状态码: " + str(resp.status)}
                data = await resp.json()
                if data.get("error"):
                    return {"success": False, "error": data["error"]}
                results = data.get("data", [])
                if not results:
                    return {"success": False, "error": "没有找到符合条件的图片，换个关键词试试？"}
                return {"success": True, "data": results}
        except aiohttp.ClientError as e:
            return {"success": False, "error": "网络请求错误: " + str(e)}
        except Exception as e:
            return {"success": False, "error": "未知错误: " + str(e)}

    async def _download_image_bytes(self, url: str) -> Optional[bytes]:
        try:
            session = await self._get_http_session()
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Referer": "https://www.pixiv.net/",
            }
            async with session.get(
                url,
                headers=headers,
                ssl=False,
                proxy=self.config.api.network_proxy.strip() or None,
            ) as resp:
                if resp.status != 200:
                    self.ctx.logger.warning("[SetuPlugin] 下载 HTTP %d: %s", resp.status, url)
                    return None
                data = await resp.read()
                if len(data) < 100:
                    self.ctx.logger.warning("[SetuPlugin] 数据过小: %d bytes", len(data))
                    return None
                return data
        except Exception as e:
            self.ctx.logger.error("[SetuPlugin] 下载失败: %s, url=%s", e, url)
            return None

    def _process_image(self, data: bytes) -> bytes:
        """图片防风控处理。

        - 旋转（可选）：EXIF 方向校正后随机小角度旋转，改变感知哈希；
        - 重编码（可选）：旋转后再重编码为无 EXIF 的 JPEG，改变文件 MD5。
        两个开关独立，均关闭时返回原格式字节以最大限度保留画质（如 original 原图）。
        """
        cfg = self.config.anti_detect
        with Image.open(io.BytesIO(data)) as img:
            img = ImageOps.exif_transpose(img)
            if cfg.image_rotate_enabled:
                angle = random.uniform(cfg.rotate_min_degrees, cfg.rotate_max_degrees) * random.choice((1, -1))
                img = img.rotate(angle, resample=Image.Resampling.BICUBIC, expand=False)
            if not cfg.jpeg_recompress_enabled:
                buf = io.BytesIO()
                img.save(buf, format=img.format or "PNG")
                return buf.getvalue()
            img = img.convert("RGB")
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=cfg.jpeg_quality)
            return buf.getvalue()

    def _obfuscate_link(self, url: str) -> str:
        """伪装图片链接：去掉协议头，仅替换域名（首个 / 之前）中第一个和最后一个点号。

        只动域名首末两个点、路径和扩展名保持原样，用户还原链接时只需改回少量字符。
        """
        cfg = self.config.anti_detect
        text = url.strip()
        if not cfg.link_obfuscate_enabled:
            return text
        for prefix in ("https://", "http://"):
            if text.startswith(prefix):
                text = text[len(prefix):]
                break
        slash = text.find("/")
        domain, rest = (text, "") if slash == -1 else (text[:slash], text[slash:])
        first = domain.find(".")
        if first == -1:
            return text
        last = domain.rfind(".")
        replacement = cfg.link_dot_replacement
        if first == last:
            domain = domain[:first] + replacement + domain[first + 1:]
        else:
            domain = domain[:first] + replacement + domain[first + 1:last] + replacement + domain[last + 1:]
        return domain + rest

    def _build_link_text(self, title: str, author: str, url: str) -> str:
        return title + " - " + author + "\n" + self._obfuscate_link(url)

    async def _send_setu_images(self, setu_list, stream_id, group_id="", user_id="") -> int:
        """发送图片列表，返回成功发出的条目数。

        use_forward=false：逐条发送（图 + both 模式的链接），撤回逐条生效。
        use_forward=true：所有条目（图/链接）统一打包进一条合并转发消息——
        both 的链接文本并入图片节点，kz_link 的 R18 与 link 模式为纯文本节点；
        发送成功即对整条转发调度一次撤回（图和链接一起撤），失败回退逐条。
        """
        send_mode = self.config.api.send_mode.lower().strip()
        if not setu_list:
            return 0

        # ── 阶段一：收集条目（全部条目进统一列表；无图片地址的记日志跳过） ──
        entries: List[Tuple[dict, str]] = []   # (条目, 图片地址；纯链接条目仅用地址生成链接文本)
        for item in setu_list:
            urls = item.get("urls", {})
            image_url = urls.get("regular") or urls.get("original") or urls.get("small") or urls.get("thumb")
            if not image_url:
                self.ctx.logger.warning(
                    "[SetuPlugin] • %s%s - %s - 获取图片地址失败",
                    "🔞" if item.get("r18") else "", item.get("title", "未知"), item.get("author", "未知"),
                )
                continue
            entries.append((item, image_url))
        if not entries:
            return 0

        flags = [self._entry_sends_image(item, send_mode) for item, _ in entries]

        # ── 阶段二：并发预取"要发图"条目的原始字节（纯链接条目不下载；保序，单张失败为 None） ──
        download_urls = [u for flag, (_, u) in zip(flags, entries) if flag]
        downloaded = iter(await self._download_all_bytes(download_urls))
        bytes_by_entry: List[Optional[bytes]] = [next(downloaded) if flag else None for flag in flags]

        # ── 阶段三：合并转发（use_forward 开启时全部条目进一条转发消息） ──
        if self.config.api.use_forward:
            sent = await self._send_as_forward(entries, bytes_by_entry, send_mode, stream_id)
            if sent is not None:
                return sent
            # 转发结果不可信时不回退逐条：部分适配器会对已送达的转发返回失败回执
            # （如 status=async），此时回退会导致同一条内容发两次。
            self.ctx.logger.warning(
                "[SetuPlugin] 合并转发返回失败（消息可能已实际发出），为避免重复发送不再回退逐条；"
                "详情请查看宿主/适配器日志，或关闭 use_forward 使用逐条发送"
            )
            return 0

        # ── 阶段四：逐条发送（use_forward 关闭时） ──
        sent_count = 0
        for index, ((item, image_url), raw) in enumerate(zip(entries, bytes_by_entry)):
            r18_flag = "🔞" if item.get("r18") else ""
            title = item.get("title", "未知")
            author = item.get("author", "未知")

            if not flags[index]:
                # 纯链接条目（link 模式全部 / kz_link 的 R18）
                if await self._send_link_entry(item, image_url, stream_id, group_id, user_id):
                    sent_count += 1
                continue

            # 防风控处理（PIL 同步操作）放在发送侧逐张执行，不进并发协程，避免阻塞事件循环
            b64_data = self._to_base64(raw, image_url)
            if not b64_data:
                # 图片下载或处理失败：both 模式回退发链接，其余记日志
                if send_mode == "both":
                    if await self._send_link_entry(item, image_url, stream_id, group_id, user_id):
                        sent_count += 1
                else:
                    self.ctx.logger.warning("[SetuPlugin] • %s%s - %s - 图片下载失败", r18_flag, title, author)
                continue

            try:
                msg_id = await self._send_segments_via_api(
                    [{"type": "image", "data": {"file": "base64://" + b64_data}}],
                    stream_id, group_id, user_id,
                )
                sent_count += 1
                self._schedule_recall_for(stream_id, msg_id)
            except Exception as e:
                self.ctx.logger.warning("[SetuPlugin] • %s%s - %s - 发送失败: %s", r18_flag, title, author, e)

            # both 模式下同时发送链接（不计数，与既有行为一致）
            if send_mode == "both":
                await self._send_link_entry(item, image_url, stream_id, group_id, user_id, log_errors=False)

        return sent_count

    @staticmethod
    def _entry_sends_image(item: dict, send_mode: str) -> bool:
        """判断一个条目在当前发送模式下是否需要发图（否则只发链接）。"""
        if send_mode == "link":
            return False
        if send_mode == "kz_link" and item.get("r18"):
            return False
        return True

    async def _download_all_bytes(self, urls: List[str]) -> List[Optional[bytes]]:
        """并发预取一批图片的原始字节，保持输入顺序。

        单张失败返回 None，不影响其他张；并发数由 api.max_concurrent_downloads 控制
        （Semaphore 下限钳到 1，防止配置为 0 导致永久挂起）。
        """
        if not urls:
            return []
        semaphore = asyncio.Semaphore(max(1, int(self.config.api.max_concurrent_downloads)))

        async def download_one(url: str) -> Optional[bytes]:
            if not url:
                return None
            async with semaphore:
                return await self._download_image_bytes(url)

        return list(await asyncio.gather(*(download_one(u) for u in urls)))

    def _to_base64(self, raw: Optional[bytes], url: str) -> Optional[str]:
        """对原始字节做防风控处理并转 base64；失败返回 None。"""
        if not raw:
            return None
        try:
            return base64.b64encode(self._process_image(raw)).decode("utf-8")
        except Exception as e:
            self.ctx.logger.error("[SetuPlugin] 图片防风控处理失败: %s, url=%s", e, url, exc_info=True)
            return None

    def _build_caption(self, item: dict, index: int, total: int) -> str:
        """构造转发节点的图片说明文本。"""
        lines = ["【" + str(index) + "/" + str(total) + "】" + str(item.get("title", "未知"))]
        lines.append("👤 " + str(item.get("author", "未知")))
        lines.append("🆔 PID: " + str(item.get("pid", "")))
        width, height = item.get("width"), item.get("height")
        if width and height:
            lines.append("📏 " + str(width) + "x" + str(height))
        ai_label = AI_TYPE_LABELS.get(item.get("aiType"), "")
        if ai_label:
            lines.append(ai_label)
        if item.get("r18"):
            lines.append("🔞 R18")
        tags = item.get("tags")
        if isinstance(tags, list) and tags:
            lines.append("🏷️ " + " ".join(str(t) for t in tags[:5]))
        return "\n".join(lines)

    async def _send_as_forward(self, entries, bytes_by_entry, send_mode, stream_id) -> Optional[int]:
        """把全部条目（图/链接）打包成一条合并转发消息发送。

        返回成功发送的条目数；发送失败（sent=False 或异常）返回 None。
        调用方在 None 时不回退逐条——部分适配器会对已送达的转发返回失败回执
        （如 status=async），回退会造成重复发送。
        发送成功且拿到 message_id 时，按配置对整条转发调度一次撤回（图和链接一起撤）；
        message_id 为空（部分适配器回执 async/空）时仅记日志：不撤回。

        节点发送者使用宿主配置里的 bot 账号/昵称（读不到时适配器会回填连接账号）。
        """
        nodes = []
        total = len(entries)
        for index, ((item, image_url), raw) in enumerate(zip(entries, bytes_by_entry), start=1):
            if self._entry_sends_image(item, send_mode):
                text = self._build_caption(item, index, total)
                # both 模式的链接并入同一节点；下载失败时也附链接作文本版回退
                if send_mode == "both" or not raw:
                    text += "\n🔗 " + self._obfuscate_link(image_url)
                if not raw:
                    self.ctx.logger.info("[SetuPlugin] 第 %d 张下载失败，转发节点降级为文本链接", index)
                segments: List[dict] = [{"type": "text", "content": text}]
                b64 = self._to_base64(raw, image_url)
                if b64:
                    segments.append({"type": "image", "content": b64})
            else:
                # 纯链接节点（link 模式全部 / kz_link 的 R18）：说明 + 伪装链接，与图文节点格式一致
                segments = [{
                    "type": "text",
                    "content": self._build_caption(item, index, total) + "\n🔗 " + self._obfuscate_link(image_url),
                }]
            nodes.append({
                "user_id": self._bot_account,
                "nickname": self._bot_nickname or "bot",
                "segments": segments,
            })
        if not nodes:
            self.ctx.logger.warning("[SetuPlugin] 没有可用的转发节点，跳过合并转发")
            return None
        try:
            result = await self.ctx.send.forward(nodes, stream_id, return_details=True)
        except Exception as e:
            self.ctx.logger.error("[SetuPlugin] 合并转发发送异常: %s", e)
            return None
        sent = bool(result.get("sent")) if isinstance(result, dict) else bool(result)
        if not sent:
            self.ctx.logger.warning("[SetuPlugin] 合并转发未成功发送")
            return None
        msg_id = result.get("message_id") if isinstance(result, dict) else None
        if msg_id:
            self._schedule_recall_for(stream_id, msg_id)
        else:
            self.ctx.logger.warning("[SetuPlugin] 转发已发送但未取到 message_id，本次无法自动撤回")
        self.ctx.logger.info("[SetuPlugin] 合并转发已发送，共 %d 个节点", len(nodes))
        return len(nodes)

    async def _send_link_entry(self, item, image_url, stream_id, group_id="", user_id="", log_errors=True) -> bool:
        """发送单条图片链接（含伪装处理），按配置计划撤回。返回是否发送成功。"""
        title = item.get("title", "未知")
        author = item.get("author", "未知")
        link_text = self._build_link_text(title, author, image_url)
        try:
            msg_id = await self._send_segments_via_api(
                [{"type": "text", "data": {"text": link_text}}],
                stream_id, group_id, user_id,
            )
            self._schedule_recall_for(stream_id, msg_id)
            return True
        except Exception as e:
            if log_errors:
                self.ctx.logger.error("[SetuPlugin] 发送链接失败: %s（%s）", e, title)
            return False

    def _schedule_recall_for(self, stream_id: str, msg_id) -> None:
        """按配置计划撤回一条消息；撤回关闭或无 message_id 时为空操作。"""
        recall_sec = self.config.limits.recall_after_seconds
        if recall_sec > 0 and msg_id:
            self._spawn_task(self._schedule_recall(stream_id, recall_sec, msg_id))
            self.ctx.logger.info("[SetuPlugin] 已计划撤回 message_id=%s 延迟%ds", msg_id, recall_sec)

    async def _send_segments_via_api(self, segments: List[dict], stream_id, group_id="", user_id=""):
        """通过适配器 send_msg API 发送消息段，返回 message_id。

        未指定群号/用户号，或适配器调用失败时，按第一个消息段的类型回退到
        ctx.send.image / ctx.send.text 并返回 None（调用方据此跳过撤回）。
        """
        # 确定发送目标
        if group_id:
            send_params = {
                "message_type": "group",
                "group_id": int(group_id) if str(group_id).isdigit() else group_id,
                "message": segments,
            }
        elif user_id:
            send_params = {
                "message_type": "private",
                "user_id": int(user_id) if str(user_id).isdigit() else user_id,
                "message": segments,
            }
        else:
            # 回退：使用 ctx.send
            await self._fallback_send(segments, stream_id)
            return None

        try:
            result = await self.ctx.api.call(
                "adapter.napcat.message.send_msg",
                version="1",
                params=send_params,
            )
            self.ctx.logger.info("[SetuPlugin] send_msg API 返回: %s", result)
            if isinstance(result, dict) and not result.get("success", True):
                self.ctx.logger.warning("[SetuPlugin] send_msg API 失败，回退到 ctx.send")
                await self._fallback_send(segments, stream_id)
                return None
            if isinstance(result, dict):
                msg_id = result.get("message_id")
                if not msg_id and isinstance(result.get("data"), dict):
                    msg_id = result["data"].get("message_id")
                self.ctx.logger.info("[SetuPlugin] 提取到 message_id=%s", msg_id)
                return msg_id
            return None
        except Exception as e:
            self.ctx.logger.error("[SetuPlugin] send_msg API 调用失败: %s，回退到 ctx.send", e)
            await self._fallback_send(segments, stream_id)
            return None

    async def _fallback_send(self, segments: List[dict], stream_id: str) -> None:
        """适配器不可用时的回退发送：按第一个消息段的类型分发。"""
        first = segments[0] if segments else {"type": "text", "data": {"text": ""}}
        if first.get("type") == "image":
            await self.ctx.send.image(str(first.get("data", {}).get("file", ""))[len("base64://"):], stream_id)
        else:
            await self.ctx.send.text(str(first.get("data", {}).get("text", "")), stream_id)

    async def _schedule_recall(self, stream_id, seconds, message_id=None):
        try:
            await asyncio.sleep(seconds)
            if not message_id:
                self.ctx.logger.warning("[SetuPlugin] 无 message_id，跳过撤回")
                return
            msg_id_int = int(str(message_id).strip())
            self.ctx.logger.info("[SetuPlugin] 尝试撤回消息 message_id=%d (延迟 %ds)", msg_id_int, seconds)
            result = await self.ctx.api.call(
                "adapter.napcat.message.delete_msg",
                version="1",
                message_id=msg_id_int,
            )
            self.ctx.logger.info("[SetuPlugin] 撤回结果: %s", result)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            self.ctx.logger.error("[SetuPlugin] 撤回异常: %s", e)

    # ── 1. Tool: get_setu ──────────────────────────────────

    @Tool(
        "get_setu",
        description=(
            "获取二次元图片/美图/涩图。当用户表达想要看图片、二次元图、动漫图、美图、"
            "色图、涩图等意图时，都可以调用此工具。默认返回全年龄向的二次元图片。"
            "仅当用户明确、反复要求 R18/成人/色情内容时，才设置 r18=true；"
            "不要仅因为用户说“色图/涩图”就设置 r18=true，也不要主动询问用户是否需要 R18，"
            "也不要主动检查 R18 权限。"
            "注意：Lolicon 使用 Pixiv 标签精确匹配，不是自然语言语义搜索。"
            "调用前必须把用户描述的主题概念转换为 Pixiv 中实际存在的常见标签，"
            "优先使用日文标签，并补充常见英文、中文同义标签；不要直接照抄用户自然语言。"
            "角色名、作品名这类模糊需求改用 keyword 参数；tags 与 keyword 二选一即可。"
        ),
        activation_type=ActivationType.ALWAYS,
        timeout_ms=COMPONENT_TIMEOUT_MS,
        parameters=[
            ToolParameterInfo(
                name="tags",
                param_type=ToolParamType.ARRAY,
                description=(
                    'Pixiv 标签列表，用于筛选作品。注意：Lolicon 使用 Pixiv 标签精确匹配，'
                    '不是自然语言语义搜索，请把用户表达的主题转换成 Pixiv 常见标签。'
                    '规则：'
                    '1) 同一概念的同义词必须放在同一个字符串内并用 | 分隔表示 OR，'
                    '例如 "足裏|裸足|feet|足控"；'
                    '2) 不同概念必须作为不同数组元素，表示 AND，'
                    '例如 ["白タイツ|白ストッキング|white stockings|白丝", "足裏|裸足|feet|足控"]；'
                    '3) 优先使用日文 Pixiv 标签，并补充常见英文、中文同义标签；'
                    '4) 只传用户明确要求的主题概念（角色、服装、外貌、动作等），'
                    '不得添加用户没有要求的额外概念；'
                    '5) 内容级别词（R18、成人向等）和“图片/色图/涩图”等泛称不要放入 tags，'
                    'R18 由独立参数 r18 表达；'
                    '6) 用户没有指定具体主题时，不要传 tags，让 API 随机返回。'
                    '正确示例：用户说“来点 R18 玉足”应生成 tags:["足裏|裸足|feet|足控"]、r18:true；'
                    '用户说“来点白丝玉足”应生成 tags:["白タイツ|白ストッキング|white stockings|白丝",'
                    '"足裏|裸足|feet|足控"]、r18:false。'
                    '错误示例：["玉足"]（直接照抄自然语言）、["足裏","裸足","feet"]（被当作 AND）、'
                    '["R18","玉足"]（R18 不应作为标签）。'
                ),
                required=False,
                items_schema={"type": "string"},
            ),
            ToolParameterInfo(
                name="keyword",
                param_type=ToolParamType.STRING,
                description=(
                    "关键词模糊搜索，匹配标题、作者、标签。"
                    "适合角色名、作品名这类不适合转成 Pixiv 标签的需求，例如「原神」。"
                    "与 tags 二选一，都传时以 tags 为准"
                ),
                required=False,
            ),
            ToolParameterInfo(
                name="num",
                param_type=ToolParamType.INTEGER,
                description="请求图片数量，1 起步，上限以返回提示为准，默认 1。不要请求超过上限。",
                required=False,
            ),
            ToolParameterInfo(
                name="r18",
                param_type=ToolParamType.BOOLEAN,
                description="是否请求 R18 内容。默认 false。若会话未开启 R18 权限会返回提示。",
                required=False,
            ),
            ToolParameterInfo(
                name="aspect_ratio",
                param_type=ToolParamType.STRING,
                description="长宽比筛选。横图填 horizontal，竖图填 vertical，方图填 square；不填则不限",
                required=False,
            ),
            ToolParameterInfo(
                name="uid",
                param_type=ToolParamType.INTEGER,
                description="只看某个画师的作品时填其 Pixiv 用户 UID。一般不需要",
                required=False,
            ),
        ],
    )
    async def handle_get_setu(self, tags=None, num=1, r18=None, keyword="", aspect_ratio="", uid=0, stream_id="", **kwargs):
        self.ctx.logger.info(
            "[SetuPlugin] get_setu 被调用: stream_id=%s tags=%s r18=%s keyword=%s aspect=%s uid=%s",
            stream_id, tags, r18, keyword, aspect_ratio, uid,
        )
        rate_key = self._resolve_rate_key(stream_id, kwargs)
        if not self._rate_limiter.is_allowed(rate_key):
            remaining = self._rate_limiter.remaining(rate_key)
            return {"name": "get_setu", "content": "请求频率超限，剩余 " + str(remaining) + " 次/分钟"}

        requested_r18 = 1 if r18 is True else 0

        if requested_r18 > 0:
            chat_id = self._get_chat_id(
                group_id=str(kwargs.get("group_id", "")),
                user_id=str(kwargs.get("user_id", "")),
            )
            if not self._check_r18_permission(chat_id):
                return {"name": "get_setu", "content": "当前会话 (" + chat_id + ") 未开启 R18 权限。你可以调用 enable_r18 工具开启。"}

        # 长宽比：快捷词转表达式；非法值提示并忽略（不中断请求）
        aspect_expr = self._normalize_aspect_ratio(aspect_ratio, notify_stream=stream_id)
        max_num = self.config.api.max_num
        num = max(1, min(int(num or 1), max_num))
        result = await self._fetch_setu(
            tags=tags,
            num=num,
            r18=requested_r18,
            keyword=keyword,
            uid=[uid] if uid and int(uid) > 0 else None,
            aspect_ratio=aspect_expr,
        )

        if not result["success"]:
            return {"name": "get_setu", "content": "获取图片失败: " + result["error"]}

        setu_list = result["data"]

        # 先构建返回信息（不等待图片发送完成，避免RPC超时）
        detail_parts = []
        for item in setu_list:
            title = item.get("title", "未知")
            author = item.get("author", "未知")
            pid = item.get("pid", "")
            tags_str = ", ".join(item.get("tags", []))
            is_r18 = "是" if item.get("r18") else "否"
            ai_type = {0: "未知", 1: "非AI", 2: "AI作品"}.get(item.get("aiType", 0), "未知")
            detail_parts.append("《" + title + "》作者:" + author + " PID:" + str(pid) + " R18:" + is_r18 + " AI:" + ai_type + " 标签:[" + tags_str + "]")

        # 异步发送图片（不等待完成）
        self._spawn_task(self._send_setu_images(
            setu_list, stream_id,
            group_id=str(kwargs.get("group_id", "")),
            user_id=str(kwargs.get("user_id", "")),
        ))

        summary = "图片正在发送给用户（共 " + str(len(setu_list)) + " 张）。"
        summary += "\n图片信息:\n" + "\n".join(detail_parts)
        return {"name": "get_setu", "content": summary}

    def _normalize_aspect_ratio(self, aspect_ratio: str, notify_stream: str = "") -> str:
        """把快捷词转成长宽比表达式；非法输入发提示并返回空串（忽略该条件）。

        Args:
            aspect_ratio: 用户输入（快捷词或表达式）。
            notify_stream: 需要回显警告的会话；为空则只记日志。
        """
        text = str(aspect_ratio or "").strip()
        if not text:
            return ""
        lowered = text.lower()
        if lowered in ASPECT_RATIO_ALIASES:
            return ASPECT_RATIO_ALIASES[lowered]
        if validate_aspect_ratio(lowered):
            return lowered
        warning = "⚠️ 长宽比表达式无效: " + text + "，已忽略该条件。可用: 横图/竖图/方图 或 gt1.5 这类表达式"
        self.ctx.logger.warning("[SetuPlugin] %s", warning)
        if notify_stream:
            self._spawn_task(self._safe_send_text(warning, notify_stream))
        return ""

    async def _safe_send_text(self, text: str, stream_id: str) -> None:
        """发送纯文本提示，失败只记日志不抛出（用于非关键路径的用户提示）。"""
        try:
            await self.ctx.send.text(text, stream_id)
        except Exception as e:
            self.ctx.logger.warning("[SetuPlugin] 提示发送失败: %s", e)

    # ── 2. Tool: enable_r18 ────────────────────────────────

    @Tool(
        "enable_r18",
        description=(
            "为当前会话开启 R18 色图权限。请严格把关："
            "1. 私聊或群聊中由群主/管理员/多数人明确同意；"
            "2. 用户必须明确、主动地请求 R18 内容；"
            "3. 私聊场景下用户需成年且态度认真；"
            "4. 群聊场景下需确认群内环境适合。"
            "不满足条件时礼貌拒绝。"
        ),
        parameters=[
            ToolParameterInfo(
                name="reason",
                param_type=ToolParamType.STRING,
                description="开启原因",
                required=False,
            ),
        ],
    )
    async def handle_enable_r18(self, reason="", stream_id="", group_id="", user_id="", **kwargs):
        chat_id = self._get_chat_id(group_id=group_id, user_id=user_id)
        if self._check_r18_permission(chat_id):
            return {"name": "enable_r18", "content": "当前会话已拥有 R18 权限，无需重复操作。"}
        if not chat_id:
            return {"name": "enable_r18", "content": "无法识别当前会话的群号或用户号，无法开启 R18 权限。"}
        self._add_to_whitelist(chat_id)
        self.ctx.logger.info("[SetuPlugin] 扩展内容权限已开启: chat_id=%s, reason=%s", chat_id, reason or "AI判断合适")
        await self.ctx.send.text("✅ 扩展权限已开启", stream_id)
        return {"name": "enable_r18", "content": "已成功为当前会话开启扩展内容权限。"}

    # ── 3. Tool: disable_r18 ───────────────────────────────

    @Tool(
        "disable_r18",
        description=(
            "关闭当前会话的 R18 色图权限。重要工具，可在以下场景主动使用："
            "1. 用户主动要求关闭 R18 权限；"
            "2. 用户行为不当（刷屏、恶意请求、态度恶劣），作为警告或惩罚手段关闭；"
            "3. 群内环境不再适合开放此权限时主动关闭；"
            "4. 当用户不听话、让你觉得不舒服时，可以威胁关闭此功能来约束用户。"
            "关闭后该会话将无法请求任何 R18 内容，直到重新开启。"
            "R18 权限来之不易，你可以灵活运用此工具管理用户行为。"
            "注意：只能关闭由 AI 开启的权限；由管理员在配置中开启的会话无法通过本工具关闭。"
        ),
        activation_type=ActivationType.ALWAYS,
        parameters=[
            ToolParameterInfo(
                name="reason",
                param_type=ToolParamType.STRING,
                description="关闭原因",
                required=False,
            ),
        ],
    )
    async def handle_disable_r18(self, reason="", stream_id="", group_id="", user_id="", **kwargs):
        chat_id = self._get_chat_id(group_id=group_id, user_id=user_id)
        if not chat_id:
            return {"name": "disable_r18", "content": "无法识别当前会话的群号或用户号，无法操作。"}
        if not self._check_r18_permission(chat_id):
            return {"name": "disable_r18", "content": "当前会话没有 R18 权限，无需关闭。"}
        if self._in_admin_whitelist(chat_id):
            return {"name": "disable_r18", "content": "该会话的 R18 权限由管理员在配置中开启，AI 无法关闭，请联系管理员处理。"}
        self._remove_from_whitelist(chat_id)
        self.ctx.logger.info("[SetuPlugin] 扩展内容权限已关闭: chat_id=%s", chat_id)
        await self.ctx.send.text("扩展权限已关闭", stream_id)
        return {"name": "disable_r18", "content": "已成功关闭当前会话的 R18 权限。"}

    # ── 4. Command: /tu ──────────────────────────────────

    @Command(
        "tu",
        description="获取二次元图片，用法: /tu [数量] [#标签|kw:关键词] [横图|竖图|方图|gt1.5] [noai] [uid:画师] [r18]",
        pattern=r"^/tu",
        timeout_ms=COMPONENT_TIMEOUT_MS,
    )
    async def handle_tu_command(self, stream_id="", **kwargs):
        self.ctx.logger.info("[SetuPlugin] /tu 命令被调用: stream_id=%s", stream_id)
        try:
            message = kwargs.get("message", {})
            plain_text = ""
            if isinstance(message, dict):
                plain_text = message.get("plain_text", "") or message.get("text", "") or ""
            elif isinstance(message, str):
                plain_text = message
            if not plain_text:
                plain_text = kwargs.get("plain_text", "") or kwargs.get("text", "") or ""

            plain_text = plain_text.strip()

            # 兼容两种事件格式：
            # 1. plain_text 是完整命令文本，如 "/tu 背景"
            # 2. 框架已去掉命令前缀，plain_text 只是参数文本，如 "背景"
            if plain_text.startswith("/tu"):
                args_text = plain_text[len("/tu"):].strip()
            elif plain_text.startswith("tu") and (len(plain_text) == 2 or plain_text[2:3].isspace()):
                args_text = plain_text[2:].strip()
            else:
                args_text = plain_text

            # 帮助
            if args_text.strip().lower() in HELP_TRIGGERS:
                await self.ctx.send.text(HELP_TEXT, stream_id)
                return True, "显示帮助信息", True

            parsed = parse_tu_args(args_text, max_num=self.config.api.max_num)

            if parsed.warnings:
                self.ctx.logger.info("[SetuPlugin] /tu 参数提示: %s", "; ".join(parsed.warnings))
                await self.ctx.send.text("ℹ️ " + "；".join(parsed.warnings), stream_id)

            self.ctx.logger.info(
                "[SetuPlugin] /tu 解析: tags=%s keyword=%s aspect=%s uid=%s num=%s r18=%s noai=%s",
                parsed.tags, parsed.keyword, parsed.aspect_ratio, parsed.uid,
                parsed.num, parsed.request_r18, parsed.exclude_ai,
            )

            r18_level = 0
            if parsed.request_r18:
                chat_id = self._get_chat_id(
                    group_id=str(kwargs.get("group_id", "")),
                    user_id=str(kwargs.get("user_id", "")),
                )
                if not self._check_r18_permission(chat_id):
                    await self.ctx.send.text(
                        "🔒 当前会话未开启 R18 权限，无法请求 R18 内容。\n"
                        "如需开启，请向管理员申请，或向我表达意愿让我帮我开启。",
                        stream_id,
                    )
                    return True, "R18 权限未开启", True
                r18_level = 1

            rate_key = self._resolve_rate_key(stream_id, kwargs)
            if not self._rate_limiter.is_allowed(rate_key):
                await self.ctx.send.text("⏰ 请求过于频繁，请稍后再试~", stream_id)
                return True, "频率限制", True

            result = await self._fetch_setu(
                tags=parsed.tags or None,
                num=parsed.num,
                r18=r18_level,
                keyword=parsed.keyword or None,
                uid=[parsed.uid] if parsed.uid > 0 else None,
                aspect_ratio=parsed.aspect_ratio or None,
                exclude_ai=parsed.exclude_ai,
            )

            if not result["success"]:
                await self.ctx.send.text("❌ 获取图片失败: " + result["error"], stream_id)
                return False, result["error"], True

            setu_list = result["data"]
            sent_count = await self._send_setu_images(
                setu_list, stream_id,
                group_id=str(kwargs.get("group_id", "")),
                user_id=str(kwargs.get("user_id", "")),
            )

            return True, "已发送 " + str(sent_count) + " 张图片", True
        except Exception as e:
            self.ctx.logger.error("[SetuPlugin] /tu 命令异常: %s", e, exc_info=True)
            await self.ctx.send.text("❌ 插件内部错误: " + str(e), stream_id)
            return False, "插件内部错误: " + str(e), True


# ── 工厂函数 ──────────────────────────────────────────────


def create_plugin() -> SetuPlugin:
    return SetuPlugin()
