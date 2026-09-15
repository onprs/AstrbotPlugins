"""官方 @/引用开关的接管：备份原值、关闭官方装饰、卸载时恢复。

AstrBot 结果装饰阶段的 ``reply_with_mention`` / ``reply_with_quote`` 是全局开关，
开启后会给所有非私聊回复统一插入 At 或 Reply。插件要按消息决定是否指向，必须
先关掉这两个官方开关，再由本插件在发送前装饰。

加载时序：``core_lifecycle.initialize()`` 先执行 ``plugin_manager.reload()``（插件
``initialize()`` 在此运行），随后才创建 ``ResultDecorateStage`` 并快照配置，因此
插件在启动阶段修改配置即可立即生效，无需用户手动关闭或重启。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MENTION_KEY = "reply_with_mention"
QUOTE_KEY = "reply_with_quote"
STATE_FILENAME = "takeover.json"


@dataclass
class TakeoverResult:
    """一次接管或恢复的结果。"""

    applied: bool
    profiles: list[str]
    backup_created: bool = False
    restored: bool = False


def _load_state(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _save_state(path: Path, state: dict[str, Any]) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2)
    except OSError:
        return False
    return True


def takeover(
    confs: dict[str, Any],
    state_path: Path,
    *,
    profile_ids: list[str] | None = None,
) -> TakeoverResult:
    """关闭官方 @/引用开关并备份原值。

    首次接管时把原值写入状态文件；重复接管只重新关闭开关，不覆盖备份，避免
    重载插件后把「已关闭」的状态记成用户原值。
    """
    targets = [
        conf_id
        for conf_id in (profile_ids or list(confs.keys()))
        if conf_id in confs
    ]
    if not targets:
        return TakeoverResult(applied=False, profiles=[])

    state = _load_state(state_path)
    backup_created = False
    if state is None:
        backup = {
            conf_id: {
                MENTION_KEY: bool(
                    confs[conf_id].get("platform_settings", {}).get(MENTION_KEY, False),
                ),
                QUOTE_KEY: bool(
                    confs[conf_id].get("platform_settings", {}).get(QUOTE_KEY, False),
                ),
            }
            for conf_id in targets
        }
        state = {"profiles": backup}
        backup_created = _save_state(state_path, state)

    changed: list[str] = []
    for conf_id in targets:
        platform_settings = confs[conf_id].get("platform_settings")
        if not isinstance(platform_settings, dict):
            continue
        if platform_settings.get(MENTION_KEY) or platform_settings.get(QUOTE_KEY):
            platform_settings[MENTION_KEY] = False
            platform_settings[QUOTE_KEY] = False
            try:
                confs[conf_id].save_config()
            except Exception:
                # 配置保存失败时，内存值已改，当前进程内仍可工作。
                pass
        changed.append(conf_id)

    return TakeoverResult(
        applied=bool(changed),
        profiles=changed,
        backup_created=backup_created,
    )


def restore(
    confs: dict[str, Any],
    state_path: Path,
    *,
    profile_ids: list[str] | None = None,
) -> TakeoverResult:
    """把官方开关恢复为接管前记录的值，并删除状态文件。"""
    state = _load_state(state_path)
    if state is None:
        return TakeoverResult(applied=False, profiles=[])

    backup = state.get("profiles")
    if not isinstance(backup, dict):
        backup = {}

    targets = profile_ids or list(backup.keys())
    restored_profiles: list[str] = []
    for conf_id in targets:
        saved = backup.get(conf_id)
        conf = confs.get(conf_id)
        if not isinstance(saved, dict) or conf is None:
            continue
        platform_settings = conf.get("platform_settings")
        if not isinstance(platform_settings, dict):
            continue
        platform_settings[MENTION_KEY] = bool(saved.get(MENTION_KEY, False))
        platform_settings[QUOTE_KEY] = bool(saved.get(QUOTE_KEY, False))
        try:
            conf.save_config()
        except Exception:
            pass
        restored_profiles.append(conf_id)

    try:
        state_path.unlink()
    except OSError:
        pass

    return TakeoverResult(
        applied=bool(restored_profiles),
        profiles=restored_profiles,
        restored=True,
    )


def backup_values(state_path: Path) -> dict[str, dict[str, bool]]:
    """读取备份的原始开关值，供日志与自检使用。"""
    state = _load_state(state_path)
    if not state:
        return {}
    backup = state.get("profiles")
    return backup if isinstance(backup, dict) else {}
