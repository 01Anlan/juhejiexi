import json
import os
import random
import re
import time
import asyncio
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.parse import quote, unquote
from urllib.request import Request, urlopen

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event import MessageChain
from astrbot.api.message_components import Image, Node, Plain, Video
from astrbot.api.star import Context, Star, register


AGGREGATE_API = (
    "https://api.zhcnli.com/api/jhjx/index.php"
    "?ckey={apikey}&url="
)
DOUYIN_PROFILE_API = (
    "http://douyin.zhcnli.cn/api.php"
    "?apikey={apikey}&url="
)
DOUYIN_COLLECTION_API = (
    "https://douyin.zhcnli.cn/account_cookie.php"
    "?apikey={apikey}"
)
URL_PATTERN = re.compile(r"https?://[^\s]+", re.IGNORECASE)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_DIR = os.path.join(BASE_DIR, "downloads")
PROFILE_RECORD_FILE = os.path.join(BASE_DIR, "douyin_profile_records.json")
AUTO_UPDATE_TARGET_FILE = os.path.join(BASE_DIR, "auto_update_target.json")
AUTO_UPDATE_STATE_FILE = os.path.join(BASE_DIR, "auto_update_state.json")
DEFAULT_REQUEST_TIMEOUT = 20
DEFAULT_DOUYIN_PROFILE_TIMEOUT = 60
DEFAULT_AUTO_UPDATE_INTERVAL = 30


