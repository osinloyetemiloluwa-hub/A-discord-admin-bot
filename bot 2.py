"""
╔══════════════════════════════════════════════════════════════════════════════╗
║                     TECO  v3  —  AI CO-ADMIN AGENT                         ║
║   Human-like · Full Logging · Embed Reading · Roles · Channels · Crypto     ║
║   Timed Bans · Autorole · Orders · VC Follow · Scheduled Messages           ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

# ═══════════════════════════════════════════════════════════════════════════════
# 1 ── IMPORTS & CONFIG
# ═══════════════════════════════════════════════════════════════════════════════

import discord
from discord.ext import commands, tasks
from discord import app_commands
import asyncpg
import aiohttp
from aiohttp import web
import asyncio
import os
import re
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from collections import defaultdict
from dotenv import load_dotenv
from typing import Optional, Callable

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
log = logging.getLogger("TECO")

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DATABASE_URL   = os.getenv("DATABASE_URL")
OWNER_IDS      = set(
    int(x) for x in os.getenv("OWNER_IDS", "").split(",") if x.strip().isdigit()
)
PREFIX         = os.getenv("PREFIX", "!")
GEMINI_MODEL   = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
PORT           = int(os.getenv("PORT", 7860))

GEMINI_URL     = (
    f"https://generativelanguage.googleapis.com"
    f"/v1beta/models/{GEMINI_MODEL}:generateContent"
)
GEMINI_HDR     = {
    "Content-Type": "application/json",
    "x-goog-api-key": GEMINI_API_KEY or "",
}

AI_COOLDOWN    = 2          # seconds between auto-mod scans per channel
_last_scan: dict[int, float] = defaultdict(float)


# ═══════════════════════════════════════════════════════════════════════════════
# 2 ── DATABASE
# ═══════════════════════════════════════════════════════════════════════════════

class Database:
    pool: asyncpg.Pool = None

    async def connect(self):
        self.pool = await asyncpg.create_pool(
            DATABASE_URL, min_size=1, max_size=10, statement_cache_size=0
        )
        await self._schema()
        log.info("✅ Database ready")

    async def _schema(self):
        async with self.pool.acquire() as c:
            # ── Core config ─────────────────────────────────────────────────
            await c.execute("""
                CREATE TABLE IF NOT EXISTS guild_config (
                    guild_id             BIGINT   PRIMARY KEY,
                    system_prompt        TEXT     DEFAULT '',
                    server_rules         TEXT     DEFAULT '',
                    auto_mod_enabled     BOOLEAN  DEFAULT FALSE,
                    log_channel          BIGINT,
                    monitored_channels   BIGINT[] DEFAULT '{}',
                    trigger_words        TEXT[]   DEFAULT '{}',
                    trusted_role_ids     BIGINT[] DEFAULT '{}',
                    autorole_id          BIGINT,
                    vc_follow_owner      BOOLEAN  DEFAULT FALSE,
                    created_at           TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            for col, defn in [
                ("autorole_id",     "BIGINT"),
                ("vc_follow_owner", "BOOLEAN DEFAULT FALSE"),
                ("trigger_words",   "TEXT[]  DEFAULT '{}'"),
                ("trusted_role_ids","BIGINT[] DEFAULT '{}'"),
            ]:
                await c.execute(
                    f"ALTER TABLE guild_config ADD COLUMN IF NOT EXISTS {col} {defn}"
                )

            # ── Rolling message history ──────────────────────────────────────
            await c.execute("""
                CREATE TABLE IF NOT EXISTS message_history (
                    id          BIGSERIAL   PRIMARY KEY,
                    guild_id    BIGINT      NOT NULL,
                    channel_id  BIGINT      NOT NULL,
                    user_id     BIGINT,
                    username    TEXT        NOT NULL,
                    content     TEXT        NOT NULL,
                    is_embed    BOOLEAN     DEFAULT FALSE,
                    created_at  TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            await c.execute(
                "CREATE INDEX IF NOT EXISTS idx_msg_ch "
                "ON message_history (channel_id, created_at DESC)"
            )

            # ── Full server event log ─────────────────────────────────────────
            await c.execute("""
                CREATE TABLE IF NOT EXISTS server_logs (
                    id          BIGSERIAL   PRIMARY KEY,
                    guild_id    BIGINT      NOT NULL,
                    event_type  TEXT        NOT NULL,
                    actor       TEXT,
                    target      TEXT,
                    channel     TEXT,
                    description TEXT,
                    created_at  TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            await c.execute(
                "CREATE INDEX IF NOT EXISTS idx_log_guild "
                "ON server_logs (guild_id, created_at DESC)"
            )

            # ── Member mod history ───────────────────────────────────────────
            await c.execute("""
                CREATE TABLE IF NOT EXISTS member_history (
                    id          BIGSERIAL   PRIMARY KEY,
                    guild_id    BIGINT      NOT NULL,
                    user_id     BIGINT      NOT NULL,
                    action      TEXT        NOT NULL,
                    reason      TEXT,
                    moderator   TEXT        DEFAULT 'TECO',
                    created_at  TIMESTAMPTZ DEFAULT NOW()
                )
            """)

            # ── Timed bans ───────────────────────────────────────────────────
            await c.execute("""
                CREATE TABLE IF NOT EXISTS timed_bans (
                    id          BIGSERIAL   PRIMARY KEY,
                    guild_id    BIGINT      NOT NULL,
                    user_id     BIGINT      NOT NULL,
                    reason      TEXT,
                    expires_at  TIMESTAMPTZ NOT NULL,
                    active      BOOLEAN     DEFAULT TRUE
                )
            """)

            # ── Scheduled messages ───────────────────────────────────────────
            await c.execute("""
                CREATE TABLE IF NOT EXISTS scheduled_messages (
                    id              BIGSERIAL   PRIMARY KEY,
                    guild_id        BIGINT      NOT NULL,
                    channel_id      BIGINT      NOT NULL,
                    content         TEXT        NOT NULL,
                    send_at         TIMESTAMPTZ NOT NULL,
                    repeat_minutes  INT         DEFAULT 0,
                    active          BOOLEAN     DEFAULT TRUE
                )
            """)

            # ── Coin price alerts ────────────────────────────────────────────
            await c.execute("""
                CREATE TABLE IF NOT EXISTS coin_alerts (
                    id          BIGSERIAL   PRIMARY KEY,
                    guild_id    BIGINT      NOT NULL,
                    channel_id  BIGINT      NOT NULL,
                    user_id     BIGINT      NOT NULL,
                    coin_id     TEXT        NOT NULL,
                    coin_name   TEXT        NOT NULL,
                    coin_symbol TEXT        NOT NULL,
                    direction   TEXT        NOT NULL,
                    price       FLOAT       NOT NULL,
                    active      BOOLEAN     DEFAULT TRUE
                )
            """)

            # ── Voice channel stat displays ──────────────────────────────────
            await c.execute("""
                CREATE TABLE IF NOT EXISTS coin_stat_channels (
                    id              BIGSERIAL   PRIMARY KEY,
                    guild_id        BIGINT      NOT NULL,
                    voice_channel_id BIGINT     NOT NULL,
                    coin_id         TEXT        NOT NULL,
                    coin_name       TEXT        NOT NULL,
                    coin_symbol     TEXT        NOT NULL,
                    active          BOOLEAN     DEFAULT TRUE,
                    UNIQUE (guild_id, voice_channel_id)
                )
            """)

            # ── Custom orders ────────────────────────────────────────────────
            await c.execute("""
                CREATE TABLE IF NOT EXISTS orders (
                    id          BIGSERIAL   PRIMARY KEY,
                    guild_id    BIGINT      NOT NULL,
                    name        TEXT        NOT NULL,
                    description TEXT        NOT NULL,
                    UNIQUE (guild_id, name)
                )
            """)

    # ── Helpers ─────────────────────────────────────────────────────────────

    async def get_config(self, guild_id: int) -> dict:
        async with self.pool.acquire() as c:
            row = await c.fetchrow(
                "SELECT * FROM guild_config WHERE guild_id=$1", guild_id
            )
            if not row:
                await c.execute(
                    "INSERT INTO guild_config (guild_id) VALUES ($1) ON CONFLICT DO NOTHING",
                    guild_id
                )
                row = await c.fetchrow(
                    "SELECT * FROM guild_config WHERE guild_id=$1", guild_id
                )
            return dict(row)

    async def set_config(self, guild_id: int, **kw):
        await self.get_config(guild_id)
        if not kw:
            return
        cols = ", ".join(f"{k}=${i+2}" for i, k in enumerate(kw))
        await self.pool.execute(
            f"UPDATE guild_config SET {cols} WHERE guild_id=$1",
            guild_id, *kw.values()
        )

    async def arr_append(self, guild_id: int, col: str, val):
        await self.get_config(guild_id)
        await self.pool.execute(
            f"UPDATE guild_config SET {col} = array_append({col}, $2) "
            f"WHERE guild_id=$1 AND NOT ($2=ANY({col}))",
            guild_id, val
        )

    async def arr_remove(self, guild_id: int, col: str, val):
        await self.pool.execute(
            f"UPDATE guild_config SET {col} = array_remove({col}, $2) WHERE guild_id=$1",
            guild_id, val
        )

    # ── Logging ─────────────────────────────────────────────────────────────

    async def log_event(self, guild_id: int, event_type: str,
                        actor: str = None, target: str = None,
                        channel: str = None, description: str = None):
        try:
            await self.pool.execute(
                "INSERT INTO server_logs "
                "(guild_id,event_type,actor,target,channel,description) "
                "VALUES ($1,$2,$3,$4,$5,$6)",
                guild_id, event_type, actor, target, channel, description
            )
        except Exception as e:
            log.error(f"log_event failed: {e}")

    async def get_logs(self, guild_id: int, event_type: str = None,
                       limit: int = 40) -> list[dict]:
        async with self.pool.acquire() as c:
            if event_type:
                rows = await c.fetch(
                    "SELECT * FROM server_logs WHERE guild_id=$1 AND event_type=$2 "
                    "ORDER BY created_at DESC LIMIT $3",
                    guild_id, event_type, limit
                )
            else:
                rows = await c.fetch(
                    "SELECT * FROM server_logs WHERE guild_id=$1 "
                    "ORDER BY created_at DESC LIMIT $2",
                    guild_id, limit
                )
            return [dict(r) for r in rows]

    # ── Messages ─────────────────────────────────────────────────────────────

    async def save_message(self, guild_id, channel_id, user_id,
                           username, content, is_embed=False):
        try:
            await self.pool.execute(
                "INSERT INTO message_history "
                "(guild_id,channel_id,user_id,username,content,is_embed) "
                "VALUES ($1,$2,$3,$4,$5,$6)",
                guild_id, channel_id, user_id, username, content[:2000], is_embed
            )
            await self.pool.execute(
                "DELETE FROM message_history WHERE channel_id=$1 AND id NOT IN ("
                "SELECT id FROM message_history WHERE channel_id=$1 "
                "ORDER BY created_at DESC LIMIT 300)",
                channel_id
            )
        except Exception as e:
            log.error(f"save_message failed: {e}")

    async def get_recent(self, channel_id: int, limit: int = 10) -> list[dict]:
        async with self.pool.acquire() as c:
            rows = await c.fetch(
                "SELECT username,content,is_embed,created_at FROM message_history "
                "WHERE channel_id=$1 ORDER BY created_at DESC LIMIT $2",
                channel_id, limit
            )
            return [dict(r) for r in reversed(rows)]

    async def get_user_messages(self, guild_id, user_id, limit=15) -> list[dict]:
        async with self.pool.acquire() as c:
            rows = await c.fetch(
                "SELECT content,created_at FROM message_history "
                "WHERE guild_id=$1 AND user_id=$2 ORDER BY created_at DESC LIMIT $3",
                guild_id, user_id, limit
            )
            return [dict(r) for r in rows]

    # ── Member history ───────────────────────────────────────────────────────

    async def mod_action(self, guild_id, user_id, action, reason):
        try:
            await self.pool.execute(
                "INSERT INTO member_history (guild_id,user_id,action,reason) "
                "VALUES ($1,$2,$3,$4)",
                guild_id, user_id, action, reason
            )
        except Exception as e:
            log.error(f"mod_action failed: {e}")

    async def get_member_history(self, guild_id, user_id) -> list[dict]:
        async with self.pool.acquire() as c:
            rows = await c.fetch(
                "SELECT action,reason,moderator,created_at FROM member_history "
                "WHERE guild_id=$1 AND user_id=$2 ORDER BY created_at DESC LIMIT 20",
                guild_id, user_id
            )
            return [dict(r) for r in rows]

    async def warn_count(self, guild_id, user_id) -> int:
        return await self.pool.fetchval(
            "SELECT COUNT(*) FROM member_history "
            "WHERE guild_id=$1 AND user_id=$2 AND action='WARN'",
            guild_id, user_id
        ) or 0

    # ── Timed bans ───────────────────────────────────────────────────────────

    async def add_timed_ban(self, guild_id, user_id, reason, expires_at):
        await self.pool.execute(
            "INSERT INTO timed_bans (guild_id,user_id,reason,expires_at) "
            "VALUES ($1,$2,$3,$4)",
            guild_id, user_id, reason, expires_at
        )

    async def get_expired_bans(self) -> list[dict]:
        async with self.pool.acquire() as c:
            rows = await c.fetch(
                "SELECT * FROM timed_bans WHERE active=TRUE AND expires_at<=NOW()"
            )
            return [dict(r) for r in rows]

    async def expire_ban(self, ban_id: int):
        await self.pool.execute(
            "UPDATE timed_bans SET active=FALSE WHERE id=$1", ban_id
        )

    # ── Scheduled messages ────────────────────────────────────────────────────

    async def add_schedule(self, guild_id, channel_id, content, send_at, repeat=0):
        return await self.pool.fetchval(
            "INSERT INTO scheduled_messages "
            "(guild_id,channel_id,content,send_at,repeat_minutes) "
            "VALUES ($1,$2,$3,$4,$5) RETURNING id",
            guild_id, channel_id, content, send_at, repeat
        )

    async def get_due_messages(self) -> list[dict]:
        async with self.pool.acquire() as c:
            rows = await c.fetch(
                "SELECT * FROM scheduled_messages WHERE active=TRUE AND send_at<=NOW()"
            )
            return [dict(r) for r in rows]

    async def reschedule(self, msg_id: int, repeat_minutes: int):
        if repeat_minutes > 0:
            await self.pool.execute(
                "UPDATE scheduled_messages SET send_at = NOW() + ($2 || ' minutes')::interval "
                "WHERE id=$1",
                msg_id, str(repeat_minutes)
            )
        else:
            await self.pool.execute(
                "UPDATE scheduled_messages SET active=FALSE WHERE id=$1", msg_id
            )

    async def get_schedules(self, guild_id: int) -> list[dict]:
        async with self.pool.acquire() as c:
            rows = await c.fetch(
                "SELECT * FROM scheduled_messages WHERE guild_id=$1 AND active=TRUE",
                guild_id
            )
            return [dict(r) for r in rows]

    async def cancel_schedule(self, schedule_id: int):
        await self.pool.execute(
            "UPDATE scheduled_messages SET active=FALSE WHERE id=$1", schedule_id
        )

    # ── Coin alerts ───────────────────────────────────────────────────────────

    async def add_coin_alert(self, guild_id, channel_id, user_id,
                             coin_id, name, symbol, direction, price):
        await self.pool.execute(
            "INSERT INTO coin_alerts "
            "(guild_id,channel_id,user_id,coin_id,coin_name,coin_symbol,direction,price) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8)",
            guild_id, channel_id, user_id, coin_id, name, symbol, direction, price
        )

    async def get_active_alerts(self) -> list[dict]:
        async with self.pool.acquire() as c:
            rows = await c.fetch(
                "SELECT * FROM coin_alerts WHERE active=TRUE"
            )
            return [dict(r) for r in rows]

    async def get_guild_alerts(self, guild_id: int) -> list[dict]:
        async with self.pool.acquire() as c:
            rows = await c.fetch(
                "SELECT * FROM coin_alerts WHERE guild_id=$1 AND active=TRUE",
                guild_id
            )
            return [dict(r) for r in rows]

    async def fire_alert(self, alert_id: int):
        await self.pool.execute(
            "UPDATE coin_alerts SET active=FALSE WHERE id=$1", alert_id
        )

    async def remove_coin_alerts(self, guild_id: int, coin_id: str):
        await self.pool.execute(
            "UPDATE coin_alerts SET active=FALSE "
            "WHERE guild_id=$1 AND coin_id=$2",
            guild_id, coin_id
        )

    # ── Stat channels ─────────────────────────────────────────────────────────

    async def set_stat_channel(self, guild_id, vc_id, coin_id, name, symbol):
        await self.pool.execute(
            "INSERT INTO coin_stat_channels "
            "(guild_id,voice_channel_id,coin_id,coin_name,coin_symbol) "
            "VALUES ($1,$2,$3,$4,$5) ON CONFLICT (guild_id,voice_channel_id) "
            "DO UPDATE SET coin_id=$3,coin_name=$4,coin_symbol=$5,active=TRUE",
            guild_id, vc_id, coin_id, name, symbol
        )

    async def get_stat_channels(self) -> list[dict]:
        async with self.pool.acquire() as c:
            rows = await c.fetch(
                "SELECT * FROM coin_stat_channels WHERE active=TRUE"
            )
            return [dict(r) for r in rows]

    async def remove_stat_channel(self, guild_id: int, coin_id: str):
        await self.pool.execute(
            "UPDATE coin_stat_channels SET active=FALSE "
            "WHERE guild_id=$1 AND coin_id=$2",
            guild_id, coin_id
        )

    # ── Orders ────────────────────────────────────────────────────────────────

    async def add_order(self, guild_id, name, description):
        await self.pool.execute(
            "INSERT INTO orders (guild_id,name,description) VALUES ($1,$2,$3) "
            "ON CONFLICT (guild_id,name) DO UPDATE SET description=$3",
            guild_id, name.lower(), description
        )

    async def get_order(self, guild_id, name) -> dict | None:
        async with self.pool.acquire() as c:
            row = await c.fetchrow(
                "SELECT * FROM orders WHERE guild_id=$1 AND name=$2",
                guild_id, name.lower()
            )
            return dict(row) if row else None

    async def list_orders(self, guild_id: int) -> list[dict]:
        async with self.pool.acquire() as c:
            rows = await c.fetch(
                "SELECT name,description FROM orders WHERE guild_id=$1 ORDER BY name",
                guild_id
            )
            return [dict(r) for r in rows]

    async def delete_order(self, guild_id: int, name: str):
        await self.pool.execute(
            "DELETE FROM orders WHERE guild_id=$1 AND name=$2",
            guild_id, name.lower()
        )


# ═══════════════════════════════════════════════════════════════════════════════
# 3 ── CRYPTO CLIENT  (CoinGecko — free, no key)
# ═══════════════════════════════════════════════════════════════════════════════

class CryptoClient:

    BASE = "https://api.coingecko.com/api/v3"

    async def search(self, query: str) -> dict | None:
        """Find coin by name/symbol. Returns {id, name, symbol}."""
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(
                    f"{self.BASE}/search",
                    params={"query": query},
                    timeout=aiohttp.ClientTimeout(total=8)
                ) as r:
                    if r.status != 200:
                        return None
                    data = await r.json()
                    coins = data.get("coins", [])
                    if coins:
                        c = coins[0]
                        return {
                            "id": c["id"],
                            "name": c["name"],
                            "symbol": c["symbol"].upper()
                        }
        except Exception as e:
            log.error(f"Crypto search error: {e}")
        return None

    async def price(self, coin_id: str) -> float | None:
        """Get current USD price for a coin."""
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(
                    f"{self.BASE}/simple/price",
                    params={"ids": coin_id, "vs_currencies": "usd"},
                    timeout=aiohttp.ClientTimeout(total=8)
                ) as r:
                    if r.status == 429:
                        return None   # rate limited
                    data = await r.json()
                    return data.get(coin_id, {}).get("usd")
        except Exception as e:
            log.error(f"Crypto price error: {e}")
        return None

    async def prices_batch(self, coin_ids: list[str]) -> dict[str, float]:
        """Get USD prices for multiple coins in one call."""
        if not coin_ids:
            return {}
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(
                    f"{self.BASE}/simple/price",
                    params={"ids": ",".join(coin_ids), "vs_currencies": "usd"},
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as r:
                    if r.status == 429:
                        return {}
                    data = await r.json()
                    return {cid: data.get(cid, {}).get("usd", 0)
                            for cid in coin_ids}
        except Exception as e:
            log.error(f"Crypto batch error: {e}")
        return {}

    def fmt_price(self, price: float) -> str:
        if price is None:
            return "N/A"
        if price >= 1:
            return f"${price:,.2f}"
        if price >= 0.001:
            return f"${price:.6f}"
        return f"${price:.10f}"


# ═══════════════════════════════════════════════════════════════════════════════
# 4 ── GEMINI CLIENT  (human-like personality)
# ═══════════════════════════════════════════════════════════════════════════════

TOOLS = [{
    "function_declarations": [
        {
            "name": "warn_member",
            "description": "Issue a warning to a member.",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "string"},
                    "reason":  {"type": "string"}
                },
                "required": ["user_id", "reason"]
            }
        },
        {
            "name": "kick_member",
            "description": "Kick a member from the server.",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "string"},
                    "reason":  {"type": "string"}
                },
                "required": ["user_id", "reason"]
            }
        },
        {
            "name": "ban_member",
            "description": "Ban a member. Optionally timed.",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id":      {"type": "string"},
                    "reason":       {"type": "string"},
                    "delete_days":  {"type": "integer"},
                    "duration_hours": {"type": "integer",
                                      "description": "0 means permanent"}
                },
                "required": ["user_id", "reason"]
            }
        },
        {
            "name": "timeout_member",
            "description": "Temporarily mute a member.",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id":          {"type": "string"},
                    "reason":           {"type": "string"},
                    "duration_minutes": {"type": "integer"}
                },
                "required": ["user_id", "reason", "duration_minutes"]
            }
        },
        {
            "name": "delete_message",
            "description": "Delete the message being evaluated.",
            "parameters": {
                "type": "object",
                "properties": {"reason": {"type": "string"}},
                "required": ["reason"]
            }
        },
        {
            "name": "give_role",
            "description": "Assign a role to a member.",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id":   {"type": "string"},
                    "role_name": {"type": "string"},
                    "reason":    {"type": "string"}
                },
                "required": ["user_id", "role_name", "reason"]
            }
        },
        {
            "name": "remove_role",
            "description": "Remove a role from a member.",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id":   {"type": "string"},
                    "role_name": {"type": "string"},
                    "reason":    {"type": "string"}
                },
                "required": ["user_id", "role_name", "reason"]
            }
        },
        {
            "name": "create_role",
            "description": "Create a new server role.",
            "parameters": {
                "type": "object",
                "properties": {
                    "role_name": {"type": "string"},
                    "color_hex": {"type": "string"},
                    "reason":    {"type": "string"}
                },
                "required": ["role_name", "reason"]
            }
        },
        {
            "name": "rename_role",
            "description": "Rename an existing role.",
            "parameters": {
                "type": "object",
                "properties": {
                    "old_name": {"type": "string"},
                    "new_name": {"type": "string"}
                },
                "required": ["old_name", "new_name"]
            }
        },
        {
            "name": "create_channel",
            "description": "Create a text, voice, or category channel.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name":     {"type": "string"},
                    "kind":     {"type": "string",
                                 "description": "text | voice | category"},
                    "category": {"type": "string",
                                 "description": "category name (optional)"},
                    "topic":    {"type": "string"},
                    "reason":   {"type": "string"}
                },
                "required": ["name", "kind", "reason"]
            }
        },
        {
            "name": "delete_channel",
            "description": "Delete a channel by name. Always asks for confirmation first.",
            "parameters": {
                "type": "object",
                "properties": {
                    "channel_name": {"type": "string"},
                    "reason":       {"type": "string"}
                },
                "required": ["channel_name", "reason"]
            }
        },
        {
            "name": "rename_channel",
            "description": "Rename a channel.",
            "parameters": {
                "type": "object",
                "properties": {
                    "old_name": {"type": "string"},
                    "new_name": {"type": "string"}
                },
                "required": ["old_name", "new_name"]
            }
        },
        {
            "name": "move_channel",
            "description": "Move a channel to a different category.",
            "parameters": {
                "type": "object",
                "properties": {
                    "channel_name":  {"type": "string"},
                    "category_name": {"type": "string"}
                },
                "required": ["channel_name", "category_name"]
            }
        },
        {
            "name": "set_slowmode",
            "description": "Set slowmode on a channel (0 to disable).",
            "parameters": {
                "type": "object",
                "properties": {
                    "channel_name": {"type": "string",
                                    "description": "current or channel name"},
                    "seconds":      {"type": "integer"}
                },
                "required": ["channel_name", "seconds"]
            }
        },
        {
            "name": "lock_channel",
            "description": "Lock the current channel.",
            "parameters": {
                "type": "object",
                "properties": {"reason": {"type": "string"}},
                "required": ["reason"]
            }
        },
        {
            "name": "unlock_channel",
            "description": "Unlock the current channel.",
            "parameters": {
                "type": "object",
                "properties": {"reason": {"type": "string"}},
                "required": ["reason"]
            }
        },
        {
            "name": "send_message",
            "description": "Send a message in the current channel.",
            "parameters": {
                "type": "object",
                "properties": {"content": {"type": "string"}},
                "required": ["content"]
            }
        },
        {
            "name": "add_emoji",
            "description": "Add an emoji from a URL.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name":    {"type": "string"},
                    "url":     {"type": "string"},
                    "reason":  {"type": "string"}
                },
                "required": ["name", "url", "reason"]
            }
        },
        {
            "name": "no_action",
            "description": "Message is fine. Take no action.",
            "parameters": {
                "type": "object",
                "properties": {"reason": {"type": "string"}},
                "required": ["reason"]
            }
        }
    ]
}]


