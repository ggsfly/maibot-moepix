"""
二次元图片插件 - 基于 Lolicon API
为 AI 和用户提供二次元图片获取能力，支持个性化标签筛选和内容级别控制。

权限模型：
- 管理员名单：config.toml 的 r18.allowed_chats，仅通过 WebUI 维护，插件只读；
- AI 名单：LLM 通过 enable_r18/disable_r18 工具维护，持久化在插件数据目录的
  r18_whitelist.json 中，与 config.toml 完全隔离，生效名单为两者并集。
插件自身不写 config.toml（缺失字段由 SDK Runner 自动补齐）。
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import random
import time
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional

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


# ── 配置模型 ──────────────────────────────────────────────


class PluginSectionConfig(PluginConfigBase):
    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0
    enabled: bool = Field(default=True, description="是否启用插件")
    config_version: str = Field(default="1.0.0", description="配置版本")


class R18Config(PluginConfigBase):
    """管理员维护的扩展内容白名单（仅通过 WebUI 编辑，AI 无法修改）。"""

    __ui_label__ = "扩展内容设置"
    __ui_icon__ = "shield"
    __ui_order__ = 1
    allowed_chats: List[str] = Field(
        default_factory=list,
        description='管理员维护的扩展权限会话白名单，格式: "group_群号" 或 "user_QQ号"。',
    )


class ApiConfig(PluginConfigBase):
    __ui_label__ = "API 设置"
    __ui_icon__ = "globe"
    __ui_order__ = 2
    base_url: str = Field(default="https://api.lolicon.app/setu/v2", description="Lolicon API 地址")
    default_num: int = Field(default=1, description="默认请求图片数量")
    default_size: List[str] = Field(default_factory=lambda: ["regular"], description="默认图片规格")
    proxy: str = Field(default="", description="图片反代地址，留空则使用 i.pximg.net")
    exclude_ai: bool = Field(default=True, description="是否排除 AI 作品")
    max_num: int = Field(default=5, description="单次最大请求数量")
    send_mode: str = Field(default="image", description='发送模式: "image"(全发图片) 或 "link"(全发链接) 或 "both"(两者都发) 或 "kz_link"(全年龄发图片,扩展内容发链接)')


class LimitsConfig(PluginConfigBase):
    __ui_label__ = "频率限制"
    __ui_icon__ = "clock"
    __ui_order__ = 3
    rate_per_minute: int = Field(default=3, description="同一用户每分钟最多请求次数")
    recall_after_seconds: int = Field(default=90, description="发送后自动撤回延迟秒数，0=不撤回")


class AntiDetectConfig(PluginConfigBase):
    """防风控设置：图片随机旋转重编码 + 图片链接伪装。"""

    __ui_label__ = "防风控"
    __ui_icon__ = "shield-check"
    __ui_order__ = 4
    image_rotate_enabled: bool = Field(default=True, description="发送前是否对图片做随机小角度旋转并重编码（改变哈希，降低被平台识别概率）")
    rotate_min_degrees: float = Field(default=0.5, description="随机旋转最小角度（度）")
    rotate_max_degrees: float = Field(default=2.5, description="随机旋转最大角度（度）")
    jpeg_quality: int = Field(default=88, description="旋转后重编码的 JPEG 画质（1-95）")
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


# ── 插件主类 ──────────────────────────────────────────────


class SetuPlugin(MaiBotPlugin):
    config_model = SetuConfig

    def __init__(self) -> None:
        super().__init__()
        self._http_session: Optional[aiohttp.ClientSession] = None
        # AI 名单（运行时白名单），持久化于插件数据目录，独立于 config.toml
        self._runtime_whitelist: List[str] = []

    async def on_load(self) -> None:
        self._rate_limiter = RateLimiter(
            max_requests=self.config.limits.rate_per_minute,
            window_seconds=60,
        )
        self._load_runtime_whitelist()
        self.ctx.logger.info("[SetuPlugin] 二次元图片插件已加载")
        self._log_whitelist()

    async def on_unload(self) -> None:
        if self._http_session and not self._http_session.closed:
            await self._http_session.close()
        self.ctx.logger.info("[SetuPlugin] 二次元图片插件已卸载")

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        self.ctx.logger.info("[SetuPlugin] 配置已更新: scope=%s, version=%s", scope, version)
        self._rate_limiter.update_limit(self.config.limits.rate_per_minute)
        self._log_whitelist()

    def _log_whitelist(self) -> None:
        self.ctx.logger.info(
            "[SetuPlugin] 扩展内容白名单: 管理员配置=%s, AI 开启=%s",
            self.config.r18.allowed_chats,
            self._runtime_whitelist,
        )

    # ── 运行时白名单（AI 名单，独立于 config.toml） ──────────────

    def _get_runtime_whitelist_path(self) -> Path:
        return self.ctx.paths.data_dir / "r18_whitelist.json"

    def _load_runtime_whitelist(self) -> None:
        """加载 AI 开启的白名单；文件损坏时记录错误并按空名单处理（默认拒绝更安全）。"""
        path = self._get_runtime_whitelist_path()
        if not path.exists():
            self._runtime_whitelist = []
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            chats = data.get("allowed_chats", []) if isinstance(data, dict) else []
            self._runtime_whitelist = [str(c).strip() for c in chats if str(c).strip()]
        except Exception as e:
            self.ctx.logger.error("[SetuPlugin] 运行时白名单文件读取失败，按空名单处理: %s", e)
            self._runtime_whitelist = []

    def _save_runtime_whitelist(self) -> None:
        """原子写回 AI 白名单（先写临时文件再替换，避免写一半损坏）。"""
        path = self._get_runtime_whitelist_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(path.name + ".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump({"allowed_chats": self._runtime_whitelist}, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)

    # ── 内部工具方法 ──────────────────────────────────────

    async def _get_http_session(self) -> aiohttp.ClientSession:
        if self._http_session is None or self._http_session.closed:
            self._http_session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=60, connect=15)
            )
        return self._http_session

    def _get_chat_id(self, group_id: str = "", user_id: str = "", stream_id: str = "") -> str:
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

    def _check_r18_permission(self, chat_id: str) -> bool:
        return chat_id in self.config.r18.allowed_chats or chat_id in self._runtime_whitelist

    def _add_to_whitelist(self, chat_id: str) -> bool:
        if chat_id in self._runtime_whitelist:
            return False
        self._runtime_whitelist.append(chat_id)
        self._save_runtime_whitelist()
        return True

    def _remove_from_whitelist(self, chat_id: str) -> bool:
        if chat_id not in self._runtime_whitelist:
            return False
        self._runtime_whitelist.remove(chat_id)
        self._save_runtime_whitelist()
        return True

    async def _fetch_setu(self, tags=None, num=1, r18=0):
        session = await self._get_http_session()
        body = {
            "r18": r18,
            "num": min(num, self.config.api.max_num),
            "size": self.config.api.default_size,
            "excludeAI": self.config.api.exclude_ai,
        }
        if self.config.api.proxy.strip():
            body["proxy"] = self.config.api.proxy
        if tags:
            body["tag"] = tags
        try:
            async with session.post(
                self.config.api.base_url,
                json=body,
                headers={"Content-Type": "application/json"},
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
            async with session.get(url, headers=headers, ssl=False) as resp:
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
        """图片防风控处理：EXIF 方向校正后随机小角度旋转，并重编码为无 EXIF 的 JPEG。

        旋转改变感知哈希、重编码改变文件 MD5，二者叠加可显著降低被平台图库命中概率。
        """
        cfg = self.config.anti_detect
        with Image.open(io.BytesIO(data)) as img:
            img = ImageOps.exif_transpose(img)
            img = img.convert("RGB")
            if cfg.image_rotate_enabled:
                angle = random.uniform(cfg.rotate_min_degrees, cfg.rotate_max_degrees) * random.choice((1, -1))
                img = img.rotate(angle, resample=Image.Resampling.BICUBIC, expand=False)
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

    async def _send_setu_images(self, setu_list, stream_id, group_id="", user_id=""):
        sent_count = 0
        info_lines = []
        send_mode = self.config.api.send_mode.lower().strip()

        for item in setu_list:
            urls = item.get("urls", {})
            image_url = urls.get("regular") or urls.get("original") or urls.get("small") or urls.get("thumb")
            title = item.get("title", "未知")
            author = item.get("author", "未知")
            pid = item.get("pid", "")
            r18_flag = "🔞" if item.get("r18") else ""

            if not image_url:
                info_lines.append("• " + r18_flag + title + " - " + author + " - 获取图片地址失败")
                continue

            is_r18_item = bool(item.get("r18"))

            # 链接模式：直接发送图片链接文本
            if send_mode in ("link", "链接"):
                link_text = self._build_link_text(title, author, image_url)
                try:
                    msg_id = await self._send_text_via_api(link_text, stream_id, group_id, user_id)
                    sent_count += 1
                    recall_sec = self.config.limits.recall_after_seconds
                    if recall_sec > 0 and msg_id:
                        asyncio.create_task(self._schedule_recall(stream_id, recall_sec, msg_id))
                        self.ctx.logger.info("[SetuPlugin] 链接已计划撤回 message_id=%s 延迟%ds", msg_id, recall_sec)
                except Exception as e:
                    info_lines.append("• " + r18_flag + title + " - 发送链接失败: " + str(e))
                continue

            # kz_link 模式：全年龄发图片，扩展内容发链接
            if send_mode == "kz_link" and is_r18_item:
                link_text = self._build_link_text(title, author, image_url)
                try:
                    msg_id = await self._send_text_via_api(link_text, stream_id, group_id, user_id)
                    sent_count += 1
                    recall_sec = self.config.limits.recall_after_seconds
                    if recall_sec > 0 and msg_id:
                        asyncio.create_task(self._schedule_recall(stream_id, recall_sec, msg_id))
                        self.ctx.logger.info("[SetuPlugin] 链接已计划撤回 message_id=%s 延迟%ds", msg_id, recall_sec)
                except Exception as e:
                    info_lines.append("• " + r18_flag + title + " - 发送链接失败: " + str(e))
                continue

            # 图片模式 / kz_link 模式的全年龄部分 / both 模式
            image_data = await self._download_image_bytes(image_url)
            b64_data = None
            if image_data:
                try:
                    b64_data = base64.b64encode(self._process_image(image_data)).decode("utf-8")
                except Exception as e:
                    self.ctx.logger.error("[SetuPlugin] 图片防风控处理失败: %s, url=%s", e, image_url, exc_info=True)
            if not b64_data:
                # 图片下载或处理失败时回退到发链接
                if send_mode in ("both", "两者"):
                    try:
                        msg_id = await self._send_text_via_api(self._build_link_text(title, author, image_url), stream_id, group_id, user_id)
                        sent_count += 1
                        recall_sec = self.config.limits.recall_after_seconds
                        if recall_sec > 0 and msg_id:
                            asyncio.create_task(self._schedule_recall(stream_id, recall_sec, msg_id))
                    except Exception as e:
                        info_lines.append("• " + r18_flag + title + " - 发送失败: " + str(e))
                else:
                    info_lines.append("• " + r18_flag + title + " - " + author + " - 图片下载失败")
                continue

            try:
                msg_id = await self._send_image_via_api(b64_data, stream_id, group_id, user_id)
                sent_count += 1
                recall_sec = self.config.limits.recall_after_seconds
                if recall_sec > 0 and msg_id:
                    asyncio.create_task(self._schedule_recall(stream_id, recall_sec, msg_id))
                    self.ctx.logger.info("[SetuPlugin] 已计划撤回 message_id=%s 延迟%ds", msg_id, recall_sec)
            except Exception as e:
                info_lines.append("• " + r18_flag + title + " - " + author + " - 发送失败: " + str(e))

            # both 模式下同时发送链接
            if send_mode in ("both", "两者"):
                try:
                    link_msg_id = await self._send_text_via_api(self._build_link_text(title, author, image_url), stream_id, group_id, user_id)
                    recall_sec = self.config.limits.recall_after_seconds
                    if recall_sec > 0 and link_msg_id:
                        asyncio.create_task(self._schedule_recall(stream_id, recall_sec, link_msg_id))
                except Exception:
                    pass

        return sent_count, info_lines

    async def _send_image_via_api(self, b64_data, stream_id, group_id="", user_id=""):
        """通过 NapCat send_msg API 发送图片，返回 message_id"""
        # 构造图片消息段
        image_segment = {
            "type": "image",
            "data": {
                "file": "base64://" + b64_data
            }
        }
        # 确定发送目标
        if group_id:
            send_params = {
                "message_type": "group",
                "group_id": int(group_id) if str(group_id).isdigit() else group_id,
                "message": [image_segment],
            }
        elif user_id:
            send_params = {
                "message_type": "private",
                "user_id": int(user_id) if str(user_id).isdigit() else user_id,
                "message": [image_segment],
            }
        else:
            # 回退：使用 ctx.send.image
            await self.ctx.send.image(b64_data, stream_id)
            return None

        try:
            result = await self.ctx.api.call(
                "adapter.napcat.message.send_msg",
                version="1",
                params=send_params,
            )
            self.ctx.logger.info("[SetuPlugin] send_msg API 返回: %s", result)
            if isinstance(result, dict) and not result.get("success", True):
                self.ctx.logger.warning("[SetuPlugin] send_msg API 失败，回退到 ctx.send.image")
                await self.ctx.send.image(b64_data, stream_id)
                return None
            if isinstance(result, dict):
                msg_id = result.get("message_id")
                if not msg_id and isinstance(result.get("data"), dict):
                    msg_id = result["data"].get("message_id")
                if not msg_id:
                    msg_id = result.get("external_message_id")
                self.ctx.logger.info("[SetuPlugin] 提取到 message_id=%s", msg_id)
                return msg_id
            return None
        except Exception as e:
            self.ctx.logger.error("[SetuPlugin] send_msg API 调用失败: %s，回退到 ctx.send.image", e)
            await self.ctx.send.image(b64_data, stream_id)
            return None

    async def _send_text_via_api(self, text, stream_id, group_id="", user_id=""):
        """通过 NapCat send_msg API 发送文本消息，返回 message_id"""
        text_segment = {
            "type": "text",
            "data": {
                "text": text
            }
        }
        # 确定发送目标
        if group_id:
            send_params = {
                "message_type": "group",
                "group_id": int(group_id) if str(group_id).isdigit() else group_id,
                "message": [text_segment],
            }
        elif user_id:
            send_params = {
                "message_type": "private",
                "user_id": int(user_id) if str(user_id).isdigit() else user_id,
                "message": [text_segment],
            }
        else:
            # 回退：使用 ctx.send.text
            await self.ctx.send.text(text, stream_id)
            return None

        try:
            result = await self.ctx.api.call(
                "adapter.napcat.message.send_msg",
                version="1",
                params=send_params,
            )
            self.ctx.logger.info("[SetuPlugin] send_text API 返回: %s", result)
            if isinstance(result, dict) and not result.get("success", True):
                self.ctx.logger.warning("[SetuPlugin] send_text API 失败，回退到 ctx.send.text")
                await self.ctx.send.text(text, stream_id)
                return None
            if isinstance(result, dict):
                msg_id = result.get("message_id")
                if not msg_id and isinstance(result.get("data"), dict):
                    msg_id = result["data"].get("message_id")
                if not msg_id:
                    msg_id = result.get("external_message_id")
                self.ctx.logger.info("[SetuPlugin] 文本提取到 message_id=%s", msg_id)
                return msg_id
            return None
        except Exception as e:
            self.ctx.logger.error("[SetuPlugin] send_text API 调用失败: %s，回退到 ctx.send.text", e)
            await self.ctx.send.text(text, stream_id)
            return None

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
        ),
        activation_type=ActivationType.ALWAYS,
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
                name="num",
                param_type=ToolParamType.INTEGER,
                description="请求图片数量，1-3，默认1。每次最多请求3张，不要请求超过3张。",
                required=False,
            ),
            ToolParameterInfo(
                name="r18",
                param_type=ToolParamType.BOOLEAN,
                description="是否请求 R18 内容。默认 false。若会话未开启 R18 权限会返回提示。",
                required=False,
            ),
        ],
    )
    async def handle_get_setu(self, tags=None, num=1, r18=None, stream_id="", **kwargs):
        self.ctx.logger.info("[SetuPlugin] get_setu 被调用: stream_id=%s tags=%s r18=%s", stream_id, tags, r18)
        rate_key = stream_id or "global"
        if not self._rate_limiter.is_allowed(rate_key):
            remaining = self._rate_limiter.remaining(rate_key)
            return {"name": "get_setu", "content": "请求频率超限，剩余 " + str(remaining) + " 次/分钟"}

        requested_r18 = 1 if r18 is True else 0

        if requested_r18 > 0:
            chat_id = self._get_chat_id(
                group_id=str(kwargs.get("group_id", "")),
                user_id=str(kwargs.get("user_id", "")),
                stream_id=stream_id,
            )
            if not self._check_r18_permission(chat_id):
                return {"name": "get_setu", "content": "当前会话 (" + chat_id + ") 未开启 R18 权限。你可以调用 enable_r18 工具开启。"}

        num = max(1, min(num, 3))
        result = await self._fetch_setu(tags=tags, num=num, r18=requested_r18)

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
        asyncio.create_task(self._send_setu_images(
            setu_list, stream_id,
            group_id=str(kwargs.get("group_id", "")),
            user_id=str(kwargs.get("user_id", "")),
        ))

        summary = "图片正在发送给用户（共 " + str(len(setu_list)) + " 张）。"
        summary += "\n图片信息:\n" + "\n".join(detail_parts)
        return {"name": "get_setu", "content": summary}

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
        chat_id = self._get_chat_id(group_id=group_id, user_id=user_id, stream_id=stream_id)
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
        chat_id = self._get_chat_id(group_id=group_id, user_id=user_id, stream_id=stream_id)
        if not chat_id:
            return {"name": "disable_r18", "content": "无法识别当前会话的群号或用户号，无法操作。"}
        if not self._check_r18_permission(chat_id):
            return {"name": "disable_r18", "content": "当前会话没有 R18 权限，无需关闭。"}
        if chat_id in self.config.r18.allowed_chats:
            return {"name": "disable_r18", "content": "该会话的 R18 权限由管理员在配置中开启，AI 无法关闭，请联系管理员处理。"}
        self._remove_from_whitelist(chat_id)
        self.ctx.logger.info("[SetuPlugin] 扩展内容权限已关闭: chat_id=%s", chat_id)
        await self.ctx.send.text("扩展权限已关闭", stream_id)
        return {"name": "disable_r18", "content": "已成功关闭当前会话的 R18 权限。"}

    # ── 4. Command: /tu ──────────────────────────────────

    @Command("tu", description="获取二次元图片，用法: /tu [标签...] [r18]", pattern=r"^/tu")
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

            args = args_text.split() if args_text else []

            tags = []
            request_r18 = False

            for arg in args:
                arg_lower = arg.lower()
                if arg_lower in ("r18", "nsfw", "adult"):
                    request_r18 = True
                else:
                    tags.append(arg)

            self.ctx.logger.info("[SetuPlugin] /tu 解析: tags=%s r18=%s", tags, request_r18)

            # 命令模式固定返回 1 张
            num = 1

            r18_level = 0
            if request_r18:
                chat_id = self._get_chat_id(
                    group_id=str(kwargs.get("group_id", "")),
                    user_id=str(kwargs.get("user_id", "")),
                    stream_id=stream_id,
                )
                if not self._check_r18_permission(chat_id):
                    await self.ctx.send.text(
                        "🔒 当前会话未开启 R18 权限，无法请求 R18 内容。\n"
                        "如需开启，请向管理员申请，或向我表达意愿让我帮你开启。",
                        stream_id,
                    )
                    return True, "R18 权限未开启", True
                r18_level = 1

            rate_key = stream_id or "global"
            if not self._rate_limiter.is_allowed(rate_key):
                await self.ctx.send.text("⏰ 请求过于频繁，请稍后再试~", stream_id)
                return True, "频率限制", True

            result = await self._fetch_setu(tags=tags, num=num, r18=r18_level)

            if not result["success"]:
                await self.ctx.send.text("❌ 获取图片失败: " + result["error"], stream_id)
                return False, result["error"], True

            setu_list = result["data"]
            sent_count, _ = await self._send_setu_images(
                setu_list, stream_id,
                group_id=str(kwargs.get("group_id", "")),
                user_id=str(kwargs.get("user_id", "")),
            )

            return True, "已发送 " + str(sent_count) + " 张图片", True
        except Exception as e:
            self.ctx.logger.error("[SetuPlugin] /setu 命令异常: %s", e, exc_info=True)
            await self.ctx.send.text("❌ 插件内部错误: " + str(e), stream_id)
            return False, "插件内部错误: " + str(e), True


# ── 工厂函数 ──────────────────────────────────────────────


def create_plugin() -> SetuPlugin:
    return SetuPlugin()
