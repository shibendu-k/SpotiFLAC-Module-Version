from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .api_mixins.search import search_metadata_async
from .client import AsyncSpotiFLAC
from .core.atomic_io import write_json_atomic
from .core.paths import data_path, default_download_dir
from .extensions.catalog import installed_download_services
from .core.extension_metadata import metadata_sources

logger = logging.getLogger(__name__)

_STATE_FILE = data_path("urmusicapi-telegram.json")
_QUALITIES = (
    "DOLBY_ATMOS",
    "HI_RES_LOSSLESS",
    "HI_RES",
    "LOSSLESS",
    "HIGH",
    "LOW",
)


def parse_admin_ids(raw: str | None) -> set[int]:
    ids: set[int] = set()
    for chunk in (raw or "").replace(" ", "").split(","):
        if not chunk:
            continue
        try:
            ids.add(int(chunk))
        except ValueError:
            continue
    return ids


def _today_key(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ts or time.time()))


def _track_line(index: int, track: dict[str, Any]) -> str:
    artist = str(track.get("artist") or track.get("artists") or "Unknown artist")
    title = str(track.get("title") or track.get("name") or "Unknown title")
    provider = str(track.get("provider") or "unknown")
    return f"{index + 1}. {artist} — {title} [{provider}]"


@dataclass
class BotUserSettings:
    quality: str = "LOSSLESS"
    fallback_quality: str = "LOSSLESS"
    enabled_services: list[str] | None = None
    enabled_sources: list[str] | None = None
    daily_limit: int = 0
    day_key: str = ""
    used_today: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "quality": self.quality,
            "fallback_quality": self.fallback_quality,
            "enabled_services": list(self.enabled_services or []),
            "enabled_sources": list(self.enabled_sources or []),
            "daily_limit": int(self.daily_limit),
            "day_key": self.day_key,
            "used_today": int(self.used_today),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], default_limit: int = 0) -> "BotUserSettings":
        return cls(
            quality=str(data.get("quality") or "LOSSLESS"),
            fallback_quality=str(data.get("fallback_quality") or "LOSSLESS"),
            enabled_services=list(data.get("enabled_services") or []),
            enabled_sources=list(data.get("enabled_sources") or []),
            daily_limit=max(0, int(data.get("daily_limit", default_limit) or default_limit)),
            day_key=str(data.get("day_key") or ""),
            used_today=max(0, int(data.get("used_today", 0) or 0)),
        )


