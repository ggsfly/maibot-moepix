# 二次元图片插件 (maibot-moepix)

> 不同于普通的获取二次元美图插件，此插件更具有互动性，支持 AI 智能调用，内容级别采用白名单控制，发送后可自动撤回。

---

**前置条件：** MaiBot 主程序已部署（主程序自带 aiohttp 与 Pillow 依赖，无需额外安装）。

**快速开始：** 把本文件夹放到 MaiBot 的 plugins 目录下 → 首次启动自动生成配置文件 → 即可使用。

---

## ✨ 功能特性

| 功能 | 说明 |
|------|------|
| **AI 智能调用** | AI 根据用户自然语言自主判断并获取图片 |
| **命令直接调用** | 用户通过 /tu 命令直接获取，支持标签筛选 |
| **扩展内容权限** | 双通道白名单：管理员名单（WebUI 维护 config.toml）+ AI 名单（工具维护插件数据 JSON），互不干扰 |
| **图片防风控** | 发送前随机小角度旋转 + JPEG 重编码，改变感知哈希与 MD5，降低被平台识别概率 |
| **链接防风控** | 发链接时隐藏协议头并伪装域名点号（如 `i点pximg点net`），避免完整敏感域名被检测 |
| **自动撤回** | 图片发送后指定时间自动撤回，避免长期留存 |
| **频率限制** | 滑动窗口限频，防止滥用 |
| **热重载配置** | 修改配置无需重启插件，文件监听自动生效 |
| **配置保护** | 插件从不写 config.toml，缺失字段由框架自动补齐，杜绝配置文件格式损坏 |

## 📦 安装

将 maibot-moepix 文件夹放入 MaiBot 的 plugins 目录下，目录结构如下：

    your-maibot/
    ├── plugins/
    │   └── maibot-moepix/
    │       ├── _manifest.json
    │       ├── plugin.py
    │       ├── config.example.toml
    │       ├── .gitignore
    │       ├── README.md
    │       └── _locales/
    │           └── zh-CN.json
    └── ...

首次启动时，框架会根据插件的配置模型自动生成 config.toml 并补齐缺失字段。

## ⚙️ 配置说明

配置文件为 config.toml，也可在 WebUI 配置编辑器中修改。

### 扩展内容权限设置

    [r18]
    allowed_chats = []

**白名单格式：**
- 群聊：group_群号，例如 group_123456
- 私聊：user_QQ号，例如 user_123456

**扩展内容权限有两个独立通道（分开管理、互不干扰）：**

1. **管理员名单（config.toml）：** 在 WebUI 配置编辑器中直接编辑 allowed_chats 列表，配置热重载即时生效。AI 无法修改也无法关闭其中的会话。
2. **AI 名单（插件数据 JSON）：** 用户向 AI 表达意愿，AI 觉得用户真诚则调用 enable_r18 工具开启，持久化写入 `data/plugins/<插件id>/r18_whitelist.json`，重启后依然有效；用户也可要求 AI 调用 disable_r18 关闭。

生效名单 = 两个通道的并集。**重要：未开启扩展权限的会话，无论命令还是 AI 调用，均不可请求扩展内容。**

### API 设置

    [api]
    base_url = "https://api.lolicon.app/setu/v2"
    default_num = 1
    default_size = ["regular"]
    proxy = ""
    exclude_ai = true
    max_num = 3
    send_mode = "image"

**proxy 反代地址说明：**
- 留空 → 使用 Pixiv 原始地址（需服务器能直接访问 Pixiv）
- i.yuki.sh → 自定义反代
- i.pixiv.re → Cloudflare 反代（可能间歇性不可用）

**send_mode 发送模式说明：**
- image → 直接发送图片（默认，体验最好但可能被审查）
- link → 只发送图片链接文本（安全，不会被审查，用户需自行打开）
- both → 同时发送图片和链接
- kz_link → 全年龄内容发图片，扩展内容发链接（推荐用于防审查场景）

### 频率限制与撤回

    [limits]
    rate_per_minute = 3
    recall_after_seconds = 90

自动撤回依赖 NapCat 适配器的 delete_msg API。建议群聊设置 60-180 秒，私聊可设为 0。

### 防风控设置

    [anti_detect]
    image_rotate_enabled = true
    rotate_min_degrees = 0.5
    rotate_max_degrees = 2.5
    jpeg_quality = 88
    link_obfuscate_enabled = true
    link_dot_replacement = "点"

