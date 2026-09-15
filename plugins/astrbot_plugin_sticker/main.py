"""表情包插件：将收藏表情暴露给 LLM 自主发送（QQ 表情包格式）。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import time
import uuid
import warnings
from collections import defaultdict, deque
from io import BytesIO
from pathlib import Path
from sys import maxsize
from typing import Any

from PIL import Image as PillowImage
from PIL import UnidentifiedImageError

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import BaseMessageComponent, ComponentType, Image
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star
from astrbot.api.web import error_response, file_response, json_response, request
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

PLUGIN_NAME = "astrbot_plugin_sticker"
STICKER_TOOL_NAME = "send_sticker"
DATA_DIR_NAME = PLUGIN_NAME
STICKERS_FILE = "stickers.json"
NEXT_ID_FILE = "next_id.txt"
IMAGES_DIR = "images"
PENDING_DIR = "pending"
SUPPORTED_PLATFORM = "aiocqhttp"
DEFAULT_MAX_INJECT_DESC_LEN = 120
MAX_DESCRIPTION_LEN = 200
MAX_ANIMATION_FRAMES = 500
NAPCAT_ACTION_TIMEOUT_SECONDS = 15
STICKER_SEND_TIMEOUT_SECONDS = 30
STICKER_ONLY_TTL_SECONDS = 600
STICKER_ONLY_EVENT_KEY = f"_{PLUGIN_NAME}_only_until"
STICKER_SENT_ACK = "[STICKER_SENT]"
ECHO_GUARD_SECONDS = 15
IMAGE_FORMATS_BY_EXTENSION = {
    "jpg": {"JPEG"},
    "jpeg": {"JPEG"},
    "png": {"PNG"},
    "gif": {"GIF"},
    "webp": {"WEBP"},
}


def _norm_ext(name: str) -> str:
    return Path(name).suffix.lstrip(".").lower()


def _md5_bytes(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(parsed, maximum))


def _item_local_id(item: dict[str, Any]) -> int:
    return _bounded_int(item.get("local_id"), -1, -1, 10**9)


def _first_present(item: dict[str, Any], *keys: str, default: Any = "") -> Any:
    for key in keys:
        if key in item and item[key] is not None:
            return item[key]
    return default


class StickerStoreError(RuntimeError):
    """收藏清单无法安全读取或写入。"""


class DuplicateStickerError(StickerStoreError):
    def __init__(self, local_id: int) -> None:
        super().__init__(f"已存在相同表情 #{local_id}")
        self.local_id = local_id


class StickerNotFoundError(StickerStoreError):
    pass


class StickerCapacityError(StickerStoreError):
    pass


class StickerImage(BaseMessageComponent):
    """保留 OneBot 表情包子类型的图片消息段。"""

    type: ComponentType = ComponentType.Image
    file: str
    sub_type: int = 1


class StickerStore:
    """管理 LLM 语义清单与本地表情备份。"""

    def __init__(self, base: Path) -> None:
        self.base = base
        self.file = base / STICKERS_FILE
        self.next_id_file = base / NEXT_ID_FILE
        self.images_dir = base / IMAGES_DIR
        self.pending_dir = base / PENDING_DIR
        self.lock = asyncio.Lock()
        self._ensure_dirs()

    def _ensure_dirs(self) -> None:
        self.base.mkdir(parents=True, exist_ok=True)
        self.images_dir.mkdir(parents=True, exist_ok=True)
        self.pending_dir.mkdir(parents=True, exist_ok=True)
        if not self.file.exists():
            self.file.write_text("[]", encoding="utf-8")

    def _load_unlocked(self) -> list[dict[str, Any]]:
        try:
            data = json.loads(self.file.read_text(encoding="utf-8"))
        except Exception as exc:
            raise StickerStoreError(f"收藏清单读取失败: {exc}") from exc
        if not isinstance(data, list) or not all(
            isinstance(item, dict) for item in data
        ):
            raise StickerStoreError("收藏清单格式错误，预期为对象列表")

        local_ids: set[int] = set()
        for index, item in enumerate(data, start=1):
            local_id = _item_local_id(item)
            if local_id <= 0:
                raise StickerStoreError(f"收藏清单第 {index} 项缺少有效编号")
            if local_id in local_ids:
                raise StickerStoreError(f"收藏清单包含重复编号 #{local_id}")
            local_ids.add(local_id)
        return data

    def _save_unlocked(self, items: list[dict[str, Any]]) -> None:
        tmp = self.file.with_name(f".{self.file.name}.tmp")
        try:
            tmp.write_text(
                json.dumps(items, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(self.file)
        except Exception as exc:
            tmp.unlink(missing_ok=True)
            raise StickerStoreError(f"收藏清单写入失败: {exc}") from exc

    def _reserve_next_id_unlocked(self, items: list[dict[str, Any]]) -> int:
        ids: list[int] = []
        for item in items:
            try:
                ids.append(int(item.get("local_id", 0)))
            except (TypeError, ValueError):
                continue
        floor = max(ids, default=0) + 1
        stored = floor
        if self.next_id_file.exists():
            try:
                stored = int(self.next_id_file.read_text(encoding="ascii").strip())
            except (OSError, ValueError):
                logger.warning("sticker: next_id.txt 无效，将从现有清单恢复")
        next_id = max(floor, stored, 1)
        tmp = self.next_id_file.with_name(f".{self.next_id_file.name}.tmp")
        try:
            tmp.write_text(str(next_id + 1), encoding="ascii")
            tmp.replace(self.next_id_file)
        except Exception as exc:
            tmp.unlink(missing_ok=True)
            raise StickerStoreError(f"表情编号写入失败: {exc}") from exc
        return next_id

    async def load(self) -> list[dict[str, Any]]:
        async with self.lock:
            return self._load_unlocked()

    async def find_by_md5(self, md5: str) -> dict[str, Any] | None:
        async with self.lock:
            items = self._load_unlocked()
            hit = next(
                (
                    item
                    for item in items
                    if str(item.get("md5") or "").lower() == md5.lower()
                ),
                None,
            )
            return dict(hit) if hit is not None else None

    async def add(
        self,
        item: dict[str, Any],
        max_items: int = 400,
    ) -> dict[str, Any]:
        async with self.lock:
            items = self._load_unlocked()
            md5 = str(item.get("md5") or "").lower()
            duplicate = next(
                (
                    current
                    for current in items
                    if str(current.get("md5") or "").lower() == md5
                ),
                None,
            )
            if duplicate is not None:
                raise DuplicateStickerError(_item_local_id(duplicate))
            if len(items) >= max_items:
                raise StickerCapacityError(f"表情包数量已达到上限 {max_items}")
            new_item = dict(item)
            new_item["local_id"] = self._reserve_next_id_unlocked(items)
            items.append(new_item)
            self._save_unlocked(items)
            return dict(new_item)

    async def update_description(
        self,
        local_id: int,
        description: str,
    ) -> dict[str, Any]:
        async with self.lock:
            items = self._load_unlocked()
            hit = next(
                (item for item in items if _item_local_id(item) == local_id),
                None,
            )
            if hit is None:
                raise StickerNotFoundError("未找到该表情")
            hit["description"] = description
            self._save_unlocked(items)
            return dict(hit)

    async def remove(self, local_id: int) -> dict[str, Any]:
        async with self.lock:
            items = self._load_unlocked()
            index = next(
                (
                    index
                    for index, item in enumerate(items)
                    if _item_local_id(item) == local_id
                ),
                None,
            )
            if index is None:
                raise StickerNotFoundError("未找到该表情")
            hit = items.pop(index)
            self._save_unlocked(items)
            return dict(hit)

    async def increment_use(self, local_id: int) -> dict[str, Any]:
        async with self.lock:
            items = self._load_unlocked()
            hit = next(
                (item for item in items if _item_local_id(item) == local_id),
                None,
            )
            if hit is None:
                raise StickerNotFoundError("表情已被删除")
            hit["use_count"] = (
                _bounded_int(
                    hit.get("use_count"),
                    0,
                    0,
                    10**9,
                )
                + 1
            )
            self._save_unlocked(items)
            return dict(hit)

    async def update_favorite_metadata(
        self,
        local_id: int,
        *,
        favorite_ok: bool,
        favorite_error: str,
        res_id: str,
        emoji_id: Any,
        favorite_url: str,
    ) -> dict[str, Any]:
        async with self.lock:
            items = self._load_unlocked()
            hit = next(
                (item for item in items if _item_local_id(item) == local_id),
                None,
            )
            if hit is None:
                raise StickerNotFoundError("表情已被删除")
            hit.update(
                {
                    "fav_ok": favorite_ok,
                    "fav_error": favorite_error,
                    "resId": res_id,
                    "emoji_id": str(emoji_id) if emoji_id != "" else "",
                    "url": favorite_url,
                }
            )
            self._save_unlocked(items)
            return dict(hit)


class StickerPlugin(Star):
    def __init__(self, context: Context, config: dict[str, Any] | None = None) -> None:
        super().__init__(context, config)
        self.plugin_config: dict[str, Any] = config or {}
        data_root = Path(get_astrbot_data_path()) / "plugin_data" / DATA_DIR_NAME
        self.store = StickerStore(data_root)
        self.cooldown: dict[str, float] = {}
        self._send_locks: dict[str, asyncio.Lock] = {}
        self._outgoing_stickers: dict[str, deque[tuple[float, str]]] = defaultdict(
            deque
        )

        base = f"/{PLUGIN_NAME}"
        context.register_web_api(
            f"{base}/stickers/list",
            self.api_list,
            ["GET"],
            "列出表情包",
        )
        context.register_web_api(
            f"{base}/stickers/upload",
            self.api_upload,
            ["POST"],
            "上传表情包",
        )
        context.register_web_api(
            f"{base}/stickers/save_desc",
            self.api_save_desc,
            ["POST"],
            "保存备注",
        )
        context.register_web_api(
            f"{base}/stickers/delete",
            self.api_delete,
            ["POST"],
            "删除表情包",
        )
        context.register_web_api(
            f"{base}/stickers/image/<path:sticker_id>",
            self.api_image,
            ["GET"],
            "表情缩略图",
        )

    def _cfg_int(
        self,
        key: str,
        default: int,
        minimum: int,
        maximum: int,
    ) -> int:
        return _bounded_int(
            self.plugin_config.get(key),
            default,
            minimum,
            maximum,
        )

    @staticmethod
    def _is_supported_event(event: AstrMessageEvent) -> bool:
        try:
            return event.get_platform_name() == SUPPORTED_PLATFORM
        except Exception:
            return False

    @staticmethod
    def _hide_sticker_tool(req: ProviderRequest) -> None:
        tool_set = getattr(req, "func_tool", None)
        if tool_set is None:
            return
        if tool_set.get_tool(STICKER_TOOL_NAME) is not None:
            tool_set.remove_tool(STICKER_TOOL_NAME)

    @staticmethod
    def _tool_error(message: str) -> str:
        return (
            f"[STICKER_ERROR] {message}。"
            "本次请求不要再次调用 send_sticker；直接结束或只回复一句简短文字。"
        )

    @staticmethod
    def _public_item(item: dict[str, Any]) -> dict[str, Any]:
        keys = (
            "local_id",
            "file_name",
            "url",
            "description",
            "created_at",
            "use_count",
            "fav_ok",
            "fav_error",
        )
        return {key: item.get(key) for key in keys}

    def _get_send_lock(self, origin: str) -> asyncio.Lock:
        lock = self._send_locks.get(origin)
        if lock is None:
            lock = asyncio.Lock()
            self._send_locks[origin] = lock
        return lock

    def _resolve_managed_image(self, path_value: Any) -> Path | None:
        if not path_value:
            return None
        try:
            candidate = Path(str(path_value)).resolve()
            roots = (
                self.store.images_dir.resolve(),
                self.store.pending_dir.resolve(),
            )
            if not any(candidate.is_relative_to(root) for root in roots):
                logger.warning(f"sticker: 拒绝访问数据目录外的图片 path={candidate}")
                return None
            if not candidate.exists() or not candidate.is_file():
                return None
            return candidate
        except (OSError, ValueError):
            return None

    # ---------- NapCat 收藏调用 ----------
    def _get_client(self):
        try:
            platform = self.context.get_platform(SUPPORTED_PLATFORM)
        except Exception as exc:
            logger.warning(f"sticker: 获取 aiocqhttp 平台失败: {exc}")
            return None
        if platform is None:
            return None
        try:
            return platform.get_client()
        except Exception as exc:
            logger.warning(f"sticker: 获取 aiocqhttp client 失败: {exc}")
            return None

    async def _napcat_call(self, action: str, **params: Any) -> Any:
        client = self._get_client()
        if client is None:
            raise RuntimeError("未找到 aiocqhttp 平台")
        try:
            return await asyncio.wait_for(
                client.call_action(action, **params),
                timeout=NAPCAT_ACTION_TIMEOUT_SECONDS,
            )
        except TimeoutError as exc:
            raise RuntimeError(f"NapCat {action} 调用超时") from exc

    async def _napcat_add_custom_face(self, abs_path: str) -> Any:
        return await self._napcat_call(
            "add_custom_face",
            file=abs_path,
            is_origin=True,
        )

    async def _napcat_fetch_detail(self, count: int = 400) -> list[dict[str, Any]]:
        try:
            result = await self._napcat_call("fetch_custom_face_detail", count=count)
        except Exception as exc:
            logger.warning(f"sticker: fetch_custom_face_detail 失败: {exc}")
            return []
        if isinstance(result, list):
            return [item for item in result if isinstance(item, dict)]
        if not isinstance(result, dict):
            return []
        for candidate in (
            result.get("emojiInfoList"),
            result.get("data"),
            (result.get("data") or {}).get("emojiInfoList")
            if isinstance(result.get("data"), dict)
            else None,
        ):
            if isinstance(candidate, list):
                return [item for item in candidate if isinstance(item, dict)]
        return []

    async def _napcat_delete(self, res_id: str) -> Any:
        return await self._napcat_call("delete_custom_face", res_id=res_id)

    async def _napcat_set_desc(
        self,
        emoji_id: Any,
        res_id: str,
        md5: str,
        desc: str,
    ) -> Any:
        return await self._napcat_call(
            "set_custom_face_desc",
            emoji_id=emoji_id,
            res_id=res_id,
            md5=md5,
            desc=desc,
        )

    # ---------- 文件校验 ----------
    def _check_allowed(self, filename: str, data: bytes) -> str | None:
        configured = self.plugin_config.get("allowed_exts") or list(
            IMAGE_FORMATS_BY_EXTENSION
        )
        allowed = [
            str(extension).lower().lstrip(".")
            for extension in configured
            if str(extension).strip()
        ]
        extension = _norm_ext(filename)
        if extension not in allowed or extension not in IMAGE_FORMATS_BY_EXTENSION:
            visible = [item for item in allowed if item in IMAGE_FORMATS_BY_EXTENSION]
            return f"不支持的文件类型 .{extension}，允许：{', '.join(visible)}"

        max_mb = self._cfg_int("max_file_mb", 10, 1, 100)
        if not data:
            return "图片文件为空"
        if len(data) > max_mb * 1024 * 1024:
            return f"文件过大（{len(data)} bytes），上限 {max_mb} MB"

        max_pixels = self._cfg_int(
            "max_image_pixels", 25_000_000, 1_000_000, 100_000_000
        )
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", PillowImage.DecompressionBombWarning)
                with PillowImage.open(BytesIO(data)) as image:
                    image_format = str(image.format or "").upper()
                    width, height = image.size
                    frame_count = _bounded_int(
                        getattr(image, "n_frames", 1),
                        1,
                        1,
                        MAX_ANIMATION_FRAMES + 1,
                    )
                    if width <= 0 or height <= 0:
                        return "图片尺寸无效"
                    if frame_count > MAX_ANIMATION_FRAMES:
                        return f"动画帧数过多，上限 {MAX_ANIMATION_FRAMES} 帧"
                    if width * height * frame_count > max_pixels:
                        return f"图片解码像素过大，上限 {max_pixels} 像素"
                    image.verify()
        except (
            UnidentifiedImageError,
            OSError,
            ValueError,
            PillowImage.DecompressionBombError,
            PillowImage.DecompressionBombWarning,
        ) as exc:
            return f"无法识别或图片已损坏：{exc}"

        if image_format not in IMAGE_FORMATS_BY_EXTENSION[extension]:
            return (
                f"文件内容是 {image_format or '未知格式'}，与 .{extension} 后缀不一致"
            )
        return None

    # ---------- Web API ----------
    async def api_list(self):
        if not request.username:
            return error_response("未授权", status_code=401)
        try:
            items = await self.store.load()
        except StickerStoreError as exc:
            logger.error(f"sticker: {exc}")
            return error_response("收藏清单读取失败，请检查服务日志", status_code=500)
        items.sort(key=_item_local_id)
        return json_response({"stickers": [self._public_item(item) for item in items]})

    async def api_upload(self):
        if not request.username:
            return error_response("未授权", status_code=401)
        files = await request.files()
        upload = files.get("file")
        if upload is None:
            return error_response("缺少 file 字段", status_code=400)

        raw_filename = str(getattr(upload, "filename", "") or "sticker")
        filename = Path(raw_filename.replace("\\", "/")).name or "sticker"
        pending_path = self.store.pending_dir / f"{uuid.uuid4().hex}_{filename}"
        try:
            await upload.save(pending_path)
            max_file_mb = self._cfg_int("max_file_mb", 10, 1, 100)
            file_size = pending_path.stat().st_size
            if file_size > max_file_mb * 1024 * 1024:
                pending_path.unlink(missing_ok=True)
                return error_response(
                    f"文件过大（{file_size} bytes），上限 {max_file_mb} MB",
                    status_code=400,
                )
            data = pending_path.read_bytes()
        except Exception as exc:
            pending_path.unlink(missing_ok=True)
            logger.warning(f"sticker: 保存上传文件失败: {exc}")
            return error_response("保存上传文件失败", status_code=500)

        validation_error = self._check_allowed(filename, data)
        if validation_error:
            pending_path.unlink(missing_ok=True)
            return error_response(validation_error, status_code=400)

        md5 = _md5_bytes(data)
        try:
            duplicate = await self.store.find_by_md5(md5)
        except StickerStoreError as exc:
            pending_path.unlink(missing_ok=True)
            logger.error(f"sticker: {exc}")
            return error_response("收藏清单读取失败，请检查服务日志", status_code=500)
        if duplicate is not None:
            pending_path.unlink(missing_ok=True)
            return error_response(
                f"已存在相同表情 #{duplicate.get('local_id')}",
                status_code=409,
            )

        extension = _norm_ext(filename)
        backup_path = self.store.images_dir / f"{md5}.{extension}"
        try:
            if not backup_path.exists():
                backup_path.write_bytes(data)
        except Exception as exc:
            pending_path.unlink(missing_ok=True)
            logger.error(f"sticker: 备份图片失败: {exc}")
            return error_response("备份图片失败", status_code=500)

        item: dict[str, Any] = {
            "file_name": filename,
            "md5": md5,
            "local_path": str(backup_path.resolve()),
            "pending_path": "",
            "resId": "",
            "emoji_id": "",
            "url": "",
            "description": "",
            "created_at": _now_iso(),
            "use_count": 0,
            "fav_ok": False,
            "fav_error": "",
        }
        try:
            max_stickers = self._cfg_int("max_stickers", 400, 1, 400)
            saved = await self.store.add(item, max_items=max_stickers)
        except DuplicateStickerError as exc:
            pending_path.unlink(missing_ok=True)
            return error_response(str(exc), status_code=409)
        except StickerCapacityError as exc:
            pending_path.unlink(missing_ok=True)
            return error_response(str(exc), status_code=409)
        except StickerStoreError as exc:
            pending_path.unlink(missing_ok=True)
            logger.error(f"sticker: {exc}")
            return error_response("收藏清单写入失败，请检查服务日志", status_code=500)

        favorite_ok = True
        favorite_error = ""
        res_id = ""
        emoji_id: Any = ""
        favorite_url = ""
        try:
            try:
                await self._napcat_add_custom_face(str(pending_path.resolve()))
            except Exception as exc:
                favorite_ok = False
                favorite_error = str(exc)
                logger.warning(
                    f"sticker: add_custom_face 失败（仍会以本地备份发送）: {exc}"
                )

            if favorite_ok:
                await asyncio.sleep(1)
                details = await self._napcat_fetch_detail(max_stickers)
                matched = None
                for detail in details:
                    detail_md5 = str(
                        _first_present(detail, "md5", "MD5", "md5HexStr")
                    ).lower()
                    detail_res_id = str(_first_present(detail, "resId", "res_id", "id"))
                    if (
                        detail_md5 == md5.lower()
                        or md5.lower() in detail_res_id.lower()
                    ):
                        matched = detail
                        break
                if matched is not None:
                    res_id = str(_first_present(matched, "resId", "res_id", "id"))
                    emoji_id = _first_present(
                        matched,
                        "emojiId",
                        "emoji_id",
                        default="",
                    )
                    favorite_url = str(_first_present(matched, "url", "URL"))
        finally:
            pending_path.unlink(missing_ok=True)

        try:
            saved = await self.store.update_favorite_metadata(
                _item_local_id(saved),
                favorite_ok=favorite_ok,
                favorite_error=favorite_error,
                res_id=res_id,
                emoji_id=emoji_id,
                favorite_url=favorite_url,
            )
        except StickerStoreError as exc:
            logger.error(f"sticker: QQ 收藏状态写入失败: {exc}")

        return json_response(
            {
                "sticker": self._public_item(saved),
                "fav_ok": favorite_ok,
                "fav_error": favorite_error,
            }
        )

    async def api_save_desc(self):
        if not request.username:
            return error_response("未授权", status_code=401)
        payload = await request.json(default={})
        local_id = payload.get("local_id")
        description = str(payload.get("description") or "").strip()
        if local_id is None:
            return error_response("缺少 local_id", status_code=400)
        try:
            parsed_id = int(local_id)
        except (TypeError, ValueError):
            return error_response("local_id 必须为整数", status_code=400)
        if len(description) > MAX_DESCRIPTION_LEN:
            return error_response(
                f"备注过长（最多 {MAX_DESCRIPTION_LEN} 字）",
                status_code=400,
            )

        try:
            item = await self.store.update_description(parsed_id, description)
        except StickerNotFoundError as exc:
            return error_response(str(exc), status_code=404)
        except StickerStoreError as exc:
            logger.error(f"sticker: {exc}")
            return error_response("收藏清单写入失败，请检查服务日志", status_code=500)

        favorite_sync_error = ""
        res_id = str(item.get("resId") or "")
        md5 = str(item.get("md5") or "")
        emoji_id = item.get("emoji_id")
        can_sync = res_id and md5 and emoji_id not in (None, "")
        if can_sync:
            try:
                await self._napcat_set_desc(
                    emoji_id,
                    res_id,
                    md5,
                    description or str(item.get("file_name") or ""),
                )
            except Exception as exc:
                favorite_sync_error = str(exc)
                logger.warning(f"sticker: set_custom_face_desc 失败: {exc}")

        return json_response(
            {
                "ok": True,
                "sticker": self._public_item(item),
                "favorite_synced": bool(can_sync and not favorite_sync_error),
                "favorite_sync_error": favorite_sync_error,
            }
        )

    async def api_delete(self):
        if not request.username:
            return error_response("未授权", status_code=401)
        payload = await request.json(default={})
        local_id = payload.get("local_id")
        if local_id is None:
            return error_response("缺少 local_id", status_code=400)
        try:
            parsed_id = int(local_id)
        except (TypeError, ValueError):
            return error_response("local_id 必须为整数", status_code=400)

        try:
            item = await self.store.remove(parsed_id)
        except StickerNotFoundError as exc:
            return error_response(str(exc), status_code=404)
        except StickerStoreError as exc:
            logger.error(f"sticker: {exc}")
            return error_response("收藏清单写入失败，请检查服务日志", status_code=500)

        for key in ("local_path", "pending_path"):
            path_value = item.get(key)
            image_path = self._resolve_managed_image(path_value)
            if image_path is None:
                continue
            try:
                image_path.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning(f"sticker: 删除本地文件失败 path={image_path}: {exc}")

        favorite_delete_error = ""
        res_id = str(item.get("resId") or "")
        if res_id:
            try:
                await self._napcat_delete(res_id)
            except Exception as exc:
                favorite_delete_error = str(exc)
                logger.warning(f"sticker: delete_custom_face 失败: {exc}")

        return json_response(
            {
                "ok": True,
                "favorite_deleted": bool(res_id and not favorite_delete_error),
                "favorite_delete_error": favorite_delete_error,
            }
        )

    async def api_image(self, sticker_id: str):
        if not request.username:
            return error_response("未授权", status_code=401)
        raw = sticker_id.strip().lstrip("/")
        try:
            parsed_id = int(raw.split("/")[0])
        except (TypeError, ValueError):
            return error_response("sticker_id 必须为整数", status_code=400)
        try:
            items = await self.store.load()
        except StickerStoreError as exc:
            logger.error(f"sticker: {exc}")
            return error_response("收藏清单读取失败，请检查服务日志", status_code=500)
        item = next(
            (current for current in items if _item_local_id(current) == parsed_id),
            None,
        )
        if item is None:
            return error_response("未找到该表情", status_code=404)

        content_types = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".gif": "image/gif",
            ".webp": "image/webp",
        }
        for key in ("local_path", "pending_path"):
            image_path = self._resolve_managed_image(item.get(key))
            if image_path is not None:
                return file_response(
                    image_path,
                    content_type=content_types.get(image_path.suffix.lower()),
                )
        return error_response("文件缺失，请在 WebUI 重新上传", status_code=404)

    # ---------- LLM 集成 ----------
    def _build_inject_text(self, items: list[dict[str, Any]]) -> str:
        max_items = self._cfg_int("max_inject", 50, 1, 100)
        max_description_chars = self._cfg_int(
            "max_description_chars",
            DEFAULT_MAX_INJECT_DESC_LEN,
            20,
            MAX_DESCRIPTION_LEN,
        )
        active = [item for item in items if str(item.get("description") or "").strip()]
        if not active:
            return ""
        active.sort(key=_item_local_id)
        lines: list[str] = []
        for item in active[:max_items]:
            local_id = max(_item_local_id(item), 0)
            description = " ".join(
                str(item.get("description") or "").strip().splitlines()
            )
            if len(description) > max_description_chars:
                description = description[:max_description_chars] + "…"
            lines.append(f"- #{local_id}: {description}")
        catalog = "\n".join(lines)
        return (
            "【QQ 表情包工具】\n"
            "send_sticker 是当前请求明确允许使用的表情媒体工具，"
            "不是 send_message_to_user。主动参与群聊和被 @ 回复时均可按语境使用。\n"
            f"当前可用表情包共 {len(lines)} 张：\n{catalog}\n"
            "回复方式按语境三选一：\n"
            "1. 只发表情：调用 send_sticker(sticker_id=编号, only_sticker=true)，"
            f"工具成功后按工具要求只输出 {STICKER_SENT_ACK} 确认标记；"
            "不要留空，也不要输出其他文字。确认标记会被系统隐藏。\n"
            "2. 表情加文字：调用 send_sticker(sticker_id=编号, only_sticker=false)，"
            "随后只补一句必要的短文字。\n"
            "调用 send_sticker 的 assistant 消息本身必须完全不含文字，不要先说正在发送；"
            "等待工具结果后再决定是否结束或补充文字。\n"
            "工具返回 [STICKER_ERROR] 时，本次请求不得重试 send_sticker。\n"
            "3. 只发文字：不调用工具。\n"
            "只有清单中某张表情与当前语境匹配时才调用，不要虚构编号。"
        )

    def _prune_outgoing_stickers(self) -> None:
        now = time.monotonic()
        for origin, queue in list(self._outgoing_stickers.items()):
            while queue and queue[0][0] <= now:
                queue.popleft()
            if not queue:
                self._outgoing_stickers.pop(origin, None)

    def _register_outgoing_sticker(self, origin: str, md5: str) -> None:
        self._prune_outgoing_stickers()
        self._outgoing_stickers[origin].append(
            (time.monotonic() + ECHO_GUARD_SECONDS, md5.lower())
        )

    def _remove_outgoing_sticker(self, origin: str, md5: str) -> None:
        queue = self._outgoing_stickers.get(origin)
        if not queue:
            return
        normalized = md5.lower()
        entry = next((item for item in reversed(queue) if item[1] == normalized), None)
        if entry is not None:
            queue.remove(entry)
        if not queue:
            self._outgoing_stickers.pop(origin, None)

    def _consume_outgoing_sticker(self, event: AstrMessageEvent) -> bool:
        self._prune_outgoing_stickers()
        origin = event.unified_msg_origin
        queue = self._outgoing_stickers.get(origin)
        if not queue:
            return False

        image_values: list[str] = []
        messages = list(event.get_messages() or [])
        for component in messages:
            if not isinstance(component, Image):
                continue
            for attr in ("file", "url", "path"):
                value = getattr(component, attr, None)
                if value:
                    image_values.append(str(value).lower())

        raw_message = getattr(event.message_obj, "raw_message", None)
        raw_segments = (
            raw_message.get("message", []) if hasattr(raw_message, "get") else []
        )
        if isinstance(raw_segments, list):
            for segment in raw_segments:
                if not isinstance(segment, dict) or segment.get("type") != "image":
                    continue
                data = segment.get("data")
                if not isinstance(data, dict):
                    continue
                image_values.extend(
                    str(value).lower() for value in data.values() if value
                )

        matched = next(
            (
                entry
                for entry in queue
                if entry[1] and any(entry[1] in value for value in image_values)
            ),
            None,
        )
        if matched is None:
            only_images = bool(messages) and all(
                getattr(component, "type", None) == ComponentType.Image
                for component in messages
            )
            if only_images and len(queue) == 1:
                matched = queue[0]
        if matched is None:
            return False

        queue.remove(matched)
        if not queue:
            self._outgoing_stickers.pop(origin, None)
        return True

    @filter.event_message_type(filter.EventMessageType.ALL, priority=maxsize)
    async def guard_sticker_output_echo(self, event: AstrMessageEvent) -> None:
        """拦截机器人表情回推，避免重复入库或参与主动回复抽样。"""
        if not self._is_supported_event(event):
            return
        if str(event.get_sender_id()) != str(event.get_self_id()):
            return
        if self._consume_outgoing_sticker(event):
            event.is_at_or_wake_command = True
            event.is_wake = True
            event.stop_event()

    @filter.on_decorating_result()
    async def on_decorating_result(self, event: AstrMessageEvent) -> None:
        """按会话消费只发表情标志，并清理工具后的文字结果。"""
        deadline = event.get_extra(STICKER_ONLY_EVENT_KEY)
        if not isinstance(deadline, int | float) or deadline < time.monotonic():
            event.set_extra(STICKER_ONLY_EVENT_KEY, None)
            return
        result = event.get_result()
        if result is None or not result.chain:
            return
        logger.info(
            f"sticker: 清理只发表情后的文字结果 origin={event.unified_msg_origin}"
        )
        result.chain = []

    @filter.on_llm_request()
    async def on_llm_request(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
    ) -> None:
        if not self.plugin_config.get("enable", True):
            self._hide_sticker_tool(req)
            return
        if not self._is_supported_event(event):
            self._hide_sticker_tool(req)
            return
        try:
            is_private = bool(event.is_private_chat())
        except Exception:
            is_private = False
        if is_private and not self.plugin_config.get("allow_private", False):
            self._hide_sticker_tool(req)
            return
        try:
            items = await self.store.load()
        except StickerStoreError as exc:
            self._hide_sticker_tool(req)
            logger.error(f"sticker: LLM 注入失败: {exc}")
            return
        text = self._build_inject_text(items)
        if not text:
            self._hide_sticker_tool(req)
            return
        if "【QQ 表情包工具】" not in (req.system_prompt or ""):
            req.system_prompt = (req.system_prompt or "").rstrip() + "\n\n" + text

    @filter.llm_tool(name=STICKER_TOOL_NAME)
    async def send_sticker(
        self,
        event: AstrMessageEvent,
        sticker_id: int,
        only_sticker: bool = False,
    ) -> str:
        """发送 QQ 表情包。

        Args:
            sticker_id(number): 表情包编号，对应可用表情包清单中的 #编号
            only_sticker(boolean): 是否只发表情包。true 表示工具成功后不再发送文字；false 表示允许再补一句短文字
        """
        if not self.plugin_config.get("enable", True):
            return self._tool_error("表情包功能未启用")
        if not self._is_supported_event(event):
            return self._tool_error("当前平台不支持 QQ 表情包")
        try:
            is_private = bool(event.is_private_chat())
        except Exception:
            is_private = False
        if is_private and not self.plugin_config.get("allow_private", False):
            return self._tool_error("当前会话不支持发表情包")
        try:
            if isinstance(sticker_id, bool):
                raise ValueError
            parsed_id = int(sticker_id)
            if float(sticker_id) != parsed_id:
                raise ValueError
        except (TypeError, ValueError, OverflowError):
            return self._tool_error("表情包编号必须是整数")

        origin = event.unified_msg_origin
        async with self._get_send_lock(origin):
            cooldown_seconds = self._cfg_int(
                "cooldown_seconds",
                30,
                0,
                86_400,
            )
            now = time.monotonic()
            last_sent = self.cooldown.get(origin, 0)
            if cooldown_seconds > 0 and now - last_sent < cooldown_seconds:
                remaining = math.ceil(cooldown_seconds - (now - last_sent))
                return self._tool_error(f"冷却中，还需等待 {remaining} 秒")

            try:
                items = await self.store.load()
            except StickerStoreError as exc:
                logger.error(f"sticker: 发送前读取清单失败: {exc}")
                return self._tool_error("表情包清单暂时不可用")
            item = next(
                (current for current in items if _item_local_id(current) == parsed_id),
                None,
            )
            if item is None:
                return self._tool_error(f"未找到编号为 {parsed_id} 的表情包")
            if not str(item.get("description") or "").strip():
                return self._tool_error(f"表情 #{parsed_id} 暂无备注，不可用")

            image_path = None
            for key in ("local_path", "pending_path"):
                image_path = self._resolve_managed_image(item.get(key))
                if image_path is not None:
                    break
            if image_path is None:
                return self._tool_error(
                    f"表情 #{parsed_id} 文件缺失，请在 WebUI 重新上传"
                )

            stored_md5 = str(item.get("md5") or "").lower()
            try:
                image_data = await asyncio.to_thread(image_path.read_bytes)
            except OSError as exc:
                logger.warning(f"sticker: 读取表情文件失败 path={image_path}: {exc}")
                return self._tool_error(f"表情 #{parsed_id} 文件读取失败")
            actual_md5 = _md5_bytes(image_data)
            if stored_md5 and actual_md5 != stored_md5:
                logger.error(
                    f"sticker: 表情文件校验失败 sticker_id={parsed_id} "
                    f"expected={stored_md5} actual={actual_md5}"
                )
                return self._tool_error(
                    f"表情 #{parsed_id} 文件校验失败，请在 WebUI 重新上传"
                )

            self._register_outgoing_sticker(origin, actual_md5)
            chain = MessageChain(
                chain=[
                    StickerImage(
                        file=f"base64://{base64.b64encode(image_data).decode('ascii')}",
                        sub_type=1,
                    )
                ]
            )
            try:
                sent = await asyncio.wait_for(
                    self.context.send_message(origin, chain),
                    timeout=STICKER_SEND_TIMEOUT_SECONDS,
                )
                if not sent:
                    raise RuntimeError("未找到当前会话对应的平台实例")
            except Exception as exc:
                self._remove_outgoing_sticker(origin, actual_md5)
                error_detail = str(exc).strip() or type(exc).__name__
                logger.warning(f"sticker: 发送表情失败 origin={origin}: {error_detail}")
                return self._tool_error("表情发送暂时失败")

            try:
                await self.store.increment_use(parsed_id)
            except StickerStoreError as exc:
                logger.warning(f"sticker: 表情已发送，但使用次数更新失败: {exc}")
            self.cooldown[origin] = now

            only = only_sticker is True or (
                isinstance(only_sticker, str) and only_sticker.strip().lower() == "true"
            )
            if only:
                event.set_extra(
                    STICKER_ONLY_EVENT_KEY,
                    time.monotonic() + STICKER_ONLY_TTL_SECONDS,
                )
            else:
                event.set_extra(STICKER_ONLY_EVENT_KEY, None)
            logger.info(
                f"sticker: 已发送 origin={origin} sticker_id={parsed_id} only={only}"
            )

        if only:
            return (
                f"[STICKER_ONLY] 已发送表情 #{parsed_id}。"
                f"最终回复必须只输出 {STICKER_SENT_ACK}，不要留空、不要输出其他文字；"
                "该确认标记会由系统隐藏。"
            )
        return f"已发送表情 #{parsed_id}；如有必要，只补一句短文字。"

    @filter.on_astrbot_loaded()
    async def on_loaded(self) -> None:
        self.store._ensure_dirs()
        try:
            count = len(await self.store.load())
        except StickerStoreError as exc:
            logger.error(f"sticker: 插件已加载，但收藏清单不可用: {exc}")
            return
        logger.info(f"sticker: 表情包插件已加载，共 {count} 张")

    async def terminate(self) -> None:
        self.cooldown.clear()
        self._send_locks.clear()
        self._outgoing_stickers.clear()
