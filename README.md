# astrbot_plugin_juhejiexi

AstrBot 聚合解析插件。

## 功能概览

- 聚合解析
- 火山方舟文生图
- 抖音主页解析
- 抖音扫码 Cookie 获取
- 抖音收藏解析（推荐独立部署）

核心实现位于 [`main.py`](README.md)，插件元数据位于 [`metadata.yaml`](metadata.yaml)。

## 支持的指令

| 指令 | 说明 | 状态 |
| --- | --- | --- |
| `/jx 分享链接` | 聚合解析，返回视频或图片资源链接 | 可用 |
| `/画 prompt描述` | 使用火山方舟生成图片 | 可用 |
| `/draw prompt描述` | 火山方舟画图英文别名 | 可用 |
| `/dyhome 抖音主页分享文本或链接` | 解析抖音主页并生成本地 TXT 文件；采集前会检查相同/相似作者 | 可用 |
| `/dyconfirm` | 确认继续采集存在身份冲突提示的抖音主页 | 可用 |
| `/dyskip` | 跳过当前会话待确认的抖音主页采集 | 可用 |
| `/dytrack 抖音主页分享文本或链接` | 将旧主页链接补录进更新记录 | 可用 |
| `/dytarget` | 绑定自动更新完成后的主动推送目标会话（需白名单） | 可用 |
| `/dymenu` | 查看本地可播放的抖音主页 TXT 菜单 | 可用 |
| `/dyplay 文件名` | 顺序播放或随机播放指定 TXT 中的视频链接 | 可用 |
| `/dyrand` | 每次切换到下一个主页，并随机播放其中一个视频 | 可用 |
| `/dyupdate` | 串行更新所有已记录的抖音主页，并按新增阈值跳过无变化主页（需白名单） | 可用 |
| `/dyupdateone 作者名或文件名` | 按名字匹配单个主页记录并更新 | 可用 |
| `/dyretry` | 重试上次更新失败的主页（需白名单） | 可用 |
| `/dycollection [favorite\|collection]` | 提交抖音点赞/收藏后台解析任务 | 可用 |
| `/dycollection_query 任务ID` | 查询抖音点赞/收藏任务结果 | 可用 |
| `/dyck` | 生成抖音登录二维码，并返回 Cookie 下载链接 | 可用 |

## 功能说明

### 聚合解析

- 指令：`/jx 分享链接`
- 处理函数：[`MediaParserPlugin.aggregate_parse()`](main.py:44)
- 特性：
  - 调用聚合解析接口
  - 自动识别常见平台
  - 仅返回视频和图片链接
  - 尽量过滤无关字段
  - 图集内容支持发送群/私聊合并转发消息

### 火山方舟文生图

- 指令：`/画 prompt描述`
- 别名：`/draw prompt描述`
- 处理函数：[`MediaParserPlugin.ark_image_generate()`](main.py:447)
- 特性：
  - 调用火山方舟 Ark SDK 的 `images.generate`
  - 默认模型为 `doubao-seedream-5-0-260128`
  - 画图过程可能较慢，插件会先回复“正在画图，请稍候…”
  - SDK 调用在线程中执行，避免长时间阻塞事件循环
  - 生成完成后返回图片链接并发送图片

### 抖音主页解析

- 指令：`/dyhome 抖音主页分享文本或链接`
- 处理函数：[`MediaParserPlugin.douyin_profile_parse()`](main.py:63)
- 特性：
  - 采集前先调用轻量账号资料接口获取作者昵称、作品数量和 `sec_user_id`
  - 相同/相似作者且本地已有不同 `sec_user_id` 时，会提示使用 `/dyconfirm` 继续或 `/dyskip` 跳过
  - 如果匹配到的本地记录全部没有 `sec_user_id`，不会询问，会直接采集并在记录中补齐新身份标识
  - 调用抖音主页解析接口拉取作品数据
  - 将作品链接保存为本地 TXT 文件
  - 自动记录已解析过的主页分享文本/链接、作者、作品数量、TXT 文件名和 `sec_user_id`
  - 回复中仅保留下载信息与文件信息
  - 生产环境推荐独立部署使用，以获得更稳定的解析效果

