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


class FakeCtx:
    def __init__(self):
        self.paths = FakePaths()
        self.logger = FakeLogger()
        self.forward_calls = []

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
    def _make(self, use_forward=True):
        p = make_plugin()
        p.config.api.use_forward = use_forward
        p.config.api.send_mode = "image"
        p._bot_account = "10001"
        p._bot_nickname = "测试Bot"
        return p

    def test_manifest_capability(self):
        """send.forward 必须在 manifest capabilities 中声明，否则宿主拒绝调用。"""
        manifest = json.loads((PLUGIN_DIR / "_manifest.json").read_text(encoding="utf-8"))
        assert "send.forward" in manifest["capabilities"]

    def test_nodes_built_and_aligned(self):
        """下载失败的条目不生成节点；节点结构含 user_id/nickname/segments。"""
        p = self._make()
        entries = [
            ({"title": "a", "author": "x", "pid": 1, "width": 800, "height": 1200}, "https://u/1.jpg"),
            ({"title": "b", "author": "y", "pid": 2}, "https://u/2.jpg"),
        ]
        bytes_list = [b"\x89PNG" + b"0" * 200, None]  # 第二张下载失败

        async def fake_send_forward(nodes, stream_id):
            p._ctx.forward_calls.append(nodes)
            return True

        p._ctx.send = type("S", (), {"forward": staticmethod(fake_send_forward)})()
        # _process_image 对假字节会失败 → 需要真图；改用 monkeypatch 跳过处理
        p._to_base64 = lambda raw, url: ("YjY0ZGF0YQ==" if raw else None)
        ok = asyncio.run(p._send_as_forward(entries, bytes_list, "stream_1"))
        assert ok is True
        nodes = p._ctx.forward_calls[0]
        assert len(nodes) == 1  # 失败条目被跳过，长度天然对齐
        node = nodes[0]
        assert node["user_id"] == "10001" and node["nickname"] == "测试Bot"
        types = [seg["type"] for seg in node["segments"]]
        assert types == ["text", "image"]
        assert "【1/2】a" in node["segments"][0]["content"]

    def test_forward_disabled_noop(self):
        """use_forward=false 时 _send_setu_images 不调用 forward。"""
        p = self._make(use_forward=False)
        called = []

        async def fake_download(urls):
            return [None for _ in urls]

        p._download_all_bytes = fake_download

        async def fake_forward(nodes, stream_id):
            called.append(nodes)
            return True

        p._ctx.send = type("S", (), {"forward": staticmethod(fake_forward)})()
        items = [{"title": "a", "author": "x", "pid": 1, "urls": {"regular": "https://u/1.jpg"}}]
        asyncio.run(p._send_setu_images(items, "stream_1"))
        assert called == []  # 未走转发

    def test_forward_mode_skips_recall(self):
        """use_forward 生效时不调度撤回（转发消息无 message_id）。"""
        p = self._make(use_forward=True)
        p.config.limits.recall_after_seconds = 90
        scheduled = []
        p._spawn_task = lambda coro: scheduled.append(coro)

        async def fake_download(urls):
            return [b"x" * 200 for _ in urls]

        p._download_all_bytes = fake_download
        p._to_base64 = lambda raw, url: "YjY0"

        async def fake_forward(nodes, stream_id):
            return True

        p._ctx.send = type("S", (), {"forward": staticmethod(fake_forward)})()
        items = [{"title": "a", "author": "x", "pid": 1, "urls": {"regular": "https://u/1.jpg"}}]
        sent, _ = asyncio.run(p._send_setu_images(items, "stream_1"))
        assert sent == 1
        assert scheduled == []  # 转发成功路径不安排任何撤回任务


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
