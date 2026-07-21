import base64
import json
import os
import random
import re
import time
import asyncio
import importlib.util
import sys
from collections import OrderedDict
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urljoin
from urllib.request import Request, urlopen

import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn

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
DOUYIN_ACCOUNT_PROFILE_API = (
    "https://douyin.zhcnli.cn/account_profile.php"
    "?apikey={apikey}&url="
)
DOUYIN_LOGIN_BASE_URL = "https://login.zhcnli.cn"
DOUYIN_LOGIN_QRCODE_API = f"{DOUYIN_LOGIN_BASE_URL}/api/qrcode"
DOUYIN_LOGIN_DOWNLOAD_API = f"{DOUYIN_LOGIN_BASE_URL}/api/download?id={{session_id}}"
DOUYIN_LOGIN_DOWNLOAD_FILE_PATH = "/api/download/file?id={session_id}"
URL_PATTERN = re.compile(r"https?://[^\s]+", re.IGNORECASE)
AUTO_PARSE_HOST_KEYWORDS = (
    "douyin.com",
    "iesdouyin.com",
    "douyinstatic.com",
    "jimeng.jianying.com",
    "capcut.cn",
    "qianwen.com",
    "activity.qianwen.com",
    "quark.cn",
    "quark-aistudio-cdn.quark.cn",
    "kuaishou.com",
    "chenzhongtech.com",
    "xiaohongshu.com",
    "xhslink.com",
    "xhscdn.com",
    "doubao.com",
    "pipix.com",
    "pipixia.com",
    "ppxvod.com",
    "izuiyou.com",
    "bilibili.com",
    "b23.tv",
)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_DIR = os.path.join(BASE_DIR, "downloads")
PROFILE_RECORD_FILE = os.path.join(BASE_DIR, "douyin_profile_records.json")
AUTO_UPDATE_TARGET_FILE = os.path.join(BASE_DIR, "auto_update_target.json")
AUTO_UPDATE_STATE_FILE = os.path.join(BASE_DIR, "auto_update_state.json")
DEFAULT_REQUEST_TIMEOUT = 20
DEFAULT_DOUYIN_PROFILE_TIMEOUT = 60
DEFAULT_DOUYIN_LOGIN_TIMEOUT = 120
DEFAULT_AUTO_UPDATE_INTERVAL = 30
FAIL_RECORD_FILE = os.path.join(BASE_DIR, "douyin_update_failures.json")
DEFAULT_FAIL_THRESHOLD = 3