### 抖音主页播放

- 菜单查看：`/dymenu`
- 指定播放：`/dyplay 文件名`
- 跨主页随机播放：`/dyrand`
- 说明：
  - `/dymenu` 会列出 `downloads` 目录下已有的抖音主页 TXT 菜单
  - `/dyplay` 会播放指定 TXT 文件中的视频，播放模式由 [`douyin_profile_play_mode`](_conf_schema.json) 控制
  - `/dyrand` 不需要文件名，每次执行会自动切换到下一个主页 TXT，并从该主页里随机抽一个视频发送
  - 当轮到的 TXT 为空时，会自动尝试下一个主页文件

### 抖音 HTTP API

开启 HTTP API 后，可通过浏览器或其他程序读取本地抖音视频数据：

- 开启：`/dyapi_on`
- 关闭：`/dyapi_off`
- 状态：`/dyapi_status`
- 随机播放 JSON：`/api?type=json`
- 随机播放文本：`/api?type=text`
- 随机播放视频重定向：`/api?type=video`
- 指定文件与序号：`/api?type=json&file=文件名&index=1`
- 菜单文本：`/api?type=menu`

说明：

- `file` 或 `name` 可指定本地 TXT 文件名，也可使用不带 `.txt` 的名称
- `index` 或 `video_index` 为 1 开始的视频序号
- 指定播放时会返回指定 TXT 中对应序号的视频；未指定时保留原有随机播放行为
- `type=menu` 会返回与 `/dymenu` 类似的视频系列菜单文本，便于外部系统展示可选系列

### 管理指令白名单

`/dyupdate` 和 `/dytarget` 属于管理类指令，支持配置白名单限制使用权限：

- 配置项 [`admin_user_ids`](_conf_schema.json)：填写允许使用这两条指令的用户 ID（如 QQ 号），每行一个
- **留空则不限制**，任何人均可使用
- 不在白名单中的用户触发这两条指令时，会收到提示 `⛔ 无权限：仅白名单用户可执行此指令`
- 其他指令（`/jx`、`/dyhome`、`/dyplay`、`/dyrand` 等）不受白名单限制

配置示例：

```json
{
  "admin_user_ids": ["123456789", "987654321"]
}
```

### 抖音主页批量更新

- 指令：`/dyupdate`
- 处理方式：按已记录的主页顺序逐个重新请求解析接口
- 特性：
  - 已解析过的主页会持久化记录到 [`douyin_profile_records.json`](douyin_profile_records.json)
  - 更新前先调用 `account_profile.php` 轻量查询远端作品数量
  - 远端作品数与本地 `count` 对比后，只有新增数量达到 [`douyin_profile_update_min_new_count`](_conf_schema.json) 才执行完整更新
  - 默认阈值为 `1`，表示发现至少 1 个新作品就更新；设为 `2` 时，新增 1 个作品会跳过，新增 2 个及以上才更新
  - 全部更新完成后统一回复一条汇总消息，不逐条发送
  - 汇总包含：成功数、跳过数、失败数及失败主页名称
  - 某个主页失败不会中断后续主页更新

### 失败重试

- 指令：`/dyretry`
- 说明：
  - 每次执行 `/dyupdate` 或定时自动更新时，失败的主页会被自动记录到 [`douyin_update_failures.json`](douyin_update_failures.json)
  - 执行 `/dyretry` 仅重试上次失败的主页，不重新运行全量
  - 重试成功后该主页的失败记录会被自动清除
  - 若无失败记录，指令会提示 “无失败记录，无需重试”
  - **如果一个主页连续失败 ≥ 3 次**，更新汇总或自动推送消息中会单独列出该主页名称，提示更换分享链接

### 抖音主页单个更新

- 指令：`/dyupdateone 作者名或文件名`
- 用途：按记录中的作者名或 TXT 文件名匹配单个主页，然后只更新这一条
- 说明：
  - 支持直接输入作者名
  - 也支持输入 TXT 文件名或去掉 `.txt` 后的名称
  - 匹配到后会只更新这一条记录并单独返回结果

### 旧主页补录

