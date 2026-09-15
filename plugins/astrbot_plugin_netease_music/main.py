"""网易云临时登录与歌单分析插件。"""

from __future__ import annotations

import asyncio
import base64
import json
import math
import re
import time
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from sys import maxsize
from typing import Any
from urllib.parse import urlparse

import aiohttp

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import At, Image, Plain
from astrbot.api.star import Context, Star
from astrbot.core.agent.message import (
    AssistantMessageSegment,
    TextPart,
    UserMessageSegment,
)
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

PLUGIN_NAME = "astrbot_plugin_netease_music"
BINDINGS_KEY = "account_bindings"
DEFAULT_API_BASE_URL = "http://127.0.0.1:3010"
DEFAULT_REQUEST_TIMEOUT = 20
DEFAULT_QR_POLL_INTERVAL = 3
DEFAULT_QR_LOGIN_TIMEOUT = 300
DEFAULT_CONFIRMATION_TIMEOUT = 600
DEFAULT_MESSAGE_CHUNK_CHARS = 1500
DEFAULT_ANALYSIS_TRACK_LIMIT = 600
DEFAULT_ANALYSIS_PROMPT_MAX_CHARS = 80000
DEFAULT_ANALYSIS_PROMPT = (
    "请用中文分析歌单，并依次包含：\n"
    "1. 整体画像；\n"
    "2. 音乐偏好与结构；\n"
    "3. 有数据或曲目依据的亮点与变化；\n"
    "4. 代表曲目；\n"
    "5. 简短总结。\n"
    "不要使用 Markdown 表格。"
)
ANALYSIS_PROMPT_HEADER = "\n\n【分析要求】\n"
MAX_PLAYLISTS = 20000
MAX_TRACKS = 20000
ECHO_GUARD_SECONDS = 30


class NeteaseApiError(RuntimeError):
    """网易云 API 调用失败。"""


class NeteaseAuthError(NeteaseApiError):
    """网易云登录状态失效。"""


@dataclass(slots=True)
class PendingLogin:
    """尚未由 AstrBot 用户确认的扫码登录。"""

    identity: str
    event: AstrMessageEvent
    umo: str
    platform_id: str
    user_id: str
    display_name: str
    login_key: str
    created_at: float
    task: asyncio.Task[None] | None = None
    cookie: str = ""
    uid: str = ""
    nickname: str = ""
    confirmation_deadline: float = 0.0


@dataclass(slots=True)
class PlaylistAnalysisData:
    """供 LLM 分析的歌单数据。"""

    prompt: str
    track_count: int
    included_track_count: int


def _coerce_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _compact_text(value: Any, max_length: int = 300) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= max_length:
        return text
    return text[: max(0, max_length - 3)].rstrip() + "..."


def _normalize_echo_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def normalize_analysis_prompt(value: Any) -> str:
    """规范化管理员配置的歌单分析要求。"""
    prompt = str(value or "").strip()
    return prompt or DEFAULT_ANALYSIS_PROMPT