def build_system_prompt(config: dict, guild_name: str,
                        guild_roles: list[str] = None) -> str:
    trained = config.get("system_prompt") or "Be fair, firm, and consistent."
    rules   = config.get("server_rules")  or "No rules set yet."
    roles   = ", ".join(guild_roles) if guild_roles else "none"
    return f"""You are TECO, the co-admin of "{guild_name}". You talk like a real person — not a bot, not a tool.

PERSONALITY:
- Direct. Confident. Short sentences. No filler.
- Use contractions: don't, I've, they're, it's.
- When you act, say what you did and why — one line, human tone.
- When things are calm, say so naturally. "All quiet." is a fine answer.
- When asked for logs or reports, pull specific details. Name people. Name the issue.
- Never say "I have executed the function". Say what you actually did.

HOW YOU SOUND:
❌ "I have executed the timeout_member function for a duration of 60 minutes."
✅ "Muted Jake for an hour — kept spamming after two warnings."

❌ "No moderation actions were required in the specified time period."
✅ "All quiet. Nothing to flag."

❌ "The user has received their third warning."
✅ "That's Mike's third warning. Muting him now per the escalation policy."

OWNER INSTRUCTIONS:
{trained}

SERVER RULES:
{rules}

AVAILABLE ROLES:
{roles}

TOOL RULES:
- Call exactly ONE tool per evaluation (or no_action if message is clean).
- Never act against administrators or the server owner.
- For channel deletion, always call delete_channel — the system handles confirmation.
- Match role names approximately from the available roles list.
- For ban with a duration, set duration_hours > 0. For permanent, use 0."""