- 指令：`/dytrack 抖音主页分享文本或链接`
- 用途：把历史上已经解析过、但当时还没有记录下来的主页手动补录进更新列表
- 说明：
  - 该命令只负责登记主页链接，不会立即发起解析
  - 补录成功后，可通过 [`/dyupdate`](README.md) 手动更新
  - 也可等待插件配置中的自动更新时间点触发自动更新

### 抖音主页定时自动更新

- 触发方式：通过配置中的时间点自动执行
- 行为说明：
  - 自动更新使用与 [`/dyupdate`](README.md) 相同的主页记录
  - 每天到达设定时间后自动执行一轮串行更新
  - 为避免重复触发，同一天只会执行一次
  - 自动更新结果会写入插件日志，已有主页记录仍然全部兼容，无需重新录入
  - 自动更新时间不是聊天命令设置，而是在插件配置中设置
  - 自动更新支持在每个主页请求之间增加等待间隔，降低后端短时间高频请求风险
  - 如需自动更新结束后主动推送汇总消息，请先在目标群聊/频道执行一次 [`/dytarget`](README.md)

#### 提前预检机制

自动更新支持在正式更新前提前进行一轮轻量预检：

- 配置项 [`douyin_profile_pre_check_minutes`](_conf_schema.json) 控制提前多少分钟预检，默认 30 分钟
- 预检仅调用轻量接口查询作品数量，不拉取完整数据
- 配置项 [`douyin_profile_update_min_new_count`](_conf_schema.json) 控制触发完整更新所需的最小新增作品数，默认 1
- 未达到新增阈值的主页会被跳过，正式更新时只处理达到阈值的主页，大幅减少 API 调用量
- 预检失败不影响正式更新，会自动回退为逐条判断/完整更新模式
- 设为 `0` 表示不提前预检，在正式更新时逐条判断

示例（更新时间 03:30，预检提前 30 分钟）：

```
03:00  预检扫描（轻量，仅查询作品数）
03:30  正式更新（只更新预检发现有新作品的主页）
```

#### 多实例自动错峰

当多个用户部署了同一个插件并使用相同的 API Key 时，若自动更新时间相近，可能造成 API 短时间内集中请求，导致 Cookie 失效。

插件通过以下机制自动规避：

- **首次启动**时会随机生成一个 `0~59` 分钟的偏移值并永久保存
- 实际触发时间 = 配置时间 + 偏移，不同实例自然错开
- 偏移值持久化保存，重启后不会变动
- 可通过 `/dyhelp` 查看本实例的实际触发时间，例如：`03:30 → 实际 03:47（错峰偏移 17 分钟）`

> [!NOTE]
> 最根本的解决方案是每个部署实例申请独立的 API Key，互不干扰。

示例返回：

```text
✅ 成功获取 93 个视频链接，文件已保存到 downloads 文件夹
📁 文件名: 某某_videos.txt
📥 下载链接：http://DOUYIN.ZHCNLI.CN/download.php?file=某某_videos.txt
```

相关实现：

- 输出格式化：[`MediaParserPlugin._format_douyin_profile_result()`](main.py:276)
- 文件名提取：[`MediaParserPlugin._extract_file_name()`](main.py:471)
- 域名转大写：[`MediaParserPlugin._uppercase_domain()`](main.py:542)

### 抖音点赞/收藏解析

- 提交指令：`/dycollection [favorite|collection]`
- 查询指令：`/dycollection_query 任务ID`
- 说明：
  - 通过 [`douyin_account_cookie`](_conf_schema.json) 提交后台任务
  - `favorite` 表示喜欢作品，`collection` 表示收藏作品
  - 仅支持解析当前 Cookie 对应账号自己的点赞/收藏内容
  - 若配置了 [`collection_email`](_conf_schema.json)，提交任务时会附带 `email` 参数作为异步完成通知邮箱
  - 解析完成后返回 TXT 下载链接
  - 如需更稳定长期使用，推荐独立部署

#### Cookie 获取说明

可直接使用扫码指令获取 Cookie：

- 指令：`/dyck`
- 处理函数：[`MediaParserPlugin.douyin_cookie_login()`](main.py:379)
- 行为：生成抖音登录二维码，扫码登录成功后返回 `https://login.zhcnli.cn/api/download/file?id=会话ID` 格式的 Cookie 下载链接

