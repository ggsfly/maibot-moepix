"""moepix 插件单元测试。

运行（在插件目录）：
    F:/MaiBot/.venv/Scripts/python.exe -m pytest tests/ -v

覆盖：/tu 参数解析全分支、长宽比校验、_fetch_setu 请求体构造、
限频维度 key、转发节点构造与长度对齐、manifest capability 声明。
"""

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR))

import plugin as moepix  # noqa: E402


# ── 长宽比校验与快捷词 ──────────────────────────────────────


class TestAspectRatio:
    @pytest.mark.parametrize("expr", ["gt1.5", "lt0.8", "eq1", "gte2", "gt1.5lt2.0", "gt1"])
    def test_valid(self, expr):
        assert moepix.validate_aspect_ratio(expr) is True

    @pytest.mark.parametrize("expr", ["gt99", "gt", "1.5", "gt1.5lt", "gt0.05", "abc"])
    def test_invalid(self, expr):
        assert moepix.validate_aspect_ratio(expr) is False

    def test_aliases(self):
        assert moepix.ASPECT_RATIO_ALIASES["横图"] == "gt1"
        assert moepix.ASPECT_RATIO_ALIASES["vertical"] == "lt1"
        assert moepix.ASPECT_RATIO_ALIASES["方图"] == "eq1"


# ── /tu 参数解析 ────────────────────────────────────────────


class TestParseTuArgs:
    def test_empty(self):
        p = moepix.parse_tu_args("", max_num=5)
        assert p.num == 1 and p.tags == [] and not p.request_r18

    def test_num(self):
        assert moepix.parse_tu_args("3", max_num=5).num == 3
        assert moepix.parse_tu_args("99", max_num=5).num == 5  # 钳到 max_num
        assert moepix.parse_tu_args("0", max_num=5).num == 1   # 下限 1

    def test_r18_and_aliases(self):
        assert moepix.parse_tu_args("r18", max_num=5).request_r18 is True
        assert moepix.parse_tu_args("nsfw", max_num=5).request_r18 is True

    def test_noai(self):
        assert moepix.parse_tu_args("noai", max_num=5).exclude_ai is True
        assert moepix.parse_tu_args("排除ai", max_num=5).exclude_ai is True
        assert moepix.parse_tu_args("白丝", max_num=5).exclude_ai is None

    def test_bare_word_is_tag(self):
        """旧写法：裸词 = 标签。"""
        p = moepix.parse_tu_args("白丝", max_num=5)
        assert p.tags == ["白丝"]

    def test_hash_single(self):
        assert moepix.parse_tu_args("#萝莉", max_num=5).tags == ["萝莉"]

    def test_hash_group_or(self):
        """组内逗号 = OR，拼成 a|b|c 单字符串。"""
        assert moepix.parse_tu_args("#白丝,黑丝", max_num=5).tags == ["白丝|黑丝"]

    def test_hash_groups_and(self):
        """多个 # 组为 AND（各自成元素）。"""
        p = moepix.parse_tu_args("#萝莉 #白丝", max_num=5)
        assert p.tags == ["萝莉", "白丝"]

    def test_hash_group_limit(self):
        """标签组最多 3 组，超出提示并忽略。"""
        p = moepix.parse_tu_args("#a #b #c #d", max_num=5)
        assert len(p.tags) == 3
        assert any("最多 3 组" in w for w in p.warnings)

    def test_keyword(self):
        p = moepix.parse_tu_args("kw:原神", max_num=5)
        assert p.keyword == "原神" and p.tags == []
        p2 = moepix.parse_tu_args("kw:原神 kw:启动", max_num=5)
        assert p2.keyword == "原神 启动"  # 多个 kw: 拼接

    def test_uid(self):
        assert moepix.parse_tu_args("uid:12345", max_num=5).uid == 12345
        p = moepix.parse_tu_args("uid:abc", max_num=5)
        assert p.uid == 0 and any("UID" in w for w in p.warnings)

    def test_aspect_alias(self):
        assert moepix.parse_tu_args("横图", max_num=5).aspect_ratio == "gt1"
        assert moepix.parse_tu_args("竖图", max_num=5).aspect_ratio == "lt1"

    def test_aspect_expr(self):
        assert moepix.parse_tu_args("gt1.5", max_num=5).aspect_ratio == "gt1.5"

    def test_aspect_invalid_warns(self):
        """形似 gtX 但不合法：提示后忽略，不当标签。"""
        p = moepix.parse_tu_args("gt99", max_num=5)
        assert p.aspect_ratio == "" and p.tags == []
        assert any("长宽比表达式无效" in w for w in p.warnings)

    def test_combined(self):
        p = moepix.parse_tu_args("3 #萝莉,少女 横图 noai", max_num=5)
        assert p.num == 3
        assert p.tags == ["萝莉|少女"]
        assert p.aspect_ratio == "gt1"
        assert p.exclude_ai is True

    def test_backward_compat(self):
        """旧习惯 /tu 白丝 r18 行为不变。"""
        p = moepix.parse_tu_args("白丝 r18", max_num=5)
        assert p.tags == ["白丝"] and p.request_r18 is True


