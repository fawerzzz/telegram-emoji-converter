from __future__ import annotations

import asyncio

import psycopg


class SettingsStore:
    def __init__(self, dsn: str) -> None:
        if not dsn.strip():
            raise ValueError("PostgreSQL DSN не может быть пустым")
        self.dsn = dsn

    async def init(self) -> None:
        await asyncio.to_thread(self._init_sync)

    def _init_sync(self) -> None:
        with psycopg.connect(self.dsn) as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS user_pack_settings (
                    user_id BIGINT PRIMARY KEY,
                    pack_title TEXT NOT NULL
                )
                """
            )

    async def set_pack_title(self, user_id: int, pack_title: str) -> None:
        await asyncio.to_thread(self._set_pack_title_sync, user_id, pack_title)

    def _set_pack_title_sync(self, user_id: int, pack_title: str) -> None:
        with psycopg.connect(self.dsn) as db:
            db.execute(
                """
                INSERT INTO user_pack_settings (user_id, pack_title)
                VALUES (%s, %s)
                ON CONFLICT (user_id) DO UPDATE
                SET pack_title = EXCLUDED.pack_title
                """,
                (user_id, pack_title),
            )

    async def get_pack_title(self, user_id: int, fallback: str) -> str:
        return await asyncio.to_thread(self._get_pack_title_sync, user_id, fallback)

    def _get_pack_title_sync(self, user_id: int, fallback: str) -> str:
        with psycopg.connect(self.dsn) as db:
            row = db.execute(
                "SELECT pack_title FROM user_pack_settings WHERE user_id = %s",
                (user_id,),
            ).fetchone()

        if row and row[0]:
            return str(row[0])
        return fallback