也可以手动从浏览器获取：

- 建议使用浏览器无痕模式登录抖音账号后获取，这样拿到的 Cookie 通常更完整、更稳定。
- 按 `F12` 打开开发者工具，进入“网络 / Network”。
- 刷新页面后找到以 `feed` 开头的请求。
- 打开该请求并复制其中完整的 `Cookie` 请求头内容。
- 重点确认 Cookie 中包含 `odin_tt` 字段。

![抖音 Cookie 获取说明](https://blog.zhcnli.com/wp-content/uploads/2026/04/20260410201734697-dycookie.jpg)

## 配置说明

配置界面 schema 位于 [`_conf_schema.json`](_conf_schema.json)。AstrBot 会基于该文件生成图形化配置界面。

> [!IMPORTANT]
> 当前 schema 已移除默认 API Key 与默认收藏参数，使用前必须由部署者自行填写。

### 基础配置

```json
{
  "aggregate_api_key": "你的聚合解析密钥",
  "ark_api_key": "你的火山方舟API Key",
  "douyin_profile_api_key": "你的抖音主页解析密钥",
  "douyin_profile_timeout": 60,
  "douyin_profile_auto_update_enabled": false,
  "douyin_profile_auto_update_time": "03:30",
  "douyin_profile_auto_update_interval": 30,
  "douyin_profile_pre_check_minutes": 30,
  "douyin_profile_update_min_new_count": 1,
  "douyin_account_cookie": "你的抖音登录Cookie",
  "douyin_account_mode": "collection",
  "douyin_account_filename": "我的收藏.txt",
  "api_host": "127.0.0.1",
  "api_port": 8080,
  "api_auto_start": false
}
```

字段说明：

- [`aggregate_api_key`](_conf_schema.json)：聚合解析接口密钥
- [`aggregate_image_send_mode`](_conf_schema.json)：图集发送模式，默认 `separate` 逐张发送；可设为 `forward` 尝试合并转发
- [`forward_node_uin`](_conf_schema.json)：图集合并转发显示使用的 QQ 号，仅在 `forward` 模式下生效
- [`forward_node_name`](_conf_schema.json)：图集合并转发显示使用的名称，仅在 `forward` 模式下生效
- [`ark_api_key`](_conf_schema.json)：火山方舟 API Key，用于 `/画` 和 `/draw` 文生图指令
- [`ark_base_url`](_conf_schema.json)：火山方舟 API 地址，默认北京地域
- [`ark_image_model`](_conf_schema.json)：文生图模型 ID，默认 `doubao-seedream-5-0-260128`
- [`ark_image_size`](_conf_schema.json)：文生图尺寸参数，默认 `2K`
- [`ark_image_watermark`](_conf_schema.json)：是否开启方舟图片水印
- [`douyin_profile_api_key`](_conf_schema.json)：抖音主页解析接口密钥
- [`douyin_profile_timeout`](_conf_schema.json)：主页解析超时时间（秒）
- [`douyin_profile_auto_update_enabled`](_conf_schema.json)：是否开启定时自动更新
- [`douyin_profile_auto_update_time`](_conf_schema.json)：每天自动更新的时间点，格式 `HH:MM`
- [`douyin_profile_auto_update_interval`](_conf_schema.json)：自动更新时相邻两个主页请求之间的等待秒数，默认 30 秒，实际会在 `[interval, interval×2]` 范围随机抖动
- [`douyin_profile_pre_check_minutes`](_conf_schema.json)：正式更新前提前预检的分钟数，默认 30，设为 0 不预检
- [`douyin_profile_update_min_new_count`](_conf_schema.json)：触发完整更新的最小新增作品数，默认 1；例如设为 2 时新增 1 个作品会跳过，新增 2 个及以上才更新
- [`admin_user_ids`](_conf_schema.json)：管理类指令（`/dyupdate`、`/dytarget`）白名单用户 ID 列表，留空不限制
- [`douyin_profile_auto_update_push_hint`](_conf_schema.json)：自动更新主动推送绑定提示
- [`douyin_account_cookie`](_conf_schema.json)：抖音账号 Cookie，仅用于解析当前账号自己的点赞/收藏内容
- [`douyin_account_mode`](_conf_schema.json)：`/dycollection` 默认模式，支持 `favorite` 和 `collection`
- [`douyin_account_filename`](_conf_schema.json)：点赞/收藏导出 TXT 文件名

补充说明：

- 如果旧版本解析主页时还没有建立记录文件，可使用 `/dytrack 抖音主页分享文本或链接` 手动补录。
- 自动更新时间点统一在 AstrBot 的插件配置界面中设置，对应字段为 [`douyin_profile_auto_update_time`](_conf_schema.json:66)。
- 如果需要自动更新完成后主动通知固定会话，请先在目标会话执行 [`/dytarget`](README.md) 绑定，插件会保存该会话的 `unified_msg_origin` 并在后台任务完成后主动推送汇总消息。

### 聚合解析图集发送

- 当 [`/jx`](README.md) 解析到图集或多图内容时：
  - 会先发送一条简要解析说明
  - 默认使用逐张图片发送，避免 NapCat 合并转发出现“生成节点为空”导致整条消息失败
  - 可通过 [`aggregate_image_send_mode`](_conf_schema.json) 切换发送模式：
    - `separate`：逐张图片发送（默认，兼容性最高）
    - `forward`：尝试群/私聊合并转发，失败后自动降级为逐张发送
- OneBot v11 场景下，如启用 `forward` 模式，可通过配置项设置合并转发显示身份：
  - [`forward_node_uin`](_conf_schema.json)
  - [`forward_node_name`](_conf_schema.json)

API Key 获取地址：

- 聚合解析 API Key：前往 <https://api.zhcnli.cn/> 获取
- 抖音主页解析 / 抖音收藏解析 API Key：前往 <https://douyin.zhcnli.cn/apikey_apply.php> 获取

### 扩展配置

```json
{
  "collection_email": "your@example.com",
  "collection_filename": "自定义收藏文件名",
  "debug_mode": false
}
```

字段说明：

- [`collection_email`](_conf_schema.json)：抖音点赞/收藏任务提交时使用的异步通知邮箱
- [`collection_filename`](_conf_schema.json)：抖音收藏任务提交时使用的导出文件名
- [`debug_mode`](_conf_schema.json)：是否开启调试模式

说明：

- 抖音主页解析与抖音点赞/收藏解析在生产环境中推荐独立部署，以获得更稳定的可用性与接口控制能力。
- 火山方舟文生图需要先安装 `volcengine-python-sdk[ark]`，并在插件配置中填写 [`ark_api_key`](_conf_schema.json)。
- 如果未填写 [`aggregate_api_key`](_conf_schema.json)、[`ark_api_key`](_conf_schema.json) 或 [`douyin_profile_api_key`](_conf_schema.json)，对应指令会直接提示“未配置 API 密钥”。

## 文件结构

- [`main.py`](main.py)：插件主逻辑
- [`douyin_profile_records.json`](douyin_profile_records.json)：已解析主页记录文件（运行后自动生成）
- [`metadata.yaml`](metadata.yaml)：插件元数据
- [`README.md`](README.md)：项目说明文档
- [`_conf_schema.json`](_conf_schema.json)：AstrBot 配置界面 schema

## 赞助

如果这个项目对你有帮助，欢迎赞助支持。

> [!NOTE]
> 欢迎赞助支持项目持续维护与更新。

![赞助二维码](https://blog.zhcnli.com/wp-content/uploads/2026/04/20260419122856231-sponsor-placeholder.jpg)

## 相关链接

- [独立部署说明](https://blog.zhcnli.com/876.html)
- [聚合解析 API Key 获取](https://api.zhcnli.cn/)
- [抖音主页 / 收藏解析 API Key 获取](https://douyin.zhcnli.cn/apikey_apply.php)
- [AstrBot Repo](https://github.com/AstrBotDevs/AstrBot)
- [AstrBot Plugin Development Docs (Chinese)](https://docs.astrbot.app/dev/star/plugin-new.html)
- [AstrBot Plugin Development Docs (English)](https://docs.astrbot.app/en/dev/star/plugin-new.html)
