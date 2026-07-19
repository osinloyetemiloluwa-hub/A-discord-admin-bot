"""
utils/database.py
PostgreSQL database layer using asyncpg connection pool.
Backed by Neon serverless Postgres (free tier) — connect via DATABASE_URL.

Key differences from the old SQLite version:
  - Placeholders are $1 $2 $3 (not ?)
  - Uses a shared connection pool (faster, no per-query file open)
  - asyncpg Records are dict-like but need dict() to convert
  - INSERT uses RETURNING id instead of cursor.lastrowid
  - execute() returns a status string like 'DELETE 1' (parsed below)
"""

import asyncpg
import os
import logging

logger = logging.getLogger('CoAdminBot.Database')

DATABASE_URL = os.getenv('DATABASE_URL')


class Database:
    def __init__(self):
        self.pool: asyncpg.Pool | None = None

    # ─── Schema Setup ─────────────────────────────────────────────────────────

    async def initialize(self):
        if not DATABASE_URL:
            raise RuntimeError(
                '❌ DATABASE_URL is not set. '
                'Create a free Neon project at neon.com and paste the connection string.'
            )

        self.pool = await asyncpg.create_pool(
            DATABASE_URL,
            min_size=1,
            max_size=10,
            statement_cache_size=0,   # required for Neon's pgBouncer pooler
        )

        async with self.pool.acquire() as conn:

            await conn.execute('''
                CREATE TABLE IF NOT EXISTS guild_config (
                    guild_id            BIGINT  PRIMARY KEY,
                    prefix              TEXT    DEFAULT '!',
                    log_channel         BIGINT,
                    welcome_channel     BIGINT,
                    welcome_message     TEXT,
                    farewell_message    TEXT,
                    auto_role           BIGINT,
                    automod_enabled     INTEGER DEFAULT 1,
                    antiraid_enabled    INTEGER DEFAULT 0,
                    antispam_enabled    INTEGER DEFAULT 1,
                    anti_links          INTEGER DEFAULT 0,
                    anti_caps           INTEGER DEFAULT 0,
                    caps_threshold      INTEGER DEFAULT 70,
                    max_mentions        INTEGER DEFAULT 5,
                    max_messages        INTEGER DEFAULT 5,
                    spam_interval       INTEGER DEFAULT 5,
                    created_at          TIMESTAMPTZ DEFAULT NOW()
                )
            ''')

            await conn.execute('''
                CREATE TABLE IF NOT EXISTS warnings (
                    id              SERIAL  PRIMARY KEY,
                    guild_id        BIGINT  NOT NULL,
                    user_id         BIGINT  NOT NULL,
                    moderator_id    BIGINT  NOT NULL,
                    reason          TEXT    NOT NULL,
                    timestamp       TIMESTAMPTZ DEFAULT NOW()
                )
            ''')

            await conn.execute('''
                CREATE TABLE IF NOT EXISTS banned_words (
                    id          SERIAL  PRIMARY KEY,
                    guild_id    BIGINT  NOT NULL,
                    word        TEXT    NOT NULL,
                    added_by    BIGINT  NOT NULL,
                    timestamp   TIMESTAMPTZ DEFAULT NOW(),
                    UNIQUE (guild_id, word)
                )
            ''')

            await conn.execute('''
                CREATE TABLE IF NOT EXISTS mod_logs (
                    id              SERIAL  PRIMARY KEY,
                    guild_id        BIGINT  NOT NULL,
                    action          TEXT    NOT NULL,
                    target_id       BIGINT,
                    moderator_id    BIGINT,
                    reason          TEXT,
                    timestamp       TIMESTAMPTZ DEFAULT NOW()
                )
            ''')

            await conn.execute('''
                CREATE TABLE IF NOT EXISTS user_notes (
                    id              SERIAL  PRIMARY KEY,
                    guild_id        BIGINT  NOT NULL,
                    user_id         BIGINT  NOT NULL,
                    moderator_id    BIGINT  NOT NULL,
                    note            TEXT    NOT NULL,
                    timestamp       TIMESTAMPTZ DEFAULT NOW()
                )
            ''')

            await conn.execute('''
                CREATE TABLE IF NOT EXISTS spam_records (
                    guild_id        BIGINT  NOT NULL,
                    user_id         BIGINT  NOT NULL,
                    offense_count   INTEGER DEFAULT 0,
                    last_offense    TIMESTAMPTZ DEFAULT NOW(),
                    PRIMARY KEY (guild_id, user_id)
                )
            ''')

        logger.info('✅ PostgreSQL database initialized (Neon)')

    async def initialize_guild(self, guild_id: int):
        async with self.pool.acquire() as conn:
            await conn.execute(
                'INSERT INTO guild_config (guild_id) VALUES ($1) ON CONFLICT DO NOTHING',
                guild_id
            )

    # ─── Guild Config ─────────────────────────────────────────────────────────

    async def get_guild_config(self, guild_id: int) -> dict:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT * FROM guild_config WHERE guild_id = $1', guild_id
            )
            if row:
                return dict(row)
            # Auto-create on first access
            await self.initialize_guild(guild_id)
            row = await conn.fetchrow(
                'SELECT * FROM guild_config WHERE guild_id = $1', guild_id
            )
            return dict(row)

    async def update_guild_config(self, guild_id: int, **kwargs):
        if not kwargs:
            return
        # Ensure the row exists first
        await self.initialize_guild(guild_id)
        # Build parameterised SET clause: prefix=$2, log_channel=$3 ...
        set_parts = [f'{k} = ${i + 2}' for i, k in enumerate(kwargs)]
        set_clause = ', '.join(set_parts)
        values = [guild_id] + list(kwargs.values())
        async with self.pool.acquire() as conn:
            await conn.execute(
                f'UPDATE guild_config SET {set_clause} WHERE guild_id = $1',
                *values
            )

    # ─── Warnings ─────────────────────────────────────────────────────────────

    async def add_warning(self, guild_id: int, user_id: int,
                          moderator_id: int, reason: str) -> int:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                '''INSERT INTO warnings (guild_id, user_id, moderator_id, reason)
                   VALUES ($1, $2, $3, $4) RETURNING id''',
                guild_id, user_id, moderator_id, reason
            )
            return row['id']

    async def get_warnings(self, guild_id: int, user_id: int) -> list[dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                '''SELECT * FROM warnings
                   WHERE guild_id = $1 AND user_id = $2
                   ORDER BY timestamp DESC''',
                guild_id, user_id
            )
            return [dict(r) for r in rows]

    async def get_warning_count(self, guild_id: int, user_id: int) -> int:
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                'SELECT COUNT(*) FROM warnings WHERE guild_id = $1 AND user_id = $2',
                guild_id, user_id
            ) or 0

    async def clear_warnings(self, guild_id: int, user_id: int) -> int:
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                'DELETE FROM warnings WHERE guild_id = $1 AND user_id = $2',
                guild_id, user_id
            )
            # asyncpg returns e.g. 'DELETE 5'
            return int(result.split()[-1])

    # ─── Banned Words ─────────────────────────────────────────────────────────

    async def add_banned_word(self, guild_id: int, word: str, added_by: int):
        async with self.pool.acquire() as conn:
            await conn.execute(
                '''INSERT INTO banned_words (guild_id, word, added_by)
                   VALUES ($1, $2, $3)
                   ON CONFLICT DO NOTHING''',
                guild_id, word.lower(), added_by
            )

    async def remove_banned_word(self, guild_id: int, word: str) -> bool:
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                'DELETE FROM banned_words WHERE guild_id = $1 AND word = $2',
                guild_id, word.lower()
            )
            return result == 'DELETE 1'

    async def get_banned_words(self, guild_id: int) -> list[str]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                'SELECT word FROM banned_words WHERE guild_id = $1', guild_id
            )
            return [r['word'] for r in rows]

    # ─── Mod Logs ─────────────────────────────────────────────────────────────

    async def add_mod_log(self, guild_id: int, action: str,
                          target_id: int = None, moderator_id: int = None,
                          reason: str = None):
        async with self.pool.acquire() as conn:
            await conn.execute(
                '''INSERT INTO mod_logs (guild_id, action, target_id, moderator_id, reason)
                   VALUES ($1, $2, $3, $4, $5)''',
                guild_id, action, target_id, moderator_id, reason
            )

    # ─── User Notes ───────────────────────────────────────────────────────────

    async def add_note(self, guild_id: int, user_id: int,
                       moderator_id: int, note: str) -> int:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                '''INSERT INTO user_notes (guild_id, user_id, moderator_id, note)
                   VALUES ($1, $2, $3, $4) RETURNING id''',
                guild_id, user_id, moderator_id, note
            )
            return row['id']

    async def get_notes(self, guild_id: int, user_id: int) -> list[dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                '''SELECT * FROM user_notes
                   WHERE guild_id = $1 AND user_id = $2
                   ORDER BY timestamp DESC''',
                guild_id, user_id
            )
            return [dict(r) for r in rows]

    # ─── Spam Records ─────────────────────────────────────────────────────────

    async def increment_offense(self, guild_id: int, user_id: int) -> int:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                '''INSERT INTO spam_records (guild_id, user_id, offense_count)
                   VALUES ($1, $2, 1)
                   ON CONFLICT (guild_id, user_id)
                   DO UPDATE SET
                       offense_count = spam_records.offense_count + 1,
                       last_offense  = NOW()
                   RETURNING offense_count''',
                guild_id, user_id
            )
            return row['offense_count']

    async def reset_offense(self, guild_id: int, user_id: int):
        async with self.pool.acquire() as conn:
            await conn.execute(
                '''UPDATE spam_records
                   SET offense_count = 0
                   WHERE guild_id = $1 AND user_id = $2''',
                guild_id, user_id
            )