class GeminiClient:
    _rate_hit: float = 0.0   # last 429 timestamp — shared across instances

    async def _post(self, contents: list, system: str,
                    use_tools: bool = True, temp: float = 0.1) -> dict:
        payload: dict = {
            "contents": contents,
            "systemInstruction": {"parts": [{"text": system}]},
            "generationConfig": {
                "maxOutputTokens": 350,
                "temperature": temp,
                "candidateCount": 1
            }
        }
        if use_tools:
            payload["tools"]      = TOOLS
            payload["toolConfig"] = {"functionCallingConfig": {"mode": "AUTO"}}

        timeout = aiohttp.ClientTimeout(total=12)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.post(GEMINI_URL, headers=GEMINI_HDR, json=payload) as r:
                if r.status == 429:
                    GeminiClient._rate_hit = time.time()
                    raise RuntimeError("rate_limit")
                if r.status != 200:
                    body = await r.text()
                    raise RuntimeError(f"HTTP {r.status}: {body[:200]}")
                return await r.json()

    @staticmethod
    def _parse(data: dict) -> tuple[str | None, str | None, dict | None]:
        txt, tool, args = None, None, None
        try:
            parts = data["candidates"][0]["content"]["parts"]
            for p in parts:
                if "text" in p and p["text"].strip():
                    txt = p["text"].strip()
                if "functionCall" in p:
                    tool = p["functionCall"]["name"]
                    args = p["functionCall"].get("args", {})
        except (KeyError, IndexError):
            reason = (data.get("candidates") or [{}])[0].get("finishReason", "")
            if reason == "SAFETY":
                txt = "⚠️ Safety filter blocked that response."
        return txt, tool, args

    @staticmethod
    def quick_skip(content: str) -> bool:
        """True = skip Gemini call (message is clearly fine)."""
        c = content.strip()
        if not c or len(c) < 5:
            return True
        if len(c.split()) <= 2:
            return True
        return False

    async def evaluate(self, message: discord.Message, recent: list[dict],
                       config: dict, guild_roles: list[str]) -> tuple:
        system = build_system_prompt(config, message.guild.name, guild_roles)
        ctx = "\n".join(
            f'{"[EMBED] " if m.get("is_embed") else ""}'
            f'{m["username"]}: {m["content"]}' for m in recent[-8:]
        )
        prompt = (
            f"RECENT CONTEXT:\n{ctx or '(none)'}\n\n"
            f"MESSAGE TO EVALUATE:\n"
            f"Author: {message.author.display_name} (ID: {message.author.id})\n"
            f"Content: {message.content or '[no text]'}\n\n"
            f"Call the right tool. no_action if the message is fine."
        )
        contents = [{"role": "user", "parts": [{"text": prompt}]}]
        try:
            data = await self._post(contents, system)
            return self._parse(data)
        except Exception as e:
            log.debug(f"evaluate error: {e}")
            return None, "no_action", {"reason": "AI unavailable"}

    async def owner_chat(self, question: str, config: dict,
                         guild: discord.Guild, snapshot: str,
                         history: list) -> tuple:
        system = build_system_prompt(
            config, guild.name,
            [r.name for r in guild.roles if not r.is_default()]
        )
        system += f"\n\nSERVER SNAPSHOT:\n{snapshot}"
        contents = []
        for t in history[-6:]:
            role = "user" if t["role"] == "user" else "model"
            contents.append({"role": role, "parts": [{"text": t["content"]}]})
        contents.append({"role": "user", "parts": [{"text": question}]})
        try:
            data = await self._post(contents, system, temp=0.3)
            return self._parse(data)
        except Exception as e:
            return f"⚠️ {e}", None, None

    async def simple(self, prompt: str, system: str) -> str:
        contents = [{"role": "user", "parts": [{"text": prompt}]}]
        try:
            data = await self._post(contents, system, use_tools=False, temp=0.4)
            txt, _, _ = self._parse(data)
            return txt or "No response."
        except Exception as e:
            return f"⚠️ AI error: {e}"

    async def log_query(self, question: str, logs: list[dict],
                        guild_name: str) -> str:
        log_text = "\n".join(
            f"[{l['created_at'].strftime('%b %d %H:%M')}] "
            f"{l['event_type']}"
            f"{' — ' + l['description'] if l.get('description') else ''}"
            for l in logs
        )
        system = (
            f'You are TECO, co-admin of "{guild_name}". '
            f"Answer log queries like a human staff member reading their notes. "
            f"Be specific, direct, and concise."
        )
        prompt = (
            f"Owner asked: \"{question}\"\n\n"
            f"LOG DATA:\n{log_text or 'No logs found.'}\n\n"
            f"Answer the question using this data. Talk like a person, not a report."
        )
        return await self.simple(prompt, system)


# ═══════════════════════════════════════════════════════════════════════════════
# 5 ── CONFIRMATION VIEW  (buttons for destructive actions)
# ═══════════════════════════════════════════════════════════════════════════════

class ConfirmView(discord.ui.View):
    def __init__(self, on_confirm: Callable, timeout: int = 30):
        super().__init__(timeout=timeout)
        self.on_confirm = on_confirm
        self._done = False

    @discord.ui.button(label="✅ Confirm", style=discord.ButtonStyle.green)
    async def yes(self, interaction: discord.Interaction, _: discord.ui.Button):
        if self._done:
            return
        self._done = True
        self.stop()
        await self.on_confirm(interaction)

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.red)
    async def no(self, interaction: discord.Interaction, _: discord.ui.Button):
        if self._done:
            return
        self._done = True
        self.stop()
        await interaction.response.edit_message(
            content="❌ Cancelled.", embed=None, view=None
        )

    async def on_timeout(self):
        self._done = True


# ═══════════════════════════════════════════════════════════════════════════════
# 6 ── TOOL EXECUTOR  (maps Gemini decisions → Discord actions)
# ═══════════════════════════════════════════════════════════════════════════════