@register("astrbot_plugin_juhejiexi", "Anlan", "聚合解析与抖音主页解析插件", "v5.2.3")
class MediaParserPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.play_indexes: Dict[str, int] = {}
        self.play_history: Dict[str, List[int]] = {}
        self.profile_records: List[Dict[str, Any]] = self._load_profile_records()
        self.auto_update_target = self._load_auto_update_target()
        self.auto_update_state = self._load_auto_update_state()
        self.auto_update_task: Optional[asyncio.Task] = None
        self.auto_update_running = False
        self.last_auto_update_date = str(self.auto_update_state.get("last_auto_update_date") or "")
        self.last_auto_update_check_date = str(self.auto_update_state.get("last_auto_update_check_date") or "")
        self.config.save_config()

    async def initialize(self):
        logger.info("media_parser 插件已初始化")
        if not self.auto_update_task or self.auto_update_task.done():
            self.auto_update_task = asyncio.create_task(self._auto_update_loop())

    async def auto_aggregate_parse_for_onebot(self, event: AstrMessageEvent):
        """OneBot 场景下自动识别普通消息中的链接并执行聚合解析。"""
        if not self._should_auto_parse_onebot_message(event):
            return

        async for result in self._handle_aggregate_parse(event, require_command=False):
            yield result

    @filter.command("jx")
    async def aggregate_parse(self, event: AstrMessageEvent):
        """聚合解析：输入分享链接，优先直接发送视频；图集/多图使用合并转发节点发送。"""
        async for result in self._handle_aggregate_parse(event, require_command=True):
            yield result

    @filter.command("dyhome")
    async def douyin_profile_parse(self, event: AstrMessageEvent):
        """抖音主页解析：输入抖音主页分享文本或链接，返回作品链接与 TXT 下载地址。"""
        raw_text = self._extract_profile_text(event.message_str)
        if not raw_text:
            yield event.plain_result("用法：/dyhome 抖音主页分享文本或链接")
            return

        douyin_profile_api_key = self.config.get("douyin_profile_api_key", "")
        if not douyin_profile_api_key:
            yield event.plain_result("未配置抖音主页解析 API 密钥")
            return

        try:
            api_url = self._build_douyin_profile_api(raw_text, douyin_profile_api_key)
            profile_timeout = self._get_douyin_profile_timeout()
            payload = self._request_json(api_url, timeout=profile_timeout)
            self._upsert_profile_record(raw_text, payload)
        except Exception as exc:
            logger.exception("抖音主页解析失败: %s", exc)
            yield event.plain_result(f"抖音主页解析失败：{exc}")
            return

        download_info = self._save_profile_txt(payload)
        message = self._format_douyin_profile_result(payload, download_info)
        yield event.plain_result(message)

    @filter.command("dyupdate")
    async def douyin_profile_update_all(self, event: AstrMessageEvent):
        """按记录顺序逐个更新已解析过的抖音主页。"""
        if not self.profile_records:
            yield event.plain_result("暂无已记录的抖音主页，先使用 /dyhome 解析后再执行 /dyupdate")
            return

        douyin_profile_api_key = self.config.get("douyin_profile_api_key", "")
        if not douyin_profile_api_key:
            yield event.plain_result("未配置抖音主页解析 API 密钥")
            return

        total = len(self.profile_records)
        yield event.plain_result(f"开始后台顺序更新 {total} 个抖音主页记录")

        for message in self._iter_profile_update_messages(douyin_profile_api_key):
            yield event.plain_result(message)

    @filter.command("dyupdateone")
    async def douyin_profile_update_one(self, event: AstrMessageEvent):
        """按作者名或文件名匹配单个主页记录并更新。"""
        keyword = self._extract_dyupdateone_text(event.message_str)
        if not keyword:
            yield event.plain_result("用法：/dyupdateone 作者名或文件名")
            return

        if not self.profile_records:
            yield event.plain_result("暂无已记录的抖音主页，先使用 /dyhome 解析后再执行 /dyupdateone")
            return

        douyin_profile_api_key = self.config.get("douyin_profile_api_key", "")
        if not douyin_profile_api_key:
            yield event.plain_result("未配置抖音主页解析 API 密钥")
            return

        matched_record = self._find_profile_record_by_keyword(keyword)
        if not matched_record:
            yield event.plain_result(f"未找到匹配记录：{keyword}")
            return

        display_name = str(matched_record.get("author") or matched_record.get("file_name") or keyword).strip()
        yield event.plain_result(f"开始更新：{display_name}")
        result_message = self._update_single_profile_record(douyin_profile_api_key, matched_record, index=1, total=1)
        yield event.plain_result(result_message)

    @filter.command("dytarget")
    async def bind_auto_update_target(self, event: AstrMessageEvent):
        """绑定自动更新结果主动推送目标会话。"""
        unified_msg_origin = str(getattr(event, "unified_msg_origin", "") or "").strip()
        if not unified_msg_origin:
            yield event.plain_result("当前会话不支持绑定自动更新推送目标")
            return

        self.auto_update_target = {"unified_msg_origin": unified_msg_origin}
        self._save_auto_update_target()
        yield event.plain_result(
            "✅ 已绑定自动更新推送会话\n"
            "📌 后续定时自动更新完成后，会主动向当前会话发送一条汇总消息"
        )

    @filter.command("dytrack")
    async def douyin_profile_track(self, event: AstrMessageEvent):
        """补录旧的抖音主页分享文本或链接到更新记录中。"""
        raw_text = self._extract_dytrack_text(event.message_str)
        if not raw_text:
            yield event.plain_result("用法：/dytrack 抖音主页分享文本或链接")
            return

        record = self._upsert_profile_record(raw_text)
        yield event.plain_result(
            "✅ 已加入主页更新记录\n"
            f"🔗 主页：{self._sanitize_markdown_text(str(record.get('raw_text', '') or ''))}\n"
            "📌 之后可通过 /dyupdate 或定时自动更新进行刷新"
        )

    @filter.command("dymenu")
    async def douyin_profile_menu(self, event: AstrMessageEvent):
        """展示已保存的抖音主页播放菜单。"""
        txt_files = self._list_download_txt_files()
        if not txt_files:
            yield event.plain_result(
                "┏━🎵 抖音主页菜单 ━┓\n"
                "  暂无可播放的主页记录\n"
                "  先用 /dyhome 解析主页\n"
                "  再用 /dyplay 文件名 播放\n"
                "┗━━━━━━━━━━━━━━┛"
            )
            return

        yield event.plain_result(self._format_douyin_menu(txt_files))

    @filter.command("dyplay")
    async def douyin_profile_play(self, event: AstrMessageEvent):
        """每次调用播放 TXT 中的一个视频链接。"""
        file_key = self._extract_dyplay_text(event.message_str)
        if not file_key:
            yield event.plain_result("用法：/dyplay 文件名")
            return

        file_path = self._find_download_txt(file_key)
        if not file_path:
            yield event.plain_result(f"未找到文件：{file_key}")
            return

        try:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as file:
                urls = [line.strip() for line in file if line.strip()]
        except Exception as exc:
            yield event.plain_result(f"读取文件失败：{exc}")
            return

        if not urls:
            yield event.plain_result("TXT 文件为空")
            return

        play_mode = str(self.config.get("douyin_profile_play_mode", "sequential") or "sequential").strip().lower()
        if play_mode == "random":
            current_index = self._pick_random_play_index(file_path, len(urls))
        else:
            current_index = self.play_indexes.get(file_path, 0)
            if current_index >= len(urls):
                current_index = 0
            self.play_indexes[file_path] = current_index + 1

        current_url = urls[current_index]

        playable_url = self._resolve_direct_media_url(current_url)
        play_mode_label = "随机播放" if play_mode == "random" else "顺序播放"
        message_chain = [
            Plain(
                f"🎬 正在播放：{os.path.basename(file_path)}\n"
                f"▶️ 播放模式：{play_mode_label}\n"
                f"📍 当前进度：{current_index + 1}/{len(urls)}\n"
                f"🔗 视频直链：{playable_url}"
            ),
            Video.fromURL(playable_url),
        ]
        yield event.chain_result(message_chain)

    @filter.command("dycollection")
    async def douyin_collection_parse(self, event: AstrMessageEvent):
        """抖音点赞/收藏解析：基于已配置的账号 Cookie 提交后台任务。"""
        douyin_profile_api_key = self.config.get("douyin_profile_api_key", "")
        if not douyin_profile_api_key:
            yield event.plain_result("未配置抖音主页解析 API 密钥")
            return

        cookie = str(self.config.get("douyin_account_cookie", "") or "").strip()
        if not cookie:
            yield event.plain_result("未配置抖音账号 Cookie，无法解析点赞/收藏内容")
            return

        mode = self._extract_collection_mode(event.message_str) or str(
            self.config.get("douyin_account_mode", "collection") or "collection"
        ).strip().lower()
        if mode not in {"favorite", "collection"}:
            yield event.plain_result("模式无效，仅支持 favorite（点赞）或 collection（收藏）")
            return

        filename = self._build_account_export_filename(mode)
        email = str(self.config.get("collection_email", "") or "").strip()
        try:
            submit_api = self._build_account_cookie_submit_api(douyin_profile_api_key, cookie, mode, filename, email)
            payload = self._request_json(submit_api, timeout=self._get_douyin_profile_timeout())
        except Exception as exc:
            logger.exception("抖音点赞/收藏任务提交失败: %s", exc)
            yield event.plain_result(f"抖音点赞/收藏任务提交失败：{exc}")
            return

        yield event.plain_result(self._format_account_cookie_submit_result(payload, mode, filename, email))

    @filter.command("dycollection_query")
    async def douyin_collection_query(self, event: AstrMessageEvent):
        """查询抖音点赞/收藏后台任务状态。"""
        job_id = self._extract_collection_query_text(event.message_str)
        if not job_id:
            yield event.plain_result("用法：/dycollection_query 任务ID")
            return

        douyin_profile_api_key = self.config.get("douyin_profile_api_key", "")
        if not douyin_profile_api_key:
            yield event.plain_result("未配置抖音主页解析 API 密钥")
            return

        try:
            query_api = self._build_account_cookie_query_api(douyin_profile_api_key, job_id)
            payload = self._request_json(query_api, timeout=self._get_douyin_profile_timeout())
        except Exception as exc:
            logger.exception("抖音点赞/收藏任务查询失败: %s", exc)
            yield event.plain_result(f"抖音点赞/收藏任务查询失败：{exc}")
            return

        yield event.plain_result(self._format_account_cookie_query_result(payload, job_id))

    def _request_json(self, url: str, timeout: Optional[int] = None) -> Dict[str, Any]:
        request = Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            },
        )

        request_timeout = timeout if isinstance(timeout, (int, float)) and timeout > 0 else DEFAULT_REQUEST_TIMEOUT
        with urlopen(request, timeout=request_timeout) as response:
            content = response.read().decode("utf-8", errors="ignore")

        payload = json.loads(content)
        if not isinstance(payload, dict):
            raise ValueError("接口返回格式异常")
        return payload

    def _get_douyin_profile_timeout(self) -> int:
        raw_timeout = self.config.get("douyin_profile_timeout", DEFAULT_DOUYIN_PROFILE_TIMEOUT)
        try:
            timeout = int(raw_timeout)
        except (TypeError, ValueError):
            timeout = DEFAULT_DOUYIN_PROFILE_TIMEOUT

        return max(timeout, DEFAULT_REQUEST_TIMEOUT)

    def _build_douyin_profile_api(self, raw_text: str, api_key: str) -> str:
        return (
            DOUYIN_PROFILE_API.format(apikey=quote(api_key, safe=""))
            + quote(raw_text, safe="")
            + "&type=1&post=mode&xz=1"
        )

    def _get_auto_update_interval(self) -> int:
        raw_interval = self.config.get("douyin_profile_auto_update_interval", DEFAULT_AUTO_UPDATE_INTERVAL)
        try:
            interval = int(raw_interval)
        except (TypeError, ValueError):
            interval = DEFAULT_AUTO_UPDATE_INTERVAL

        return max(interval, 0)

    def _get_forward_node_uin(self) -> int:
        raw_uin = self.config.get("forward_node_uin", 0)
        try:
            return int(raw_uin)
        except (TypeError, ValueError):
            return 0

    def _get_forward_node_name(self, fallback_name: str) -> str:
        configured_name = str(self.config.get("forward_node_name", "") or "").strip()
        return configured_name or fallback_name or "聚合解析助手"

    def _build_aggregate_image_forward_node(self, data: Dict[str, Any], image_urls: List[str]) -> Node:
        fallback_name = str(data.get("author") or data.get("title") or "聚合解析助手").strip() or "聚合解析助手"
        node_name = self._get_forward_node_name(fallback_name)
        node_uin = self._get_forward_node_uin()
        content = [Plain(f"图集共 {len(image_urls)} 张")]
        content.extend(Image.fromURL(image_url) for image_url in image_urls)
        return Node(uin=node_uin, name=node_name, content=content)

    def _supports_forward_node(self, event: AstrMessageEvent) -> bool:
        unified_msg_origin = str(getattr(event, "unified_msg_origin", "") or "").lower()
        return any(
            keyword in unified_msg_origin
            for keyword in ["onebot", "v11", "friendmessage", "groupmessage"]
        )

    def _is_onebot_event(self, event: AstrMessageEvent) -> bool:
        unified_msg_origin = str(getattr(event, "unified_msg_origin", "") or "").lower()
        return any(keyword in unified_msg_origin for keyword in ["onebot", "v11"])

    def _should_auto_parse_onebot_message(self, event: AstrMessageEvent) -> bool:
        if not self._is_onebot_event(event):
            return False

        text = str(getattr(event, "message_str", "") or "").strip()
        if not text:
            return False

        lowered_text = text.lower()
        if lowered_text.startswith("/"):
            return False

        if lowered_text.startswith(("jx ", "dyhome", "dyupdate", "dyupdateone", "dytarget", "dytrack", "dymenu", "dyplay", "dycollection", "dycollection_query")):
            return False

        return self._extract_url(text) is not None

    async def _handle_aggregate_parse(self, event: AstrMessageEvent, require_command: bool):
        target_url = self._extract_url(event.message_str)
        if not target_url:
            if require_command:
                yield event.plain_result("用法：/jx 分享链接")
            return

        aggregate_api_key = self.config.get("aggregate_api_key", "")
        if not aggregate_api_key:
            if require_command:
                yield event.plain_result("未配置聚合解析 API 密钥")
            return

        try:
            api_url = AGGREGATE_API.format(apikey=quote(aggregate_api_key, safe="")) + quote(target_url, safe="")
            payload = self._request_json(api_url)
        except Exception as exc:
            logger.exception("聚合解析失败: %s", exc)
            if require_command:
                yield event.plain_result(f"聚合解析失败：{exc}")
            return

        data = payload.get("data") or {}
        message = self._format_aggregate_result(payload)
        video_url = self._pick_video_url(data) if isinstance(data, dict) else None
        image_urls = self._pick_image_urls(data) if isinstance(data, dict) else []

        if video_url:
            playable_url = self._resolve_direct_media_url(video_url)
            yield event.plain_result(message)
            yield event.chain_result([Video.fromURL(playable_url)])
            return

        if image_urls:
            yield event.plain_result(self._format_aggregate_summary(payload, include_image_links=False))
            if self._supports_forward_node(event):
                node = self._build_aggregate_image_forward_node(data if isinstance(data, dict) else {}, image_urls)
                yield event.chain_result([node])
            else:
                for image_url in image_urls:
                    yield event.chain_result([Image.fromURL(image_url)])
            return

        yield event.plain_result(message)

    def _is_auto_update_enabled(self) -> bool:
        return bool(self.config.get("douyin_profile_auto_update_enabled", False))

    def _get_auto_update_time(self) -> str:
        raw_time = str(self.config.get("douyin_profile_auto_update_time", "") or "").strip()
        if re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", raw_time):
            return raw_time
        return ""

    async def _auto_update_loop(self):
        while True:
            try:
                auto_time = self._get_auto_update_time()
                if not self._is_auto_update_enabled() or not auto_time:
                    await asyncio.sleep(30)
                    continue

                now = datetime.now()
                current_date = now.strftime("%Y-%m-%d")
                current_time = now.strftime("%H:%M")
                if self.last_auto_update_check_date != current_date:
                    self.last_auto_update_check_date = current_date
                    self.auto_update_state["last_auto_update_check_date"] = current_date
                    self._save_auto_update_state()
                    await asyncio.sleep(30)
                    continue
                if current_time >= auto_time and self.last_auto_update_date != current_date:
                    await self._run_auto_update_once()
                    self.last_auto_update_date = current_date
                    self.auto_update_state["last_auto_update_date"] = current_date
                    self.auto_update_state["last_auto_update_check_date"] = current_date
                    self._save_auto_update_state()
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.exception("定时自动更新检查失败: %s", exc)
                await asyncio.sleep(30)

    async def _run_auto_update_once(self):
        if self.auto_update_running:
            logger.info("自动更新任务仍在执行，跳过本轮定时更新")
            return
        if not self.profile_records:
            logger.info("定时自动更新跳过：暂无主页记录")
            return

        douyin_profile_api_key = self.config.get("douyin_profile_api_key", "")
        if not douyin_profile_api_key:
            logger.warning("定时自动更新失败：未配置抖音主页解析 API 密钥")
            return

        self.auto_update_running = True
        try:
            total = len(self.profile_records)
            logger.info("开始执行定时自动更新，共 %s 个主页记录", total)
            success_count = 0
            failed_count = 0
            auto_update_interval = self._get_auto_update_interval()
            for index, message in enumerate(self._iter_profile_update_messages(douyin_profile_api_key), start=1):
                if message.startswith("✅"):
                    success_count += 1
                elif message.startswith("❌"):
                    failed_count += 1
                else:
                    logger.info("自动更新跳过项: %s", message.replace("\n", " | "))
                if index < total and auto_update_interval > 0:
                    logger.info("自动更新节流等待 %s 秒后继续下一项", auto_update_interval)
                    await asyncio.sleep(auto_update_interval)
            logger.info(
                "抖音主页自动更新全部完成：共 %s 个，成功 %s 个，失败 %s 个",
                total,
                success_count,
                failed_count,
            )
            await self._notify_auto_update_summary(total, success_count, failed_count)
        finally:
            self.auto_update_running = False

    def _iter_profile_update_messages(self, douyin_profile_api_key: str, records: Optional[List[Dict[str, Any]]] = None):
        target_records = list(records) if records is not None else list(self.profile_records)
        total = len(target_records)
        for index, record in enumerate(target_records, start=1):
            yield self._update_single_profile_record(douyin_profile_api_key, record, index=index, total=total)

    def _update_single_profile_record(self, douyin_profile_api_key: str, record: Dict[str, Any], index: int, total: int) -> str:
        raw_text = str(record.get("raw_text") or "").strip()
        if not raw_text:
            return f"⚠️ 第 {index}/{total} 个记录缺少主页链接，已跳过"

        author = self._normalize_author_name(str(record.get("author") or "").strip()) or f"记录{index}"
        old_urls = self._load_existing_profile_urls(record)
        try:
            api_url = self._build_douyin_profile_api(raw_text, douyin_profile_api_key)
            payload = self._request_json(api_url, timeout=self._get_douyin_profile_timeout())
            download_info = self._save_profile_txt(payload)
            updated_record = self._upsert_profile_record(raw_text, payload)
            count = payload.get("count") or 0
            new_urls = self._extract_profile_urls(payload)
            new_count = len([url for url in new_urls if url not in old_urls])
            file_name = ""
            payload_author = self._normalize_author_name(str(payload.get('author') or "").strip())
            display_author = self._sanitize_markdown_text(payload_author or author)
            if download_info:
                file_name = str(download_info.get("file_name") or "").strip()
            if not file_name:
                file_name = str(updated_record.get("file_name") or "").strip()
            display_file_name = self._sanitize_markdown_text(file_name or "未生成")
            return (
                f"✅ {index}/{total} 更新完成\n"
                f"👤 作者：{display_author}\n"
                f"📦 作品数：{count}\n"
                f"🆕 新增链接：{new_count}\n"
                f"📁 文件：{display_file_name}"
            )
        except Exception as exc:
            logger.exception("批量更新抖音主页失败: %s", exc)
            display_author = self._sanitize_markdown_text(author)
            display_exc = self._sanitize_markdown_text(str(exc))
            return (
                f"❌ {index}/{total} 更新失败\n"
                f"👤 记录：{display_author}\n"
                f"原因：{display_exc}"
            )

    def _sanitize_markdown_text(self, text: str) -> str:
        if not isinstance(text, str) or not text:
            return ""

        sanitized = text.replace("\\", "\\\\")
        for char in ["*", "_", "`", "[", "]", "(", ")", "~", ">", "#", "+", "-", "=", "|", "{", "}", ".", "!"]:
            sanitized = sanitized.replace(char, f"\\{char}")
        return sanitized

    def _load_auto_update_target(self) -> Dict[str, str]:
        if not os.path.exists(AUTO_UPDATE_TARGET_FILE):
            return {}

        try:
            with open(AUTO_UPDATE_TARGET_FILE, "r", encoding="utf-8") as file:
                payload = json.load(file)
        except Exception as exc:
            logger.warning("读取自动更新推送目标失败: %s", exc)
            return {}

        if not isinstance(payload, dict):
            return {}
        return payload

    def _save_auto_update_target(self) -> None:
        try:
            with open(AUTO_UPDATE_TARGET_FILE, "w", encoding="utf-8") as file:
                json.dump(self.auto_update_target, file, ensure_ascii=False, indent=2)
        except Exception as exc:
            logger.warning("保存自动更新推送目标失败: %s", exc)

    def _load_auto_update_state(self) -> Dict[str, str]:
        if not os.path.exists(AUTO_UPDATE_STATE_FILE):
            return {}

        try:
            with open(AUTO_UPDATE_STATE_FILE, "r", encoding="utf-8") as file:
                payload = json.load(file)
        except Exception as exc:
            logger.warning("读取自动更新状态失败: %s", exc)
            return {}

        if not isinstance(payload, dict):
            return {}
        return payload

    def _save_auto_update_state(self) -> None:
        try:
            with open(AUTO_UPDATE_STATE_FILE, "w", encoding="utf-8") as file:
                json.dump(self.auto_update_state, file, ensure_ascii=False, indent=2)
        except Exception as exc:
            logger.warning("保存自动更新状态失败: %s", exc)

    async def _notify_auto_update_summary(self, total: int, success_count: int, failed_count: int):
        unified_msg_origin = str(self.auto_update_target.get("unified_msg_origin") or "").strip()
        if not unified_msg_origin:
            return

        try:
            message_chain = MessageChain([
                Plain(
                    f"✅ 抖音主页自动更新全部完成\n"
                    f"📦 总数：{total}\n"
                    f"✔️ 成功：{success_count}\n"
                    f"❌ 失败：{failed_count}"
                )
            ])
            await self.context.send_message(unified_msg_origin, message_chain)
        except Exception as exc:
            logger.exception("发送自动更新汇总消息失败: %s", exc)

    def _find_profile_record_by_keyword(self, keyword: str) -> Optional[Dict[str, Any]]:
        normalized_keyword = self._sanitize_file_name(keyword).lower()
        raw_keyword = keyword.strip().lower()
        for record in self.profile_records:
            author = self._normalize_author_name(str(record.get("author") or "").strip()).lower()
            file_name = str(record.get("file_name") or "").strip().lower()
            file_stem = os.path.splitext(file_name)[0].strip().lower() if file_name else ""
            normalized_author = self._sanitize_file_name(author).lower() if author else ""
            if raw_keyword in {author, file_name, file_stem}:
                return record
            if normalized_keyword and normalized_keyword in {normalized_author, self._sanitize_file_name(file_stem).lower()}:
                return record
        return None

    def _load_existing_profile_urls(self, record: Dict[str, Any]) -> List[str]:
        file_name = str(record.get("file_name") or "").strip()
        if not file_name:
            author = self._normalize_author_name(str(record.get("author") or "").strip())
            if author:
                file_name = f"{self._sanitize_file_name(author)}.txt"

        if not file_name:
            return []

        file_path = self._find_download_txt(file_name)
        if not file_path or not os.path.exists(file_path):
            return []

        try:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as file:
                return [line.strip() for line in file if line.strip()]
        except Exception:
            return []

    def _extract_profile_urls(self, payload: Dict[str, Any]) -> List[str]:
        data = payload.get("data")
        if not isinstance(data, list):
            return []
        return [item.strip() for item in data if isinstance(item, str) and item.strip()]

    def _load_profile_records(self) -> List[Dict[str, Any]]:
        if not os.path.exists(PROFILE_RECORD_FILE):
            return []

        try:
            with open(PROFILE_RECORD_FILE, "r", encoding="utf-8") as file:
                payload = json.load(file)
        except Exception as exc:
            logger.warning("读取抖音主页记录失败: %s", exc)
            return []

        if not isinstance(payload, list):
            return []

        records: List[Dict[str, Any]] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            raw_text = str(item.get("raw_text") or "").strip()
            if not raw_text:
                continue
            records.append(item)
        return records

    def _save_profile_records(self) -> None:
        try:
            with open(PROFILE_RECORD_FILE, "w", encoding="utf-8") as file:
                json.dump(self.profile_records, file, ensure_ascii=False, indent=2)
        except Exception as exc:
            logger.warning("保存抖音主页记录失败: %s", exc)

    def _upsert_profile_record(self, raw_text: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        normalized_raw_text = raw_text.strip()
        record: Dict[str, Any] = {
            "raw_text": normalized_raw_text,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }

        if isinstance(payload, dict):
            author = self._normalize_author_name(str(payload.get("author") or "").strip())
            file_name = ""
            download = self._extract_download_url(payload)
            if download:
                file_name = self._extract_file_name(download)
            if author:
                record["author"] = author
            record["count"] = payload.get("count") or 0
            if file_name:
                record["file_name"] = file_name

        for index, item in enumerate(self.profile_records):
            if str(item.get("raw_text") or "").strip() == normalized_raw_text:
                merged = {**item, **record}
                self.profile_records[index] = merged
                self._save_profile_records()
                return merged

        self.profile_records.append(record)
        self._save_profile_records()
        return record

    def _extract_url(self, text: str) -> Optional[str]:
        if not text:
            return None

        cleaned_text = text.strip()
        if cleaned_text.startswith("/jx"):
            cleaned_text = cleaned_text[3:].strip()

        match = URL_PATTERN.search(cleaned_text)
        return match.group(0) if match else None

    def _extract_profile_text(self, text: str) -> Optional[str]:
        if not text:
            return None

        cleaned_text = text.strip()
        if cleaned_text.startswith("/dyhome"):
            cleaned_text = cleaned_text[7:].strip()
        elif cleaned_text.startswith("dyhome"):
            cleaned_text = cleaned_text[len("dyhome"):].strip()

        return cleaned_text or None

    def _extract_dytrack_text(self, text: str) -> Optional[str]:
        if not text:
            return None

        cleaned_text = text.strip()
        if cleaned_text.startswith("/dytrack"):
            cleaned_text = cleaned_text[len("/dytrack"):].strip()
        elif cleaned_text.startswith("dytrack"):
            cleaned_text = cleaned_text[len("dytrack"):].strip()

        return cleaned_text or None

    def _extract_collection_text(self, text: str) -> Optional[str]:
        if not text:
            return None

        cleaned_text = text.strip()
        if cleaned_text.startswith("/dycollection"):
            cleaned_text = cleaned_text[len("/dycollection"):].strip()
        elif cleaned_text.startswith("dycollection"):
            cleaned_text = cleaned_text[len("dycollection"):].strip()

        return cleaned_text or None

    def _build_collection_submit_api(self, raw_text: str) -> str:
        douyin_profile_api_key = self.config.get("douyin_profile_api_key", "")
        collection_email = self.config.get("collection_email", "")
        collection_filename = self.config.get("collection_filename", "")
        return (
            f"{DOUYIN_COLLECTION_API.format(apikey=quote(douyin_profile_api_key, safe=''))}"
            f"&url={quote(raw_text, safe='')}"
            "&type=1"
            "&mode=collection"
            f"&email={quote(collection_email, safe='')}"
            f"&filename={quote(collection_filename, safe='')}"
            "&xz=1"
        )

    def _build_account_cookie_submit_api(self, api_key: str, cookie: str, mode: str, filename: str, email: str) -> str:
        api_url = (
            f"{DOUYIN_COLLECTION_API.format(apikey=quote(api_key, safe=''))}"
            f"&cookie={quote(cookie, safe='')}"
            f"&mode={quote(mode, safe='')}"
            f"&filename={quote(filename, safe='')}"
            "&type=1"
        )
        if email:
            api_url += f"&email={quote(email, safe='')}"
        return api_url

    def _build_account_cookie_query_api(self, api_key: str, job_id: str) -> str:
        return (
            f"{DOUYIN_COLLECTION_API.format(apikey=quote(api_key, safe=''))}"
            f"&job_id={quote(job_id, safe='')}"
        )

    def _extract_collection_query_text(self, text: str) -> Optional[str]:
        if not text:
            return None

        cleaned_text = text.strip()
        if cleaned_text.startswith("/dycollection_query"):
            cleaned_text = cleaned_text[len("/dycollection_query"):].strip()
        elif cleaned_text.startswith("dycollection_query"):
            cleaned_text = cleaned_text[len("dycollection_query"):].strip()

        return cleaned_text or None

    def _extract_collection_mode(self, text: str) -> Optional[str]:
        if not text:
            return None

        cleaned_text = text.strip()
        if cleaned_text.startswith("/dycollection"):
            cleaned_text = cleaned_text[len("/dycollection"):].strip()
        elif cleaned_text.startswith("dycollection"):
            cleaned_text = cleaned_text[len("dycollection"):].strip()

        if not cleaned_text:
            return None

        mode = cleaned_text.split()[0].strip().lower()
        return mode or None

    def _build_account_export_filename(self, mode: str) -> str:
        configured = str(self.config.get("douyin_account_filename", "") or "").strip()
        if configured:
            return configured if configured.lower().endswith(".txt") else f"{configured}.txt"
        prefix = "我的喜欢" if mode == "favorite" else "我的收藏"
        return f"{prefix}.txt"

    def _format_account_cookie_submit_result(self, payload: Dict[str, Any], mode: str, filename: str, email: str) -> str:
        job_id = self._extract_job_id(payload)
        mode_label = "喜欢作品" if mode == "favorite" else "收藏作品"
        msg = str(payload.get("msg") or "任务已提交").strip()
        lines = [
            f"✅ {msg}",
            f"📂 解析类型：{mode_label}",
            f"📁 导出文件：{filename}",
            "⚠️ 仅支持解析当前 Cookie 对应账号自己的点赞/收藏内容",
        ]
        if email:
            lines.append(f"📧 通知邮箱：{email}")
        if job_id:
            lines.append(f"🆔 任务ID：{job_id}")
            lines.append(f"🔍 查询命令：/dycollection_query {job_id}")
        return "\n".join(lines)

    def _format_account_cookie_query_result(self, payload: Dict[str, Any], job_id: str) -> str:
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        status = str(data.get("status") or payload.get("status") or "").strip().lower()
        if status != "done":
            return (
                f"🕒 {str(payload.get('msg') or '获取成功').strip()}\n"
                f"📌 任务状态：{status or 'queued'}\n"
                f"🆔 任务ID：{job_id}"
            )

        file_name = str(data.get("filename") or "").strip()
        download_url = str(data.get("download_url") or "").strip()
        count = data.get("count") or 0
        mode = str(data.get("mode") or "collection").strip().lower()
        mode_label = "喜欢作品" if mode == "favorite" else "收藏作品"
        if download_url:
            download_url = self._uppercase_domain(download_url)
        lines = [
            f"✅ {str(data.get('message') or payload.get('msg') or '解析完成').strip()}",
            f"📂 解析类型：{mode_label}",
            f"📦 数量：{count}",
            f"🆔 任务ID：{job_id}",
            "⚠️ 仅支持解析当前 Cookie 对应账号自己的点赞/收藏内容",
        ]
        if file_name:
            lines.append(f"📁 文件名: {file_name}")
        if download_url:
            lines.append(f"📥 下载链接：{download_url}")
        return "\n".join(lines)

    def _format_aggregate_result(self, payload: Dict[str, Any]) -> str:
        return self._format_aggregate_summary(payload, include_image_links=False)

    def _format_aggregate_summary(self, payload: Dict[str, Any], include_image_links: bool = True) -> str:
        code = payload.get("code")
        if code != 200:
            return f"聚合解析失败：{payload.get('msg', '接口未返回成功状态')}"

        data = payload.get("data") or {}
        if not isinstance(data, dict):
            return "聚合解析失败：接口 data 字段格式异常"

        platform = self._detect_platform(data)
        lines: List[str] = [f"✅ 解析成功：{payload.get('msg', '成功')}"]
        lines.append(f"🌐 平台：{platform}")

        author = data.get("author")
        title = data.get("title")
        if author:
            lines.append(f"👤 作者：{self._normalize_author_name(str(author))}")
        if title:
            lines.append(f"📝 标题：{title}")

        video_url = self._pick_video_url(data)
        image_urls = self._pick_image_urls(data)
        image_count = len(image_urls)

        if video_url or image_urls:
            lines.append("📦 资源概览：")
            lines.append(f"- 视频：{'1 个' if video_url else '0 个'}")
            lines.append(f"- 图片：{image_count} 张")

        if video_url:
            lines.append("🎬 视频链接：")
            lines.append(video_url)
        if image_urls and include_image_links:
            lines.append("🖼️ 图片链接：")
            lines.extend([f"{index}. {item}" for index, item in enumerate(image_urls, start=1)])

        if not video_url and not image_urls:
            return "⚠️ 聚合解析完成，但未找到视频或图片链接"

        return "\n".join(lines)

    def _format_douyin_profile_result(self, payload: Dict[str, Any], download_info: Optional[Dict[str, str]] = None) -> str:
        code = payload.get("code")
        if code != 200:
            return f"抖音主页解析失败：{payload.get('msg', '接口未返回成功状态')}"

        author = self._normalize_author_name(str(payload.get("author") or "").strip())
        count = payload.get("count") or 0
        raw_download = self._extract_download_url(payload)
        download = self._uppercase_domain(raw_download) if raw_download else ""
        file_name = self._extract_file_name(download)

        if download_info:
            file_name = download_info.get("file_name") or file_name

        if not file_name and author:
            file_name = f"{author}.txt"

        if not download and file_name:
            download = self._uppercase_domain(
                f"http://douyin.zhcnli.cn/download.php?file={quote(file_name, safe='')}"
            )

        lines: List[str] = [f"✅ 成功获取 {count} 个视频链接，文件已保存到 downloads 文件夹"]
        if author:
            lines.append(f"👤 作者：{author}")
        if file_name:
            lines.append(f"📁 文件名: {file_name}")
            lines.append(f"📂 本地保存：{file_name}")
        if download:
            lines.append(f"📥 下载链接：{download}")
        else:
            fallback_file_name = self._extract_file_name(raw_download) or (f"{author}.txt" if author else "")
            if fallback_file_name:
                fallback_download = self._uppercase_domain(
                    f"http://douyin.zhcnli.cn/download.php?file={quote(fallback_file_name, safe='')}"
                )
                lines.append(f"📁 文件名: {fallback_file_name}")
                lines.append(f"📥 下载链接：{fallback_download}")
            else:
                lines.append("📥 下载链接：接口未返回下载地址")

        return "\n".join(lines)

    def _save_profile_txt(self, payload: Dict[str, Any]) -> Optional[Dict[str, str]]:
        data = payload.get("data")
        if not isinstance(data, list):
            return None

        author = self._normalize_author_name(str(payload.get("author") or "").strip()) or "douyin_profile"
        file_name = f"{self._sanitize_file_name(author)}.txt"
        os.makedirs(DOWNLOAD_DIR, exist_ok=True)
        file_path = os.path.join(DOWNLOAD_DIR, file_name)

        urls = [item.strip() for item in data if isinstance(item, str) and item.strip()]
        if not urls:
            return None

        with open(file_path, "w", encoding="utf-8") as file:
            file.write("\n".join(urls))

        return {"file_name": file_name, "file_path": file_path}

    def _list_download_txt_files(self) -> List[str]:
        if not os.path.isdir(DOWNLOAD_DIR):
            return []

        file_names = [
            item for item in os.listdir(DOWNLOAD_DIR)
            if item.lower().endswith(".txt") and os.path.isfile(os.path.join(DOWNLOAD_DIR, item))
        ]
        return sorted(file_names, key=lambda item: os.path.getmtime(os.path.join(DOWNLOAD_DIR, item)), reverse=True)

    def _format_douyin_menu(self, file_names: List[str]) -> str:
        display_names = [os.path.splitext(item)[0] for item in file_names]
        rows: List[str] = []

        for start_index in range(0, len(display_names), 3):
            row_items = []
            for column_offset, name in enumerate(display_names[start_index:start_index + 3], start=1):
                index = start_index + column_offset
                icon = "◉" if index <= 3 else "○"
                row_items.append(f"{icon} {name}")
            rows.append("  ".join(row_items))

        lines: List[str] = ["┏━🎵 抖音主页菜单 ━┓", *rows, "┣━ 使用方法", "┃ /dyplay 猫姨", "┗━━━━━━━━━━━━━━┛"]
        return "\n".join(lines)

    def _sanitize_file_name(self, value: str) -> str:
        sanitized = re.sub(r'[\\/:*?"<>|]+', "_", value).strip()
        return sanitized or "douyin_profile"

    def _normalize_author_name(self, value: str) -> str:
        if not isinstance(value, str) or not value:
            return ""

        filtered = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", value)
        return filtered.strip()

    def _extract_dyplay_text(self, text: str) -> Optional[str]:
        if not text:
            return None
        cleaned_text = text.strip()
        if cleaned_text.startswith("/dyplay"):
            cleaned_text = cleaned_text[len("/dyplay"):].strip()
        elif cleaned_text.startswith("dyplay"):
            cleaned_text = cleaned_text[len("dyplay"):].strip()
        return cleaned_text or None

    def _extract_dyupdateone_text(self, text: str) -> Optional[str]:
        if not text:
            return None
        cleaned_text = text.strip()
        if cleaned_text.startswith("/dyupdateone"):
            cleaned_text = cleaned_text[len("/dyupdateone"):].strip()
        elif cleaned_text.startswith("dyupdateone"):
            cleaned_text = cleaned_text[len("dyupdateone"):].strip()
        return cleaned_text or None

    def _find_download_txt(self, file_key: str) -> Optional[str]:
        normalized = self._sanitize_file_name(file_key)
        candidates = [
            os.path.join(DOWNLOAD_DIR, f"{normalized}.txt"),
            os.path.join(DOWNLOAD_DIR, normalized),
        ]
        for candidate in candidates:
            if os.path.exists(candidate):
                return candidate
        return None

    def _pick_random_play_index(self, file_path: str, total: int) -> int:
        if total <= 1:
            return 0

        history = self.play_history.get(file_path, [])
        if len(history) >= total:
            history = []

        available_indexes = [index for index in range(total) if index not in history]
        if not available_indexes:
            history = []
            available_indexes = list(range(total))

        current_index = random.choice(available_indexes)
        history.append(current_index)
        self.play_history[file_path] = history
        return current_index

    def _resolve_direct_media_url(self, url: str) -> str:
        if not isinstance(url, str) or not url:
            return ""

        request = Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            },
        )

        try:
            with urlopen(request, timeout=20) as response:
                final_url = response.geturl()
                return final_url or url
        except Exception as exc:
            logger.warning("直链访问失败，回退原链接: %s", exc)
            return url

    def _format_collection_submit_result(self, payload: Dict[str, Any]) -> str:
        msg = str(payload.get("msg") or "获取成功").strip()
        job_id = self._extract_job_id(payload)
        lines = [f"🕒 {msg}", "📌 任务状态：正在解析", "⏳ 正在等待后台解析完成..."]
        if job_id:
            lines.append(f"🆔 任务ID：{job_id}")
        return "\n".join(lines)

    def _build_collection_query_api(self, job_id: str) -> str:
        douyin_profile_api_key = self.config.get("douyin_profile_api_key", "")
        return (
            f"{DOUYIN_COLLECTION_API.format(apikey=quote(douyin_profile_api_key, safe=''))}"
            f"&job_id={quote(job_id, safe='')}"
        )

    def _format_collection_query_result(self, payload: Dict[str, Any], job_id: str) -> str:
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        status = str(data.get("status") or payload.get("status") or "").strip().lower()
        if status != "done":
            return f"🕒 获取成功\n📌 任务状态：正在解析\n⏳ 正在等待后台解析完成...\n🆔 任务ID：{job_id}"

        return self._format_collection_done_result(payload)

    def _extract_job_id(self, payload: Dict[str, Any]) -> str:
        direct_job_id = str(payload.get("job_id") or "").strip()
        if direct_job_id:
            return direct_job_id

        data = payload.get("data")
        if isinstance(data, dict):
            nested_job_id = str(data.get("job_id") or "").strip()
            if nested_job_id:
                return nested_job_id

        query = str(payload.get("query") or "").strip()
        if query:
            match = re.search(r"[?&]job_id=([^&]+)", query, re.IGNORECASE)
            if match:
                return match.group(1).strip()

        payload_text = json.dumps(payload, ensure_ascii=False)
        regex_patterns = [
            r'"job_id"\s*:\s*"([^"]+)"',
            r'[?&]job_id=([^&"\s]+)',
        ]
        for pattern in regex_patterns:
            match = re.search(pattern, payload_text, re.IGNORECASE)
            if match:
                return match.group(1).strip()

        return ""

    def _format_collection_done_result(self, payload: Dict[str, Any]) -> str:
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        count = data.get("count") or 0
        file_name = str(data.get("filename") or "").strip()
        download_url = str(data.get("download_url") or "").strip()
        job_id = str(data.get("job_id") or "").strip()
        message = str(data.get("message") or payload.get("msg") or "解析完成").strip()

        if download_url:
            download_url = self._uppercase_domain(download_url)
        elif file_name:
            download_url = self._uppercase_domain(
                f"http://douyin.zhcnli.cn/download.php?file={quote(file_name, safe='')}"
            )

        lines = [f"✅ {message}"]
        if job_id:
            lines.append(f"🆔 任务ID：{job_id}")
        if file_name:
            lines.append(f"📁 文件名: {file_name}")
        if download_url:
            lines.append(f"📥 下载链接：{download_url}")
        return "\n".join(lines)

    def _extract_download_url(self, payload: Dict[str, Any]) -> str:
        direct_download = payload.get("download")
        if isinstance(direct_download, str) and direct_download.strip():
            return direct_download.strip()

        payload_text = json.dumps(payload, ensure_ascii=False)
        match = re.search(r'"download"\s*:\s*"(https?://[^"\\]+)"', payload_text, re.IGNORECASE)
        if match:
            return match.group(1).strip()

        return ""

    def _extract_file_name(self, url: str) -> str:
        if not isinstance(url, str) or not url:
            return ""

        match = re.search(r"[?&]file=([^&]+)", url, re.IGNORECASE)
        if match:
            return unquote(match.group(1))

        tail = url.rsplit("/", 1)[-1]
        return tail or ""

    def _detect_platform(self, data: Dict[str, Any]) -> str:
        url_text = " ".join(self._collect_candidate_urls(data)).lower()
        if "douyin" in url_text or "aweme" in url_text:
            return "抖音"
        if "kuaishou" in url_text or "yximgs" in url_text or "djvod" in url_text:
            return "快手"
        if "xhscdn" in url_text or "xiaohongshu" in url_text:
            return "小红书"
        if "ppxvod" in url_text or "pipixia" in url_text:
            return "皮皮虾"
        if "izuiyou" in url_text:
            return "最右"
        if "bilivideo" in url_text or "bilibili" in url_text:
            return "哔哩哔哩"
        return "未知"

    def _pick_video_url(self, data: Dict[str, Any]) -> Optional[str]:
        candidates = [
            data.get("url"),
            data.get("video"),
            data.get("video_url"),
            data.get("play"),
            data.get("play_url"),
        ]

        videos = data.get("videos")
        if isinstance(videos, list):
            for item in videos:
                if isinstance(item, dict) and item.get("url"):
                    candidates.append(item.get("url"))

        for item in candidates:
            if isinstance(item, str) and item.startswith(("http://", "https://")):
                return item
        return None

    def _pick_image_urls(self, data: Dict[str, Any]) -> List[str]:
        candidates: List[str] = []
        for key in ["images", "image", "imgurl", "image_urls", "pics"]:
            value = data.get(key)
            if isinstance(value, list):
                candidates.extend(
                    item for item in value if isinstance(item, str) and item.startswith(("http://", "https://"))
                )
            elif isinstance(value, str) and value.startswith(("http://", "https://")):
                candidates.append(value)

        return candidates

    def _collect_candidate_urls(self, data: Dict[str, Any]) -> List[str]:
        urls: List[str] = []
        for value in data.values():
            if isinstance(value, str) and value.startswith(("http://", "https://")):
                urls.append(value)
            elif isinstance(value, list):
                urls.extend(item for item in value if isinstance(item, str))
            elif isinstance(value, dict):
                urls.extend(self._collect_candidate_urls(value))
        return urls

    def _uppercase_domain(self, url: str) -> str:
        if not isinstance(url, str) or not url:
            return ""

        match = re.match(r"^(https?://)([^/]+)(.*)$", url, re.IGNORECASE)
        if not match:
            return url

        prefix, domain, rest = match.groups()
        return f"{prefix}{domain.upper()}{rest}"

    async def terminate(self):
        if self.auto_update_task and not self.auto_update_task.done():
            self.auto_update_task.cancel()
        logger.info("media_parser 插件已卸载")