class TelegramBot:
    def __init__(
        self,
        *,
        token: str,
        admin_user_ids: set[int] | None = None,
        output_dir: str | None = None,
        default_daily_limit: int = 0,
    ) -> None:
        self.token = token.strip()
        self.admin_user_ids = set(admin_user_ids or set())
        self.default_daily_limit = max(0, int(default_daily_limit))
        self.output_dir = output_dir or default_download_dir()
        self._offset = 0
        self._state_lock = asyncio.Lock()
        self._active_tasks: set[asyncio.Task] = set()
        self._tracks_by_chat: dict[int, list[dict[str, Any]]] = {}
        self._selected_track: dict[int, int] = {}
        self._state = self._load_state()

    def _load_state(self) -> dict[str, Any]:
        try:
            if _STATE_FILE.exists():
                return json.loads(_STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("Could not load telegram bot state.")
        return {"users": {}}

    def _save_state(self) -> None:
        _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(_STATE_FILE, self._state, private=False)

    def _settings(self, user_id: int) -> BotUserSettings:
        users = self._state.setdefault("users", {})
        key = str(user_id)
        current = BotUserSettings.from_dict(users.get(key) or {}, self.default_daily_limit)
        users[key] = current.to_dict()
        return current

    async def _api(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        from .core.http import AsyncHttpClient

        client = AsyncHttpClient("telegram", timeout_s=40)
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        response = await client.post(url, json=payload)
        data = response.json()
        if not data.get("ok"):
            raise RuntimeError(str(data))
        return data

    async def _send_message(
        self,
        chat_id: int,
        text: str,
        keyboard: list[list[dict[str, str]]] | None = None,
    ) -> None:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text[:4000]}
        if keyboard:
            payload["reply_markup"] = {"inline_keyboard": keyboard}
        await self._api("sendMessage", payload)

    async def _edit_message(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        keyboard: list[list[dict[str, str]]] | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text[:4000],
        }
        if keyboard:
            payload["reply_markup"] = {"inline_keyboard": keyboard}
        await self._api("editMessageText", payload)

    async def _answer_callback(self, callback_id: str, text: str = "") -> None:
        await self._api(
            "answerCallbackQuery",
            {"callback_query_id": callback_id, "text": text[:200]},
        )

    async def run_forever(self) -> None:
        if not self.token:
            raise ValueError("Telegram bot token is required.")
        while True:
            try:
                updates = await self._api(
                    "getUpdates",
                    {
                        "timeout": 30,
                        "offset": self._offset,
                        "allowed_updates": ["message", "callback_query"],
                    },
                )
                for update in updates.get("result") or []:
                    self._offset = max(self._offset, int(update.get("update_id", 0)) + 1)
                    await self._handle_update(update)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Telegram bot poll failed: %s", exc)
                await asyncio.sleep(2.0)

    async def _handle_update(self, update: dict[str, Any]) -> None:
        if update.get("message"):
            await self._handle_message(update["message"])
        elif update.get("callback_query"):
            await self._handle_callback(update["callback_query"])

    def _is_admin(self, user_id: int) -> bool:
        return user_id in self.admin_user_ids

    async def _handle_message(self, message: dict[str, Any]) -> None:
        text = str(message.get("text") or "").strip()
        if not text:
            return
        user = message.get("from") or {}
        chat = message.get("chat") or {}
        user_id = int(user.get("id") or 0)
        chat_id = int(chat.get("id") or 0)
        if not user_id or not chat_id:
            return
        if text.startswith("/start"):
            await self._send_message(
                chat_id,
                "Welcome to urmusicapi bot. Send a song query to search.",
                keyboard=[[{"text": "⚙️ Settings", "callback_data": "st:menu"}]],
            )
            return
        if text.startswith("/settings"):
            await self._show_settings(chat_id, message_id=None)
            return
        if text.startswith("/setlimit"):
            await self._handle_setlimit(chat_id, user_id, text)
            return
        if text.startswith("/stats"):
            await self._handle_stats(chat_id, user_id)
            return
        await self._handle_search(chat_id, text)

    async def _handle_stats(self, chat_id: int, user_id: int) -> None:
        if not self._is_admin(user_id):
            await self._send_message(chat_id, "Admin only command.")
            return
        rows = []
        for key, payload in (self._state.get("users") or {}).items():
            settings = BotUserSettings.from_dict(payload, self.default_daily_limit)
            rows.append(
                f"{key}: used={settings.used_today} limit={settings.daily_limit or 'unlimited'}"
            )
        text = "No users yet." if not rows else "User limits:\n" + "\n".join(rows[:40])
        await self._send_message(chat_id, text)

    async def _handle_setlimit(self, chat_id: int, user_id: int, text: str) -> None:
        if not self._is_admin(user_id):
            await self._send_message(chat_id, "Admin only command.")
            return
        parts = text.split()
        if len(parts) != 3:
            await self._send_message(chat_id, "Usage: /setlimit <user_id> <daily_limit>")
            return
        try:
            target_id = int(parts[1])
            limit = max(0, int(parts[2]))
        except ValueError:
            await self._send_message(chat_id, "Invalid values.")
            return
        async with self._state_lock:
            settings = self._settings(target_id)
            settings.daily_limit = limit
            self._state.setdefault("users", {})[str(target_id)] = settings.to_dict()
            self._save_state()
        await self._send_message(chat_id, f"User {target_id} limit set to {limit}.")

    async def _handle_search(self, chat_id: int, query: str) -> None:
        settings = self._settings(chat_id)
        available_sources = [s["id"] for s in metadata_sources() if s.get("id")]
        enabled_sources = settings.enabled_sources or available_sources
        if not enabled_sources:
            enabled_sources = ["spotify"]
        tasks = []
        for source in enabled_sources:
            source_id = None if source == "spotify" else source
            tasks.append(search_metadata_async(query, limit=8, source=source_id))
        results = await asyncio.gather(*tasks, return_exceptions=True)
        tracks: list[dict[str, Any]] = []
        seen: set[str] = set()
        for source, result in zip(enabled_sources, results, strict=True):
            if isinstance(result, Exception):
                continue
            for item in result.get("tracks") or []:
                key = str(item.get("external_url") or item.get("id") or "")
                if not key or key in seen:
                    continue
                seen.add(key)
                row = dict(item)
                row["provider"] = source
                tracks.append(row)
                if len(tracks) >= 20:
                    break
            if len(tracks) >= 20:
                break
        if not tracks:
            await self._send_message(chat_id, "No tracks found.")
            return
        self._tracks_by_chat[chat_id] = tracks
        keyboard = [
            [
                {
                    "text": f"{i + 1}. {str(t.get('title') or t.get('name') or 'track')[:45]}",
                    "callback_data": f"pick:{i}",
                }
            ]
            for i, t in enumerate(tracks[:10])
        ]
        keyboard.append([{"text": "⚙️ Settings", "callback_data": "st:menu"}])
        lines = ["Select a track:", *[_track_line(i, t) for i, t in enumerate(tracks[:10])]]
        await self._send_message(chat_id, "\n".join(lines), keyboard=keyboard)

    async def _show_settings(self, chat_id: int, message_id: int | None) -> None:
        settings = self._settings(chat_id)
        body = (
            "urmusicapi settings\n"
            f"quality: {settings.quality}\n"
            f"fallback: {settings.fallback_quality}\n"
            f"daily limit: {settings.daily_limit or 'unlimited'}"
        )
        keyboard = [
            [{"text": "Preferred quality", "callback_data": "st:quality"}],
            [{"text": "Fallback quality", "callback_data": "st:fallback"}],
            [{"text": "Toggle extensions", "callback_data": "st:services"}],
            [{"text": "Toggle search providers", "callback_data": "st:sources"}],
        ]
        if message_id is None:
            await self._send_message(chat_id, body, keyboard=keyboard)
        else:
            await self._edit_message(chat_id, message_id, body, keyboard=keyboard)

    async def _show_quality_menu(
        self,
        chat_id: int,
        message_id: int,
        *,
        callback_prefix: str,
        current: str,
    ) -> None:
        keyboard = [
            [
                {
                    "text": f"{'✅ ' if q == current else ''}{q}",
                    "callback_data": f"{callback_prefix}:{q}",
                }
            ]
            for q in _QUALITIES
        ]
        keyboard.append([{"text": "⬅️ Back", "callback_data": "st:menu"}])
        await self._edit_message(chat_id, message_id, "Choose quality:", keyboard=keyboard)

    async def _show_services(self, chat_id: int, message_id: int) -> None:
        services = [str(r["id"]) for r in installed_download_services() if r.get("id")]
        settings = self._settings(chat_id)
        enabled = set(settings.enabled_services or services)
        keyboard = []
        for service in services:
            keyboard.append(
                [
                    {
                        "text": f"{'✅' if service in enabled else '❌'} {service}",
                        "callback_data": f"svc:{service}",
                    }
                ]
            )
        keyboard.append([{"text": "⬅️ Back", "callback_data": "st:menu"}])
        await self._edit_message(
            chat_id,
            message_id,
            "Toggle provider extensions for downloads.",
            keyboard=keyboard,
        )

    async def _show_sources(self, chat_id: int, message_id: int) -> None:
        sources = [str(s["id"]) for s in metadata_sources() if s.get("id")]
        settings = self._settings(chat_id)
        enabled = set(settings.enabled_sources or sources)
        keyboard = []
        for source in sources:
            keyboard.append(
                [
                    {
                        "text": f"{'✅' if source in enabled else '❌'} {source}",
                        "callback_data": f"src:{source}",
                    }
                ]
            )
        keyboard.append([{"text": "⬅️ Back", "callback_data": "st:menu"}])
        await self._edit_message(
            chat_id,
            message_id,
            "Toggle metadata providers for text search.",
            keyboard=keyboard,
        )

    async def _toggle_setting_list(
        self,
        chat_id: int,
        key: str,
        value: str,
        available: list[str],
    ) -> None:
        async with self._state_lock:
            settings = self._settings(chat_id)
            current = list(getattr(settings, key) or available)
            if value in current:
                current.remove(value)
            else:
                current.append(value)
            current = [item for item in current if item in available]
            setattr(settings, key, current)
            self._state.setdefault("users", {})[str(chat_id)] = settings.to_dict()
            self._save_state()

    def _within_limit(self, settings: BotUserSettings, user_id: int) -> bool:
        if self._is_admin(user_id):
            return True
        day = _today_key()
        used = settings.used_today if settings.day_key == day else 0
        return settings.daily_limit <= 0 or used < settings.daily_limit

    async def _consume_quota(self, user_id: int) -> bool:
        async with self._state_lock:
            settings = self._settings(user_id)
            if not self._within_limit(settings, user_id):
                return False
            day = _today_key()
            if settings.day_key != day:
                settings.day_key = day
                settings.used_today = 0
            settings.used_today += 1
            self._state.setdefault("users", {})[str(user_id)] = settings.to_dict()
            self._save_state()
            return True

    async def _handle_callback(self, callback: dict[str, Any]) -> None:
        user = callback.get("from") or {}
        user_id = int(user.get("id") or 0)
        msg = callback.get("message") or {}
        chat_id = int((msg.get("chat") or {}).get("id") or 0)
        message_id = int(msg.get("message_id") or 0)
        callback_id = str(callback.get("id") or "")
        data = str(callback.get("data") or "")
        if not user_id or not chat_id or not callback_id:
            return
        await self._answer_callback(callback_id)

        if data == "st:menu":
            await self._show_settings(chat_id, message_id)
            return
        if data == "st:quality":
            settings = self._settings(chat_id)
            await self._show_quality_menu(
                chat_id,
                message_id,
                callback_prefix="setq",
                current=settings.quality,
            )
            return
        if data == "st:fallback":
            settings = self._settings(chat_id)
            await self._show_quality_menu(
                chat_id,
                message_id,
                callback_prefix="setf",
                current=settings.fallback_quality,
            )
            return
        if data == "st:services":
            await self._show_services(chat_id, message_id)
            return
        if data == "st:sources":
            await self._show_sources(chat_id, message_id)
            return
        if data.startswith("setq:"):
            value = data.split(":", 1)[1]
            if value in _QUALITIES:
                async with self._state_lock:
                    settings = self._settings(chat_id)
                    settings.quality = value
                    self._state.setdefault("users", {})[str(chat_id)] = settings.to_dict()
                    self._save_state()
            await self._show_settings(chat_id, message_id)
            return
        if data.startswith("setf:"):
            value = data.split(":", 1)[1]
            if value in _QUALITIES:
                async with self._state_lock:
                    settings = self._settings(chat_id)
                    settings.fallback_quality = value
                    self._state.setdefault("users", {})[str(chat_id)] = settings.to_dict()
                    self._save_state()
            await self._show_settings(chat_id, message_id)
            return
        if data.startswith("svc:"):
            value = data.split(":", 1)[1]
            services = [str(r["id"]) for r in installed_download_services() if r.get("id")]
            await self._toggle_setting_list(chat_id, "enabled_services", value, services)
            await self._show_services(chat_id, message_id)
            return
        if data.startswith("src:"):
            value = data.split(":", 1)[1]
            sources = [str(s["id"]) for s in metadata_sources() if s.get("id")]
            await self._toggle_setting_list(chat_id, "enabled_sources", value, sources)
            await self._show_sources(chat_id, message_id)
            return
        if data.startswith("pick:"):
            try:
                idx = int(data.split(":", 1)[1])
            except ValueError:
                return
            tracks = self._tracks_by_chat.get(chat_id) or []
            if idx < 0 or idx >= len(tracks):
                await self._send_message(chat_id, "Track list expired. Search again.")
                return
            self._selected_track[chat_id] = idx
            settings = self._settings(chat_id)
            keyboard = [
                [
                    {
                        "text": f"{'✅ ' if q == settings.quality else ''}{q}",
                        "callback_data": f"q:{idx}:{q}",
                    }
                ]
                for q in _QUALITIES
            ]
            await self._edit_message(
                chat_id,
                message_id,
                "Select quality for download:",
                keyboard=keyboard,
            )
            return
        if data.startswith("q:"):
            chunks = data.split(":")
            if len(chunks) != 3:
                return
            try:
                idx = int(chunks[1])
            except ValueError:
                return
            quality = chunks[2]
            tracks = self._tracks_by_chat.get(chat_id) or []
            if idx < 0 or idx >= len(tracks):
                await self._send_message(chat_id, "Track list expired. Search again.")
                return
            if not await self._consume_quota(chat_id):
                await self._send_message(chat_id, "Daily download limit reached.")
                return
            track = tracks[idx]
            await self._send_message(
                chat_id,
                f"Queued download: {_track_line(idx, track)} @ {quality}",
            )
            task = asyncio.create_task(self._run_download(chat_id, track, quality))
            self._active_tasks.add(task)
            task.add_done_callback(self._active_tasks.discard)

    async def _run_download(self, chat_id: int, track: dict[str, Any], quality: str) -> None:
        settings = self._settings(chat_id)
        services_all = [str(r["id"]) for r in installed_download_services() if r.get("id")]
        services = [s for s in (settings.enabled_services or services_all) if s in services_all]
        if not services:
            services = services_all or ["ext:tidal-web"]
        url = str(track.get("external_url") or track.get("id") or "").strip()
        if not url:
            await self._send_message(chat_id, "Cannot download: missing track URL.")
            return
        title = str(track.get("title") or track.get("name") or "track")
        artist = str(track.get("artist") or track.get("artists") or "unknown artist")
        chosen_ok = False
        error = ""
        try:
            async with AsyncSpotiFLAC(
                output_dir=self.output_dir,
                services=services,
                quality=quality,
                allow_fallback=False,
                sync_extensions=False,
            ) as client:
                failed = await client.download_track(url)
                chosen_ok = not failed
                if failed:
                    error = str(getattr(failed[0], "error", "") or "")
        except Exception as exc:
            error = str(exc)
        if chosen_ok:
            await self._send_message(chat_id, f"✅ Downloaded: {artist} — {title}")
            return

        fallback = settings.fallback_quality
        if fallback and fallback != quality:
            try:
                async with AsyncSpotiFLAC(
                    output_dir=self.output_dir,
                    services=services,
                    quality=fallback,
                    allow_fallback=True,
                    sync_extensions=False,
                ) as client:
                    failed = await client.download_track(url)
                    if not failed:
                        await self._send_message(
                            chat_id,
                            f"⚠️ Downloaded with fallback {fallback}: {artist} — {title}",
                        )
                        return
            except Exception as exc:
                error = f"{error}; {exc}" if error else str(exc)

        await self._send_message(
            chat_id,
            f"❌ Download failed: {artist} — {title}\n{error or 'unknown error'}",
        )


async def run_telegram_bot_async(
    *,
    token: str,
    admin_user_ids: set[int] | None = None,
    output_dir: str | None = None,
    default_daily_limit: int = 0,
) -> None:
    bot = TelegramBot(
        token=token,
        admin_user_ids=admin_user_ids,
        output_dir=output_dir,
        default_daily_limit=default_daily_limit,
    )
    await bot.run_forever()