class ToolExecutor:
    def __init__(self, db: Database):
        self.db = db

    def _role(self, guild: discord.Guild, name: str) -> discord.Role | None:
        name_l = name.lower().strip()
        for r in guild.roles:
            if r.name.lower() == name_l:
                return r
        for r in guild.roles:
            if name_l in r.name.lower():
                return r
        return None

    def _channel(self, guild: discord.Guild, name: str):
        name_l = name.lower().strip().replace(" ", "-")
        for ch in guild.channels:
            if ch.name.lower() == name_l:
                return ch
        for ch in guild.channels:
            if name_l in ch.name.lower():
                return ch
        return None

    def _category(self, guild: discord.Guild, name: str) -> discord.CategoryChannel | None:
        if not name:
            return None
        name_l = name.lower()
        for cat in guild.categories:
            if name_l in cat.name.lower():
                return cat
        return None

    async def run(self, tool: str, args: dict,
                  message: discord.Message,
                  anchor: discord.Message = None) -> str:
        anchor  = anchor or message
        guild   = message.guild
        channel = message.channel
        reason  = args.get("reason", "AI decision")

        try:
            # ── No action ──────────────────────────────────────────────────
            if tool == "no_action":
                return f"✅ {reason}"

            # ── Warn ───────────────────────────────────────────────────────
            if tool == "warn_member":
                uid    = int(args["user_id"])
                member = guild.get_member(uid)
                if not member:
                    return f"⚠️ Can't find member {uid}"
                await self.db.mod_action(guild.id, uid, "WARN", reason)
                count = await self.db.warn_count(guild.id, uid)
                await channel.send(
                    f"⚠️ {member.mention} — **Warning #{count}**: {reason}",
                    delete_after=20
                )
                try:
                    await member.send(f"⚠️ Warning in **{guild.name}**: {reason}")
                except Exception:
                    pass
                await self.db.log_event(guild.id, "WARN", "TECO",
                    member.display_name, f"#{channel.name}",
                    f"Warning #{count}: {reason}"
                )
                return f"⚠️ Warned {member.display_name} (#{count}): {reason}"

            # ── Kick ───────────────────────────────────────────────────────
            if tool == "kick_member":
                uid    = int(args["user_id"])
                member = guild.get_member(uid)
                if not member:
                    return f"⚠️ Can't find member {uid}"
                await self.db.mod_action(guild.id, uid, "KICK", reason)
                try:
                    await member.send(f"👢 Kicked from **{guild.name}**: {reason}")
                except Exception:
                    pass
                await member.kick(reason=f"[TECO] {reason}")
                await self.db.log_event(guild.id, "KICK", "TECO",
                    member.display_name, f"#{channel.name}", reason
                )
                return f"👢 Kicked {member.display_name}: {reason}"

            # ── Ban ────────────────────────────────────────────────────────
            if tool == "ban_member":
                uid      = int(args["user_id"])
                del_days = int(args.get("delete_days", 1))
                hours    = int(args.get("duration_hours", 0))
                member   = guild.get_member(uid) or discord.Object(id=uid)
                await self.db.mod_action(guild.id, uid, "BAN", reason)
                if isinstance(member, discord.Member):
                    try:
                        await member.send(
                            f"🔨 Banned from **{guild.name}**: {reason}"
                        )
                    except Exception:
                        pass
                await guild.ban(member, reason=f"[TECO] {reason}",
                                delete_message_days=max(0, min(7, del_days)))
                if hours > 0:
                    expires = discord.utils.utcnow() + timedelta(hours=hours)
                    await self.db.add_timed_ban(guild.id, uid, reason, expires)
                    dur = f"{hours}h timed"
                else:
                    dur = "permanent"
                await self.db.log_event(guild.id, "BAN", "TECO",
                    str(uid), None, f"{dur}: {reason}"
                )
                return f"🔨 Banned {uid} ({dur}): {reason}"

            # ── Timeout ────────────────────────────────────────────────────
            if tool == "timeout_member":
                uid    = int(args["user_id"])
                mins   = max(1, min(40320, int(args.get("duration_minutes", 10))))
                member = guild.get_member(uid)
                if not member:
                    return f"⚠️ Can't find member {uid}"
                until = discord.utils.utcnow() + timedelta(minutes=mins)
                await member.timeout(until, reason=f"[TECO] {reason}")
                await self.db.mod_action(guild.id, uid, f"MUTE_{mins}m", reason)
                await self.db.log_event(guild.id, "MUTE", "TECO",
                    member.display_name, f"#{channel.name}",
                    f"{mins}min: {reason}"
                )
                return f"🔇 Muted {member.display_name} for {mins}min: {reason}"

            # ── Delete message ──────────────────────────────────────────────
            if tool == "delete_message":
                try:
                    await message.delete()
                except discord.NotFound:
                    pass
                await self.db.mod_action(
                    guild.id, message.author.id, "MSG_DELETE", reason
                )
                await self.db.log_event(guild.id, "MSG_DELETE", "TECO",
                    message.author.display_name, f"#{channel.name}", reason
                )
                return f"🗑️ Deleted message from {message.author.display_name}: {reason}"

            # ── Give role ───────────────────────────────────────────────────
            if tool == "give_role":
                uid    = int(args["user_id"])
                member = guild.get_member(uid)
                if not member:
                    return f"⚠️ Can't find member {uid}"
                role = self._role(guild, args["role_name"])
                if not role:
                    return f"⚠️ Role \"{args['role_name']}\" not found"
                if role in member.roles:
                    return f"ℹ️ {member.display_name} already has {role.name}"
                await member.add_roles(role, reason=f"[TECO] {reason}")
                await self.db.mod_action(guild.id, uid, f"ROLE_ADD:{role.name}", reason)
                await self.db.log_event(guild.id, "ROLE_ADD", "TECO",
                    member.display_name, None, f"{role.name}: {reason}"
                )
                return f"✅ Gave **{role.name}** to {member.display_name}"

            # ── Remove role ─────────────────────────────────────────────────
            if tool == "remove_role":
                uid    = int(args["user_id"])
                member = guild.get_member(uid)
                if not member:
                    return f"⚠️ Can't find member {uid}"
                role = self._role(guild, args["role_name"])
                if not role:
                    return f"⚠️ Role \"{args['role_name']}\" not found"
                if role not in member.roles:
                    return f"ℹ️ {member.display_name} doesn't have {role.name}"
                await member.remove_roles(role, reason=f"[TECO] {reason}")
                await self.db.mod_action(guild.id, uid, f"ROLE_REMOVE:{role.name}", reason)
                await self.db.log_event(guild.id, "ROLE_REMOVE", "TECO",
                    member.display_name, None, f"{role.name}: {reason}"
                )
                return f"✅ Removed **{role.name}** from {member.display_name}"

            # ── Create role ─────────────────────────────────────────────────
            if tool == "create_role":
                name  = args["role_name"]
                color = discord.Color.default()
                if args.get("color_hex"):
                    try:
                        color = discord.Color(int(args["color_hex"].lstrip("#"), 16))
                    except Exception:
                        pass
                new_role = await guild.create_role(
                    name=name, color=color, reason=f"[TECO] {reason}"
                )
                await self.db.log_event(guild.id, "ROLE_CREATE", "TECO",
                    None, None, f"Created role: {name}"
                )
                return f"✅ Created role **{new_role.name}**"

            # ── Rename role ─────────────────────────────────────────────────
            if tool == "rename_role":
                role = self._role(guild, args["old_name"])
                if not role:
                    return f"⚠️ Role \"{args['old_name']}\" not found"
                old = role.name
                await role.edit(name=args["new_name"], reason="[TECO] rename")
                await self.db.log_event(guild.id, "ROLE_RENAME", "TECO",
                    None, None, f"{old} → {args['new_name']}"
                )
                return f"✅ Renamed role: {old} → {args['new_name']}"

            # ── Create channel ──────────────────────────────────────────────
            if tool == "create_channel":
                name     = args["name"].replace(" ", "-").lower()
                kind     = args.get("kind", "text").lower()
                category = self._category(guild, args.get("category", ""))
                topic    = args.get("topic", "")

                async def do_create(interaction: discord.Interaction):
                    if kind == "voice":
                        ch = await guild.create_voice_channel(
                            name, category=category,
                            reason=f"[TECO] {reason}"
                        )
                        label = f"🔊 {ch.name}"
                    elif kind == "category":
                        ch = await guild.create_category(
                            name, reason=f"[TECO] {reason}"
                        )
                        label = f"📁 {ch.name}"
                    else:
                        ch = await guild.create_text_channel(
                            name, topic=topic, category=category,
                            reason=f"[TECO] {reason}"
                        )
                        label = ch.mention
                    await self.db.log_event(guild.id, "CHANNEL_CREATE", "TECO",
                        None, ch.name, reason
                    )
                    await interaction.response.edit_message(
                        content=f"✅ Created {label}", embed=None, view=None
                    )

                e = discord.Embed(
                    title=f"Create {kind} channel?",
                    description=(
                        f"**Name:** `{name}`\n"
                        f"**Type:** {kind}\n"
                        f"**Category:** {category.name if category else 'None'}\n"
                        f"**Reason:** {reason}"
                    ),
                    color=discord.Color.blue()
                )
                await anchor.channel.send(embed=e, view=ConfirmView(do_create))
                return f"📝 Awaiting confirmation to create #{name}"

            # ── Delete channel ──────────────────────────────────────────────
            if tool == "delete_channel":
                ch = self._channel(guild, args["channel_name"])
                if not ch:
                    return f"⚠️ Channel \"{args['channel_name']}\" not found"

                async def do_delete(interaction: discord.Interaction):
                    await self.db.log_event(guild.id, "CHANNEL_DELETE", "TECO",
                        None, ch.name, reason
                    )
                    await interaction.response.edit_message(
                        content=f"✅ Deleted #{ch.name}", embed=None, view=None
                    )
                    await ch.delete(reason=f"[TECO] {reason}")

                e = discord.Embed(
                    title="⚠️ Delete channel?",
                    description=f"**Channel:** {ch.mention}\n**Reason:** {reason}\n\n"
                                f"*This cannot be undone.*",
                    color=discord.Color.red()
                )
                await anchor.channel.send(embed=e, view=ConfirmView(do_delete))
                return f"⚠️ Awaiting confirmation to delete #{ch.name}"

            # ── Rename channel ──────────────────────────────────────────────
            if tool == "rename_channel":
                ch = self._channel(guild, args["old_name"])
                if not ch:
                    return f"⚠️ Channel \"{args['old_name']}\" not found"
                old = ch.name
                await ch.edit(name=args["new_name"], reason="[TECO] rename")
                await self.db.log_event(guild.id, "CHANNEL_RENAME", "TECO",
                    None, old, f"→ {args['new_name']}"
                )
                return f"✅ Renamed #{old} → #{args['new_name']}"

            # ── Move channel ────────────────────────────────────────────────
            if tool == "move_channel":
                ch  = self._channel(guild, args["channel_name"])
                cat = self._category(guild, args["category_name"])
                if not ch:
                    return f"⚠️ Channel \"{args['channel_name']}\" not found"
                if not cat:
                    return f"⚠️ Category \"{args['category_name']}\" not found"
                await ch.edit(category=cat, reason="[TECO] move")
                await self.db.log_event(guild.id, "CHANNEL_MOVE", "TECO",
                    None, ch.name, f"→ {cat.name}"
                )
                return f"✅ Moved #{ch.name} → {cat.name}"

            # ── Slowmode ────────────────────────────────────────────────────
            if tool == "set_slowmode":
                name_hint = args.get("channel_name", "")
                secs      = max(0, min(21600, int(args.get("seconds", 0))))
                ch = (
                    self._channel(guild, name_hint)
                    if name_hint.lower() != "current"
                    else channel
                )
                if not ch or not isinstance(ch, discord.TextChannel):
                    ch = channel
                await ch.edit(slowmode_delay=secs)
                label = f"{secs}s" if secs else "off"
                await self.db.log_event(guild.id, "SLOWMODE", "TECO",
                    None, ch.name, f"Set to {label}"
                )
                return f"⏱️ Slowmode {label} in #{ch.name}"

            # ── Lock / Unlock ───────────────────────────────────────────────
            if tool == "lock_channel":
                ow = channel.overwrites_for(guild.default_role)
                ow.send_messages = False
                await channel.set_permissions(guild.default_role, overwrite=ow)
                await channel.send(f"🔒 Locked — {reason}")
                await self.db.log_event(guild.id, "LOCK", "TECO",
                    None, f"#{channel.name}", reason
                )
                return f"🔒 Locked #{channel.name}"

            if tool == "unlock_channel":
                ow = channel.overwrites_for(guild.default_role)
                ow.send_messages = None
                await channel.set_permissions(guild.default_role, overwrite=ow)
                await channel.send(f"🔓 Unlocked — {reason}")
                await self.db.log_event(guild.id, "UNLOCK", "TECO",
                    None, f"#{channel.name}", reason
                )
                return f"🔓 Unlocked #{channel.name}"

            # ── Send message ────────────────────────────────────────────────
            if tool == "send_message":
                await channel.send(args.get("content", "")[:2000])
                return "💬 Message sent"

            # ── Add emoji ───────────────────────────────────────────────────
            if tool == "add_emoji":
                name = args["name"].replace(" ", "_")
                url  = args["url"]
                async with aiohttp.ClientSession() as s:
                    async with s.get(url) as resp:
                        img = await resp.read()
                new_em = await guild.create_custom_emoji(
                    name=name, image=img,
                    reason=f"[TECO] {args.get('reason', '')}"
                )
                await self.db.log_event(guild.id, "EMOJI_ADD", "TECO",
                    None, None, f"Added :{new_em.name}:"
                )
                return f"✅ Added emoji :{new_em.name}:"

            return f"Unknown tool: {tool}"

        except discord.Forbidden:
            return f"❌ No permission for {tool} — check my role position"
        except discord.HTTPException as e:
            log.error(f"Tool {tool} HTTP error: {e}")
            return f"❌ Discord error in {tool}: {e.text[:100]}"
        except Exception as e:
            log.error(f"Tool {tool} error: {e}")
            return f"❌ {tool} failed: {e}"