**图片防风控：** 发送前对图片做 EXIF 方向校正 → 随机小角度旋转（默认 0.5°–2.5°，肉眼几乎无感）→ 去除 EXIF → 按 jpeg_quality 重编码为 JPEG。旋转改变感知哈希、重编码改变文件 MD5，双重避免命中平台图库黑名单。依赖 Pillow（主程序已内置）。

**链接防风控：** 发送链接时去掉 `https://` 协议头，并把域名中的第一个和最后一个 `.` 替换为 `link_dot_replacement`（默认汉字"点"）：

    原始：https://i.pximg.net/img-master/img/0000/123.jpg
    伪装：i点pximg点net/img-master/img/0000/123.jpg

路径与扩展名保持原样，用户还原链接时只需改回域名里的一两个字符。

## 🎮 使用方法

### 命令调用

    /tu                    # 随机 1 张
    /tu 白丝               # 标签"白丝"，1 张

### AI 智能调用

直接用自然语言与机器人对话，AI 会自动判断是否调用色图工具：

- "来张白丝萝莉图"
- "多来几张二次元图"
- "有没有某画师的图"

#### 自适应 Pixiv Tag 搜索

AI 调用 `get_setu` 时，Lolicon 使用 Pixiv 标签精确匹配，而不是自然语言语义搜索。因此插件会在 Tool Prompt 中要求 AI 将用户描述转换为 Pixiv 常见标签：

- 同一概念的同义词放在同一个 `tags` 字符串中，用 `|` 分隔表示 OR。
- 不同概念作为不同数组元素，表示 AND。
- 优先使用日文 Pixiv 标签，并补充常见英文、中文同义标签。
- 只转换用户明确要求的主题，不添加用户没有要求的额外概念。
- `R18`、`成人向` 等内容级别词只用于设置 `r18` 参数，不放入 `tags`。
- 用户没有指定具体主题时，不传 `tags`，执行随机搜索。

示例：

```text
用户：来点 R18 玉足
AI 调用：{"r18": true, "num": 3, "tags": ["足裏|裸足|feet|足控"]}

用户：来点白丝玉足
AI 调用：{"r18": false, "num": 3, "tags": ["白タイツ|白ストッキング|white stockings|白丝", "足裏|裸足|feet|足控"]}

用户：来点 R18
AI 调用：{"r18": true, "num": 3}
```

### 扩展内容使用流程

**AI 动态开启：**

1. 用户向 AI 表达意愿
2. AI 评估用户态度和场景，认为合适则调用
3. 插件将该会话加入 AI 名单（写入插件数据目录 JSON，自动持久化）
4. 用户可随时要求 AI 关闭（管理员名单中的会话除外）

**管理员手动配置：**

1. 在 WebUI 配置编辑器中编辑
2. 添加群号或用户号
3. 配置自动热重载，立即生效

## 🔒 安全设计

1. **默认安全：** 安装后白名单为空，任何会话均不可请求扩展内容。
2. **统一控制：** 无论 AI 还是命令，权限检查一致，不可绕过。
3. **权限分离：** 管理员名单在 config.toml（WebUI 维护），AI 名单在插件数据目录 JSON（工具维护），AI 永远无法修改管理员名单。
4. **配置只读：** 插件运行期间从不写入 config.toml，缺失字段由框架自动补齐，杜绝配置文件格式损坏。
5. **图片防风控：** 随机旋转 + 重编码改变图片哈希，降低封号概率。
6. **链接防风控：** 域名点号伪装，避免完整敏感链接被平台检测。
7. **频率限制：** 基于滑动窗口的频率限制，防止滥用。
8. **自动撤回：** 图片发送后自动撤回，避免内容长期留存。
9. **日志审计：** 所有关键操作均通过框架日志系统记录。

## 📁 文件结构

    maibot-moepix/
    ├── _manifest.json          # 插件元数据清单（manifest v2）
    ├── plugin.py               # 插件主入口（配置模型 + 所有组件）
    ├── config.example.toml     # 配置模板文件（随 git 更新）
    ├── config.toml             # 用户配置文件（不纳入 git，受保护）
    ├── .gitignore              # 排除 config.toml
    ├── README.md               # 本文件
    └── _locales/
        └── zh-CN.json          # 中文

AI 名单（运行时白名单）保存在主程序数据目录：`data/plugins/Emilia-awa.maibot-moepix/r18_whitelist.json`。

## 📄 许可证

WTFPL
