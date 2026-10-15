from __future__ import annotations

import asyncio

from SpotiFLAC.telegram_bot import BotUserSettings, TelegramBot, parse_admin_ids


def test_parse_admin_ids_ignores_invalid_values() -> None:
    assert parse_admin_ids("123,abc,456,, 789 ") == {123, 456, 789}


def test_quota_check_resets_on_new_day(monkeypatch) -> None:
    bot = TelegramBot(token="x", default_daily_limit=2)
    monkeypatch.setattr("SpotiFLAC.telegram_bot._today_key", lambda _ts=None: "2026-10-15")
    settings = BotUserSettings(daily_limit=2, day_key="2026-10-14", used_today=2)
    assert bot._within_limit(settings, user_id=11)


def test_consume_quota_blocks_after_limit(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("SpotiFLAC.telegram_bot._STATE_FILE", tmp_path / "telegram.json")
    monkeypatch.setattr("SpotiFLAC.telegram_bot._today_key", lambda _ts=None: "2026-10-15")
    bot = TelegramBot(token="x", default_daily_limit=1)
    assert asyncio.run(bot._consume_quota(42))
    assert not asyncio.run(bot._consume_quota(42))