@register("astrbot_plugin_juhejiexi", "Anlan", "聚合解析与抖音主页解析插件", "v1.0.0")
class MediaParserPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.play_indexes: Dict[str, int] = {}
        self.play_history: Dict[str, List[int]] = {}
        self.random_profile_index: int = 0
        self.profile_records: List[Dict[str, Any]] = self._load_profile_records()
        self.auto_update_target = self._load_auto_update_target()
        self.auto_update_state = self._load_auto_update_state()
        self.auto_update_task: Optional[asyncio.Task] = None
        self.auto_update_running = False
        self.pre_check_running = False
        self.last_auto_update_date = str(self.auto_update_state.get("last_auto_update_date") or "")
        self.last_auto_update_check_date = str(self.auto_update_state.get("last_auto_update_check_date") or "")
        self.last_pre_check_date = str(self.auto_update_state.get("last_pre_check_date") or "")
        self.pending_update_records: Optional[List[Dict[str, Any]]] = None
        self.pending_profile_collects: Dict[str, Dict[str, Any]] = {}
        self.instance_time_offset: int = self._init_instance_time_offset()
        self.fail_records: Dict[str, Dict[str, Any]] = self._load_fail_records()
        self.api_http_server = None
        self.api_server_thread = None
        self.api_running = False
        self.config.save_config()

    async def initialize(self):
        logger.info("media_parser 插件已初始化")
        if not self.auto_update_task or self.auto_update_task.done():
            self.auto_update_task = asyncio.create_task(self._auto_update_loop())
        if self.config.get("api_auto_start", False):
            try:
                self._start_api_server()
                logger.info("API 服务已自动启动")
            except Exception as exc:
                logger.exception("API 服务自动启动失败: %s", exc)

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def auto_aggregate_parse_for_onebot(self, event: AstrMessageEvent):
        """OneBot 场景下自动识别普通消息中的链接并执行聚合解析。"""
        if not self._should_auto_parse_onebot_message(event):
            return

        logger.info(
            "自动聚合解析触发: origin=%s session=%s text=%s",
            getattr(event, "unified_msg_origin", ""),
            getattr(event, "session_id", ""),
            getattr(event, "message_str", ""),
        )

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
            account_info = self._get_account_profile_info(raw_text, douyin_profile_api_key)
            conflict_message = self._build_profile_collect_conflict_message(raw_text, account_info)
        except Exception as exc:
            logger.warning("抖音主页采集前置检查失败，将继续完整解析: %s", exc)
            account_info = {}
            conflict_message = ""

        if conflict_message:
            pending_key = self._get_profile_collect_pending_key(event)
            self.pending_profile_collects[pending_key] = {
                "raw_text": raw_text,
                "account_info": account_info,
                "created_at": time.time(),
            }
            yield event.plain_result(conflict_message)
            return

        yield event.plain_result("正在解析，请稍候…")
        try:
            message = self._collect_douyin_profile(raw_text, douyin_profile_api_key, account_info)
        except Exception as exc:
            logger.exception("抖音主页解析失败: %s", exc)
            yield event.plain_result(f"抖音主页解析失败：{exc}")
            return

        yield event.plain_result(message)

    @filter.command("dyconfirm")
    async def douyin_profile_collect_confirm(self, event: AstrMessageEvent):
        """确认继续采集存在作者名冲突的抖音主页。"""
        douyin_profile_api_key = self.config.get("douyin_profile_api_key", "")
        if not douyin_profile_api_key:
            yield event.plain_result("未配置抖音主页解析 API 密钥")
            return

        pending_key = self._get_profile_collect_pending_key(event)
        pending = self.pending_profile_collects.pop(pending_key, None)
        if not pending:
            yield event.plain_result("当前会话没有待确认的抖音主页采集任务")
            return

        raw_text = str(pending.get("raw_text") or "").strip()
        account_info = pending.get("account_info") if isinstance(pending.get("account_info"), dict) else {}
        if not raw_text:
            yield event.plain_result("待确认采集任务缺少主页链接，已取消")
            return

        yield event.plain_result("已确认继续采集，正在解析，请稍候…")
        try:
            message = self._collect_douyin_profile(raw_text, douyin_profile_api_key, account_info)
        except Exception as exc:
            logger.exception("确认采集抖音主页失败: %s", exc)
            yield event.plain_result(f"抖音主页解析失败：{exc}")
            return

        yield event.plain_result(message)

    @filter.command("dyskip")
    async def douyin_profile_collect_skip(self, event: AstrMessageEvent):
        """跳过当前会话待确认的抖音主页采集。"""
        pending_key = self._get_profile_collect_pending_key(event)
        pending = self.pending_profile_collects.pop(pending_key, None)
        if not pending:
            yield event.plain_result("当前会话没有待跳过的抖音主页采集任务")
            return
        yield event.plain_result("已跳过本次抖音主页采集")

    @filter.command("dyupdate")
    async def douyin_profile_update_all(self, event: AstrMessageEvent):
        """按记录顺序逐个更新已解析过的抖音主页。"""
        if not self._is_admin_user(event):
            yield event.plain_result("⛔ 无权限：仅白名单用户可执行此指令")
            return
        if not self.profile_records:
            yield event.plain_result("暂无已记录的抖音主页，先使用 /dyhome 解析后再执行 /dyupdate")
            return

        douyin_profile_api_key = self.config.get("douyin_profile_api_key", "")
        if not douyin_profile_api_key:
            yield event.plain_result("未配置抖音主页解析 API 密钥")
            return

        total = len(self.profile_records)
        yield event.plain_result(f"正在解析，请稍候…\n开始更新 {total} 个抖音主页记录")
        summary = self._run_profile_update_batch(douyin_profile_api_key, list(self.profile_records))
        yield event.plain_result(summary)

    @filter.command("dyretry")
    async def douyin_profile_retry_failed(self, event: AstrMessageEvent):
        """重试上次更新失败的抖音主页。"""
        if not self._is_admin_user(event):
            yield event.plain_result("⛔ 无权限：仅白名单用户可执行此指令")
            return

        failed_keys = list(self.fail_records.keys())
        if not failed_keys:
            yield event.plain_result("✅ 无失败记录，无需重试")
            return

        douyin_profile_api_key = self.config.get("douyin_profile_api_key", "")
        if not douyin_profile_api_key:
            yield event.plain_result("未配置抖音主页解析 API 密钥")
            return

        retry_records = [
            r for r in self.profile_records
            if str(r.get("raw_text") or "").strip() in failed_keys
        ]
        if not retry_records:
            self.fail_records.clear()
            self._save_fail_records()
            yield event.plain_result("✅ 失败记录已清除（对应主页记录不存在）")
            return

        yield event.plain_result(f"正在解析，请稍候…\n开始重试 {len(retry_records)} 个失败主页")
        summary = self._run_profile_update_batch(douyin_profile_api_key, retry_records)
        yield event.plain_result(summary)

    @filter.command("dyfixsec")
    async def douyin_profile_fix_sec_user_id(self, event: AstrMessageEvent):
        """隐藏命令：为缺少 sec_user_id 的主页记录错峰补齐账号身份标识。"""
        if not self._is_admin_user(event):
            yield event.plain_result("⛔ 无权限：仅白名单用户可执行此指令")
            return
        if not self.profile_records:
            yield event.plain_result("暂无已记录的抖音主页，无法补齐 sec_user_id")
            return

        douyin_profile_api_key = self.config.get("douyin_profile_api_key", "")
        if not douyin_profile_api_key:
            yield event.plain_result("未配置抖音主页解析 API 密钥")
            return

        missing_records = [
            record for record in self.profile_records
            if str(record.get("raw_text") or "").strip()
            and not str(record.get("sec_user_id") or "").strip()
        ]
        if not missing_records:
            yield event.plain_result("✅ 本地记录已全部包含 sec_user_id，无需补齐")
            return

        yield event.plain_result(f"正在补齐 sec_user_id，请稍候…\n待处理账号：{len(missing_records)} 个")
        summary = await self._fix_missing_sec_user_ids(douyin_profile_api_key, missing_records)
        yield event.plain_result(summary)

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
        yield event.plain_result(f"正在解析，请稍候…\n开始更新：{display_name}")
        result_message = self._update_single_profile_record(douyin_profile_api_key, matched_record, index=1, total=1)
        yield event.plain_result(result_message)

    @filter.command("dytarget")
    async def bind_auto_update_target(self, event: AstrMessageEvent):
        """绑定自动更新结果主动推送目标会话。"""
        if not self._is_admin_user(event):
            yield event.plain_result("⛔ 无权限：仅白名单用户可执行此指令")
            return
        unified_msg_origin = str(getattr(event, "unified_msg_origin", "") or "").strip()
        if not unified_msg_origin:
            yield event.plain_result("当前会话不支持绑定自动更新推送目标")
            return

        self.auto_update_target = {"unified_msg_origin": unified_msg_origin}
        self._save_auto_update_target()
        if self._is_auto_update_push_unsupported_origin(unified_msg_origin):
            yield event.plain_result(
                "⚠️ 已记录当前会话，但当前 QQ 官方 Webhook 适配器暂不支持插件主动推送\n"
                "📌 定时自动更新仍会正常执行，汇总结果会写入插件日志"
            )
            return
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

    @filter.command("dyrand")
    async def douyin_profile_random_play(self, event: AstrMessageEvent):
        """每次切换到下一个主页 TXT，并随机播放其中一个视频。"""
        result = self._get_dyrand_data()
        if result is None:
            yield event.plain_result("暂无可播放的主页记录，先用 /dyhome 解析主页")
            return

        message_chain = [
            Plain(
                f"🎲 随机播放主页：{result['file_name']}\n"
                f"📍 主页进度：{result['profile_index'] + 1}/{result['total_profiles']}\n"
                f"🎬 视频序号：{result['video_index'] + 1}/{result['total_videos']}\n"
                f"🔗 视频直链：{result['playable_url']}"
            ),
            Video.fromURL(result["playable_url"]),
        ]
        yield event.chain_result(message_chain)

    @filter.command("dyapi_on")
    async def api_server_start(self, event: AstrMessageEvent):
        """开启 HTTP API 服务，通过接口获取随机视频数据。"""
        if not self._is_admin_user(event):
            yield event.plain_result("⛔ 无权限：仅白名单用户可执行此指令")
            return

        if self.api_running:
            yield event.plain_result("⚠️ API 服务已在运行中\n" + self._format_api_status())
            return

        try:
            self._start_api_server()
            yield event.plain_result(
                "✅ API 服务已启动\n"
                + self._format_api_status()
                + "\n📌 接口说明：\n"
                + "  /api?type=json  - 返回 JSON 格式随机视频数据\n"
                + "  /api?type=text  - 返回纯文本随机视频信息\n"
                + "  /api?type=video - 302 重定向到视频直链\n"
                + "  /api?type=menu  - 返回视频系列菜单列表\n"
                + "  /api?type=json&file=文件名&index=1 - 返回指定主页/指定视频\n"
                + "  /api?type=menu&file=文件名 - 返回仅该文件所属菜单"
            )
        except Exception as exc:
            logger.exception("启动 API 服务失败: %s", exc)
            yield event.plain_result(f"❌ API 服务启动失败：{exc}")

    @filter.command("dyapi_off")
    async def api_server_stop(self, event: AstrMessageEvent):
        """关闭 HTTP API 服务。"""
        if not self._is_admin_user(event):
            yield event.plain_result("⛔ 无权限：仅白名单用户可执行此指令")
            return

        if not self.api_running:
            yield event.plain_result("⚠️ API 服务未在运行")
            return

        try:
            self._stop_api_server()
            yield event.plain_result("✅ API 服务已关闭")
        except Exception as exc:
            logger.exception("关闭 API 服务失败: %s", exc)
            yield event.plain_result(f"❌ API 服务关闭失败：{exc}")

    @filter.command("dyapi_status")
    async def api_server_status(self, event: AstrMessageEvent):
        """查看 HTTP API 服务状态与本机连通性诊断。"""
        if not self._is_admin_user(event):
            yield event.plain_result("⛔ 无权限：仅白名单用户可执行此指令")
            return
        yield event.plain_result(self._format_api_status())

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
        yield event.plain_result("正在解析，请稍候…")
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

        yield event.plain_result("正在解析，请稍候…")
        try:
            query_api = self._build_account_cookie_query_api(douyin_profile_api_key, job_id)
            payload = self._request_json(query_api, timeout=self._get_douyin_profile_timeout())
        except Exception as exc:
            logger.exception("抖音点赞/收藏任务查询失败: %s", exc)
            yield event.plain_result(f"抖音点赞/收藏任务查询失败：{exc}")
            return

        yield event.plain_result(self._format_account_cookie_query_result(payload, job_id))

    @filter.command("dyck")
    async def douyin_cookie_login(self, event: AstrMessageEvent):
        """生成抖音登录二维码，并返回登录成功后的 Cookie 下载链接。"""
        try:
            payload = self._request_json(DOUYIN_LOGIN_QRCODE_API, timeout=DEFAULT_DOUYIN_LOGIN_TIMEOUT)
            session_id, qrcode_path = self._save_douyin_login_qrcode(payload)
            download_url = self._build_douyin_login_download_file_url(session_id)
        except Exception as exc:
            logger.exception("抖音 Cookie 登录二维码生成失败: %s", exc)
            yield event.plain_result(f"抖音 Cookie 登录二维码生成失败：{exc}")
            return

        text_before = (
            "请尽快使用抖音扫码登录，登录成功后访问下方链接下载 Cookie。\n"
            f"会话ID：{session_id}\n"
        )
        text_after = f"\nCookie 下载链接：{download_url}"
        yield event.chain_result([
            Plain(text_before),
            Image.fromFileSystem(qrcode_path),
            Plain(text_after),
        ])

    @filter.command("画")
    async def ark_image_generate(self, event: AstrMessageEvent):
        """火山方舟文生图：/画 prompt描述。"""
        async for result in self._handle_ark_image_generate(event):
            yield result

    @filter.command("draw")
    async def ark_image_generate_alias(self, event: AstrMessageEvent):
        """火山方舟文生图英文别名：/draw prompt描述。"""
        async for result in self._handle_ark_image_generate(event):
            yield result

    @filter.command("arkdiag")
    async def ark_diagnostics(self, event: AstrMessageEvent):
        """检查 AstrBot 当前运行环境中的火山方舟 SDK 导入状态。"""
        yield event.plain_result(self._format_ark_diagnostics())

    @filter.command("dyhelp")
    async def douyin_help(self, event: AstrMessageEvent):
        """显示抖音相关指令帮助与统计。"""
        yield event.plain_result(self._format_dyhelp())

    async def _handle_ark_image_generate(self, event: AstrMessageEvent):
        prompt = self._extract_draw_prompt(event.message_str)
        if not prompt:
            yield event.plain_result("用法：/画 prompt描述；发送或引用单张图片时会自动图生图")
            return

        api_key = str(self.config.get("ark_api_key", "") or "").strip()
        if not api_key:
            yield event.plain_result("未配置火山方舟 API Key")
            return

        input_image_url = self._extract_event_image_url(event)
        yield event.plain_result("正在画图，请稍候…")
        try:
            image_url = await asyncio.to_thread(self._generate_ark_image_url, prompt, api_key, input_image_url)
        except Exception as exc:
            logger.exception("火山方舟画图失败: %s", exc)
            yield event.plain_result(f"画图失败：{exc}")
            return

        mode_label = "图生图" if input_image_url else "文生图"
        result_text = f"✅ {mode_label}完成\n📝 提示词：{prompt}\n图片链接：{image_url}"
        if input_image_url:
            result_text += f"\n参考图：{input_image_url}"
        yield event.plain_result(result_text)
        yield event.image_result(image_url)

    def _generate_ark_image_url(self, prompt: str, api_key: str, input_image_url: Optional[str] = None) -> str:
        try:
            from volcenginesdkarkruntime import Ark
        except Exception as exc:
            raise RuntimeError(self._format_ark_import_error(exc)) from exc

        base_url = str(self.config.get("ark_base_url", "") or "https://ark.cn-beijing.volces.com/api/v3").strip()
        model = str(self.config.get("ark_image_model", "") or "doubao-seedream-5-0-260128").strip()
        size = str(self.config.get("ark_image_size", "") or "2K").strip()
        watermark = bool(self.config.get("ark_image_watermark", True))

        request_args = {
            "model": model,
            "prompt": prompt,
            "sequential_image_generation": "disabled",
            "response_format": "url",
            "size": size,
            "stream": False,
            "watermark": watermark,
        }
        if input_image_url:
            request_args["image"] = input_image_url

        client = Ark(base_url=base_url, api_key=api_key)
        response = client.images.generate(**request_args)

        data = getattr(response, "data", None)
        if not data:
            raise ValueError("方舟接口未返回图片数据")

        first_item = data[0]
        image_url = str(getattr(first_item, "url", "") or "").strip()
        if not image_url:
            raise ValueError("方舟接口未返回图片 URL")
        return image_url

    def _format_network_error(self, exc: Exception) -> str:
        if isinstance(exc, HTTPError):
            return f"接口请求失败：HTTP {exc.code} {exc.reason}"

        if isinstance(exc, URLError):
            reason = getattr(exc, "reason", None)
            if isinstance(reason, OSError) and getattr(reason, "errno", None) == 101:
                return "接口请求失败：当前运行环境网络不可达，请检查容器/服务器网络、DNS、代理或防火墙配置"
            if isinstance(reason, TimeoutError):
                return "接口请求失败：连接超时，请稍后重试或检查网络连通性"
            return f"接口请求失败：{reason or exc}"

        if isinstance(exc, TimeoutError):
            return "接口请求失败：连接超时，请稍后重试或检查网络连通性"

        if isinstance(exc, OSError) and getattr(exc, "errno", None) == 101:
            return "接口请求失败：当前运行环境网络不可达，请检查容器/服务器网络、DNS、代理或防火墙配置"

        return f"接口请求失败：{exc}"

    def _request_json(self, url: str, timeout: Optional[int] = None) -> Dict[str, Any]:
        request = Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            },
        )

        request_timeout = timeout if isinstance(timeout, (int, float)) and timeout > 0 else DEFAULT_REQUEST_TIMEOUT
        try:
            with urlopen(request, timeout=request_timeout) as response:
                content = response.read().decode("utf-8", errors="ignore").strip()
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            raise RuntimeError(self._format_network_error(exc)) from exc

        if not content:
            raise ValueError("接口返回空内容")

        try:
            payload = json.loads(content)
        except json.JSONDecodeError as exc:
            preview = content[:200].replace("\r", " ").replace("\n", " ")
            raise ValueError(f"接口返回非 JSON 内容：{preview or '空'}") from exc

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

    def _build_douyin_account_profile_api(self, raw_text: str, api_key: str) -> str:
        return DOUYIN_ACCOUNT_PROFILE_API.format(apikey=quote(api_key, safe="")) + quote(raw_text, safe="")

    def _get_account_profile_info(self, raw_text: str, api_key: str) -> Dict[str, Any]:
        """调用账号主页接口获取昵称、作品数和 sec_user_id 等轻量资料。"""
        if not raw_text or not api_key:
            return {}

        api_url = self._build_douyin_account_profile_api(raw_text, api_key)
        payload = self._request_json(api_url, timeout=DEFAULT_REQUEST_TIMEOUT)
        code = payload.get("code")
        if code not in (200, "200", 0, "0"):
            raise ValueError(str(payload.get("msg") or "账号主页接口返回失败"))
        return payload

    def _check_profile_count(self, raw_text: str, api_key: str) -> Optional[int]:
        """调用账号主页接口获取远端作品数量；失败时返回 None，交由更新流程兜底。"""
        if not raw_text or not api_key:
            return None

        try:
            payload = self._get_account_profile_info(raw_text, api_key)
        except Exception as exc:
            logger.warning("获取抖音主页作品数量失败，将继续尝试完整更新: %s", exc)
            return None

        code = payload.get("code")
        if code not in (200, "200", 0, "0"):
            logger.warning("获取抖音主页作品数量接口返回失败: %s", payload.get("msg") or payload)
            return None

        try:
            count = int(payload.get("count") or 0)
        except (TypeError, ValueError):
            logger.warning("获取抖音主页作品数量接口 count 字段异常: %s", payload.get("count"))
            return None

        return max(count, 0)

    def _save_douyin_login_qrcode(self, payload: Dict[str, Any]) -> Tuple[str, str]:
        code = payload.get("code")
        if code not in (0, "0", None):
            message = str(payload.get("message") or "接口返回失败")
            raise ValueError(message)

        data = payload.get("data")
        if not isinstance(data, dict):
            raise ValueError("二维码接口返回格式异常")

        session_id = str(data.get("session_id") or "").strip()
        if not session_id:
            raise ValueError("二维码接口未返回会话ID")

        qrcode_base64 = self._extract_qrcode_base64(data)
        try:
            image_bytes = base64.b64decode(qrcode_base64, validate=True)
        except Exception as exc:
            raise ValueError("二维码 Base64 解码失败") from exc

        if not image_bytes:
            raise ValueError("二维码图片内容为空")

        os.makedirs(DOWNLOAD_DIR, exist_ok=True)
        qrcode_path = os.path.join(DOWNLOAD_DIR, f"douyin_login_{session_id}.png")
        with open(qrcode_path, "wb") as file:
            file.write(image_bytes)
        return session_id, qrcode_path

    def _extract_qrcode_base64(self, data: Dict[str, Any]) -> str:
        qrcode = str(data.get("qrcode") or "").strip()
        qrcode_uri = str(data.get("qrcode_uri") or "").strip()
        raw_qrcode = qrcode or qrcode_uri
        if not raw_qrcode:
            raise ValueError("二维码接口未返回图片内容")

        if raw_qrcode.startswith("data:image") and "," in raw_qrcode:
            raw_qrcode = raw_qrcode.split(",", 1)[1]
        return re.sub(r"\s+", "", raw_qrcode)

    def _get_douyin_login_download_url(self, session_id: str) -> str:
        fallback_url = self._build_douyin_login_download_file_url(session_id)
        try:
            payload = self._request_json(
                DOUYIN_LOGIN_DOWNLOAD_API.format(session_id=quote(session_id, safe="")),
                timeout=DEFAULT_DOUYIN_LOGIN_TIMEOUT,
            )
        except Exception as exc:
            logger.warning("获取抖音 Cookie 下载链接失败，使用默认拼装链接: %s", exc)
            return fallback_url

        download_url = self._extract_douyin_login_download_url(payload)
        return download_url or fallback_url

    def _extract_douyin_login_download_url(self, payload: Dict[str, Any]) -> str:
        data = payload.get("data")
        if not isinstance(data, dict):
            return ""

        relative_url = str(data.get("download_url") or data.get("file_url") or "").strip()
        if not relative_url:
            return ""
        return urljoin(DOUYIN_LOGIN_BASE_URL, relative_url)

    def _build_douyin_login_download_file_url(self, session_id: str) -> str:
        relative_url = DOUYIN_LOGIN_DOWNLOAD_FILE_PATH.format(session_id=quote(session_id, safe=""))
        return urljoin(DOUYIN_LOGIN_BASE_URL, relative_url)

    def _get_pre_check_minutes(self) -> int:
        raw = self.config.get("douyin_profile_pre_check_minutes", 30)
        try:
            minutes = int(raw)
        except (TypeError, ValueError):
            minutes = 30
        return max(minutes, 0)

    def _get_profile_update_min_new_count(self) -> int:
        raw = self.config.get("douyin_profile_update_min_new_count", 1)
        try:
            min_count = int(raw)
        except (TypeError, ValueError):
            min_count = 1
        return max(min_count, 1)

    def _calc_pre_check_time(self, auto_time: str, pre_check_minutes: int) -> str:
        """根据正式更新时间和提前分钟数，计算预检应触发的时间字符串 HH:MM。"""
        try:
            hour, minute = int(auto_time[:2]), int(auto_time[3:])
            total_minutes = hour * 60 + minute - pre_check_minutes
            total_minutes = total_minutes % (24 * 60)
            return f"{total_minutes // 60:02d}:{total_minutes % 60:02d}"
        except Exception:
            return ""

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

    def _can_use_forward_nodes(self) -> bool:
        return self._get_forward_node_uin() > 0

    def _get_forward_node_name(self, fallback_name: str) -> str:
        configured_name = str(self.config.get("forward_node_name", "") or "").strip()
        return configured_name or fallback_name or "聚合解析助手"

    def _get_aggregate_image_send_mode(self) -> str:
        mode = str(self.config.get("aggregate_image_send_mode", "separate") or "separate").strip().lower()
        return "forward" if mode == "forward" else "separate"

    def _build_forward_nodes(self, summary: str, image_urls: List[str], fallback_name: str) -> List[Node]:
        uin = self._get_forward_node_uin()
        name = self._get_forward_node_name(fallback_name)
        nodes: List[Node] = [
            Node(
                uin=uin,
                name=name,
                content=[Plain(summary)],
            )
        ]
        for index, image_url in enumerate(image_urls, start=1):
            nodes.append(
                Node(
                    uin=uin,
                    name=name,
                    content=[
                        Plain(f"图片 {index}/{len(image_urls)}"),
                        Image.fromURL(self._compress_image_url(image_url)),
                    ],
                )
            )
        return nodes

    def _is_admin_user(self, event: AstrMessageEvent) -> bool:
        raw_whitelist = self.config.get("admin_user_ids", [])
        if not raw_whitelist:
            return True
        if isinstance(raw_whitelist, str):
            whitelist = [item.strip() for item in raw_whitelist.replace(",", "\n").splitlines() if item.strip()]
        elif isinstance(raw_whitelist, list):
            whitelist = [str(item).strip() for item in raw_whitelist if str(item).strip()]
        else:
            whitelist = []
        if not whitelist:
            return True
        try:
            sender_id = str(event.message_obj.sender.user_id).strip()
        except Exception:
            sender_id = ""
        return sender_id in whitelist

    def _is_onebot_event(self, event: AstrMessageEvent) -> bool:
        unified_msg_origin = str(getattr(event, "unified_msg_origin", "") or "").lower()
        if any(keyword in unified_msg_origin for keyword in ["onebot", "v11", "aiocqhttp"]):
            return True

        message_obj = getattr(event, "message_obj", None)
        if message_obj is not None:
            raw_type = str(getattr(message_obj, "type", "") or "").lower()
            if any(keyword in raw_type for keyword in ["group", "private"]):
                return True

        return False

    def _should_auto_parse_onebot_message(self, event: AstrMessageEvent) -> bool:
        if not self._is_onebot_event(event):
            return False

        text = str(getattr(event, "message_str", "") or "").strip()
        if not text:
            return False

        lowered_text = text.lower()
        if lowered_text.startswith("/"):
            return False

        if lowered_text.startswith(("jx ", "dyhome", "dyupdate", "dyupdateone", "dytarget", "dytrack", "dymenu", "dyplay", "dyrand", "dycollection", "dycollection_query", "dyck", "dyhelp", "dyapi_on", "dyapi_off", "dyapi_status", "draw", "画")):
            return False

        target_url = self._extract_url(text)
        if not target_url:
            return False

        return self._is_supported_auto_parse_url(target_url)

    def _is_supported_auto_parse_url(self, url: str) -> bool:
        if not isinstance(url, str) or not url:
            return False

        lowered_url = url.lower()
        return any(keyword in lowered_url for keyword in AUTO_PARSE_HOST_KEYWORDS)

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

        yield event.plain_result("正在解析，请稍候…")
        try:
            api_url = AGGREGATE_API.format(apikey=quote(aggregate_api_key, safe="")) + quote(target_url, safe="")
            payload = self._request_json(api_url)
            data = payload.get("data")
            if isinstance(data, dict):
                data["raw_share_url"] = target_url
        except Exception as exc:
            logger.exception("聚合解析失败: %s", exc)
            if require_command:
                yield event.plain_result(f"聚合解析失败：{exc}")
            return

        data = payload.get("data") or {}
        message = self._format_aggregate_result(payload)
        video_url = self._pick_video_url(data) if isinstance(data, dict) else None
        image_urls = self._pick_image_urls(data) if isinstance(data, dict) else []
        live_video_urls = self._pick_live_video_urls(data) if isinstance(data, dict) else []
        if isinstance(data, dict) and self._is_live_resource(data) and live_video_urls:
            image_urls = []

        if video_url:
            playable_url = self._resolve_direct_media_url(video_url)
            yield event.plain_result(message)
            yield event.chain_result([Video.fromURL(playable_url)])
            return

        if image_urls or live_video_urls:
            safe_image_urls = [image_url for image_url in image_urls if self._should_embed_image_url(image_url)]
            has_unsafe_image_urls = len(safe_image_urls) != len(image_urls)
            summary = self._format_aggregate_summary(
                payload,
                include_image_links=has_unsafe_image_urls or not safe_image_urls,
            )
            use_forward_nodes = (
                image_urls
                and not has_unsafe_image_urls
                and self._get_aggregate_image_send_mode() == "forward"
                and self._can_use_forward_nodes()
            )
            if image_urls and self._get_aggregate_image_send_mode() == "forward" and not self._can_use_forward_nodes():
                logger.warning("聚合解析已配置为合并转发，但未配置有效的 forward_node_uin，已回退为普通图片发送")
            if has_unsafe_image_urls:
                logger.warning(
                    "聚合解析图片链接包含不适合直接下载的直链，已回退为文本链接发送：%s",
                    ", ".join(image_urls),
                )

            if use_forward_nodes:
                author_name = self._format_aggregate_author(data.get("author")) if isinstance(data, dict) else ""
                nodes = self._build_forward_nodes(summary, safe_image_urls, author_name)
                yield event.chain_result(nodes)
            else:
                yield event.plain_result(summary)
                if not has_unsafe_image_urls:
                    for image_url in safe_image_urls:
                        yield event.image_result(self._compress_image_url(image_url))
            for live_video_url in live_video_urls:
                playable_url = self._resolve_direct_media_url(live_video_url)
                yield event.chain_result([Video.fromURL(playable_url)])
            return

        yield event.plain_result(message)

    def _is_auto_update_enabled(self) -> bool:
        return bool(self.config.get("douyin_profile_auto_update_enabled", False))

    def _get_auto_update_time(self) -> str:
        raw_time = str(self.config.get("douyin_profile_auto_update_time", "") or "").strip()
        if re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", raw_time):
            return raw_time
        return ""

    def _init_instance_time_offset(self) -> int:
        existing = self.auto_update_state.get("instance_time_offset")
        if isinstance(existing, int) and 0 <= existing < 60:
            return existing
        offset = random.randint(0, 59)
        self.auto_update_state["instance_time_offset"] = offset
        self._save_auto_update_state()
        logger.info("生成实例时间偏移 %s 分钟，用于多实例自动错峰", offset)
        return offset

    def _apply_time_offset(self, base_time: str, offset_minutes: int) -> str:
        """将 HH:MM 时间字符串加上偏移分钟数后返回新的 HH:MM（跨天自动取模）。"""
        try:
            hour, minute = int(base_time[:2]), int(base_time[3:])
            total = (hour * 60 + minute + offset_minutes) % (24 * 60)
            return f"{total // 60:02d}:{total % 60:02d}"
        except Exception:
            return base_time

    async def _auto_update_loop(self):
        while True:
            try:
                auto_time = self._get_auto_update_time()
                if not self._is_auto_update_enabled() or not auto_time:
                    await asyncio.sleep(30)
                    continue

                actual_update_time = self._apply_time_offset(auto_time, self.instance_time_offset)
                pre_check_minutes = self._get_pre_check_minutes()
                actual_pre_check_time = (
                    self._calc_pre_check_time(actual_update_time, pre_check_minutes)
                    if pre_check_minutes > 0 else ""
                )

                now = datetime.now()
                current_date = now.strftime("%Y-%m-%d")
                current_time = now.strftime("%H:%M")

                if self.last_auto_update_check_date != current_date:
                    self.last_auto_update_check_date = current_date
                    self.auto_update_state["last_auto_update_check_date"] = current_date
                    self._save_auto_update_state()
                    logger.info(
                        "新的一天，实际更新时间 %s（配置 %s + 偏移 %s 分钟），预检时间 %s",
                        actual_update_time, auto_time, self.instance_time_offset,
                        actual_pre_check_time or "不预检",
                    )
                    await asyncio.sleep(30)
                    continue

                if (
                    actual_pre_check_time
                    and current_time >= actual_pre_check_time
                    and self.last_pre_check_date != current_date
                    and self.last_auto_update_date != current_date
                ):
                    self.last_pre_check_date = current_date
                    self.auto_update_state["last_pre_check_date"] = current_date
                    self._save_auto_update_state()
                    logger.info("触发预检扫描（预检时间 %s）", actual_pre_check_time)
                    await self._run_pre_check_scan()

                if current_time >= actual_update_time and self.last_auto_update_date != current_date:
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
            pending = self.pending_update_records
            if pending is not None:
                target_records = pending
                logger.info("使用预检缓存，本次更新 %s 个有新作品的主页（共 %s 个记录）", len(target_records), len(self.profile_records))
            else:
                target_records = None
                logger.info("无预检缓存，对全部 %s 个主页记录执行更新", len(self.profile_records))
            self.pending_update_records = None
            total_all = len(self.profile_records)
            total = len(target_records) if target_records is not None else total_all
            logger.info("开始执行定时自动更新，共 %s 个主页记录", total)
            success_count = 0
            failed_count = 0
            skip_count = max(total_all - total, 0) if target_records is not None else 0
            auto_update_interval = self._get_auto_update_interval()
            actual_records = list(target_records) if target_records is not None else list(self.profile_records)
            threshold = DEFAULT_FAIL_THRESHOLD
            replace_hints: List[str] = []
            for index, record in enumerate(actual_records, start=1):
                message = self._update_single_profile_record(douyin_profile_api_key, record, index=index, total=total)
                raw_text = str(record.get("raw_text") or "").strip()
                author = self._normalize_author_name(str(record.get("author") or "").strip()) or f"记录{index}"
                if message.startswith("✅"):
                    success_count += 1
                    if raw_text and raw_text in self.fail_records:
                        del self.fail_records[raw_text]
                elif message.startswith("⏭️"):
                    skip_count += 1
                    logger.info("自动更新跳过项: %s", message.replace("\n", " | "))
                elif message.startswith("❌"):
                    failed_count += 1
                    logger.info("自动更新失败项: %s", message.replace("\n", " | "))
                    if raw_text:
                        entry = self.fail_records.setdefault(raw_text, {"author": author, "count": 0, "last_at": ""})
                        entry["count"] = entry.get("count", 0) + 1
                        entry["last_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
                        entry["author"] = author
                        if entry["count"] >= threshold:
                            replace_hints.append(author)
                else:
                    logger.info("自动更新其他项: %s", message.replace("\n", " | "))
                if index < total and auto_update_interval > 0:
                    jitter = random.uniform(auto_update_interval, auto_update_interval * 2)
                    logger.info("自动更新节流等待 %.1f 秒后继续下一项（随机抖动 %s~%s 秒）", jitter, auto_update_interval, auto_update_interval * 2)
                    await asyncio.sleep(jitter)
            self._save_fail_records()
            logger.info(
                "抖音主页自动更新全部完成：共 %s 个，更新 %s 个，无需更新 %s 个，失败 %s 个",
                total_all,
                success_count,
                skip_count,
                failed_count,
            )
            await self._notify_auto_update_summary(total_all, success_count, skip_count, failed_count, replace_hints)
        finally:
            self.auto_update_running = False

    async def _run_pre_check_scan(self):
        if self.pre_check_running:
            logger.info("预检扫描任务仍在执行，跳过本轮")
            return
        if not self.profile_records:
            logger.info("预检扫描跳过：暂无主页记录")
            return

        douyin_profile_api_key = self.config.get("douyin_profile_api_key", "")
        if not douyin_profile_api_key:
            logger.warning("预检扫描失败：未配置抖音主页解析 API 密钥")
            return

        self.pre_check_running = True
        try:
            total = len(self.profile_records)
            min_new_count = self._get_profile_update_min_new_count()
            logger.info("开始预检扫描，共 %s 个主页记录，最小新增阈值 %s", total, min_new_count)
            pending: List[Dict[str, Any]] = []
            auto_update_interval = self._get_auto_update_interval()
            for index, record in enumerate(self.profile_records, start=1):
                raw_text = str(record.get("raw_text") or "").strip()
                if not raw_text:
                    continue
                local_count = int(record.get("count") or 0)
                remote_count = self._check_profile_count(raw_text, douyin_profile_api_key)
                author = self._normalize_author_name(str(record.get("author") or "").strip()) or f"记录{index}"
                if remote_count is None or local_count <= 0:
                    logger.info(
                        "预检 %s/%s %s：数量未知（远端 %s，本地 %s），加入待更新队列兜底",
                        index, total, author,
                        remote_count if remote_count is not None else "?", local_count,
                    )
                    pending.append(record)
                else:
                    new_count = remote_count - local_count
                    if new_count >= min_new_count:
                        logger.info(
                            "预检 %s/%s %s：新增 %s 个，达到阈值 %s，加入待更新队列",
                            index, total, author, new_count, min_new_count,
                        )
                        pending.append(record)
                    else:
                        logger.info(
                            "预检 %s/%s %s：新增 %s 个，未达到阈值 %s，跳过",
                            index, total, author, new_count, min_new_count,
                        )
                if index < total and auto_update_interval > 0:
                    jitter = random.uniform(auto_update_interval, auto_update_interval * 2)
                    await asyncio.sleep(jitter)
            self.pending_update_records = pending
            logger.info(
                "预检扫描完成：共 %s 个，需更新 %s 个，无变化 %s 个",
                total, len(pending), total - len(pending),
            )
        except Exception as exc:
            logger.exception("预检扫描异常，将在正式更新时 fallback 全量: %s", exc)
            self.pending_update_records = None
        finally:
            self.pre_check_running = False

    def _run_profile_update_batch(self, douyin_profile_api_key: str, records: List[Dict[str, Any]]) -> str:
        """同步执行一批主页更新，记录失败，返回汇总文本。"""
        total = len(records)
        success_count = 0
        skip_count = 0
        failed_authors: List[str] = []
        threshold = DEFAULT_FAIL_THRESHOLD
        replace_hints: List[str] = []

        for index, record in enumerate(records, start=1):
            message = self._update_single_profile_record(douyin_profile_api_key, record, index=index, total=total)
            raw_text = str(record.get("raw_text") or "").strip()
            author = self._normalize_author_name(str(record.get("author") or "").strip()) or f"记录{index}"
            if message.startswith("✅"):
                success_count += 1
                if raw_text and raw_text in self.fail_records:
                    del self.fail_records[raw_text]
            elif message.startswith("⏭️"):
                skip_count += 1
            elif message.startswith("❌"):
                failed_authors.append(author)
                if raw_text:
                    entry = self.fail_records.setdefault(raw_text, {"author": author, "count": 0, "last_at": ""})
                    entry["count"] = entry.get("count", 0) + 1
                    entry["last_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
                    entry["author"] = author
                    if entry["count"] >= threshold:
                        replace_hints.append(author)

        self._save_fail_records()

        lines = [
            "📊 更新完成汇总",
            f"📦 总数：{total}",
            f"✔️ 成功：{success_count}",
            f"⏭️ 无需更新账号：{skip_count}",
            f"❌ 失败：{len(failed_authors)}",
        ]
        if failed_authors:
            lines.append("失败主页：" + "、".join(failed_authors))
            lines.append("💡 可执行 /dyretry 重试失败主页")
        if replace_hints:
            lines.append("")
            lines.append("⚠️ 以下主页连续失败 ≥{} 次，建议更换分享链接：".format(threshold))
            for name in replace_hints:
                lines.append(f"  · {name}")
        return "\n".join(lines)

    async def _fix_missing_sec_user_ids(self, douyin_profile_api_key: str, records: List[Dict[str, Any]]) -> str:
        """错峰补齐缺失的 sec_user_id，并返回汇总文本。"""
        total = len(records)
        success_count = 0
        skip_count = 0
        failed_items: List[str] = []
        auto_update_interval = self._get_auto_update_interval()

        for index, record in enumerate(records, start=1):
            raw_text = str(record.get("raw_text") or "").strip()
            author = self._normalize_author_name(str(record.get("author") or "").strip()) or f"记录{index}"
            if not raw_text:
                skip_count += 1
                continue
            if str(record.get("sec_user_id") or "").strip():
                skip_count += 1
                continue

            try:
                account_info = self._get_account_profile_info(raw_text, douyin_profile_api_key)
                sec_user_id = str(account_info.get("sec_user_id") or "").strip()
                if not sec_user_id:
                    failed_items.append(f"{author}（接口未返回 sec_user_id）")
                else:
                    record["sec_user_id"] = sec_user_id
                    nickname = self._normalize_author_name(str(account_info.get("nickname") or "").strip())
                    if nickname and not str(record.get("author") or "").strip():
                        record["author"] = nickname
                    if account_info.get("count") is not None:
                        record["count"] = account_info.get("count") or 0
                    share_url = str(account_info.get("share_url") or "").strip()
                    if share_url and not str(record.get("share_url") or "").strip():
                        record["share_url"] = share_url
                    record["updated_at"] = datetime.now().isoformat(timespec="seconds")
                    success_count += 1
            except Exception as exc:
                failed_items.append(f"{author}（{self._format_network_error(exc)}）")
                logger.warning("补齐 sec_user_id 失败：%s，error=%s", author, exc)

            if index < total and auto_update_interval > 0:
                jitter = random.uniform(auto_update_interval, auto_update_interval * 2)
                logger.info("补齐 sec_user_id 错峰等待 %.1f 秒后继续下一项（随机抖动 %s~%s 秒）", jitter, auto_update_interval, auto_update_interval * 2)
                await asyncio.sleep(jitter)

        if success_count > 0:
            self._save_profile_records()

        lines = [
            "✅ sec_user_id 补齐完成",
            f"📦 待处理：{total}",
            f"✔️ 补齐成功：{success_count}",
            f"⏭️ 已有或无链接跳过：{skip_count}",
            f"❌ 失败：{len(failed_items)}",
        ]
        if failed_items:
            lines.append("失败账号：" + "、".join(failed_items[:10]))
            if len(failed_items) > 10:
                lines.append(f"另有 {len(failed_items) - 10} 个失败账号未展示")
        return "\n".join(lines)

    def _load_fail_records(self) -> Dict[str, Dict[str, Any]]:
        if not os.path.exists(FAIL_RECORD_FILE):
            return {}
        try:
            with open(FAIL_RECORD_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception as exc:
            logger.warning("加载失败记录文件出错: %s", exc)
            return {}

    def _save_fail_records(self) -> None:
        try:
            with open(FAIL_RECORD_FILE, "w", encoding="utf-8") as f:
                json.dump(self.fail_records, f, ensure_ascii=False, indent=2)
        except Exception as exc:
            logger.warning("保存失败记录文件出错: %s", exc)

    def _get_profile_collect_pending_key(self, event: AstrMessageEvent) -> str:
        unified_msg_origin = str(getattr(event, "unified_msg_origin", "") or "").strip()
        session_id = str(getattr(event, "session_id", "") or "").strip()
        sender_id = ""
        try:
            sender_id = str(event.message_obj.sender.user_id).strip()
        except Exception:
            sender_id = ""
        return unified_msg_origin or session_id or sender_id or "default"

    def _build_profile_collect_conflict_message(self, raw_text: str, account_info: Dict[str, Any]) -> str:
        new_author = self._normalize_author_name(
            str(account_info.get("nickname") or account_info.get("author") or "").strip()
        )
        new_sec_user_id = str(account_info.get("sec_user_id") or "").strip()
        if not new_author:
            return ""

        conflict_records: List[Tuple[Dict[str, Any], str]] = []
        for record in self.profile_records:
            old_author = self._normalize_author_name(str(record.get("author") or "").strip())
            if not old_author:
                continue
            old_sec_user_id = str(record.get("sec_user_id") or "").strip()
            same_author = old_author == new_author
            similar_author = (
                old_author != new_author
                and len(old_author) >= 2
                and len(new_author) >= 2
                and (old_author in new_author or new_author in old_author)
            )
            if not same_author and not similar_author:
                continue
            if not old_sec_user_id:
                continue
            if new_sec_user_id and new_sec_user_id == old_sec_user_id:
                continue
            conflict_records.append((record, "相同作者名" if same_author else "相似作者名"))

        if not conflict_records:
            return ""

        lines = [
            "⚠️ 检测到可能重复的抖音主页采集",
            f"👤 新主页作者：{self._sanitize_markdown_text(new_author)}",
        ]
        if new_sec_user_id:
            lines.append(f"🆔 新 sec_user_id：{new_sec_user_id}")
        lines.append("📌 本地相似记录：")
        for record, reason in conflict_records[:5]:
            old_author = self._sanitize_markdown_text(str(record.get("author") or "未知作者"))
            old_sec_user_id = str(record.get("sec_user_id") or "未记录").strip()
            old_count = record.get("count") or 0
            lines.append(f"- {reason}：{old_author}｜作品数 {old_count}｜sec_user_id {old_sec_user_id}")
        if len(conflict_records) > 5:
            lines.append(f"- 另有 {len(conflict_records) - 5} 条相似记录未展示")
        lines.extend([
            "",
            "如确认这是新主页或仍要覆盖/采集，请发送 /dyconfirm",
            "如不采集，请发送 /dyskip",
        ])
        return "\n".join(lines)

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
            local_count = int(record.get("count") or 0)
            remote_count = self._check_profile_count(raw_text, douyin_profile_api_key)
            min_new_count = self._get_profile_update_min_new_count()
            if remote_count is not None and local_count > 0:
                display_author = self._sanitize_markdown_text(author)
                new_count = remote_count - local_count
                if new_count < min_new_count:
                    return (
                        f"⏭️ {index}/{total} 新增作品数未达到阈值，已跳过\n"
                        f"👤 作者：{display_author}\n"
                        f"📦 远端作品数：{remote_count}\n"
                        f"📦 本地记录数：{local_count}\n"
                        f"🆕 新增作品数：{new_count}\n"
                        f"🎯 更新阈值：{min_new_count}"
                    )

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

    def _get_douyin_command_definitions(self) -> "OrderedDict[str, str]":
        return OrderedDict([
            ("/画 prompt描述", "使用火山方舟生成图片"),
            ("/arkdiag", "检查火山方舟 SDK 运行环境"),
            ("/dyhome", "解析抖音主页并生成本地 TXT 文件"),
            ("/dyconfirm", "确认继续采集存在作者名冲突的抖音主页"),
            ("/dyskip", "跳过当前待确认的抖音主页采集"),
            ("/dytrack", "补录旧主页分享文本或链接到更新记录"),
            ("/dyupdate", "顺序更新全部已记录的抖音主页"),
            ("/dyupdateone", "按作者名或文件名更新单个主页记录"),
            ("/dytarget", "绑定自动更新结果的主动推送会话"),
            ("/dyretry", "重试上次更新失败的主页"),
            ("/dymenu", "查看已保存的主页 TXT 播放菜单"),
            ("/dyplay", "按文件名播放 TXT 中的视频链接"),
            ("/dyrand", "轮流切换主页并随机播放其中一个视频"),
            ("/dyapi_on", "开启 HTTP API 随机视频接口"),
            ("/dyapi_off", "关闭 HTTP API 随机视频接口"),
            ("/dyapi_status", "查看 HTTP API 服务状态与连通性诊断"),
            ("/dycollection", "提交点赞或收藏内容解析任务"),
            ("/dycollection_query", "查询点赞或收藏解析任务状态"),
            ("/dyck", "生成抖音登录二维码并返回 Cookie 下载链接"),
            ("/dyhelp", "查看抖音指令帮助与统计"),
        ])

    def _format_dyhelp(self) -> str:
        command_definitions = self._get_douyin_command_definitions()
        command_count = len(command_definitions)
        tracked_profile_count = len(self.profile_records)
        txt_files = self._list_download_txt_files()
        saved_txt_count = len(txt_files)
        play_mode = str(self.config.get("douyin_profile_play_mode", "sequential") or "sequential").strip().lower()
        play_mode_display = "随机" if play_mode == "random" else "顺序"
        next_random_profile = "暂无"
        if txt_files:
            next_index = self.random_profile_index % len(txt_files)
            next_random_profile = os.path.splitext(txt_files[next_index])[0]
        auto_update_enabled = "开启" if self._is_auto_update_enabled() else "关闭"
        auto_update_time = self._get_auto_update_time() or "未设置"
        if auto_update_time != "未设置" and self.instance_time_offset != 0:
            actual_update_time = self._apply_time_offset(auto_update_time, self.instance_time_offset)
            auto_update_time_display = f"{auto_update_time} → 实际 {actual_update_time}（错峰偏移 {self.instance_time_offset} 分钟）"
        else:
            auto_update_time_display = auto_update_time

        lines: List[str] = [
            "┏━📘 抖音指令统计 ━┓",
            f"┃ 指令总数：{command_count}",
            f"┃ 主页记录：{tracked_profile_count}",
            f"┃ TXT 文件：{saved_txt_count}",
            f"┃ /dyplay 模式：{play_mode_display}",
            f"┃ /dyrand 下个主页：{next_random_profile}",
            f"┃ 自动更新：{auto_update_enabled} / {auto_update_time_display}",
            "┣━ 指令列表",
        ]
        for command_name, description in command_definitions.items():
            lines.append(f"┃ {command_name} - {description}")
        lines.append("┗━━━━━━━━━━━━━━┛")
        return "\n".join(lines)

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

    def _is_auto_update_push_unsupported_origin(self, unified_msg_origin: str) -> bool:
        """判断当前主动推送目标是否存在已知适配器兼容问题。"""
        normalized_origin = str(unified_msg_origin or "").strip().lower()
        return (
            "qqofficial_webhook" in normalized_origin
            or "qqofficial" in normalized_origin
            or ("qq" in normalized_origin and "webhook" in normalized_origin)
        )

    async def _notify_auto_update_summary(
        self,
        total: int,
        success_count: int,
        skip_count: int,
        failed_count: int,
        replace_hints: Optional[List[str]] = None,
    ):
        unified_msg_origin = str(self.auto_update_target.get("unified_msg_origin") or "").strip()
        if not unified_msg_origin:
            return

        try:
            lines = [
                "✅ 抖音主页自动更新全部完成",
                f"📦 总数：{total}",
                f"✔️ 成功：{success_count}",
                f"⏭️ 无需更新账号：{skip_count}",
                f"❌ 失败：{failed_count}",
            ]
            if failed_count > 0:
                lines.append("💡 可执行 /dyretry 重试失败主页")
            if replace_hints:
                lines.append("")
                lines.append(f"⚠️ 以下主页连续失败 ≥{DEFAULT_FAIL_THRESHOLD} 次，建议更换分享链接：")
                for name in replace_hints:
                    lines.append(f"  · {name}")
            summary_text = "\n".join(lines)
            logger.info("抖音主页自动更新汇总消息:\n%s", summary_text)
            if self._is_auto_update_push_unsupported_origin(unified_msg_origin):
                logger.warning(
                    "跳过自动更新汇总主动推送：QQ 官方 Webhook 适配器存在已知 send_message 兼容问题，origin=%s",
                    unified_msg_origin,
                )
                return
            message_chain = MessageChain([Plain(summary_text)])
            await self.context.send_message(unified_msg_origin, message_chain)
        except TypeError as exc:
            if "super(type, obj)" in str(exc):
                logger.warning(
                    "跳过自动更新汇总主动推送：当前平台适配器 send_message 存在兼容问题，origin=%s，error=%s",
                    unified_msg_origin,
                    exc,
                )
                return
            logger.exception("发送自动更新汇总消息失败: %s", exc)
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

    def _collect_douyin_profile(
        self,
        raw_text: str,
        douyin_profile_api_key: str,
        account_info: Optional[Dict[str, Any]] = None,
    ) -> str:
        api_url = self._build_douyin_profile_api(raw_text, douyin_profile_api_key)
        profile_timeout = self._get_douyin_profile_timeout()
        payload = self._request_json(api_url, timeout=profile_timeout)
        if isinstance(account_info, dict) and account_info:
            payload = {**account_info, **payload}
            if account_info.get("nickname") and not payload.get("author"):
                payload["author"] = account_info.get("nickname")
        self._upsert_profile_record(raw_text, payload)
        download_info = self._save_profile_txt(payload)
        return self._format_douyin_profile_result(payload, download_info)

    def _upsert_profile_record(self, raw_text: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        normalized_raw_text = raw_text.strip()
        record: Dict[str, Any] = {
            "raw_text": normalized_raw_text,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }

        if isinstance(payload, dict):
            author = self._normalize_author_name(str(payload.get("author") or payload.get("nickname") or "").strip())
            sec_user_id = str(payload.get("sec_user_id") or "").strip()
            file_name = ""
            download = self._extract_download_url(payload)
            if download:
                file_name = self._extract_file_name(download)
            if author:
                record["author"] = author
            if sec_user_id:
                record["sec_user_id"] = sec_user_id
            record["count"] = payload.get("count") or 0
            if file_name:
                record["file_name"] = file_name

        for index, item in enumerate(self.profile_records):
            if str(item.get("raw_text") or "").strip() == normalized_raw_text:
                merged = {**item, **record}
                self.profile_records[index] = merged
                self._save_profile_records()
                return merged

        if record.get("sec_user_id"):
            for index, item in enumerate(self.profile_records):
                if str(item.get("sec_user_id") or "").strip() == str(record.get("sec_user_id") or "").strip():
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

    def _format_ark_import_error(self, exc: BaseException) -> str:
        return (
            "火山方舟 SDK 导入失败，当前 AstrBot 运行环境未能加载 volcenginesdkarkruntime。\n"
            f"Python：{sys.executable}\n"
            f"错误：{type(exc).__name__}: {exc}\n"
            "请把 SDK 安装到上面这个 Python 环境里。"
        )

    def _format_ark_diagnostics(self) -> str:
        module_name = "volcenginesdkarkruntime"
        lines = [
            "火山方舟 SDK 诊断",
            f"Python：{sys.executable}",
            f"sys.path：{os.pathsep.join(sys.path[:8])}",
        ]

        spec = importlib.util.find_spec(module_name)
        if spec is None:
            lines.append("模块查找：未找到 volcenginesdkarkruntime")
        else:
            lines.append(f"模块查找：{spec.origin or '已找到，但无 origin'}")

        try:
            from volcenginesdkarkruntime import Ark
            lines.append("导入测试：成功")
            lines.append(f"Ark 类：{Ark}")
        except Exception as exc:
            lines.append(f"导入测试：失败，{type(exc).__name__}: {exc}")
        return "\n".join(lines)

    def _extract_draw_prompt(self, text: str) -> Optional[str]:
        raw_text = str(text or "").strip()
        for prefix in ["/画", "画", "/draw", "draw"]:
            if raw_text.lower().startswith(prefix.lower()):
                prompt = raw_text[len(prefix):].strip()
                return prompt or None
        return raw_text or None

    def _extract_event_image_url(self, event: AstrMessageEvent) -> Optional[str]:
        candidates = [
            getattr(event, "message_obj", None),
            getattr(event, "message_chain", None),
            getattr(event, "message_str", None),
        ]
        for candidate in candidates:
            image_url = self._extract_image_url_from_value(candidate, set())
            if image_url:
                return image_url
        return None

    def _extract_image_url_from_value(self, value: Any, seen: set) -> Optional[str]:
        if value is None:
            return None

        if isinstance(value, str):
            return self._extract_image_url_from_text(value)

        if isinstance(value, (int, float, bool)):
            return None

        value_id = id(value)
        if value_id in seen:
            return None
        seen.add(value_id)

        if isinstance(value, dict):
            image_url = self._extract_image_url_from_dict(value, seen)
            if image_url:
                return image_url
            for item in value.values():
                image_url = self._extract_image_url_from_value(item, seen)
                if image_url:
                    return image_url
            return None

        if isinstance(value, (list, tuple, set)):
            for item in value:
                image_url = self._extract_image_url_from_value(item, seen)
                if image_url:
                    return image_url
            return None

        if isinstance(value, Image) or self._is_image_like_object(value):
            for attr_name in ["url", "file", "path", "image", "image_url"]:
                image_url = self._normalize_input_image_url(getattr(value, attr_name, None))
                if image_url:
                    return image_url

        for attr_name in [
            "message",
            "messages",
            "message_chain",
            "chain",
            "raw_message",
            "raw",
            "source",
            "reply",
            "quote",
        ]:
            if hasattr(value, attr_name):
                image_url = self._extract_image_url_from_value(getattr(value, attr_name, None), seen)
                if image_url:
                    return image_url
        return None

    def _extract_image_url_from_dict(self, value: Dict[str, Any], seen: set) -> Optional[str]:
        data = value.get("data") if isinstance(value.get("data"), dict) else value
        raw_type = str(value.get("type") or value.get("msg_type") or value.get("message_type") or "").lower()
        is_image = raw_type in {"image", "pic", "picture"} or any(key in data for key in ["image", "image_url"])

        if is_image:
            for key in ["url", "file", "path", "image", "image_url"]:
                image_url = self._normalize_input_image_url(data.get(key))
                if image_url:
                    return image_url

        for key in ["message", "messages", "message_chain", "chain", "raw_message", "raw", "source", "reply", "quote"]:
            image_url = self._extract_image_url_from_value(value.get(key), seen)
            if image_url:
                return image_url
        return None

    def _extract_image_url_from_text(self, text: str) -> Optional[str]:
        if not text:
            return None

        cq_match = re.search(r"\[CQ:image,[^\]]*(?:url|file)=([^,\]]+)", text, re.IGNORECASE)
        if cq_match:
            image_url = self._normalize_input_image_url(unquote(cq_match.group(1)))
            if image_url:
                return image_url

        for match in URL_PATTERN.finditer(text):
            image_url = self._normalize_input_image_url(match.group(0))
            if image_url:
                return image_url
        return None

    def _is_image_like_object(self, value: Any) -> bool:
        class_name = value.__class__.__name__.lower()
        raw_type = str(getattr(value, "type", "") or getattr(value, "msg_type", "") or "").lower()
        return "image" in class_name or raw_type in {"image", "componenttype.image"}

    def _normalize_input_image_url(self, value: Any) -> Optional[str]:
        if not isinstance(value, str):
            return None

        image_url = value.strip().strip('"\'')
        if not image_url.startswith(("http://", "https://")):
            return None
        return image_url

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

        author = self._format_aggregate_author(data.get("author"))
        title = str(data.get("title") or data.get("desc") or "").strip()
        resource_type = str(data.get("type") or "").strip().lower()
        if resource_type:
            display_type = "live_video" if self._is_live_resource(data) and self._pick_live_video_urls(data) else resource_type
            lines.append(f"📌 类型：{display_type}")
        if author:
            lines.append(f"👤 作者：{author}")
        if title:
            lines.append(f"📝 标题：{title}")

        video_url = self._pick_video_url(data)
        image_urls = self._pick_image_urls(data)
        live_video_urls = self._pick_live_video_urls(data)
        if self._is_live_resource(data) and live_video_urls:
            image_urls = []
        live_photo_count = self._count_live_photos(data)
        backup_count = self._count_video_backups(data)
        image_count = len(image_urls)
        video_count = (1 if video_url else 0) + len(live_video_urls)

        if video_url or image_urls or live_video_urls:
            lines.append("📦 资源概览：")
            lines.append(f"- 视频：{video_count} 个")
            lines.append(f"- 图片：{image_count} 张")
            if live_photo_count:
                lines.append(f"- 实况：{live_photo_count} 个")
            if backup_count:
                lines.append(f"- 备份画质：{backup_count} 个")

        if video_url:
            lines.append("🎬 视频链接：")
            lines.append(video_url)
        if live_video_urls:
            lines.append("🎬 实况视频链接：")
            lines.extend([f"{index}. {item}" for index, item in enumerate(live_video_urls, start=1)])
        if image_urls and include_image_links:
            lines.append("🖼️ 图片链接：")
            lines.extend([f"{index}. {item}" for index, item in enumerate(image_urls, start=1)])

        if not video_url and not image_urls and not live_video_urls:
            return "⚠️ 聚合解析完成，但未找到视频或图片链接"

        return "\n".join(lines)

    def _format_aggregate_author(self, author: Any) -> str:
        if isinstance(author, dict):
            name = str(author.get("name") or author.get("nickname") or "").strip()
            author_id = str(author.get("id") or author.get("uid") or author.get("user_id") or "").strip()
            display_name = self._normalize_author_name(name) or name
            if display_name and author_id:
                return f"{display_name}（{author_id}）"
            return display_name or author_id

        if isinstance(author, str):
            return self._normalize_author_name(author) or author.strip()
        return ""

    def _count_video_backups(self, data: Dict[str, Any]) -> int:
        video_backup = data.get("video_backup")
        return len(video_backup) if isinstance(video_backup, list) else 0

    def _count_live_photos(self, data: Dict[str, Any]) -> int:
        live_photo = data.get("live_photo")
        if isinstance(live_photo, list):
            return len(live_photo)

        live_photos = data.get("livePhotos")
        if isinstance(live_photos, list):
            return len(live_photos)

        images = data.get("images")
        if isinstance(images, list):
            return sum(1 for item in images if isinstance(item, dict) and item.get("livePhoto"))
        return 0

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
            row = display_names[start_index:start_index + 3]
            if len(row) == 3:
                rows.append("┆".join(row))
            else:
                rows.append("┆".join(row))

        if not rows:
            rows.append("暂无可播放内容")

        lines: List[str] = [
            "──   ──",
            "视频系列",
            "────        ──────",
            *rows,
            "───     ─────",
        ]
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

    def _load_profile_play_urls(self, file_path: str) -> List[str]:
        try:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as file:
                return [line.strip() for line in file if line.strip()]
        except Exception as exc:
            logger.warning("读取抖音主页播放文件失败: %s", exc)
            return []

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
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            logger.warning("直链访问失败，回退原链接: %s", self._format_network_error(exc))
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
        if "activity.qianwen.com" in url_text or "ai-studio-mobile" in url_text or "qwen-share" in url_text:
            return "通义千问 AI Studio"
        if "qianwen.com/share/chat" in url_text or "qianwen.com" in url_text:
            return "通义千问"
        if "quark-aistudio" in url_text:
            return "通义千问 AI Studio"
        if "doubao.com" in url_text:
            return "豆包"
        if "jimeng.jianying.com" in url_text or "capcut.cn" in url_text:
            return "即梦"
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
        resource_type = str(data.get("type") or "").strip().lower()
        if resource_type in {"image", "images", "photo", "album", "gallery", "img", "live"}:
            return None

        primary_url = data.get("url")
        if self._is_probable_video_url(primary_url):
            return primary_url

        video_backup = data.get("video_backup")
        if isinstance(video_backup, list):
            sorted_backups = sorted(
                [item for item in video_backup if isinstance(item, dict)],
                key=lambda item: (
                    self._safe_int(item.get("quality_type")),
                    self._safe_int(item.get("height")),
                    self._safe_int(item.get("width")),
                    self._safe_int(item.get("bit_rate")),
                ),
                reverse=True,
            )
            for item in sorted_backups:
                candidate_url = item.get("url")
                if self._is_probable_video_url(candidate_url):
                    return candidate_url

        raw_payload = data.get("raw") if isinstance(data.get("raw"), dict) else {}
        raw_media = raw_payload.get("media") if isinstance(raw_payload.get("media"), list) else []

        for item in raw_media:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("kind") or item.get("type") or "").strip().lower()
            candidate_url = item.get("url")
            if kind == "video" and self._is_probable_video_url(candidate_url):
                return candidate_url

        videos = data.get("videos")
        if isinstance(videos, list):
            sorted_videos = sorted(
                [item for item in videos if isinstance(item, dict)],
                key=lambda item: (
                    self._safe_int(item.get("height")),
                    self._safe_int(item.get("width")),
                    self._safe_int(item.get("duration")),
                ),
                reverse=True,
            )
            for item in sorted_videos:
                candidate_url = item.get("url")
                item_type = str(item.get("type") or "video").strip().lower()
                if item_type == "video" and self._is_probable_video_url(candidate_url):
                    return candidate_url

        urls = data.get("urls")
        if isinstance(urls, list):
            for item in urls:
                if self._is_probable_video_url(item):
                    return item

        candidates = [
            data.get("play"),
            data.get("play_url"),
            data.get("download_url"),
            data.get("source_url"),
            data.get("video"),
            data.get("video_url"),
        ]
        for item in candidates:
            if self._is_probable_video_url(item):
                return item
        return None

    def _safe_int(self, value: Any) -> int:
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return 0

    def _is_probable_video_url(self, value: Any) -> bool:
        if not isinstance(value, str):
            return False

        url = value.strip()
        if not url.startswith(("http://", "https://")):
            return False

        lowered_url = url.lower()
        if lowered_url.endswith((".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg")):
            return False

        if any(keyword in lowered_url for keyword in ["/ies-music/", "music", "audio", "song"]):
            return False

        return True

    def _is_live_resource(self, data: Dict[str, Any]) -> bool:
        resource_type = str(data.get("type") or "").strip().lower()
        if resource_type in {"live", "live_photo", "livephoto", "实况"}:
            return True

        live_photos = data.get("livePhotos")
        if isinstance(live_photos, list) and live_photos:
            return True

        images = data.get("images")
        if isinstance(images, list):
            return any(isinstance(item, dict) and item.get("livePhoto") for item in images)
        return False

    def _compress_image_url(self, url: str) -> str:
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            return url

        lowered_url = url.lower()
        if "xiaohongshu.com" not in lowered_url and "xhscdn" not in lowered_url:
            return url

        clean_url = re.sub(r"imageView2/2/w/\d+(?:/format/[a-z0-9]+)?", "imageView2/2/w/1080/format/jpg", url, flags=re.IGNORECASE)
        if clean_url == url:
            separator = "&" if "?" in clean_url else "?"
            clean_url = f"{clean_url}{separator}imageView2/2/w/1080/format/jpg"
        return clean_url

    def _should_embed_image_url(self, url: str) -> bool:
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            return False

        lowered_url = url.lower()
        unsafe_markers = (
            "byteimg.com",
            "imagex-sign",
            "flow-imagex",
            "xiaohongshu.com",
            "xhslink.com",
            "xhscdn",
        )
        return not any(marker in lowered_url for marker in unsafe_markers)

    def _pick_live_video_urls(self, data: Dict[str, Any]) -> List[str]:
        candidates: List[str] = []
        for key in ["live_photo", "livePhotos", "images"]:
            value = data.get(key)
            if not isinstance(value, list):
                continue
            for item in value:
                if not isinstance(item, dict):
                    continue
                if key == "images" and not item.get("livePhoto"):
                    continue
                video_url = item.get("video")
                if self._is_probable_video_url(video_url):
                    candidates.append(video_url)

        unique_candidates: List[str] = []
        seen = set()
        for item in candidates:
            if item in seen:
                continue
            seen.add(item)
            unique_candidates.append(item)
        return unique_candidates

    def _pick_image_urls(self, data: Dict[str, Any]) -> List[str]:
        candidates: List[str] = []

        live_photo = data.get("live_photo")
        if isinstance(live_photo, list):
            for item in live_photo:
                if not isinstance(item, dict):
                    continue
                image_url = item.get("image")
                if isinstance(image_url, str) and image_url.startswith(("http://", "https://")):
                    candidates.append(image_url)

        raw_payload = data.get("raw") if isinstance(data.get("raw"), dict) else {}
        raw_media = raw_payload.get("media") if isinstance(raw_payload.get("media"), list) else []
        for item in raw_media:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("kind") or item.get("type") or "").strip().lower()
            if kind != "image":
                continue
            for key in ["url", "preview_url", "download_url"]:
                candidate_url = item.get(key)
                if isinstance(candidate_url, str) and candidate_url.startswith(("http://", "https://")):
                    candidates.append(candidate_url)

        for key in ["images", "image", "imgurl", "image_urls", "pics", "cover"]:
            value = data.get(key)
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, str) and item.startswith(("http://", "https://")):
                        candidates.append(item)
                    elif isinstance(item, dict):
                        for nested_key in ["url", "preview_url", "download_url"]:
                            candidate_url = item.get(nested_key)
                            if isinstance(candidate_url, str) and candidate_url.startswith(("http://", "https://")):
                                candidates.append(candidate_url)
            elif isinstance(value, str) and value.startswith(("http://", "https://")):
                candidates.append(value)

        urls = data.get("urls")
        if isinstance(urls, list):
            candidates.extend(
                item for item in urls
                if isinstance(item, str)
                and item.startswith(("http://", "https://"))
                and not self._is_probable_video_url(item)
            )

        unique_candidates: List[str] = []
        seen = set()
        for item in candidates:
            if item in seen:
                continue
            seen.add(item)
            unique_candidates.append(item)

        return unique_candidates

    def _collect_candidate_urls(self, data: Dict[str, Any]) -> List[str]:
        urls: List[str] = []
        for value in data.values():
            if isinstance(value, str) and value.startswith(("http://", "https://")):
                urls.append(value)
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, str) and item.startswith(("http://", "https://")):
                        urls.append(item)
                    elif isinstance(item, dict):
                        urls.extend(self._collect_candidate_urls(item))
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

    def _get_api_host(self) -> str:
        host = str(self.config.get("api_host", "0.0.0.0") or "0.0.0.0").strip()
        return host or "0.0.0.0"

    def _get_api_port(self) -> int:
        raw_port = self.config.get("api_port", 8080)
        try:
            port = int(raw_port)
        except (TypeError, ValueError):
            port = 8080
        return max(port, 1)

    def _get_api_local_test_url(self, path: str = "/health") -> str:
        port = self._get_api_port()
        return f"http://127.0.0.1:{port}{path}"

    def _check_api_local_connectivity(self) -> Tuple[bool, str]:
        if not self.api_running:
            return False, "API 服务未运行"
        try:
            with urlopen(self._get_api_local_test_url(), timeout=3) as response:
                body = response.read().decode("utf-8", errors="ignore").strip()
                return True, f"HTTP {response.status} {body}"
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"

    def _format_api_status(self) -> str:
        host = self._get_api_host()
        port = self._get_api_port()
        thread_alive = bool(self.api_server_thread and self.api_server_thread.is_alive())
        ok, check_message = self._check_api_local_connectivity()
        status = "运行中" if self.api_running else "未运行"
        check_label = "成功" if ok else "失败"
        return (
            f"📊 API 服务状态：{status}\n"
            f"🌐 监听地址：{host}:{port}\n"
            f"🧵 服务线程：{'存活' if thread_alive else '未存活'}\n"
            f"🩺 本机自检：{check_label}（{check_message}）\n"
            f"🔗 本机测试：{self._get_api_local_test_url('/api?type=json')}\n"
            f"📌 指定播放：/api?type=json&file=文件名&index=1\n"
            f"⚠️ 如果 AstrBot 跑在 Docker/容器里，宿主机执行 127.0.0.1:{port} 必须先映射端口，例如 -p {port}:{port}；否则只能在容器内部 curl。"
        )

    def _get_dyrand_data(
        self,
        file_key: Optional[str] = None,
        video_index: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        """获取随机或指定主页/视频数据，供指令和 API 共用。"""
        txt_files = self._list_download_txt_files()
        if not txt_files:
            return None

        if file_key:
            file_path = self._find_download_txt(file_key)
            if not file_path:
                return None
            file_name = os.path.basename(file_path)
            try:
                current_index = txt_files.index(file_name)
            except ValueError:
                current_index = 0
            urls = self._load_profile_play_urls(file_path)
            if not urls:
                return None
        else:
            current_index = self.random_profile_index % len(txt_files)
            file_name = txt_files[current_index]
            self.random_profile_index = (current_index + 1) % len(txt_files)
            file_path = os.path.join(DOWNLOAD_DIR, file_name)

            urls = self._load_profile_play_urls(file_path)
            checked_count = 1
            while not urls and checked_count < len(txt_files):
                current_index = self.random_profile_index % len(txt_files)
                file_name = txt_files[current_index]
                self.random_profile_index = (current_index + 1) % len(txt_files)
                file_path = os.path.join(DOWNLOAD_DIR, file_name)
                urls = self._load_profile_play_urls(file_path)
                checked_count += 1

            if not urls:
                return None

        if video_index is None:
            video_index = self._pick_random_play_index(file_path, len(urls))
        elif video_index < 0 or video_index >= len(urls):
            return None

        playable_url = self._resolve_direct_media_url(urls[video_index])
        return {
            "file_name": file_name,
            "profile_index": current_index,
            "total_profiles": len(txt_files),
            "video_index": video_index,
            "total_videos": len(urls),
            "video_url": urls[video_index],
            "playable_url": playable_url,
        }

    def _start_api_server(self) -> None:
        """启动 HTTP API 服务（使用内置 http.server 在守护线程中运行）。"""
        if self.api_running:
            logger.warning("API 服务已在运行，跳过重复启动")
            return

        host = self._get_api_host()
        port = self._get_api_port()
        plugin_ref = self

        class _ApiHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                from urllib.parse import parse_qs, urlparse
                parsed = urlparse(self.path)
                if parsed.path == "/health":
                    body = b"ok"
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return

                if parsed.path != "/api":
                    self.send_response(404)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.end_headers()
                    self.wfile.write("404 Not Found".encode("utf-8"))
                    return

                params = parse_qs(parsed.query)
                response_type = str(params.get("type", ["json"])[0]).strip().lower()
                file_key = str(params.get("file", [""])[0] or params.get("name", [""])[0]).strip()
                index_text = str(params.get("index", [""])[0] or params.get("video_index", [""])[0]).strip()
                if response_type == "menu":
                    menu_text = plugin_ref._format_douyin_menu(plugin_ref._list_download_txt_files())
                    if menu_text.strip():
                        body = menu_text.encode("utf-8")
                        self.send_response(200)
                        self.send_header("Content-Type", "text/plain; charset=utf-8")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                    else:
                        body = "暂无可播放的主页记录".encode("utf-8")
                        self.send_response(404)
                        self.send_header("Content-Type", "text/plain; charset=utf-8")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                    return
                requested_index: Optional[int] = None
                if index_text:
                    try:
                        requested_index = int(index_text)
                    except ValueError:
                        error_message = "index 参数必须是整数"
                        if response_type == "text":
                            self.send_response(400)
                            self.send_header("Content-Type", "text/plain; charset=utf-8")
                            self.end_headers()
                            self.wfile.write(error_message.encode("utf-8"))
                        else:
                            body = json.dumps({"code": 400, "msg": error_message}, ensure_ascii=False).encode("utf-8")
                            self.send_response(400)
                            self.send_header("Content-Type", "application/json; charset=utf-8")
                            self.send_header("Content-Length", str(len(body)))
                            self.end_headers()
                            self.wfile.write(body)
                        return

                if requested_index is not None and requested_index < 1:
                    error_message = "index 参数必须从 1 开始"
                    if response_type == "text":
                        self.send_response(400)
                        self.send_header("Content-Type", "text/plain; charset=utf-8")
                        self.end_headers()
                        self.wfile.write(error_message.encode("utf-8"))
                    else:
                        body = json.dumps({"code": 400, "msg": error_message}, ensure_ascii=False).encode("utf-8")
                        self.send_response(400)
                        self.send_header("Content-Type", "application/json; charset=utf-8")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                    return

                if file_key and not plugin_ref._find_download_txt(file_key):
                    error_message = f"未找到文件：{file_key}"
                    if response_type == "text":
                        self.send_response(404)
                        self.send_header("Content-Type", "text/plain; charset=utf-8")
                        self.end_headers()
                        self.wfile.write(error_message.encode("utf-8"))
                    else:
                        body = json.dumps({"code": 404, "msg": error_message}, ensure_ascii=False).encode("utf-8")
                        self.send_response(404)
                        self.send_header("Content-Type", "application/json; charset=utf-8")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                    return

                result = plugin_ref._get_dyrand_data(
                    file_key=file_key or None,
                    video_index=(requested_index - 1) if requested_index is not None and requested_index != -1 else None,
                )
                if result is None:
                    if file_key and requested_index is not None and requested_index != -1:
                        error_message = "指定的 index 超出当前 TXT 文件范围"
                        status_code = 404
                    elif file_key:
                        error_message = f"TXT 文件为空：{file_key}"
                        status_code = 404
                    else:
                        error_message = "暂无可播放的主页记录"
                        status_code = 404

                    if response_type == "text":
                        self.send_response(status_code)
                        self.send_header("Content-Type", "text/plain; charset=utf-8")
                        self.end_headers()
                        self.wfile.write(error_message.encode("utf-8"))
                    else:
                        body = json.dumps({"code": status_code, "msg": error_message}, ensure_ascii=False).encode("utf-8")
                        self.send_response(status_code)
                        self.send_header("Content-Type", "application/json; charset=utf-8")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                    return

                if response_type == "video":
                    self.send_response(302)
                    self.send_header("Location", result["playable_url"])
                    self.end_headers()
                    return

                if response_type == "text":
                    mode_label = "指定播放" if (file_key or requested_index is not None) else "随机播放"
                    text = (
                        f"🎲 {mode_label}主页：{result['file_name']}\n"
                        f"📍 主页进度：{result['profile_index'] + 1}/{result['total_profiles']}\n"
                        f"🎬 视频序号：{result['video_index'] + 1}/{result['total_videos']}\n"
                        f"🔗 视频直链：{result['playable_url']}"
                    )
                    body = text.encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return

                # default: json
                json_data = {"code": 200, "msg": "success", "data": result}
                if file_key or requested_index is not None:
                    json_data["mode"] = "specified"
                    json_data["request"] = {
                        "file": file_key or "",
                        "index": requested_index,
                    }
                body = json.dumps(json_data, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format, *args):
                logger.debug("API %s", format % args)

        class _ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
            allow_reuse_address = True
            daemon_threads = True

        self.api_http_server = _ThreadedHTTPServer((host, port), _ApiHandler)
        self.api_server_thread = threading.Thread(
            target=self.api_http_server.serve_forever,
            daemon=True,
            name="dyrand-api-server",
        )
        self.api_server_thread.start()
        self.api_running = True
        ok, check_message = self._check_api_local_connectivity()
        logger.info("API 服务已启动，监听 %s:%s，本机自检：%s %s", host, port, "成功" if ok else "失败", check_message)

    def _stop_api_server(self) -> None:
        """停止 HTTP API 服务。"""
        if self.api_http_server is not None:
            self.api_http_server.shutdown()
            self.api_http_server.server_close()
            self.api_http_server = None
        if self.api_server_thread is not None:
            self.api_server_thread.join(timeout=5)
            self.api_server_thread = None
        self.api_running = False
        logger.info("API 服务已关闭")

    async def terminate(self):
        if self.auto_update_task and not self.auto_update_task.done():
            self.auto_update_task.cancel()
        if self.api_running:
            self._stop_api_server()
        logger.info("media_parser 插件已卸载")