# ═══════════════════════════════════════════════════════════════════════════════
# 7 ── THE BOT
# ═══════════════════════════════════════════════════════════════════════════════

class TECO(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix=PREFIX,
            intents=discord.Intents.all(),
            help_command=None
        )
        self.db      = Database()
        self.ai      = GeminiClient()
        self.crypto  = CryptoClient()
        self.exe     = ToolExecutor(self.db)
        self._hist: dict[int, list] = defaultdict(list)   # owner convo history

    async def setup_hook(self):
        await self.db.connect()
        await self.load_extension("cogs.tasks")
        try:
            synced = await self.tree.sync()
            log.info(f"✅ Synced {len(synced)} slash command(s)")
        except Exception as e:
            log.error(f"Sync error: {e}")

    async def on_ready(self):
        log.info(f"✅ TECO online — {self.user}")
        await self.change_presence(
            status=discord.Status.online,
            activity=discord.Activity(
                type=discord.ActivityType.watching, name="your server"
            )
        )

    async def on_guild_join(self, guild: discord.Guild):
        await self.db.get_config(guild.id)

    # ── Full event logging ───────────────────────────────────────────────────

    async def on_message_delete(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        await self.db.log_event(
            message.guild.id, "MSG_DELETE",
            message.author.display_name, None,
            f"#{message.channel.name}",
            (message.content or "[no text]")[:300]
        )
        await self._post_log(message.guild, (
            f"🗑️ Message deleted in #{message.channel.name} "
            f"by **{message.author.display_name}**: "
            f"{(message.content or '[embed/attachment]')[:150]}"
        ))

    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if before.author.bot or not before.guild:
            return
        if before.content == after.content:
            return
        await self.db.log_event(
            before.guild.id, "MSG_EDIT",
            before.author.display_name, None,
            f"#{before.channel.name}",
            f"Before: {before.content[:150]} | After: {after.content[:150]}"
        )
        await self._post_log(before.guild, (
            f"✏️ **{before.author.display_name}** edited in #{before.channel.name}\n"
            f"Before: {before.content[:100]}\n"
            f"After: {after.content[:100]}"
        ))

    async def on_member_join(self, member: discord.Member):
        config = await self.db.get_config(member.guild.id)
        # Autorole
        if config.get("autorole_id"):
            role = member.guild.get_role(config["autorole_id"])
            if role:
                try:
                    await member.add_roles(role, reason="TECO autorole")
                except Exception:
                    pass
        await self.db.log_event(
            member.guild.id, "MEMBER_JOIN",
            member.display_name, None, None,
            f"Account age: {(discord.utils.utcnow()-member.created_at).days}d"
        )
        await self._post_log(member.guild, (
            f"📥 **{member.display_name}** joined. "
            f"Account: {(discord.utils.utcnow()-member.created_at).days} days old."
        ))

    async def on_member_remove(self, member: discord.Member):
        await self.db.log_event(
            member.guild.id, "MEMBER_LEAVE",
            member.display_name, None, None, None
        )
        await self._post_log(member.guild,
            f"📤 **{member.display_name}** left the server."
        )

    async def on_member_ban(self, guild: discord.Guild, user: discord.User):
        await self.db.log_event(
            guild.id, "BAN", None, user.display_name, None, None
        )

    async def on_member_unban(self, guild: discord.Guild, user: discord.User):
        await self.db.log_event(
            guild.id, "UNBAN", None, user.display_name, None, None
        )
        await self._post_log(guild, f"✅ **{user.display_name}** was unbanned.")

    async def on_guild_channel_create(self, channel):
        await self.db.log_event(
            channel.guild.id, "CHANNEL_CREATE",
            None, None, channel.name, f"Type: {channel.type}"
        )

    async def on_guild_channel_delete(self, channel):
        await self.db.log_event(
            channel.guild.id, "CHANNEL_DELETE",
            None, None, channel.name, None
        )

    async def on_guild_role_create(self, role: discord.Role):
        await self.db.log_event(
            role.guild.id, "ROLE_CREATE",
            None, None, None, f"Role: {role.name}"
        )

    async def on_member_update(self, before: discord.Member, after: discord.Member):
        added   = set(after.roles) - set(before.roles)
        removed = set(before.roles) - set(after.roles)
        for r in added:
            await self.db.log_event(after.guild.id, "ROLE_ADD",
                None, after.display_name, None, f"Got: {r.name}"
            )
        for r in removed:
            await self.db.log_event(after.guild.id, "ROLE_REMOVE",
                None, after.display_name, None, f"Lost: {r.name}"
            )

    async def on_voice_state_update(self, member: discord.Member,
                                    before: discord.VoiceState,
                                    after: discord.VoiceState):
        if before.channel == after.channel:
            return
        if after.channel:
            desc = f"Joined #{after.channel.name}"
        else:
            desc = f"Left #{before.channel.name}"
        await self.db.log_event(
            member.guild.id, "VOICE",
            member.display_name, None, None, desc
        )

    # ── Main message pipeline ────────────────────────────────────────────────

    async def on_message(self, message: discord.Message):
        if not message.guild:
            return
        if message.author.bot and message.author.id != self.user.id:
            # Read and log embeds from other bots
            if message.embeds:
                await self._read_embeds(message)
            return

        if message.author.bot:
            return

        # Save message to history
        if message.content:
            await self.db.save_message(
                message.guild.id, message.channel.id,
                message.author.id, message.author.display_name,
                message.content
            )

        await self.process_commands(message)

        config   = await self.db.get_config(message.guild.id)
        content_l = message.content.lower() if message.content else ""

        # ── Trigger word / @mention detection ───────────────────────────────
        triggers  = config.get("trigger_words") or []
        triggered = any(
            re.search(rf"\b{re.escape(t)}\b", content_l)
            for t in triggers
        ) if triggers else False
        mentioned = self.user in message.mentions

        if triggered or mentioned:
            clean = message.content or ""
            for t in triggers:
                clean = re.sub(rf"\b{re.escape(t)}\b", "", clean,
                               flags=re.IGNORECASE)
            clean = (clean
                     .replace(f"<@{self.user.id}>", "")
                     .replace(f"<@!{self.user.id}>", "")
                     .strip())
            if not clean:
                await message.reply("Hey — what do you need?",
                                    mention_author=False)
                return

            is_priv = (
                message.author.id in OWNER_IDS
                or message.author.guild_permissions.administrator
                or any(
                    r.id in (config.get("trusted_role_ids") or [])
                    for r in message.author.roles
                )
            )

            if is_priv:
                await self._privileged_chat(message, clean, config)
            else:
                guild_roles = [r.name for r in message.guild.roles
                               if not r.is_default()]
                system = build_system_prompt(config, message.guild.name, guild_roles)
                async with message.channel.typing():
                    reply = await self.ai.simple(clean, system)
                await message.reply(reply[:2000], mention_author=False)
            return

        # ── Auto-mod pipeline ────────────────────────────────────────────────
        if message.author.id in OWNER_IDS:
            return
        if message.author.guild_permissions.administrator:
            return

        monitored = config.get("monitored_channels") or []
        if message.channel.id not in monitored:
            return
        if not config.get("auto_mod_enabled"):
            return
        if not message.content or self.ai.quick_skip(message.content):
            return

        now = time.time()
        if now - _last_scan[message.channel.id] < AI_SCAN_COOLDOWN:
            return
        _last_scan[message.channel.id] = now

        guild_roles = [r.name for r in message.guild.roles if not r.is_default()]
        recent      = await self.db.get_recent(message.channel.id, limit=8)
        txt, tool, args = await self.ai.evaluate(
            message, recent, config, guild_roles
        )
        if not tool:
            return

        result = await self.exe.run(tool, args or {}, message)
        if tool != "no_action":
            await self._post_log(message.guild, f"🤖 {result}")
            await self.db.log_event(
                message.guild.id, "AI_ACTION",
                "TECO", message.author.display_name,
                f"#{message.channel.name}", result
            )

    async def _read_embeds(self, message: discord.Message):
        """Extract content from bot embeds and save to history + log."""
        for embed in message.embeds:
            parts = []
            if embed.title:
                parts.append(f"Title: {embed.title}")
            if embed.description:
                parts.append(f"Desc: {embed.description[:200]}")
            for field in embed.fields:
                parts.append(f"{field.name}: {field.value[:100]}")
            if embed.footer and embed.footer.text:
                parts.append(f"Footer: {embed.footer.text[:80]}")
            if parts:
                content = " | ".join(parts)
                await self.db.save_message(
                    message.guild.id, message.channel.id,
                    message.author.id,
                    f"[BOT: {message.author.display_name}]",
                    content, is_embed=True
                )
                await self.db.log_event(
                    message.guild.id, "EMBED_RECEIVED",
                    message.author.display_name, None,
                    f"#{message.channel.name}", content[:300]
                )

    async def _privileged_chat(self, message: discord.Message,
                                query: str, config: dict):
        """Full agent mode for owner/admin/trusted role."""
        async with message.channel.typing():
            decisions = await self.db.get_logs(message.guild.id, limit=5)
            snapshot = (
                f"Server: {message.guild.name} | Members: {message.guild.member_count}\n"
                "Recent events:\n" +
                "\n".join(
                    f"- {l['event_type']}: {l.get('description','')}"
                    for l in decisions
                )
            )
            history = self._hist[message.author.id]
            txt, tool, args = await self.ai.owner_chat(
                query, config, message.guild, snapshot, history
            )
            history.append({"role": "user",      "content": query})
            history.append({"role": "assistant",  "content": txt or f"→ {tool}"})
            if len(history) > 12:
                self._hist[message.author.id] = history[-12:]

            parts = []
            if txt:
                parts.append(txt)
            if tool and tool != "no_action":
                result = await self.exe.run(tool, args or {}, message, anchor=message)
                parts.append(f"⚡ {result}")
                await self.db.log_event(
                    message.guild.id, "OWNER_CMD",
                    message.author.display_name, None,
                    f"#{message.channel.name}", result
                )

        reply = "\n\n".join(parts) or "✅"
        for chunk in [reply[i:i+1990] for i in range(0, len(reply), 1990)]:
            await message.reply(chunk, mention_author=False)

    async def _post_log(self, guild: discord.Guild, text: str):
        """Send a log entry to the guild's log channel."""
        try:
            config = await self.db.get_config(guild.id)
            ch_id  = config.get("log_channel")
            if not ch_id:
                return
            ch = guild.get_channel(ch_id)
            if not ch:
                return
            e = discord.Embed(
                description=text,
                color=discord.Color.gold(),
                timestamp=discord.utils.utcnow()
            )
            await ch.send(embed=e)
        except Exception as ex:
            log.debug(f"_post_log failed: {ex}")

    async def on_command_error(self, ctx, error):
        if isinstance(error, commands.CommandNotFound):
            return
        log.error(f"Command error: {error}")


# ═══════════════════════════════════════════════════════════════════════════════
# 8 ── SLASH COMMANDS  (7 groups)
# ═══════════════════════════════════════════════════════════════════════════════

bot = TECO()


def is_priv():
    async def p(i: discord.Interaction):
        return (
            i.user.id in OWNER_IDS
            or i.user.guild_permissions.administrator
        )
    return app_commands.check(p)


# ─────────────────────────────────────────────────────────────────────────────
# /setup
# ─────────────────────────────────────────────────────────────────────────────
class Setup(app_commands.Group):
    """Configure TECO for this server."""
    def __init__(self):
        super().__init__(name="setup", description="Configure TECO")

    @app_commands.command(name="train", description="Set TECO's personality and escalation rules")
    @app_commands.describe(instructions="Tell TECO how to act, tone, and escalation order")
    @is_priv()
    async def train(self, i: discord.Interaction, instructions: str):
        await bot.db.set_config(i.guild.id, system_prompt=instructions)
        e = discord.Embed(
            title="🧠 Training saved",
            description=instructions,
            color=discord.Color.green()
        )
        await i.response.send_message(embed=e)

    @app_commands.command(name="rules", description="Manage server rules (action: add/set/clear/list)")
    @app_commands.describe(
        action="add | set | clear | list",
        text="Rule text (for add/set)"
    )
    async def rules(self, i: discord.Interaction, action: str, text: str = ""):
        action = action.lower()
        config = await bot.db.get_config(i.guild.id)
        if action == "list":
            rules = config.get("server_rules") or "No rules set."
            await i.response.send_message(
                embed=discord.Embed(title="📋 Rules", description=rules,
                                    color=discord.Color.blue()), ephemeral=True
            )
        elif action == "add":
            if not text:
                return await i.response.send_message("❌ Provide a rule to add.", ephemeral=True)
            lines = [r.strip() for r in (config.get("server_rules") or "").split("\n") if r.strip()]
            lines.append(f"{len(lines)+1}. {text}")
            await bot.db.set_config(i.guild.id, server_rules="\n".join(lines))
            await i.response.send_message(f"✅ Rule added. Total: **{len(lines)}**")
        elif action == "set":
            if not text:
                return await i.response.send_message("❌ Provide rules text.", ephemeral=True)
            await bot.db.set_config(i.guild.id, server_rules=text)
            await i.response.send_message(f"✅ Rules updated.")
        elif action == "clear":
            await bot.db.set_config(i.guild.id, server_rules="")
            await i.response.send_message("✅ Rules cleared.")
        else:
            await i.response.send_message("❌ Unknown action. Use: add / set / clear / list",
                                          ephemeral=True)

    @app_commands.command(name="logs", description="Set TECO's action log channel")
    @is_priv()
    async def logs(self, i: discord.Interaction, channel: discord.TextChannel):
        await bot.db.set_config(i.guild.id, log_channel=channel.id)
        await i.response.send_message(f"✅ Logs → {channel.mention}")

    @app_commands.command(name="monitor", description="Add or remove a channel from TECO's watch list")
    @app_commands.describe(
        action="add | remove",
        channel="Channel (defaults to current)"
    )
    @is_priv()
    async def monitor(self, i: discord.Interaction, action: str,
                      channel: Optional[discord.TextChannel] = None):
        ch = channel or i.channel
        if action.lower() == "add":
            await bot.db.arr_append(i.guild.id, "monitored_channels", ch.id)
            await i.response.send_message(f"✅ Monitoring {ch.mention}")
        elif action.lower() == "remove":
            await bot.db.arr_remove(i.guild.id, "monitored_channels", ch.id)
            await i.response.send_message(f"✅ Stopped monitoring {ch.mention}")
        else:
            await i.response.send_message("❌ Use: add or remove", ephemeral=True)

    @app_commands.command(name="automod", description="Toggle autonomous TECO actions")
    @is_priv()
    async def automod(self, i: discord.Interaction, enabled: bool):
        await bot.db.set_config(i.guild.id, auto_mod_enabled=enabled)
        await i.response.send_message(
            "✅ TECO is now acting autonomously." if enabled
            else "⏸️ TECO is watching only — no auto-actions."
        )

    @app_commands.command(name="trigger", description="Manage wake words (action: add/remove/list)")
    @app_commands.describe(action="add | remove | list", word="The trigger word")
    @is_priv()
    async def trigger(self, i: discord.Interaction, action: str, word: str = ""):
        action = action.lower()
        if action == "add":
            if not word:
                return await i.response.send_message("❌ Provide a word.", ephemeral=True)
            await bot.db.arr_append(i.guild.id, "trigger_words", word.lower())
            await i.response.send_message(f"✅ Trigger `{word.lower()}` added.")
        elif action == "remove":
            await bot.db.arr_remove(i.guild.id, "trigger_words", word.lower())
            await i.response.send_message(f"✅ Trigger `{word.lower()}` removed.")
        elif action == "list":
            cfg = await bot.db.get_config(i.guild.id)
            trigs = cfg.get("trigger_words") or []
            await i.response.send_message(
                "🔔 Triggers: " + (", ".join(f"`{t}`" for t in trigs) or "None"),
                ephemeral=True
            )
        else:
            await i.response.send_message("❌ Use: add / remove / list", ephemeral=True)

    @app_commands.command(name="trusted", description="Roles that can command TECO (action: add/remove/list)")
    @app_commands.describe(action="add | remove | list", role="The role")
    @is_priv()
    async def trusted(self, i: discord.Interaction, action: str,
                      role: Optional[discord.Role] = None):
        action = action.lower()
        if action == "list":
            cfg  = await bot.db.get_config(i.guild.id)
            ids  = cfg.get("trusted_role_ids") or []
            text = " ".join(f"<@&{r}>" for r in ids) if ids else "None"
            await i.response.send_message(f"🔑 Trusted: {text}", ephemeral=True)
        elif action in ("add", "remove") and role:
            if action == "add":
                await bot.db.arr_append(i.guild.id, "trusted_role_ids", role.id)
                await i.response.send_message(f"✅ {role.mention} can now command TECO.")
            else:
                await bot.db.arr_remove(i.guild.id, "trusted_role_ids", role.id)
                await i.response.send_message(f"✅ {role.mention} removed.")
        else:
            await i.response.send_message("❌ Provide action (add/remove/list) and role.", ephemeral=True)

    @app_commands.command(name="autorole", description="Auto-assign a role when members join (None to disable)")
    @is_priv()
    async def autorole(self, i: discord.Interaction, role: Optional[discord.Role] = None):
        await bot.db.set_config(i.guild.id, autorole_id=role.id if role else None)
        await i.response.send_message(
            f"✅ Autorole set to {role.mention}." if role else "✅ Autorole disabled."
        )

    @app_commands.command(name="vcfollow", description="Toggle: TECO joins VC when you do")
    @is_priv()
    async def vcfollow(self, i: discord.Interaction, enabled: bool):
        await bot.db.set_config(i.guild.id, vc_follow_owner=enabled)
        await i.response.send_message(
            "✅ TECO will follow you into voice channels." if enabled
            else "✅ VC follow disabled."
        )

    @app_commands.command(name="status", description="Show TECO's current config")
    async def status(self, i: discord.Interaction):
        cfg = await bot.db.get_config(i.guild.id)
        monitored = cfg.get("monitored_channels") or []
        triggers  = cfg.get("trigger_words") or []
        trusted   = cfg.get("trusted_role_ids") or []
        e = discord.Embed(title="🤖 TECO Status", color=discord.Color.blurple(),
                          timestamp=discord.utils.utcnow())
        e.add_field(name="Auto-Mod",
                    value="✅ Active" if cfg["auto_mod_enabled"] else "⏸️ Off", inline=True)
        e.add_field(name="Trained",    value="✅" if cfg["system_prompt"] else "❌", inline=True)
        e.add_field(name="VC Follow",  value="✅" if cfg["vc_follow_owner"] else "❌", inline=True)
        e.add_field(name="Monitored",
                    value=" ".join(f"<#{c}>" for c in monitored) or "None", inline=False)
        e.add_field(name="Triggers",
                    value=" ".join(f"`{t}`" for t in triggers) or "None", inline=True)
        e.add_field(name="Trusted",
                    value=" ".join(f"<@&{r}>" for r in trusted) or "None", inline=True)
        await i.response.send_message(embed=e)


bot.tree.add_command(Setup())


# ─────────────────────────────────────────────────────────────────────────────
# /mod
# ─────────────────────────────────────────────────────────────────────────────
class Mod(app_commands.Group):
    def __init__(self):
        super().__init__(name="mod", description="Moderation commands")

    @app_commands.command(name="warn")
    @app_commands.describe(member="Target", reason="Reason")
    @is_priv()
    async def warn(self, i: discord.Interaction, member: discord.Member, reason: str):
        await i.response.defer(ephemeral=True)
        fake = i.message or await i.original_response()
        # Build a minimal Message-like context for the executor
        class _M:
            guild   = i.guild
            channel = i.channel
            author  = member
            delete  = staticmethod(lambda: None)
        result = await bot.exe.run("warn_member",
            {"user_id": str(member.id), "reason": reason}, _M())
        await i.followup.send(result, ephemeral=True)

    @app_commands.command(name="mute")
    @app_commands.describe(member="Target", minutes="Duration in minutes", reason="Reason")
    @is_priv()
    async def mute(self, i: discord.Interaction, member: discord.Member,
                   minutes: int, reason: str):
        await i.response.defer(ephemeral=True)
        class _M:
            guild   = i.guild
            channel = i.channel
            author  = member
        result = await bot.exe.run("timeout_member",
            {"user_id": str(member.id), "duration_minutes": minutes, "reason": reason}, _M())
        await i.followup.send(result, ephemeral=True)

    @app_commands.command(name="unmute")
    @app_commands.describe(member="Target")
    @is_priv()
    async def unmute(self, i: discord.Interaction, member: discord.Member):
        await i.response.defer(ephemeral=True)
        try:
            await member.timeout(None, reason="[TECO] unmute")
            await bot.db.mod_action(i.guild.id, member.id, "UNMUTE", "Manual")
            await bot.db.log_event(i.guild.id, "UNMUTE", i.user.display_name,
                member.display_name, None, "Manual unmute"
            )
            await i.followup.send(f"✅ {member.display_name} unmuted.", ephemeral=True)
        except Exception as e:
            await i.followup.send(f"❌ {e}", ephemeral=True)

    @app_commands.command(name="kick")
    @app_commands.describe(member="Target", reason="Reason")
    @is_priv()
    async def kick(self, i: discord.Interaction, member: discord.Member, reason: str):
        await i.response.defer(ephemeral=True)
        class _M:
            guild   = i.guild
            channel = i.channel
            author  = member
        result = await bot.exe.run("kick_member",
            {"user_id": str(member.id), "reason": reason}, _M())
        await i.followup.send(result, ephemeral=True)

    @app_commands.command(name="ban")
    @app_commands.describe(
        member="Target", reason="Reason",
        hours="Ban duration in hours (0 = permanent)"
    )
    @is_priv()
    async def ban(self, i: discord.Interaction, member: discord.Member,
                  reason: str, hours: int = 0):
        await i.response.defer(ephemeral=True)
        class _M:
            guild   = i.guild
            channel = i.channel
            author  = member
        result = await bot.exe.run("ban_member",
            {"user_id": str(member.id), "reason": reason,
             "duration_hours": hours, "delete_days": 1}, _M())
        await i.followup.send(result, ephemeral=True)

    @app_commands.command(name="unban")
    @app_commands.describe(user_id="Discord user ID to unban")
    @is_priv()
    async def unban(self, i: discord.Interaction, user_id: str):
        await i.response.defer(ephemeral=True)
        try:
            user = await bot.fetch_user(int(user_id))
            await i.guild.unban(user, reason=f"[TECO] {i.user.display_name}")
            await bot.db.log_event(i.guild.id, "UNBAN",
                i.user.display_name, user.display_name, None, "Manual unban"
            )
            await i.followup.send(f"✅ Unbanned {user.display_name}", ephemeral=True)
        except Exception as e:
            await i.followup.send(f"❌ {e}", ephemeral=True)

    @app_commands.command(name="review")
    @app_commands.describe(member="Member to assess")
    @is_priv()
    async def review(self, i: discord.Interaction, member: discord.Member):
        await i.response.defer()
        config  = await bot.db.get_config(i.guild.id)
        history = await bot.db.get_member_history(i.guild.id, member.id)
        msgs    = await bot.db.get_user_messages(i.guild.id, member.id, 15)

        h_text = "\n".join(
            f"- {h['action']}: {h['reason']} ({str(h['created_at'])[:10]})"
            for h in history
        ) or "Clean record"
        m_text = "\n".join(
            f"[{str(m['created_at'])[:16]}] {m['content']}" for m in msgs
        ) or "No messages"

        prompt = (
            f"Risk assessment for {member.display_name} "
            f"(account: {(discord.utils.utcnow()-member.created_at).days}d old)\n\n"
            f"MOD HISTORY:\n{h_text}\n\nRECENT MESSAGES:\n{m_text}\n\n"
            f"Give: 1) Risk (LOW/MEDIUM/HIGH)  2) Pattern  3) Recommendation"
        )
        system = build_system_prompt(config, i.guild.name)
        result = await bot.ai.simple(prompt, system)
        e = discord.Embed(
            title=f"🔍 {member.display_name}",
            description=result, color=discord.Color.orange()
        )
        e.set_thumbnail(url=member.display_avatar.url)
        await i.followup.send(embed=e)

    @app_commands.command(name="history")
    @app_commands.describe(member="Target")
    async def history(self, i: discord.Interaction, member: discord.Member):
        records = await bot.db.get_member_history(i.guild.id, member.id)
        if not records:
            return await i.response.send_message(
                f"✅ {member.mention} has a clean record.", ephemeral=True
            )
        e = discord.Embed(title=f"📋 {member.display_name}", color=discord.Color.red())
        for r in records[:10]:
            e.add_field(
                name=f"{r['action']} — {str(r['created_at'])[:10]}",
                value=r["reason"] or "—", inline=False
            )
        await i.response.send_message(embed=e, ephemeral=True)


bot.tree.add_command(Mod())


# ─────────────────────────────────────────────────────────────────────────────
# /channel
# ─────────────────────────────────────────────────────────────────────────────
class Channel(app_commands.Group):
    def __init__(self):
        super().__init__(name="channel", description="Manage channels")

    def _mk(self, i: discord.Interaction, ch=None):
        class _M:
            guild   = i.guild
            channel = ch or i.channel
            author  = i.user
        return _M()

    @app_commands.command(name="create")
    @app_commands.describe(
        kind="text | voice | category",
        name="Channel name",
        category="Category to place it in (optional)",
        topic="Topic/description (text channels)"
    )
    @is_priv()
    async def create(self, i: discord.Interaction, kind: str, name: str,
                     category: str = "", topic: str = ""):
        await i.response.defer()
        m = self._mk(i)
        r = await bot.exe.run("create_channel",
            {"kind": kind, "name": name, "category": category,
             "topic": topic, "reason": f"Created by {i.user.display_name}"}, m)
        await i.followup.send(r)

    @app_commands.command(name="delete")
    @app_commands.describe(channel_name="Channel to delete")
    @is_priv()
    async def delete(self, i: discord.Interaction, channel_name: str):
        await i.response.defer()
        m = self._mk(i)
        r = await bot.exe.run("delete_channel",
            {"channel_name": channel_name,
             "reason": f"Deleted by {i.user.display_name}"}, m)
        await i.followup.send(r)

    @app_commands.command(name="rename")
    @app_commands.describe(channel="Channel to rename", new_name="New name")
    @is_priv()
    async def rename(self, i: discord.Interaction,
                     channel: discord.abc.GuildChannel, new_name: str):
        await i.response.defer(ephemeral=True)
        m = self._mk(i)
        r = await bot.exe.run("rename_channel",
            {"old_name": channel.name, "new_name": new_name}, m)
        await i.followup.send(r, ephemeral=True)

    @app_commands.command(name="move")
    @app_commands.describe(channel="Channel to move", category="Destination category")
    @is_priv()
    async def move(self, i: discord.Interaction,
                   channel: discord.abc.GuildChannel, category: str):
        await i.response.defer(ephemeral=True)
        m = self._mk(i)
        r = await bot.exe.run("move_channel",
            {"channel_name": channel.name, "category_name": category}, m)
        await i.followup.send(r, ephemeral=True)

    @app_commands.command(name="slowmode")
    @app_commands.describe(
        seconds="Delay in seconds (0 to disable)",
        channel="Channel (defaults to current)"
    )
    @is_priv()
    async def slowmode(self, i: discord.Interaction, seconds: int,
                       channel: Optional[discord.TextChannel] = None):
        await i.response.defer(ephemeral=True)
        ch = channel or i.channel
        m  = self._mk(i, ch)
        r  = await bot.exe.run("set_slowmode",
            {"channel_name": ch.name, "seconds": seconds}, m)
        await i.followup.send(r, ephemeral=True)

    @app_commands.command(name="schedule")
    @app_commands.describe(
        channel="Where to post",
        content="Message content",
        minutes="Repeat every N minutes (0 = one-time only)",
        delay="Delay before first send in minutes (default 0)"
    )
    @is_priv()
    async def schedule(self, i: discord.Interaction,
                       channel: discord.TextChannel,
                       content: str, minutes: int = 0, delay: int = 0):
        await i.response.defer(ephemeral=True)
        send_at = discord.utils.utcnow() + timedelta(minutes=max(0, delay))
        sid = await bot.db.add_schedule(
            i.guild.id, channel.id, content, send_at, minutes
        )
        label = f"every {minutes}min" if minutes else "one-time"
        await i.followup.send(
            f"✅ Scheduled #{sid} in {channel.mention} ({label}): {content[:80]}",
            ephemeral=True
        )

    @app_commands.command(name="schedules")
    async def schedules(self, i: discord.Interaction):
        msgs = await bot.db.get_schedules(i.guild.id)
        if not msgs:
            return await i.response.send_message("No active schedules.", ephemeral=True)
        e = discord.Embed(title="⏰ Scheduled Messages", color=discord.Color.blue())
        for m in msgs[:10]:
            ch   = i.guild.get_channel(m["channel_id"])
            name = ch.mention if ch else f"<#{m['channel_id']}>"
            rep  = f" (every {m['repeat_minutes']}min)" if m["repeat_minutes"] else ""
            e.add_field(
                name=f"#{m['id']}{rep} → {name}",
                value=m["content"][:80], inline=False
            )
        await i.response.send_message(embed=e, ephemeral=True)

    @app_commands.command(name="unschedule")
    @app_commands.describe(schedule_id="ID from /channel schedules")
    @is_priv()
    async def unschedule(self, i: discord.Interaction, schedule_id: int):
        await bot.db.cancel_schedule(schedule_id)
        await i.response.send_message(f"✅ Schedule #{schedule_id} cancelled.", ephemeral=True)


bot.tree.add_command(Channel())


# ─────────────────────────────────────────────────────────────────────────────
# /role
# ─────────────────────────────────────────────────────────────────────────────
class Role(app_commands.Group):
    def __init__(self):
        super().__init__(name="role", description="Manage roles")

    def _mk(self, i: discord.Interaction):
        class _M:
            guild   = i.guild
            channel = i.channel
            author  = i.user
        return _M()

    @app_commands.command(name="give")
    @is_priv()
    async def give(self, i: discord.Interaction,
                   member: discord.Member, role: discord.Role, reason: str = "Manual"):
        await i.response.defer(ephemeral=True)
        m = self._mk(i)
        r = await bot.exe.run("give_role",
            {"user_id": str(member.id), "role_name": role.name, "reason": reason}, m)
        await i.followup.send(r, ephemeral=True)

    @app_commands.command(name="remove")
    @is_priv()
    async def remove(self, i: discord.Interaction,
                     member: discord.Member, role: discord.Role, reason: str = "Manual"):
        await i.response.defer(ephemeral=True)
        m = self._mk(i)
        r = await bot.exe.run("remove_role",
            {"user_id": str(member.id), "role_name": role.name, "reason": reason}, m)
        await i.followup.send(r, ephemeral=True)

    @app_commands.command(name="create")
    @is_priv()
    async def create(self, i: discord.Interaction, name: str,
                     color_hex: str = ""):
        await i.response.defer(ephemeral=True)
        m = self._mk(i)
        r = await bot.exe.run("create_role",
            {"role_name": name, "color_hex": color_hex,
             "reason": f"Created by {i.user.display_name}"}, m)
        await i.followup.send(r, ephemeral=True)

    @app_commands.command(name="rename")
    @is_priv()
    async def rename(self, i: discord.Interaction, role: discord.Role, new_name: str):
        await i.response.defer(ephemeral=True)
        m = self._mk(i)
        r = await bot.exe.run("rename_role",
            {"old_name": role.name, "new_name": new_name}, m)
        await i.followup.send(r, ephemeral=True)

    @app_commands.command(name="auto", description="Set role auto-assigned to new members")
    @is_priv()
    async def auto(self, i: discord.Interaction, role: Optional[discord.Role] = None):
        await bot.db.set_config(i.guild.id, autorole_id=role.id if role else None)
        await i.response.send_message(
            f"✅ Autorole → {role.mention}" if role else "✅ Autorole disabled.",
            ephemeral=True
        )


bot.tree.add_command(Role())


# ─────────────────────────────────────────────────────────────────────────────
# /order
# ─────────────────────────────────────────────────────────────────────────────
class Order(app_commands.Group):
    def __init__(self):
        super().__init__(name="order", description="Saved AI commands")

    @app_commands.command(name="add", description="Save a custom order by name")
    @app_commands.describe(
        name="Short name to call it by",
        description="What TECO should do when this order is run"
    )
    @is_priv()
    async def add(self, i: discord.Interaction, name: str, description: str):
        await bot.db.add_order(i.guild.id, name, description)
        await i.response.send_message(
            f"✅ Order `{name}` saved: {description[:80]}"
        )

    @app_commands.command(name="run", description="Execute a saved order")
    @is_priv()
    async def run(self, i: discord.Interaction, name: str):
        order = await bot.db.get_order(i.guild.id, name)
        if not order:
            return await i.response.send_message(f"❌ No order named `{name}`.",
                                                  ephemeral=True)
        await i.response.defer()
        config = await bot.db.get_config(i.guild.id)

        class _M:
            guild   = i.guild
            channel = i.channel
            author  = i.user

        snapshot = f"Order \"{name}\" triggered by {i.user.display_name}"
        txt, tool, args = await bot.ai.owner_chat(
            order["description"], config, i.guild, snapshot, []
        )
        parts = []
        if txt:
            parts.append(txt)
        if tool and tool != "no_action":
            result = await bot.exe.run(tool, args or {}, _M())
            parts.append(f"⚡ {result}")
            await bot.db.log_event(i.guild.id, "ORDER", i.user.display_name,
                None, None, f"Order '{name}': {result}"
            )
        await i.followup.send("\n".join(parts) or "✅ Done.")

    @app_commands.command(name="list")
    async def list_orders(self, i: discord.Interaction):
        orders = await bot.db.list_orders(i.guild.id)
        if not orders:
            return await i.response.send_message("No orders saved yet.", ephemeral=True)
        e = discord.Embed(title="📋 Orders", color=discord.Color.blurple())
        for o in orders:
            e.add_field(name=f"`{o['name']}`", value=o["description"][:80], inline=False)
        await i.response.send_message(embed=e, ephemeral=True)

    @app_commands.command(name="delete")
    @is_priv()
    async def delete_order(self, i: discord.Interaction, name: str):
        await bot.db.delete_order(i.guild.id, name)
        await i.response.send_message(f"✅ Order `{name}` deleted.", ephemeral=True)


bot.tree.add_command(Order())


# ─────────────────────────────────────────────────────────────────────────────
# /coin
# ─────────────────────────────────────────────────────────────────────────────
class Coin(app_commands.Group):
    def __init__(self):
        super().__init__(name="coin", description="Crypto tracking and alerts")

    async def _resolve(self, i: discord.Interaction, coin: str) -> dict | None:
        await i.response.defer()
        result = await bot.crypto.search(coin)
        if not result:
            await i.followup.send(f"❌ Couldn't find `{coin}` on CoinGecko.",
                                   ephemeral=True)
        return result

    @app_commands.command(name="price", description="Get current price of any coin/token")
    @app_commands.describe(coin="Name or symbol (e.g. bitcoin, doge, pepe, shib)")
    async def price(self, i: discord.Interaction, coin: str):
        info = await self._resolve(i, coin)
        if not info:
            return
        p = await bot.crypto.price(info["id"])
        e = discord.Embed(
            title=f"{info['name']} ({info['symbol']})",
            description=f"**{bot.crypto.fmt_price(p)}**",
            color=discord.Color.gold()
        )
        e.set_footer(text="CoinGecko • prices may be delayed ~1-2 min")
        await i.followup.send(embed=e)

    @app_commands.command(name="alert", description="Get pinged when a coin crosses a price")
    @app_commands.describe(
        coin="Coin name or symbol",
        direction="above | below",
        price="Price threshold in USD",
        channel="Where to post the alert (defaults to current)"
    )
    async def alert(self, i: discord.Interaction, coin: str, direction: str,
                    price: float, channel: Optional[discord.TextChannel] = None):
        info = await self._resolve(i, coin)
        if not info:
            return
        if direction.lower() not in ("above", "below"):
            return await i.followup.send("❌ Use `above` or `below`.", ephemeral=True)
        ch = channel or i.channel
        await bot.db.add_coin_alert(
            i.guild.id, ch.id, i.user.id,
            info["id"], info["name"], info["symbol"],
            direction.lower(), price
        )
        await i.followup.send(
            f"✅ Alert set: **{info['name']}** {direction} "
            f"**{bot.crypto.fmt_price(price)}** → {ch.mention}"
        )

    @app_commands.command(name="track", description="Show live price in a voice channel name (updates every 7 min)")
    @app_commands.describe(
        coin="Coin name or symbol",
        voice_channel="Voice channel to rename"
    )
    @is_priv()
    async def track(self, i: discord.Interaction, coin: str,
                    voice_channel: discord.VoiceChannel):
        info = await self._resolve(i, coin)
        if not info:
            return
        await bot.db.set_stat_channel(
            i.guild.id, voice_channel.id,
            info["id"], info["name"], info["symbol"]
        )
        await i.followup.send(
            f"✅ {voice_channel.mention} will show **{info['name']}** price "
            f"every 7 minutes."
        )

    @app_commands.command(name="alerts", description="List all active price alerts")
    async def alerts(self, i: discord.Interaction):
        await i.response.defer(ephemeral=True)
        items = await bot.db.get_guild_alerts(i.guild.id)
        if not items:
            return await i.followup.send("No active alerts.", ephemeral=True)
        e = discord.Embed(title="🔔 Active Alerts", color=discord.Color.gold())
        for a in items:
            e.add_field(
                name=f"{a['coin_name']} ({a['coin_symbol']})",
                value=f"{a['direction']} **{bot.crypto.fmt_price(a['price'])}** "
                      f"→ <#{a['channel_id']}>",
                inline=False
            )
        await i.followup.send(embed=e, ephemeral=True)

    @app_commands.command(name="stop", description="Remove all alerts/tracking for a coin")
    @app_commands.describe(coin="Coin name or symbol")
    @is_priv()
    async def stop(self, i: discord.Interaction, coin: str):
        info = await self._resolve(i, coin)
        if not info:
            return
        await bot.db.remove_coin_alerts(i.guild.id, info["id"])
        await bot.db.remove_stat_channel(i.guild.id, info["id"])
        await i.followup.send(
            f"✅ Stopped all tracking for **{info['name']}**.", ephemeral=True
        )


bot.tree.add_command(Coin())


# ─────────────────────────────────────────────────────────────────────────────
# /emoji
# ─────────────────────────────────────────────────────────────────────────────
class Emoji(app_commands.Group):
    def __init__(self):
        super().__init__(name="emoji", description="Manage server emojis")

    @app_commands.command(name="add", description="Add a custom emoji from a URL")
    @app_commands.describe(name="Emoji name", url="Image URL (PNG/GIF)")
    @is_priv()
    async def add(self, i: discord.Interaction, name: str, url: str):
        await i.response.defer(ephemeral=True)
        class _M:
            guild   = i.guild
            channel = i.channel
            author  = i.user
        r = await bot.exe.run("add_emoji",
            {"name": name, "url": url, "reason": f"Added by {i.user.display_name}"},
            _M())
        await i.followup.send(r, ephemeral=True)

    @app_commands.command(name="remove", description="Remove a custom emoji by name")
    @is_priv()
    async def remove(self, i: discord.Interaction, name: str):
        await i.response.defer(ephemeral=True)
        emoji = discord.utils.get(i.guild.emojis, name=name)
        if not emoji:
            return await i.followup.send(f"❌ Emoji `:{name}:` not found.", ephemeral=True)
        await emoji.delete(reason=f"[TECO] {i.user.display_name}")
        await bot.db.log_event(i.guild.id, "EMOJI_REMOVE", i.user.display_name,
            None, None, f"Removed :{name}:"
        )
        await i.followup.send(f"✅ Removed `:{name}:`", ephemeral=True)

    @app_commands.command(name="list", description="Show all custom emojis")
    async def list_emojis(self, i: discord.Interaction):
        emojis = i.guild.emojis
        if not emojis:
            return await i.response.send_message("No custom emojis.", ephemeral=True)
        e = discord.Embed(
            title=f"😀 Emojis ({len(emojis)}/{i.guild.emoji_limit})",
            description=" ".join(str(em) for em in emojis[:50]),
            color=discord.Color.blurple()
        )
        await i.response.send_message(embed=e, ephemeral=True)


bot.tree.add_command(Emoji())


# ─────────────────────────────────────────────────────────────────────────────
# !ai prefix  (owner direct chat, no slash needed)
# ─────────────────────────────────────────────────────────────────────────────
@bot.command(name="ai")
async def ai_cmd(ctx: commands.Context, *, query: str = ""):
    if (ctx.author.id not in OWNER_IDS and
            not ctx.author.guild_permissions.administrator):
        return
    if not query:
        return await ctx.send("Usage: `!ai [anything]`")
    if query.strip().lower() == "clear":
        bot._hist[ctx.author.id].clear()
        return await ctx.send("🧹 Conversation cleared.")

    config = await bot.db.get_config(ctx.guild.id)
    await bot._privileged_chat(ctx.message, query, config)


# ═══════════════════════════════════════════════════════════════════════════════
# 9 ── KEEP-ALIVE WEB SERVER
# ═══════════════════════════════════════════════════════════════════════════════

async def run_webserver():
    async def health(req):
        return web.json_response({
            "status": "ok",
            "bot":    str(bot.user) if bot.is_ready() else "starting",
            "guilds": len(bot.guilds) if bot.is_ready() else 0
        })
    app    = web.Application()
    app.router.add_get("/",       health)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    log.info(f"✅ Keep-alive on port {PORT}")


# ═══════════════════════════════════════════════════════════════════════════════
# 10 ── MAIN
# ═══════════════════════════════════════════════════════════════════════════════

async def main():
    for var in ("DISCORD_TOKEN", "GEMINI_API_KEY", "DATABASE_URL"):
        if not os.getenv(var):
            raise RuntimeError(f"❌ {var} is not set")
    async with bot:
        await asyncio.gather(run_webserver(), bot.start(DISCORD_TOKEN))


if __name__ == "__main__":
    asyncio.run(main())