def compose_analysis_request(
    data_prompt: str,
    analysis_prompt: str,
    max_chars: int,
) -> str:
    """在总长度限制内组合歌单数据与可配置分析要求。"""
    max_chars = max(2000, int(max_chars))
    guidance = normalize_analysis_prompt(analysis_prompt)
    max_guidance_chars = max(500, max_chars // 3)
    if len(guidance) > max_guidance_chars:
        guidance = guidance[: max_guidance_chars - 3].rstrip() + "..."
    data_budget = max(500, max_chars - len(ANALYSIS_PROMPT_HEADER) - len(guidance))
    data = data_prompt[:data_budget].rstrip()
    return data + ANALYSIS_PROMPT_HEADER + guidance


def decode_qr_image(data_url: str) -> bytes:
    """解码 Enhanced API 返回的二维码 data URL。"""
    if not isinstance(data_url, str) or "," not in data_url:
        raise NeteaseApiError("网易云接口没有返回有效二维码图片")
    header, encoded = data_url.split(",", 1)
    if ";base64" not in header.lower():
        raise NeteaseApiError("网易云接口返回了不支持的二维码格式")
    try:
        image = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise NeteaseApiError("网易云二维码图片解码失败") from exc
    if not image:
        raise NeteaseApiError("网易云二维码图片为空")
    return image


def normalize_profile(payload: dict[str, Any]) -> tuple[str, str]:
    """从 login/status 响应中提取 UID 和昵称。"""
    data = payload.get("data")
    if not isinstance(data, dict):
        data = payload
    code = _coerce_int(data.get("code"), _coerce_int(payload.get("code")))
    profile = data.get("profile")
    account = data.get("account")
    if code == 301 or not isinstance(profile, dict):
        raise NeteaseAuthError("网易云登录状态已失效，请重新临时登录")
    uid = str(
        profile.get("userId")
        or (account.get("id") if isinstance(account, dict) else "")
        or ""
    ).strip()
    nickname = _compact_text(profile.get("nickname"), 80)
    if not uid:
        raise NeteaseAuthError("网易云账号信息不完整，请重新临时登录")
    return uid, nickname or f"UID {uid}"


def normalize_playlist(item: dict[str, Any], owner_uid: str) -> dict[str, Any]:
    """提取歌单展示与索引所需字段。"""
    creator = item.get("creator")
    creator_uid = ""
    if isinstance(creator, dict):
        creator_uid = str(creator.get("userId") or "")
    playlist_id = str(item.get("id") or "").strip()
    return {
        "netease_id": playlist_id,
        "name": _compact_text(item.get("name"), 120) or f"歌单 {playlist_id}",
        "track_count": max(0, _coerce_int(item.get("trackCount"))),
        "owned": bool(creator_uid and creator_uid == str(owner_uid)),
    }


def format_playlist_list(nickname: str, playlists: list[dict[str, Any]]) -> str:
    """生成带 1 起始索引的完整歌单列表。"""
    lines = [f"网易云账号：{nickname}", f"共 {len(playlists)} 个歌单："]
    for index, playlist in enumerate(playlists, start=1):
        ownership = "创建" if playlist.get("owned") else "收藏"
        lines.append(
            f"{index}. {playlist.get('name', '未命名歌单')} "
            f"（{playlist.get('track_count', 0)} 首，{ownership}）"
        )
    if not playlists:
        lines.append("暂无歌单。")
    return "\n".join(lines)


def split_message(text: str, max_chars: int) -> list[str]:
    """优先按行拆分长消息，且不丢失任何文本。"""
    max_chars = max(200, int(max_chars))
    if len(text) <= max_chars:
        return [text]
    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        while len(line) > max_chars:
            if current:
                chunks.append(current.rstrip("\n"))
                current = ""
            chunks.append(line[:max_chars].rstrip("\n"))
            line = line[max_chars:]
        if current and len(current) + len(line) > max_chars:
            chunks.append(current.rstrip("\n"))
            current = ""
        current += line
    if current:
        chunks.append(current.rstrip("\n"))
    return [chunk for chunk in chunks if chunk]


def _track_artists(track: dict[str, Any]) -> list[str]:
    artists = track.get("ar") or track.get("artists") or []
    if not isinstance(artists, list):
        return []
    names = []
    for artist in artists:
        if isinstance(artist, dict):
            name = _compact_text(artist.get("name"), 80)
            if name:
                names.append(name)
    return names


def _track_album(track: dict[str, Any]) -> str:
    album = track.get("al") or track.get("album") or {}
    if not isinstance(album, dict):
        return ""
    return _compact_text(album.get("name"), 100)


def _track_year(track: dict[str, Any]) -> int | None:
    published_at = _coerce_int(track.get("publishTime"))
    if published_at <= 0:
        return None
    try:
        return datetime.fromtimestamp(published_at / 1000, tz=timezone.utc).year
    except (OverflowError, OSError, ValueError):
        return None


def _even_sample(items: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    if limit <= 0 or len(items) <= limit:
        return list(items)
    if limit == 1:
        return [items[0]]
    indexes = {
        round(position * (len(items) - 1) / (limit - 1)) for position in range(limit)
    }
    return [items[index] for index in sorted(indexes)]


def build_analysis_data(
    detail: dict[str, Any],
    tracks: list[dict[str, Any]],
    track_limit: int,
    max_chars: int,
) -> PlaylistAnalysisData:
    """基于全量统计和均匀曲目样本构造受控长度的 LLM 输入。"""
    artist_counts: Counter[str] = Counter()
    album_counts: Counter[str] = Counter()
    year_counts: Counter[int] = Counter()
    total_duration_ms = 0
    valid_tracks: list[dict[str, Any]] = []

    for track in tracks:
        if not isinstance(track, dict):
            continue
        valid_tracks.append(track)
        artist_counts.update(_track_artists(track))
        album = _track_album(track)
        if album:
            album_counts[album] += 1
        year = _track_year(track)
        if year:
            year_counts[year] += 1
        total_duration_ms += max(
            0, _coerce_int(track.get("dt") or track.get("duration"))
        )

    playlist = (
        detail.get("playlist") if isinstance(detail.get("playlist"), dict) else detail
    )
    name = _compact_text(playlist.get("name"), 160) or "未命名歌单"
    description = _compact_text(playlist.get("description"), 1000) or "无"
    tags = playlist.get("tags") if isinstance(playlist.get("tags"), list) else []
    tag_text = "、".join(_compact_text(tag, 40) for tag in tags if tag) or "无"
    total_hours = total_duration_ms / 3_600_000

    summary_lines = [
        "请分析以下网易云歌单。所有结论都要以数据或曲目为依据，不要推断用户的敏感属性、心理诊断或现实身份。",
        "",
        "【歌单信息】",
        f"名称：{name}",
        f"描述：{description}",
        f"标签：{tag_text}",
        f"歌曲总数：{len(valid_tracks)}",
        f"总时长：约 {total_hours:.1f} 小时",
        f"播放量：{max(0, _coerce_int(playlist.get('playCount')))}",
        f"收藏数：{max(0, _coerce_int(playlist.get('subscribedCount')))}",
        "",
        "【全量统计】",
        "高频歌手："
        + (
            "、".join(
                f"{name}({count})" for name, count in artist_counts.most_common(15)
            )
            or "无"
        ),
        "高频专辑："
        + (
            "、".join(
                f"{name}({count})" for name, count in album_counts.most_common(10)
            )
            or "无"
        ),
        "发行年份："
        + (
            "、".join(f"{year}({count})" for year, count in sorted(year_counts.items()))
            or "无"
        ),
        "",
        "【曲目样本】",
    ]
    prompt = "\n".join(summary_lines)

    sample = _even_sample(valid_tracks, max(1, track_limit))
    included = 0
    for position, track in enumerate(sample, start=1):
        title = _compact_text(track.get("name"), 120) or "未命名歌曲"
        artists = " / ".join(_track_artists(track)) or "未知歌手"
        album = _track_album(track) or "未知专辑"
        year = _track_year(track)
        line = f"{position}. {title} - {artists} | {album}"
        if year:
            line += f" | {year}"
        if len(prompt) + len(line) + 1 > max(2000, max_chars):
            break
        prompt += "\n" + line
        included += 1

    return PlaylistAnalysisData(
        prompt=prompt,
        track_count=len(valid_tracks),
        included_track_count=included,
    )


class NeteaseMusicClient:
    """只通过本机 Enhanced API 访问网易云的异步客户端。"""

    def __init__(self, base_url: str, timeout_seconds: int = DEFAULT_REQUEST_TIMEOUT):
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("网易云 API 地址必须是有效的 HTTP(S) 地址")
        self.base_url = base_url.rstrip("/")
        self.timeout = aiohttp.ClientTimeout(total=max(5, timeout_seconds))
        self._session: aiohttp.ClientSession | None = None

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=self.timeout,
                cookie_jar=aiohttp.DummyCookieJar(),
            )
        return self._session

    async def _post(
        self, path: str, data: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        session = await self._get_session()
        params = {"timestamp": str(int(time.time() * 1000))}
        try:
            async with session.post(
                self.base_url + path,
                params=params,
                data=data or {},
            ) as response:
                if response.status < 200 or response.status >= 300:
                    raise NeteaseApiError(
                        f"网易云服务请求失败（HTTP {response.status}）"
                    )
                payload = await response.json(content_type=None)
        except asyncio.TimeoutError as exc:
            raise NeteaseApiError("网易云服务请求超时") from exc
        except aiohttp.ClientError as exc:
            raise NeteaseApiError("无法连接网易云服务") from exc
        except (ValueError, TypeError) as exc:
            raise NeteaseApiError("网易云服务返回了无效数据") from exc
        if not isinstance(payload, dict):
            raise NeteaseApiError("网易云服务返回了无效数据")
        return payload

    @staticmethod
    def _require_success(payload: dict[str, Any], action: str) -> None:
        code = _coerce_int(payload.get("code"), 200)
        if code == 301:
            raise NeteaseAuthError("网易云登录状态已失效，请重新临时登录")
        if code != 200:
            raise NeteaseApiError(f"{action}失败（网易云状态码 {code}）")

    async def create_login_qr(self) -> tuple[str, bytes]:
        key_payload = await self._post("/login/qr/key")
        self._require_success(key_payload, "生成登录二维码")
        key_data = key_payload.get("data")
        login_key = (
            str(key_data.get("unikey") or "").strip()
            if isinstance(key_data, dict)
            else ""
        )
        if not login_key:
            raise NeteaseApiError("网易云接口没有返回二维码登录密钥")

        qr_payload = await self._post(
            "/login/qr/create",
            {"key": login_key, "qrimg": "true"},
        )
        self._require_success(qr_payload, "生成登录二维码")
        qr_data = qr_payload.get("data")
        qr_image = qr_data.get("qrimg") if isinstance(qr_data, dict) else ""
        return login_key, decode_qr_image(str(qr_image or ""))

    async def check_login_qr(self, login_key: str) -> dict[str, Any]:
        return await self._post(
            "/login/qr/check",
            {"key": login_key, "noCookie": "true"},
        )

    async def get_profile(self, cookie: str) -> tuple[str, str]:
        payload = await self._post("/login/status", {"cookie": cookie})
        return normalize_profile(payload)

    async def logout(self, cookie: str) -> None:
        if not cookie:
            return
        try:
            await self._post("/logout", {"cookie": cookie})
        except NeteaseApiError:
            logger.warning("[%s] 清理未确认的网易云临时会话失败", PLUGIN_NAME)

    async def list_playlists(self, uid: str, cookie: str) -> list[dict[str, Any]]:
        playlists: list[dict[str, Any]] = []
        seen: set[str] = set()
        limit = 200
        offset = 0
        while offset < MAX_PLAYLISTS:
            payload = await self._post(
                "/user/playlist",
                {
                    "uid": uid,
                    "limit": str(limit),
                    "offset": str(offset),
                    "cookie": cookie,
                },
            )
            self._require_success(payload, "获取歌单")
            batch = payload.get("playlist")
            if not isinstance(batch, list):
                raise NeteaseApiError("网易云服务没有返回有效歌单列表")
            for item in batch:
                if not isinstance(item, dict):
                    continue
                normalized = normalize_playlist(item, uid)
                playlist_id = normalized["netease_id"]
                if playlist_id and playlist_id not in seen:
                    playlists.append(normalized)
                    seen.add(playlist_id)
            if payload.get("more") is False or len(batch) < limit:
                break
            offset += limit
        if offset >= MAX_PLAYLISTS:
            raise NeteaseApiError("歌单数量超过插件可处理上限")
        return playlists

    async def get_playlist_detail(
        self, playlist_id: str, cookie: str
    ) -> dict[str, Any]:
        payload = await self._post(
            "/playlist/detail",
            {"id": playlist_id, "s": "0", "cookie": cookie},
        )
        self._require_success(payload, "获取歌单详情")
        if not isinstance(payload.get("playlist"), dict):
            raise NeteaseApiError("网易云服务没有返回有效歌单详情")
        return payload

    async def get_playlist_tracks(
        self,
        playlist_id: str,
        cookie: str,
        expected_count: int,
    ) -> list[dict[str, Any]]:
        tracks: list[dict[str, Any]] = []
        limit = 500
        offset = 0
        target_count = min(max(0, expected_count), MAX_TRACKS)
        while offset < MAX_TRACKS and (target_count == 0 or offset < target_count):
            page_limit = min(limit, target_count - offset) if target_count else limit
            payload = await self._post(
                "/playlist/track/all",
                {
                    "id": playlist_id,
                    "limit": str(page_limit),
                    "offset": str(offset),
                    "cookie": cookie,
                },
            )
            self._require_success(payload, "获取歌单歌曲")
            batch = payload.get("songs")
            if not isinstance(batch, list):
                raise NeteaseApiError("网易云服务没有返回有效歌曲列表")
            tracks.extend(item for item in batch if isinstance(item, dict))
            if len(batch) < page_limit:
                break
            offset += page_limit
        if expected_count > MAX_TRACKS:
            raise NeteaseApiError("歌单歌曲数量超过插件可处理上限")
        return tracks


class NeteaseMusicPlugin(Star):
    """网易云临时登录与歌单分析。"""

    def __init__(self, context: Context, config: dict[str, Any] | None = None):
        super().__init__(context, config)
        self.config = config or {}
        self.enabled = bool(self.config.get("enable", True))
        self.api = NeteaseMusicClient(
            str(self.config.get("api_base_url", DEFAULT_API_BASE_URL)),
            _coerce_int(self.config.get("request_timeout"), DEFAULT_REQUEST_TIMEOUT),
        )
        self.qr_poll_interval = max(
            1,
            _coerce_int(self.config.get("qr_poll_interval"), DEFAULT_QR_POLL_INTERVAL),
        )
        self.qr_login_timeout = max(
            30,
            _coerce_int(self.config.get("qr_login_timeout"), DEFAULT_QR_LOGIN_TIMEOUT),
        )
        self.confirmation_timeout = max(
            30,
            _coerce_int(
                self.config.get("confirmation_timeout"),
                DEFAULT_CONFIRMATION_TIMEOUT,
            ),
        )
        self.message_chunk_chars = max(
            500,
            _coerce_int(
                self.config.get("message_chunk_chars"),
                DEFAULT_MESSAGE_CHUNK_CHARS,
            ),
        )
        self.analysis_track_limit = max(
            50,
            _coerce_int(
                self.config.get("analysis_track_limit"),
                DEFAULT_ANALYSIS_TRACK_LIMIT,
            ),
        )
        self.analysis_prompt_max_chars = max(
            10000,
            _coerce_int(
                self.config.get("analysis_prompt_max_chars"),
                DEFAULT_ANALYSIS_PROMPT_MAX_CHARS,
            ),
        )
        self.analysis_prompt = normalize_analysis_prompt(
            self.config.get("analysis_prompt", DEFAULT_ANALYSIS_PROMPT)
        )
        self.data_dir = Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_NAME
        self.qr_dir = self.data_dir / "qr"
        self._pending_logins: dict[str, PendingLogin] = {}
        self._outgoing_echoes: dict[tuple[str, str], deque[float]] = defaultdict(deque)
        self._context_locks: dict[str, asyncio.Lock] = {}
        self._storage_lock = asyncio.Lock()
        self._analysis_users: set[str] = set()

    @filter.command_group("网易云")
    def netease(self):
        """网易云账号与歌单。"""

    @netease.command("临时登录")
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def temporary_login(self, event: AstrMessageEvent):
        """发送网易云登录二维码。"""
        event.stop_event()
        if not self.enabled:
            await self._reply_command(event, "网易云插件当前未启用。")
            return

        identity = self._identity(event)
        await self._discard_pending(identity, logout=True)
        try:
            login_key, image_bytes = await self.api.create_login_qr()
            self.qr_dir.mkdir(parents=True, exist_ok=True)
            image_path = self.qr_dir / f"{event.get_sender_id()}-{time.time_ns()}.png"
            image_path.write_bytes(image_bytes)
        except (NeteaseApiError, OSError, ValueError) as exc:
            logger.warning("[%s] 创建登录二维码失败: %s", PLUGIN_NAME, exc)
            await self._reply_command(event, str(exc))
            return

        pending = PendingLogin(
            identity=identity,
            event=event,
            umo=event.unified_msg_origin,
            platform_id=event.get_platform_id(),
            user_id=event.get_sender_id(),
            display_name=event.get_sender_name() or event.get_sender_id(),
            login_key=login_key,
            created_at=time.monotonic(),
        )
        self._pending_logins[identity] = pending
        response_text = (
            "请使用网易云音乐 App 扫描二维码并在 App 内确认登录。"
            f"二维码将在 {math.ceil(self.qr_login_timeout / 60)} 分钟内失效。"
        )
        try:
            await self._send_message(
                event,
                [
                    At(qq=pending.user_id),
                    Plain(" " + response_text),
                    Image.fromFileSystem(str(image_path)),
                ],
            )
            await self._record_command_context(event, response_text)
        except Exception as exc:
            logger.exception("[%s] 发送登录二维码失败: %s", PLUGIN_NAME, exc)
            await self._discard_pending(identity, logout=False)
            await self._reply_command(event, "登录二维码发送失败，请稍后重试。")
            return
        finally:
            try:
                image_path.unlink(missing_ok=True)
            except OSError:
                logger.warning("[%s] 未能删除临时二维码文件", PLUGIN_NAME)

        pending.task = asyncio.create_task(self._poll_login(identity, login_key))

    @netease.command("临时登录确认")
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def confirm_temporary_login(self, event: AstrMessageEvent, confirmation: str):
        """确认扫码账号是否属于当前用户。"""
        event.stop_event()
        identity = self._identity(event)
        answer = confirmation.strip().lower()
        if answer not in {"yes", "no"}:
            await self._reply_command(
                event,
                "请使用 /网易云 临时登录确认 yes 或 /网易云 临时登录确认 no。",
            )
            return

        pending = self._pending_logins.get(identity)
        if not pending or not pending.cookie or not pending.uid:
            await self._reply_command(
                event,
                "当前没有等待你确认的网易云登录，请先使用 /网易云 临时登录。",
            )
            return
        if time.monotonic() > pending.confirmation_deadline:
            await self._discard_pending(identity, logout=True)
            await self._reply_command(
                event, "本次登录确认已超时，请重新使用 /网易云 临时登录。"
            )
            return

        if answer == "no":
            nickname = pending.nickname
            await self._discard_pending(identity, logout=True)
            await self._reply_command(
                event, f"已拒绝绑定网易云账号“{nickname}”，临时登录已清理。"
            )
            return

        binding = {
            "uid": pending.uid,
            "nickname": pending.nickname,
            "cookie": pending.cookie,
            "confirmed_at": datetime.now(timezone.utc).isoformat(),
            "playlists": [],
        }
        await self._save_binding(identity, binding)
        nickname = pending.nickname
        uid = pending.uid
        await self._discard_pending(identity, logout=False)
        await self._reply_command(
            event,
            f"已确认并绑定网易云账号“{nickname}”（UID {uid}）。",
        )

    @netease.command("歌单list")
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def list_user_playlists(self, event: AstrMessageEvent):
        """展示当前绑定账号的全部歌单。"""
        event.stop_event()
        if not self.enabled:
            await self._reply_command(event, "网易云插件当前未启用。")
            return
        identity = self._identity(event)
        binding = await self._get_binding(identity)
        if not binding:
            await self._reply_command(
                event, "尚未绑定网易云账号，请先使用 /网易云 临时登录。"
            )
            return

        try:
            uid, nickname = await self.api.get_profile(str(binding.get("cookie", "")))
            playlists = await self.api.list_playlists(
                uid, str(binding.get("cookie", ""))
            )
        except NeteaseAuthError as exc:
            await self._reply_command(event, str(exc))
            return
        except NeteaseApiError as exc:
            logger.warning("[%s] 获取用户歌单失败: %s", PLUGIN_NAME, exc)
            await self._reply_command(event, str(exc))
            return

        binding["uid"] = uid
        binding["nickname"] = nickname
        binding["playlists"] = playlists
        await self._save_binding(identity, binding)
        response_text = format_playlist_list(nickname, playlists)
        await self._reply_command(event, response_text)

    @netease.command("歌单分析")
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def analyze_playlist(self, event: AstrMessageEvent, playlist_index: str):
        """使用当前 LLM 分析指定索引的歌单。"""
        event.stop_event()
        if not self.enabled:
            await self._reply_command(event, "网易云插件当前未启用。")
            return
        try:
            index = int(playlist_index)
        except ValueError:
            await self._reply_command(event, "歌单 ID 必须是大于 0 的整数。")
            return
        if index <= 0:
            await self._reply_command(event, "歌单 ID 必须是大于 0 的整数。")
            return

        identity = self._identity(event)
        if identity in self._analysis_users:
            await self._reply_command(event, "你的歌单正在分析中，请稍候。")
            return
        binding = await self._get_binding(identity)
        if not binding:
            await self._reply_command(
                event, "尚未绑定网易云账号，请先使用 /网易云 临时登录。"
            )
            return
        playlists = binding.get("playlists")
        if not isinstance(playlists, list) or not playlists:
            await self._reply_command(
                event, "请先使用 /网易云 歌单list 获取歌单和对应 ID。"
            )
            return
        if index > len(playlists):
            await self._reply_command(
                event, f"歌单 ID 超出范围，当前可用范围是 1-{len(playlists)}。"
            )
            return
        playlist = playlists[index - 1]
        if not isinstance(playlist, dict) or not playlist.get("netease_id"):
            await self._reply_command(
                event, "这个歌单索引已经失效，请重新使用 /网易云 歌单list。"
            )
            return

        self._analysis_users.add(identity)
        progress_text = (
            f"正在读取并分析歌单“{playlist.get('name', '未命名歌单')}”，请稍候。"
        )
        try:
            await self._send_text(event, progress_text)
            cookie = str(binding.get("cookie", ""))
            await self.api.get_profile(cookie)
            detail = await self.api.get_playlist_detail(
                str(playlist["netease_id"]), cookie
            )
            detail_playlist = detail.get("playlist", {})
            expected_count = _coerce_int(
                detail_playlist.get("trackCount")
                if isinstance(detail_playlist, dict)
                else playlist.get("track_count")
            )
            tracks = await self.api.get_playlist_tracks(
                str(playlist["netease_id"]), cookie, expected_count
            )
            analysis_data_budget = max(
                2000,
                self.analysis_prompt_max_chars
                - len(ANALYSIS_PROMPT_HEADER)
                - len(self.analysis_prompt),
            )
            analysis_data = build_analysis_data(
                detail,
                tracks,
                self.analysis_track_limit,
                analysis_data_budget,
            )
            request_prompt = compose_analysis_request(
                analysis_data.prompt,
                self.analysis_prompt,
                self.analysis_prompt_max_chars,
            )
            provider_id = await self.context.get_current_chat_provider_id(
                umo=event.unified_msg_origin
            )
            if not provider_id:
                raise RuntimeError("当前会话没有可用的 LLM 提供商")
            llm_response = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=request_prompt,
                system_prompt=(
                    "你是严谨的音乐歌单分析助手。请基于输入中的完整统计和曲目样本分析，"
                    "明确区分数据事实与合理概括，避免无证据判断。输出适合群聊阅读的中文文本。"
                ),
            )
            result_text = str(llm_response.completion_text or "").strip()
            if not result_text:
                raise RuntimeError("当前 LLM 没有返回分析结果")
            await self._send_text(event, result_text)
            await self._record_command_context(
                event, progress_text + "\n\n" + result_text
            )
        except NeteaseAuthError as exc:
            await self._send_and_record_failure(event, progress_text, str(exc))
        except (NeteaseApiError, RuntimeError) as exc:
            logger.warning("[%s] 歌单分析失败: %s", PLUGIN_NAME, exc)
            await self._send_and_record_failure(event, progress_text, str(exc))
        except Exception as exc:
            logger.exception("[%s] 歌单分析异常: %s", PLUGIN_NAME, exc)
            await self._send_and_record_failure(
                event, progress_text, "歌单分析失败，请稍后重试。"
            )
        finally:
            self._analysis_users.discard(identity)

    @filter.event_message_type(filter.EventMessageType.ALL, priority=maxsize)
    async def guard_plugin_output_echo(self, event: AstrMessageEvent) -> None:
        """拦截插件输出的机器人回推，避免进入主动回复抽样。"""
        if event.get_sender_id() != event.get_self_id():
            return
        if self._consume_outgoing_message(
            event.unified_msg_origin,
            event.message_str or "",
        ):
            event.is_at_or_wake_command = True
            event.is_wake = True
            event.stop_event()

    async def _poll_login(self, identity: str, login_key: str) -> None:
        consecutive_errors = 0
        while True:
            await asyncio.sleep(self.qr_poll_interval)
            pending = self._pending_logins.get(identity)
            if not pending or pending.login_key != login_key:
                return
            if time.monotonic() - pending.created_at > self.qr_login_timeout:
                await self._expire_qr_login(identity, login_key)
                return
            try:
                status = await self.api.check_login_qr(login_key)
                consecutive_errors = 0
            except NeteaseApiError as exc:
                consecutive_errors += 1
                logger.warning(
                    "[%s] 二维码状态查询失败（%d/3）: %s",
                    PLUGIN_NAME,
                    consecutive_errors,
                    exc,
                )
                if consecutive_errors >= 3:
                    await self._send_message(
                        pending.event,
                        [
                            At(qq=pending.user_id),
                            Plain(" 登录状态查询失败，请重新使用 /网易云 临时登录。"),
                        ],
                    )
                    await self._append_assistant_context(
                        pending,
                        "登录状态查询失败，请重新使用 /网易云 临时登录。",
                    )
                    self._pending_logins.pop(identity, None)
                    return
                continue

            code = _coerce_int(status.get("code"))
            if code in {801, 802}:
                continue
            if code == 800:
                await self._expire_qr_login(identity, login_key)
                return
            if code != 803:
                continue

            cookie = str(status.get("cookie") or "").strip()
            if not cookie:
                consecutive_errors += 1
                continue
            try:
                uid, nickname = await self.api.get_profile(cookie)
            except NeteaseApiError as exc:
                logger.warning("[%s] 扫码账号信息读取失败: %s", PLUGIN_NAME, exc)
                await self.api.logout(cookie)
                await self._send_message(
                    pending.event,
                    [
                        At(qq=pending.user_id),
                        Plain(" 扫码成功，但账号信息读取失败，请重新临时登录。"),
                    ],
                )
                await self._append_assistant_context(
                    pending, "扫码成功，但账号信息读取失败，请重新临时登录。"
                )
                self._pending_logins.pop(identity, None)
                return

            pending = self._pending_logins.get(identity)
            if not pending or pending.login_key != login_key:
                await self.api.logout(cookie)
                return
            pending.cookie = cookie
            pending.uid = uid
            pending.nickname = nickname
            pending.confirmation_deadline = time.monotonic() + self.confirmation_timeout
            confirmation_text = (
                f"扫码登录的网易云账号是“{nickname}”（UID {uid}）。这是你本人吗？\n"
                "请使用 /网易云 临时登录确认 yes 或 /网易云 临时登录确认 no。"
            )
            await self._send_message(
                pending.event,
                [At(qq=pending.user_id), Plain(" " + confirmation_text)],
            )
            await self._append_assistant_context(pending, confirmation_text)
            expiry_task = asyncio.create_task(
                self._expire_confirmation(identity, login_key)
            )
            pending.task = expiry_task
            return

    async def _expire_qr_login(self, identity: str, login_key: str) -> None:
        pending = self._pending_logins.get(identity)
        if not pending or pending.login_key != login_key:
            return
        self._pending_logins.pop(identity, None)
        text = "网易云登录二维码已过期，请重新使用 /网易云 临时登录。"
        await self._send_message(
            pending.event,
            [At(qq=pending.user_id), Plain(" " + text)],
        )
        await self._append_assistant_context(pending, text)

    async def _expire_confirmation(self, identity: str, login_key: str) -> None:
        await asyncio.sleep(self.confirmation_timeout)
        pending = self._pending_logins.get(identity)
        if not pending or pending.login_key != login_key or not pending.cookie:
            return
        self._pending_logins.pop(identity, None)
        await self.api.logout(pending.cookie)
        text = "网易云账号绑定确认已超时，临时登录已清理。"
        await self._send_message(
            pending.event,
            [At(qq=pending.user_id), Plain(" " + text)],
        )
        await self._append_assistant_context(pending, text)

    async def _discard_pending(self, identity: str, logout: bool) -> None:
        pending = self._pending_logins.pop(identity, None)
        if not pending:
            return
        current = asyncio.current_task()
        if pending.task and pending.task is not current and not pending.task.done():
            pending.task.cancel()
        if logout and pending.cookie:
            await self.api.logout(pending.cookie)

    async def _send_and_record_failure(
        self, event: AstrMessageEvent, progress_text: str, failure_text: str
    ) -> None:
        await self._send_text(event, failure_text)
        await self._record_command_context(event, progress_text + "\n\n" + failure_text)

    async def _reply_command(self, event: AstrMessageEvent, text: str) -> None:
        await self._send_text(event, text)
        await self._record_command_context(event, text)

    async def _send_text(self, event: AstrMessageEvent, text: str) -> None:
        for chunk in split_message(text, self.message_chunk_chars):
            await self._send_message(event, [Plain(chunk)])

    async def _send_message(self, event: AstrMessageEvent, chain: list[Any]) -> None:
        message_chain = MessageChain(chain)
        plain_text = "".join(
            component.text for component in chain if isinstance(component, Plain)
        )
        key = (event.unified_msg_origin, _normalize_echo_text(plain_text))
        self._register_outgoing_echo(key)
        sent = False
        try:
            await event.send(message_chain)
            sent = True
            await self._persist_output(event, message_chain)
        except Exception:
            if not sent:
                self._remove_outgoing_echo(key)
            raise

    async def _persist_output(
        self, event: AstrMessageEvent, message_chain: MessageChain
    ) -> None:
        settings = self.context.get_config(umo=event.unified_msg_origin).get(
            "provider_ltm_settings", {}
        )
        if not settings.get("group_message_history_enable", False):
            return
        try:
            max_messages = max(
                1,
                int(settings.get("group_message_history_max_cnt", 700)),
            )
        except (TypeError, ValueError):
            max_messages = 700
        await self.context.message_history_manager.insert_message_chain(
            platform_id=event.get_platform_id(),
            user_id=event.unified_msg_origin,
            message_chain=message_chain,
            role="bot",
            sender_id=event.get_self_id() or "bot",
            sender_name="bot",
            max_messages=max_messages,
        )

    def _register_outgoing_echo(self, key: tuple[str, str]) -> None:
        self._prune_outgoing_echoes()
        self._outgoing_echoes[key].append(time.monotonic() + ECHO_GUARD_SECONDS)

    def _remove_outgoing_echo(self, key: tuple[str, str]) -> None:
        queue = self._outgoing_echoes.get(key)
        if not queue:
            return
        queue.pop()
        if not queue:
            self._outgoing_echoes.pop(key, None)

    def _consume_outgoing_message(self, umo: str, message_text: str) -> bool:
        self._prune_outgoing_echoes()
        normalized = _normalize_echo_text(message_text)
        exact_key = (umo, normalized)
        if exact_key in self._outgoing_echoes:
            return self._consume_outgoing_echo(exact_key)

        candidates = [
            key
            for key in self._outgoing_echoes
            if key[0] == umo and key[1] and normalized.endswith(key[1])
        ]
        if not candidates:
            return False
        longest_match = max(candidates, key=lambda key: len(key[1]))
        return self._consume_outgoing_echo(longest_match)

    def _consume_outgoing_echo(self, key: tuple[str, str]) -> bool:
        self._prune_outgoing_echoes()
        queue = self._outgoing_echoes.get(key)
        if not queue:
            return False
        queue.popleft()
        if not queue:
            self._outgoing_echoes.pop(key, None)
        return True

    def _prune_outgoing_echoes(self) -> None:
        now = time.monotonic()
        for key, queue in list(self._outgoing_echoes.items()):
            while queue and queue[0] <= now:
                queue.popleft()
            if not queue:
                self._outgoing_echoes.pop(key, None)

    def _identity(self, event: AstrMessageEvent) -> str:
        return f"{event.get_platform_id()}:{event.get_sender_id()}"

    def _command_text(self, event: AstrMessageEvent) -> str:
        original = getattr(event.message_obj, "message_str", "")
        return str(original or event.message_str or "").strip()

    async def _record_command_context(
        self, event: AstrMessageEvent, response_text: str
    ) -> None:
        manager = getattr(self.context, "conversation_manager", None)
        if manager is None:
            return
        umo = event.unified_msg_origin
        lock = self._context_locks.setdefault(umo, asyncio.Lock())
        try:
            async with lock:
                conversation_id = await manager.get_curr_conversation_id(umo)
                if not conversation_id:
                    conversation_id = await manager.new_conversation(
                        umo, platform_id=event.get_platform_id()
                    )
                await manager.add_message_pair(
                    cid=conversation_id,
                    user_message=UserMessageSegment(
                        content=[TextPart(text=self._command_text(event))]
                    ),
                    assistant_message=AssistantMessageSegment(
                        content=[TextPart(text=response_text)]
                    ),
                )
        except Exception as exc:
            logger.warning("[%s] 指令结果写入 LLM 上下文失败: %s", PLUGIN_NAME, exc)

    async def _append_assistant_context(
        self, pending: PendingLogin, response_text: str
    ) -> None:
        manager = getattr(self.context, "conversation_manager", None)
        if manager is None:
            return
        lock = self._context_locks.setdefault(pending.umo, asyncio.Lock())
        try:
            async with lock:
                conversation_id = await manager.get_curr_conversation_id(pending.umo)
                if not conversation_id:
                    conversation_id = await manager.new_conversation(
                        pending.umo, platform_id=pending.platform_id
                    )
                conversation = await manager.get_conversation(
                    pending.umo, conversation_id
                )
                if not conversation:
                    return
                history = json.loads(conversation.history or "[]")
                history.append(
                    AssistantMessageSegment(
                        content=[TextPart(text=response_text)]
                    ).model_dump()
                )
                await manager.update_conversation(
                    unified_msg_origin=pending.umo,
                    conversation_id=conversation_id,
                    history=history,
                )
        except Exception as exc:
            logger.warning("[%s] 登录状态写入 LLM 上下文失败: %s", PLUGIN_NAME, exc)

    async def _get_bindings(self) -> dict[str, dict[str, Any]]:
        data = await self.get_kv_data(BINDINGS_KEY, {}) or {}
        if not isinstance(data, dict):
            return {}
        return data

    async def _get_binding(self, identity: str) -> dict[str, Any] | None:
        async with self._storage_lock:
            bindings = await self._get_bindings()
            binding = bindings.get(identity)
            return dict(binding) if isinstance(binding, dict) else None

    async def _save_binding(self, identity: str, binding: dict[str, Any]) -> None:
        async with self._storage_lock:
            bindings = await self._get_bindings()
            bindings[identity] = binding
            await self.put_kv_data(BINDINGS_KEY, bindings)

    @filter.on_plugin_unloaded()
    async def on_plugin_unloaded(self, metadata) -> None:
        identities = list(self._pending_logins)
        for identity in identities:
            await self._discard_pending(identity, logout=True)
        await self.api.close()
