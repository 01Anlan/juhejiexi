# astrbot_plugin_juhejiexi

AstrBot 聚合解析插件，支持抖音(实况)，快手，小红书(实况)，哔站，人民日报，豆包，即梦，通义千问解析。

## 功能概览

- 聚合解析
- 抖音主页解析
- 抖音收藏解析（推荐独立部署，独立部署更稳定）
- OneBot 协议下普通消息自动识别分享链接并聚合解析

核心实现位于 [`main.py`](README.md)，插件元数据位于 [`metadata.yaml`](metadata.yaml)。

## 支持的指令

| 指令 | 说明 | 状态 |
| --- | --- | --- |
| `/jx 分享链接` | 手动触发聚合解析，返回视频或图片资源链接 | 可用 |
| `/dyhome 抖音主页分享文本或链接` | 解析抖音主页并生成本地 TXT 文件 | 可用 |
| `/dytrack 抖音主页分享文本或链接` | 将旧主页链接补录进更新记录 | 可用 |
| `/dytarget` | 绑定自动更新完成后的主动推送目标会话 | 可用 |
| `/dyplay 文件名` | 顺序播放指定 TXT 中的视频链接 | 可用 |
| `/dyupdate` | 串行更新所有已记录的抖音主页，并逐条回报结果 | 可用 |
| `/dyupdateone 作者名或文件名` | 按名字匹配单个主页记录并更新 | 可用 |
| `/dycollection [favorite\|collection]` | 提交抖音点赞/收藏后台解析任务 | 可用 |
| `/dycollection_query 任务ID` | 查询抖音点赞/收藏任务结果 | 可用 |

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

### OneBot 自动聚合解析

- 触发方式：OneBot 协议下，普通消息中包含分享链接时自动触发
- 处理函数：[`MediaParserPlugin.auto_aggregate_parse_for_onebot()`](main.py:44)
- 规则：
  - 仅在 OneBot 场景下生效
  - 无需输入 [`/jx`](README.md:17)
  - 普通消息里只要识别到链接，就会自动按聚合解析逻辑回复
  - 以 `/` 开头的命令消息不会触发自动解析，避免和其他指令冲突
  - [`/dyhome`](README.md:18)、[`/dyupdate`](README.md:22)、[`/dyupdateone`](README.md:23)、[`/dytarget`](README.md:20)、[`/dytrack`](README.md:19)、[`/dymenu`](README.md:21)、[`/dyplay`](README.md:21)、[`/dycollection`](README.md:24)、[`/dycollection_query`](README.md:25) 等文本指令也不会被当作自动解析消息处理

### 抖音主页解析

- 指令：`/dyhome 抖音主页分享文本或链接`
- 处理函数：[`MediaParserPlugin.douyin_profile_parse()`](main.py:63)
- 特性：
  - 调用抖音主页解析接口
  - 将作品链接保存为本地 TXT 文件
  - 自动记录已解析过的主页分享文本/链接
  - 回复中仅保留下载信息与文件信息
  - 生产环境推荐独立部署使用，以获得更稳定的解析效果

### 抖音主页批量更新

- 指令：`/dyupdate`
- 处理方式：按已记录的主页顺序逐个重新请求解析接口
- 特性：
  - 已解析过的主页会持久化记录到 [`douyin_profile_records.json`](douyin_profile_records.json)
  - 后台按顺序轮播更新，一次只更新一个主页
  - 每完成一个主页更新，就立即回复一条结果
  - 某个主页失败不会中断后续主页更新

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
  "douyin_profile_api_key": "你的抖音主页解析密钥",
  "douyin_profile_timeout": 60,
  "douyin_profile_auto_update_enabled": false,
  "douyin_profile_auto_update_time": "03:30",
  "douyin_account_cookie": "你的抖音登录Cookie",
  "douyin_account_mode": "collection",
  "douyin_account_filename": "我的收藏.txt"
}
```

字段说明：

- [`aggregate_api_key`](_conf_schema.json)：聚合解析接口密钥
- [`forward_node_uin`](_conf_schema.json)：图集合并转发显示使用的 QQ 号
- [`forward_node_name`](_conf_schema.json)：图集合并转发显示使用的名称
- [`douyin_profile_api_key`](_conf_schema.json)：抖音主页解析接口密钥
- [`douyin_profile_timeout`](_conf_schema.json)：主页解析超时时间（秒）
- [`douyin_profile_auto_update_enabled`](_conf_schema.json)：是否开启定时自动更新
- [`douyin_profile_auto_update_time`](_conf_schema.json)：每天自动更新的时间点，格式 `HH:MM`
- [`douyin_profile_auto_update_interval`](_conf_schema.json)：自动更新时相邻两个主页请求之间的等待秒数，默认 30 秒
- [`douyin_profile_auto_update_push_hint`](_conf_schema.json)：自动更新主动推送绑定提示
- [`douyin_account_cookie`](_conf_schema.json)：抖音账号 Cookie，仅用于解析当前账号自己的点赞/收藏内容
- [`douyin_account_mode`](_conf_schema.json)：`/dycollection` 默认模式，支持 `favorite` 和 `collection`
- [`douyin_account_filename`](_conf_schema.json)：点赞/收藏导出 TXT 文件名

补充说明：

- 如果旧版本解析主页时还没有建立记录文件，可使用 `/dytrack 抖音主页分享文本或链接` 手动补录。
- 自动更新时间点统一在 AstrBot 的插件配置界面中设置，对应字段为 [`douyin_profile_auto_update_time`](_conf_schema.json:38)。
- 如果需要自动更新完成后主动通知固定会话，请先在目标会话执行 [`/dytarget`](README.md) 绑定，插件会保存该会话的 `unified_msg_origin` 并在后台任务完成后主动推送汇总消息。

### 聚合解析图集合并转发

- 当 [`/jx`](README.md) 解析到图集或多图内容时：
  - 会先发送一条简要解析说明
  - 再发送一条群/私聊合并转发消息
  - 该合并转发消息只包含 **一个** [`Node`](main.py) 节点，节点内容里合并全部图片，而不是一张图片一个节点
- OneBot v11 场景下，可通过配置项设置合并转发显示身份：
  - [`forward_node_uin`](_conf_schema.json)
  - [`forward_node_name`](_conf_schema.json)

API Key 获取地址：

- 聚合解析 API Key：前往 <https://api.zhcnli.com/> 获取
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
- 如果未填写 [`aggregate_api_key`](_conf_schema.json) 或 [`douyin_profile_api_key`](_conf_schema.json)，对应指令会直接提示“未配置 API 密钥”。

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
- [聚合解析 API Key 获取](https://api.zhcnli.com/)
- [抖音主页 / 收藏解析 API Key 获取](https://douyin.zhcnli.cn/apikey_apply.php)
- [AstrBot Repo](https://github.com/AstrBotDevs/AstrBot)
- [AstrBot Plugin Development Docs (Chinese)](https://docs.astrbot.app/dev/star/plugin-new.html)
- [AstrBot Plugin Development Docs (English)](https://docs.astrbot.app/en/dev/star/plugin-new.html)