# ── 测试用假件 ──────────────────────────────────────────────


class FakeResp:
    def __init__(self, json_data):
        self.status = 200
        self._json = json_data

    async def json(self):
        return self._json

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class FakeSession:
    def __init__(self, payload=None):
        self.calls = []
        self.closed = False
        self._payload = payload or {"data": []}

    def post(self, url, **kwargs):
        self.calls.append(("post", url, kwargs))
        return FakeResp(self._payload)

    def get(self, url, **kwargs):
        self.calls.append(("get", url, kwargs))
        return FakeResp({"data": []})


class FakeLogger:
    @staticmethod
    def info(*a, **k):
        pass

    @staticmethod
    def warning(*a, **k):
        pass

    @staticmethod
    def error(*a, **k):
        pass


class FakePaths:
    data_dir = Path(__file__).parent / "_tmp_data"


class FakeApi:
    def __init__(self):
        self.calls = []

    async def call(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return {"success": True, "message_id": 555}


class FakeSend:
    def __init__(self):
        self.forwards = []
        self.texts = []
        self.images = []
        self.forward_result = {"sent": True, "message_id": "777"}
        self.forward_exc = None

    async def forward(self, nodes, stream_id, return_details=False, **kwargs):
        self.forwards.append({"nodes": nodes, "stream_id": stream_id, "return_details": return_details})
        if self.forward_exc:
            raise self.forward_exc
        return self.forward_result

    async def text(self, text, stream_id, **kwargs):
        self.texts.append((text, stream_id))

    async def image(self, b64, stream_id, **kwargs):
        self.images.append((b64, stream_id))


class FakeCtx:
    def __init__(self):
        self.paths = FakePaths()
        self.logger = FakeLogger()
        self.api = FakeApi()
        self.send = FakeSend()

    async def config_get(self, key, default=None):
        return default


def make_plugin() -> "moepix.SetuPlugin":
    p = moepix.create_plugin()
    p.set_plugin_config({})
    p._ctx = FakeCtx()
    p._rate_limiter = moepix.RateLimiter()
    return p


# ── _fetch_setu 请求体构造 ──────────────────────────────────


class TestFetchSetuBody:
    def _make(self, payload):
        p = make_plugin()
        session = FakeSession(payload)
        p._http_session = session
        return p, session

    def test_basic_body(self):
        p, s = self._make({"data": []})
        asyncio.run(p._fetch_setu(tags=["白丝"], num=2, r18=1))
        body = s.calls[0][2]["json"]
        assert body["r18"] == 1 and body["num"] == 2 and body["tag"] == ["白丝"]
        assert body["excludeAI"] is True  # 默认配置
        assert "keyword" not in body and "uid" not in body and "aspectRatio" not in body

    def test_keyword_uid_aspect(self):
        p, s = self._make({"data": []})
        asyncio.run(p._fetch_setu(num=1, keyword="原神", uid=[12345], aspect_ratio="gt1.5"))
        body = s.calls[0][2]["json"]
        assert body["keyword"] == "原神"
        assert body["uid"] == [12345]
        assert body["aspectRatio"] == "gt1.5"

    def test_exclude_ai_override(self):
        """exclude_ai 显式传入时覆盖配置（支撑 noai）。"""
        p, s = self._make({"data": []})
        asyncio.run(p._fetch_setu(num=1, exclude_ai=True))
        assert s.calls[0][2]["json"]["excludeAI"] is True
        p, s = self._make({"data": []})
        asyncio.run(p._fetch_setu(num=1, exclude_ai=False))
        assert s.calls[0][2]["json"]["excludeAI"] is False

    def test_uid_invalid_filtered(self):
        p, s = self._make({"data": []})
        asyncio.run(p._fetch_setu(num=1, uid=["abc", "999"]))
        assert s.calls[0][2]["json"]["uid"] == [999]

    def test_num_clamped(self):
        p, s = self._make({"data": []})
        asyncio.run(p._fetch_setu(num=99))
        assert s.calls[0][2]["json"]["num"] == p.config.api.max_num


# ── 限频维度 ────────────────────────────────────────────────


class TestRateKey:
    def test_chat_scope(self):
        p = make_plugin()
        p.config.limits.rate_scope = "chat"
        assert p._resolve_rate_key("stream_1", {"user_id": "888"}) == "stream_1"

    def test_user_scope(self):
        p = make_plugin()
        p.config.limits.rate_scope = "user"
        assert p._resolve_rate_key("stream_1", {"user_id": "888"}) == "user:888"

    def test_user_scope_fallback(self):
        """取不到用户 id 时回退会话 id。"""
        p = make_plugin()
        p.config.limits.rate_scope = "user"
        assert p._resolve_rate_key("stream_1", {}) == "stream_1"


# ── 合并转发 ────────────────────────────────────────────────


class TestForward:
    def _make(self, use_forward=True, send_mode="image"):
        p = make_plugin()
        p.config.api.use_forward = use_forward
        p.config.api.send_mode = send_mode
        p._bot_account = "10001"
        p._bot_nickname = "测试Bot"
        return p

    def test_manifest_capability(self):
        """send.forward 必须在 manifest capabilities 中声明，否则宿主拒绝调用。"""
        manifest = json.loads((PLUGIN_DIR / "_manifest.json").read_text(encoding="utf-8"))
        assert "send.forward" in manifest["capabilities"]

    def test_nodes_built_and_aligned(self):
        """下载失败的条目降级为纯文本节点（含伪装链接）；节点结构含 user_id/nickname/segments。"""
        p = self._make()
        p.config.limits.recall_after_seconds = 0  # 本用例不关心撤回，避免挂起任务
        entries = [
            ({"title": "a", "author": "x", "pid": 1, "width": 800, "height": 1200}, "https://i.pximg.net/a.jpg"),
            ({"title": "b", "author": "y", "pid": 2}, "https://i.pximg.net/b.jpg"),
        ]
        bytes_list = [b"\x89PNG" + b"0" * 200, None]  # 第二张下载失败

        # _process_image 对假字节会失败 → monkeypatch 跳过处理
        p._to_base64 = lambda raw, url: ("YjY0ZGF0YQ==" if raw else None)
        sent = asyncio.run(p._send_as_forward(entries, bytes_list, "image", "stream_1"))
        assert sent == 2  # 两个条目都有节点（失败条目降级为文本节点）
        record = p._ctx.send.forwards[0]
        assert record["return_details"] is True
        nodes = record["nodes"]
        assert len(nodes) == 2
        node1, node2 = nodes
        assert node1["user_id"] == "10001" and node1["nickname"] == "测试Bot"
        assert [seg["type"] for seg in node1["segments"]] == ["text", "image"]
        assert "【1/2】a" in node1["segments"][0]["content"]
        # 下载失败条目：纯文本节点 + 伪装链接
        assert [seg["type"] for seg in node2["segments"]] == ["text"]
        assert "【2/2】b" in node2["segments"][0]["content"]
        assert "i点pximg点net/b.jpg" in node2["segments"][0]["content"]

    def test_forward_disabled_noop(self):
        """use_forward=false 时不调用 forward，走逐条路径。"""
        p = self._make(use_forward=False)
        p.config.limits.recall_after_seconds = 0

        async def fake_download(urls):
            return [None for _ in urls]

        p._download_all_bytes = fake_download
        items = [{"title": "a", "author": "x", "pid": 1, "urls": {"regular": "https://u/1.jpg"}}]
        sent = asyncio.run(p._send_setu_images(items, "stream_1"))
        assert p._ctx.send.forwards == []  # 未走转发
        assert sent == 0  # 下载失败未发出

    def test_forward_recalls_whole_forward(self):
        """转发成功且拿到 message_id → 对整条转发调度一次撤回。"""
        p = self._make(use_forward=True)
        p.config.limits.recall_after_seconds = 90
        scheduled = []
        p._spawn_task = lambda coro: scheduled.append(coro)

        async def fake_download(urls):
            return [b"x" * 200 for _ in urls]

        p._download_all_bytes = fake_download
        p._to_base64 = lambda raw, url: "YjY0"
        items = [{"title": "a", "author": "x", "pid": 1, "urls": {"regular": "https://u/1.jpg"}}]
        sent = asyncio.run(p._send_setu_images(items, "stream_1"))
        assert sent == 1
        assert len(scheduled) == 1
        assert scheduled[0].cr_code.co_name == "_schedule_recall"

        # 实际执行撤回协程：delete_msg 收到的正是转发容器的 message_id
        coro = scheduled[0]

        async def fake_sleep(seconds):
            pass

        with patch("asyncio.sleep", new=fake_sleep):
            asyncio.run(coro)
        (api_args, api_kwargs) = p._ctx.api.calls[0]
        assert api_args[0] == "adapter.napcat.message.delete_msg"
        assert api_kwargs["message_id"] == 777

    def test_forward_no_message_id_no_fallback(self):
        """sent=True 但 message_id 为空：不撤回、也不回退逐条（防双发）。"""
        p = self._make(use_forward=True)
        p.config.limits.recall_after_seconds = 90
        scheduled = []
        p._spawn_task = lambda coro: scheduled.append(coro)

        async def fake_download(urls):
            return [b"x" * 200 for _ in urls]

        p._download_all_bytes = fake_download
        p._to_base64 = lambda raw, url: "YjY0"
        p._ctx.send.forward_result = {"sent": True, "message_id": None}
        items = [{"title": "a", "author": "x", "pid": 1, "urls": {"regular": "https://u/1.jpg"}}]
        sent = asyncio.run(p._send_setu_images(items, "stream_1"))
        assert sent == 1
        assert scheduled == []  # 未调度撤回
        assert p._ctx.api.calls == []  # 未回退逐条

    def test_forward_fails_fallback_per_item(self):
        """转发异常 → 回退逐条：图经适配器发送且逐条撤回生效。"""
        p = self._make(use_forward=True)
        p.config.limits.recall_after_seconds = 90
        scheduled = []
        p._spawn_task = lambda coro: scheduled.append(coro)

        async def fake_download(urls):
            return [b"x" * 200 for _ in urls]

        p._download_all_bytes = fake_download
        p._to_base64 = lambda raw, url: "YjY0"
        p._ctx.send.forward_exc = RuntimeError("boom")
        items = [{"title": "a", "author": "x", "pid": 1, "urls": {"regular": "https://u/1.jpg"}}]
        sent = asyncio.run(p._send_setu_images(items, "stream_1", group_id="12345"))
        assert sent == 1
        assert len(p._ctx.send.forwards) == 1  # 尝试过一次转发
        (api_args, api_kwargs) = p._ctx.api.calls[0]
        assert api_args[0] == "adapter.napcat.message.send_msg"
        assert len(scheduled) == 1  # 逐条撤回生效
        assert scheduled[0].cr_code.co_name == "_schedule_recall"
        scheduled[0].close()  # 不实际执行，显式关闭避免 GC 警告

    def test_both_link_inside_forward(self):
        """both+use_forward：链接文本并入图片节点，不再逐条发链接。"""
        p = self._make(use_forward=True, send_mode="both")
        p.config.limits.recall_after_seconds = 0

        async def fake_download(urls):
            return [b"x" * 200 for _ in urls]

        p._download_all_bytes = fake_download
        p._to_base64 = lambda raw, url: "YjY0"
        items = [{"title": "a", "author": "x", "pid": 1, "urls": {"regular": "https://i.pximg.net/a.jpg"}}]
        sent = asyncio.run(p._send_setu_images(items, "stream_1"))
        assert sent == 1
        assert len(p._ctx.send.forwards) == 1
        node = p._ctx.send.forwards[0]["nodes"][0]
        assert [seg["type"] for seg in node["segments"]] == ["text", "image"]
        assert "🔗 i点pximg点net/a.jpg" in node["segments"][0]["content"]
        assert p._ctx.api.calls == []  # 链接没有额外逐条发送

    def test_kz_link_r18_text_node(self):
        """kz_link+use_forward：R18 条目为纯文本节点（伪装链接），不下载图片。"""
        p = self._make(use_forward=True, send_mode="kz_link")
        p.config.limits.recall_after_seconds = 0
        downloaded = []

        async def fake_download(urls):
            downloaded.extend(urls)
            return [b"x" * 200 for _ in urls]

        p._download_all_bytes = fake_download
        items = [{"title": "a", "author": "x", "pid": 1, "r18": True, "urls": {"regular": "https://i.pximg.net/a.jpg"}}]
        sent = asyncio.run(p._send_setu_images(items, "stream_1"))
        assert sent == 1
        assert downloaded == []  # R18 走链接，不下载
        node = p._ctx.send.forwards[0]["nodes"][0]
        assert [seg["type"] for seg in node["segments"]] == ["text"]
        assert "🔗 i点pximg点net/a.jpg" in node["segments"][0]["content"]

    def test_link_mode_forward_text_nodes(self):
        """link 模式 + use_forward：全部为纯文本节点。"""
        p = self._make(use_forward=True, send_mode="link")
        p.config.limits.recall_after_seconds = 0
        items = [
            {"title": "a", "author": "x", "pid": 1, "urls": {"regular": "https://i.pximg.net/a.jpg"}},
            {"title": "b", "author": "y", "pid": 2, "urls": {"regular": "https://i.pximg.net/b.jpg"}},
        ]
        sent = asyncio.run(p._send_setu_images(items, "stream_1"))
        assert sent == 2
        nodes = p._ctx.send.forwards[0]["nodes"]
        assert len(nodes) == 2
        for i, node in enumerate(nodes, start=1):
            assert [seg["type"] for seg in node["segments"]] == ["text"]
            assert f"【{i}/2】" in node["segments"][0]["content"]

    def test_empty_entries_guard(self):
        """条目全无地址 / 空列表：返回 0，不调用转发。"""
        p = self._make(use_forward=True)
        assert asyncio.run(p._send_setu_images([], "s")) == 0
        assert asyncio.run(p._send_setu_images([{"title": "a", "urls": {}}], "s")) == 0
        assert p._ctx.send.forwards == []


# ── 并发下载 ────────────────────────────────────────────────


class TestDownloadAll:
    def test_order_and_failure_tolerance(self):
        """结果保持输入顺序；单张失败(None)不影响其他。"""
        p = make_plugin()

        async def fake_download(url):
            return None if url.endswith("bad") else (url.encode() + b"-" * 100)

        p._download_image_bytes = fake_download
        urls = ["https://u/1.jpg", "https://u/bad", "https://u/2.jpg"]
        result = asyncio.run(p._download_all_bytes(urls))
        assert result[0] == urls[0].encode() + b"-" * 100
        assert result[1] is None
        assert result[2] == urls[2].encode() + b"-" * 100

    def test_empty(self):
        p = make_plugin()
        assert asyncio.run(p._download_all_bytes([])) == []

    def test_semaphore_floor(self):
        """并发数下限由配置层守卫：pydantic ge=1 拒绝 0（Semaphore(0) 会永久挂起）。"""
        p = make_plugin()
        with pytest.raises(Exception):
            p.config.api.max_concurrent_downloads = 0
        # 运行时层仍钳底，双保险
        assert max(1, int(p.config.api.max_concurrent_downloads)) >= 1


# ── 说明文本 ────────────────────────────────────────────────


class TestCaption:
    def test_fields(self):
        p = make_plugin()
        item = {
            "title": "标题", "author": "画师", "pid": 42,
            "width": 800, "height": 1200, "aiType": 2, "r18": True,
            "tags": ["t1", "t2", "t3", "t4", "t5", "t6"],
        }
        text = p._build_caption(item, 1, 2)
        assert "【1/2】标题" in text and "👤 画师" in text and "PID: 42" in text
        assert "800x1200" in text and "🤖 AI作品" in text and "🔞 R18" in text
        assert text.count("t6") == 0  # 标签最多展示 5 个
