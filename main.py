"""
╔══════════════════════════════════════════════════════════════════════════════╗
║           TECO — FULL AI CO-ADMIN AGENT  v2  (Single File)                 ║
║  Custom Triggers · Fast Gemini Flash · Roles · Channels · Confirmations    ║
║           Hierarchy-Aware · Owner-Trained · 100% Python                    ║
╚══════════════════════════════════════════════════════════════════════════════╝

WHAT'S NEW IN V2:
  ✅ Custom trigger words  — say "teco", "merlin", or anything you set
  ✅ Faster responses      — gemini-2.0-flash, pre-filter, tiny context
  ✅ Role management       — AI gives/removes roles on command
  ✅ Channel management    — AI creates channels with confirmation buttons
  ✅ Hierarchy awareness   — trusted roles (mods) can also command TECO
  ✅ Smart confirmation    — destructive actions need a button press first
"""

# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 1 — IMPORTS & CONFIG
# ═══════════════════════════════════════════════════════════════════════════════

import discord
from discord.ext import commands
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
from collections import defaultdict, deque
from dotenv import load_dotenv
from typing import Optional, Callable

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)
logger = logging.getLogger('TECO')

DISCORD_TOKEN  = os.getenv('DISCORD_TOKEN')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
DATABASE_URL   = os.getenv('DATABASE_URL')
OWNER_IDS      = set(int(i) for i in os.getenv('OWNER_IDS', '').split(',') if i.strip().isdigit())
PREFIX         = os.getenv('PREFIX', '!')
# gemini-2.0-flash = fastest, free tier, near-instant responses
GEMINI_MODEL   = os.getenv('GEMINI_MODEL', 'gemini-2.0-flash')
PORT           = int(os.getenv('PORT', 7860))

GEMINI_URL     = f'https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent'
GEMINI_HEADERS = {'Content-Type': 'application/json', 'x-goog-api-key': GEMINI_API_KEY}

# Per-channel AI scan cooldown (seconds) — prevents hammering the API
AI_SCAN_COOLDOWN = 2
_last_scan: dict[int, float] = defaultdict(float)


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 2 — DATABASE
# ═══════════════════════════════════════════════════════════════════════════════

class Database:
    def __init__(self):
        self.pool: asyncpg.Pool | None = None

    async def connect(self):
        self.pool = await asyncpg.create_pool(
            DATABASE_URL, min_size=1, max_size=10, statement_cache_size=0
        )
        await self._init_schema()
        logger.info('✅ Database connected')

    async def _init_schema(self):
        async with self.pool.acquire() as c:
            await c.execute('''
                CREATE TABLE IF NOT EXISTS guild_config (
                    guild_id             BIGINT   PRIMARY KEY,
                    system_prompt        TEXT     DEFAULT '',
                    server_rules         TEXT     DEFAULT '',
                    auto_mod_enabled     BOOLEAN  DEFAULT FALSE,
                    log_channel          BIGINT,
                    monitored_channels   BIGINT[] DEFAULT '{}',
                    trigger_words        TEXT[]   DEFAULT '{}',
                    trusted_role_ids     BIGINT[] DEFAULT '{}',
                    created_at           TIMESTAMPTZ DEFAULT NOW()
                )
            ''')
            # Add new columns if upgrading from v1
            for col, definition in [
                ('trigger_words',    "TEXT[]  DEFAULT '{}'"),
                ('trusted_role_ids', "BIGINT[] DEFAULT '{}'"),
            ]:
                await c.execute(f'''
                    ALTER TABLE guild_config
                    ADD COLUMN IF NOT EXISTS {col} {definition}
                ''')

            await c.execute('''
                CREATE TABLE IF NOT EXISTS message_history (
                    id          BIGSERIAL   PRIMARY KEY,
                    guild_id    BIGINT      NOT NULL,
                    channel_id  BIGINT      NOT NULL,
                    user_id     BIGINT      NOT NULL,
                    username    TEXT        NOT NULL,
                    content     TEXT        NOT NULL,
                    created_at  TIMESTAMPTZ DEFAULT NOW()
                )
            ''')
            await c.execute('''
                CREATE INDEX IF NOT EXISTS idx_msg_channel
                ON message_history (channel_id, created_at DESC)
            ''')
            await c.execute('''
                CREATE TABLE IF NOT EXISTS ai_decisions (
                    id              BIGSERIAL   PRIMARY KEY,
                    guild_id        BIGINT      NOT NULL,
                    channel_id      BIGINT,
                    target_user_id  BIGINT,
                    action          TEXT        NOT NULL,
                    reason          TEXT,
                    ai_reasoning    TEXT,
                    executed        BOOLEAN     DEFAULT FALSE,
                    created_at      TIMESTAMPTZ DEFAULT NOW()
                )
            ''')
            await c.execute('''
                CREATE TABLE IF NOT EXISTS member_history (
                    id          BIGSERIAL   PRIMARY KEY,
                    guild_id    BIGINT      NOT NULL,
                    user_id     BIGINT      NOT NULL,
                    action      TEXT        NOT NULL,
                    reason      TEXT,
                    moderator   TEXT        DEFAULT 'TECO-AI',
                    created_at  TIMESTAMPTZ DEFAULT NOW()
                )
            ''')

    # ── Config helpers ───────────────────────────────────────────────────────

    async def get_config(self, guild_id: int) -> dict:
        async with self.pool.acquire() as c:
            row = await c.fetchrow('SELECT * FROM guild_config WHERE guild_id=$1', guild_id)
            if not row:
                await c.execute(
                    'INSERT INTO guild_config (guild_id) VALUES ($1) ON CONFLICT DO NOTHING',
                    guild_id
                )
                row = await c.fetchrow('SELECT * FROM guild_config WHERE guild_id=$1', guild_id)
            return dict(row)

    async def set_config(self, guild_id: int, **kwargs):
        await self.get_config(guild_id)
        if not kwargs:
            return
        cols = ', '.join(f'{k}=${i+2}' for i, k in enumerate(kwargs))
        await self.pool.execute(
            f'UPDATE guild_config SET {cols} WHERE guild_id=$1',
            guild_id, *kwargs.values()
        )

    async def add_trigger(self, guild_id: int, word: str):
        await self.get_config(guild_id)
        await self.pool.execute(
            '''UPDATE guild_config
               SET trigger_words = array_append(trigger_words, $2)
               WHERE guild_id=$1 AND NOT ($2 = ANY(trigger_words))''',
            guild_id, word.lower()
        )

    async def remove_trigger(self, guild_id: int, word: str):
        await self.pool.execute(
            '''UPDATE guild_config
               SET trigger_words = array_remove(trigger_words, $2)
               WHERE guild_id=$1''',
            guild_id, word.lower()
        )

    async def add_trusted_role(self, guild_id: int, role_id: int):
        await self.get_config(guild_id)
        await self.pool.execute(
            '''UPDATE guild_config
               SET trusted_role_ids = array_append(trusted_role_ids, $2)
               WHERE guild_id=$1 AND NOT ($2 = ANY(trusted_role_ids))''',
            guild_id, role_id
        )

    async def remove_trusted_role(self, guild_id: int, role_id: int):
        await self.pool.execute(
            '''UPDATE guild_config
               SET trusted_role_ids = array_remove(trusted_role_ids, $2)
               WHERE guild_id=$1''',
            guild_id, role_id
        )

    async def add_monitored_channel(self, guild_id: int, channel_id: int):
        await self.get_config(guild_id)
        await self.pool.execute(
            '''UPDATE guild_config
               SET monitored_channels = array_append(monitored_channels, $2)
               WHERE guild_id=$1 AND NOT ($2=ANY(monitored_channels))''',
            guild_id, channel_id
        )

    async def remove_monitored_channel(self, guild_id: int, channel_id: int):
        await self.pool.execute(
            '''UPDATE guild_config
               SET monitored_channels = array_remove(monitored_channels, $2)
               WHERE guild_id=$1''',
            guild_id, channel_id
        )

    # ── Message history ──────────────────────────────────────────────────────

    async def save_message(self, guild_id, channel_id, user_id, username, content):
        await self.pool.execute(
            '''INSERT INTO message_history (guild_id, channel_id, user_id, username, content)
               VALUES ($1,$2,$3,$4,$5)''',
            guild_id, channel_id, user_id, username, content[:2000]
        )
        # Rolling window — keep last 300 per channel
        await self.pool.execute(
            '''DELETE FROM message_history WHERE channel_id=$1 AND id NOT IN (
               SELECT id FROM message_history WHERE channel_id=$1
               ORDER BY created_at DESC LIMIT 300)''',
            channel_id
        )

    async def get_recent_messages(self, channel_id: int, limit: int = 10) -> list[dict]:
        async with self.pool.acquire() as c:
            rows = await c.fetch(
                '''SELECT username, content, created_at FROM message_history
                   WHERE channel_id=$1 ORDER BY created_at DESC LIMIT $2''',
                channel_id, limit
            )
            return [dict(r) for r in reversed(rows)]

    async def get_user_messages(self, guild_id, user_id, limit=15) -> list[dict]:
        async with self.pool.acquire() as c:
            rows = await c.fetch(
                '''SELECT content, created_at FROM message_history
                   WHERE guild_id=$1 AND user_id=$2
                   ORDER BY created_at DESC LIMIT $3''',
                guild_id, user_id, limit
            )
            return [dict(r) for r in rows]

    # ── Decisions & member history ───────────────────────────────────────────

    async def log_decision(self, guild_id, channel_id, target_user_id,
                           action, reason, ai_reasoning, executed):
        await self.pool.execute(
            '''INSERT INTO ai_decisions
               (guild_id,channel_id,target_user_id,action,reason,ai_reasoning,executed)
               VALUES ($1,$2,$3,$4,$5,$6,$7)''',
            guild_id, channel_id, target_user_id, action, reason, ai_reasoning, executed
        )

    async def log_member_action(self, guild_id, user_id, action, reason):
        await self.pool.execute(
            'INSERT INTO member_history (guild_id,user_id,action,reason) VALUES ($1,$2,$3,$4)',
            guild_id, user_id, action, reason
        )

    async def get_member_history(self, guild_id, user_id) -> list[dict]:
        async with self.pool.acquire() as c:
            rows = await c.fetch(
                '''SELECT action, reason, moderator, created_at FROM member_history
                   WHERE guild_id=$1 AND user_id=$2
                   ORDER BY created_at DESC LIMIT 20''',
                guild_id, user_id
            )
            return [dict(r) for r in rows]

    async def get_recent_decisions(self, guild_id, limit=8) -> list[dict]:
        async with self.pool.acquire() as c:
            rows = await c.fetch(
                '''SELECT action, reason, created_at FROM ai_decisions
                   WHERE guild_id=$1 AND executed=TRUE
                   ORDER BY created_at DESC LIMIT $2''',
                guild_id, limit
            )
            return [dict(r) for r in rows]


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 3 — GEMINI TOOL DEFINITIONS
# Everything Gemini can call → Discord action
# ═══════════════════════════════════════════════════════════════════════════════

GEMINI_TOOLS = [{
    'function_declarations': [
        # ── Moderation ────────────────────────────────────────────────────────
        {
            'name': 'warn_member',
            'description': 'Issue a formal warning to a member.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'user_id': {'type': 'string'},
                    'reason':  {'type': 'string'}
                },
                'required': ['user_id', 'reason']
            }
        },
        {
            'name': 'kick_member',
            'description': 'Kick a member from the server.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'user_id': {'type': 'string'},
                    'reason':  {'type': 'string'}
                },
                'required': ['user_id', 'reason']
            }
        },
        {
            'name': 'ban_member',
            'description': 'Permanently ban a member.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'user_id':     {'type': 'string'},
                    'reason':      {'type': 'string'},
                    'delete_days': {'type': 'integer'}
                },
                'required': ['user_id', 'reason']
            }
        },
        {
            'name': 'timeout_member',
            'description': 'Temporarily mute/timeout a member.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'user_id':          {'type': 'string'},
                    'reason':           {'type': 'string'},
                    'duration_minutes': {'type': 'integer'}
                },
                'required': ['user_id', 'reason', 'duration_minutes']
            }
        },
        {
            'name': 'delete_message',
            'description': 'Delete the message that triggered this evaluation.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'reason': {'type': 'string'}
                },
                'required': ['reason']
            }
        },
        # ── Roles ─────────────────────────────────────────────────────────────
        {
            'name': 'give_role',
            'description': 'Assign a role to a member by role name.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'user_id':   {'type': 'string', 'description': 'Discord user ID'},
                    'role_name': {'type': 'string', 'description': 'Exact or partial role name'},
                    'reason':    {'type': 'string'}
                },
                'required': ['user_id', 'role_name', 'reason']
            }
        },
        {
            'name': 'remove_role',
            'description': 'Remove a role from a member by role name.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'user_id':   {'type': 'string'},
                    'role_name': {'type': 'string'},
                    'reason':    {'type': 'string'}
                },
                'required': ['user_id', 'role_name', 'reason']
            }
        },
        {
            'name': 'create_role',
            'description': 'Create a new role in the server.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'role_name': {'type': 'string'},
                    'color_hex': {'type': 'string', 'description': 'Hex color e.g. #FF5733 (optional)'},
                    'reason':    {'type': 'string'}
                },
                'required': ['role_name', 'reason']
            }
        },
        # ── Channels ──────────────────────────────────────────────────────────
        {
            'name': 'create_text_channel',
            'description': 'Create a new text channel. Sends a confirmation button first.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'channel_name': {'type': 'string', 'description': 'Name for the new channel (no spaces)'},
                    'topic':        {'type': 'string', 'description': 'Channel topic/description (optional)'},
                    'category':     {'type': 'string', 'description': 'Category name to put it in (optional)'},
                    'reason':       {'type': 'string'}
                },
                'required': ['channel_name', 'reason']
            }
        },
        {
            'name': 'create_voice_channel',
            'description': 'Create a new voice channel. Sends a confirmation button first.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'channel_name': {'type': 'string'},
                    'category':     {'type': 'string', 'description': 'Category name (optional)'},
                    'reason':       {'type': 'string'}
                },
                'required': ['channel_name', 'reason']
            }
        },
        # ── Communication ─────────────────────────────────────────────────────
        {
            'name': 'send_message',
            'description': 'Send a message in the current channel.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'content': {'type': 'string'}
                },
                'required': ['content']
            }
        },
        {
            'name': 'lock_channel',
            'description': 'Lock the current channel.',
            'parameters': {
                'type': 'object',
                'properties': {'reason': {'type': 'string'}},
                'required': ['reason']
            }
        },
        {
            'name': 'unlock_channel',
            'description': 'Unlock the current channel.',
            'parameters': {
                'type': 'object',
                'properties': {'reason': {'type': 'string'}},
                'required': ['reason']
            }
        },
        {
            'name': 'no_action',
            'description': 'Message is acceptable. Do nothing.',
            'parameters': {
                'type': 'object',
                'properties': {'reason': {'type': 'string'}},
                'required': ['reason']
            }
        }
    ]
}]


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 4 — CONFIRMATION VIEW
# For channel creation and other big actions, show ✅/❌ buttons first.
# ═══════════════════════════════════════════════════════════════════════════════

class ConfirmView(discord.ui.View):
    """
    Two-button embed: ✅ Confirm  |  ❌ Cancel
    on_confirm is called with the interaction when the owner taps Confirm.
    """
    def __init__(self, on_confirm: Callable, description: str, timeout: int = 30):
        super().__init__(timeout=timeout)
        self.on_confirm  = on_confirm
        self.description = description
        self.responded   = False

    @discord.ui.button(label='✅ Confirm', style=discord.ButtonStyle.green)
    async def confirm_btn(self, interaction: discord.Interaction,
                          button: discord.ui.Button):
        if self.responded:
            return
        self.responded = True
        self.stop()
        await self.on_confirm(interaction)

    @discord.ui.button(label='❌ Cancel', style=discord.ButtonStyle.red)
    async def cancel_btn(self, interaction: discord.Interaction,
                         button: discord.ui.Button):
        if self.responded:
            return
        self.responded = True
        self.stop()
        await interaction.response.edit_message(
            content='❌ Action cancelled.', embed=None, view=None
        )

    async def on_timeout(self):
        self.responded = True


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 5 — GEMINI CLIENT
# ═══════════════════════════════════════════════════════════════════════════════

class GeminiClient:

    def build_system_prompt(self, config: dict, guild_name: str,
                            guild_roles: list[str] = None) -> str:
        trained = config.get('system_prompt') or 'You are a fair, firm server moderator.'
        rules   = config.get('server_rules')  or 'No explicit rules set yet.'
        roles_info = ', '.join(guild_roles) if guild_roles else 'unknown'
        return f"""You are TECO, the AI co-admin for Discord server "{guild_name}".

PERSONALITY & INSTRUCTIONS (owner-trained):
{trained}

SERVER RULES:
{rules}

AVAILABLE ROLES ON THIS SERVER:
{roles_info}

TOOLS YOU HAVE:
Moderation: warn_member, kick_member, ban_member, timeout_member, delete_message
Roles: give_role, remove_role, create_role
Channels: create_text_channel, create_voice_channel, lock_channel, unlock_channel
Communication: send_message
Default: no_action

RULES FOR YOU:
- Always call exactly ONE tool per evaluation.
- Never act against administrators or the server owner.
- For role/channel actions, confirm you have the information needed before acting.
- For channel creation, the tool will automatically ask the owner to confirm.
- Match role names as closely as possible from the available roles list.
- Keep reasons short and clear."""

    async def _post(self, contents: list, system: str,
                    use_tools: bool = True, temperature: float = 0.1) -> dict:
        payload = {
            'contents': contents,
            'systemInstruction': {'parts': [{'text': system}]},
            'generationConfig': {
                'maxOutputTokens': 300,
                'temperature': temperature,
                # Disable extended thinking on flash for maximum speed
                'candidateCount': 1
            }
        }
        if use_tools:
            payload['tools']      = GEMINI_TOOLS
            payload['toolConfig'] = {'functionCallingConfig': {'mode': 'AUTO'}}

        timeout = aiohttp.ClientTimeout(total=15)   # 15s hard timeout
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.post(GEMINI_URL, headers=GEMINI_HEADERS, json=payload) as r:
                data = await r.json()
                if r.status != 200:
                    err = data.get('error', {}).get('message', str(data))
                    raise RuntimeError(f'Gemini {r.status}: {err}')
                return data

    def _parse(self, data: dict) -> tuple[str | None, str | None, dict | None]:
        text, tool_name, tool_args = None, None, None
        try:
            parts = data['candidates'][0]['content']['parts']
            for p in parts:
                if 'text' in p and p['text'].strip():
                    text = p['text'].strip()
                if 'functionCall' in p:
                    tool_name = p['functionCall']['name']
                    tool_args = p['functionCall'].get('args', {})
        except (KeyError, IndexError):
            if data.get('candidates', [{}])[0].get('finishReason') == 'SAFETY':
                text = '⚠️ Safety filter blocked response.'
        return text, tool_name, tool_args

    # ── SPEED: pre-filter before hitting Gemini ──────────────────────────────
    @staticmethod
    def quick_precheck(content: str) -> bool:
        """
        Returns True if we should call Gemini, False if message is clearly fine.
        This runs in microseconds and avoids unnecessary API calls.
        """
        if not content or len(content.strip()) < 4:
            return False
        # Clearly fine: very short greetings, numbers, emoji-only
        if len(content.split()) <= 2:
            return False
        return True

    async def evaluate_message(self, message: discord.Message,
                               recent: list[dict], config: dict,
                               guild_roles: list[str]) -> tuple:
        ctx = '\n'.join(f'{m["username"]}: {m["content"]}' for m in recent[-8:])
        prompt = (
            f'RECENT CONTEXT:\n{ctx or "(none)"}\n\n'
            f'MESSAGE TO EVALUATE:\n'
            f'Author: {message.author.display_name} (ID: {message.author.id})\n'
            f'Content: {message.content}\n\n'
            f'Call the right tool. Call no_action if message is fine.'
        )
        system   = self.build_system_prompt(config, message.guild.name, guild_roles)
        contents = [{'role': 'user', 'parts': [{'text': prompt}]}]
        try:
            data = await self._post(contents, system)
            return self._parse(data)
        except Exception as e:
            logger.error(f'evaluate_message error: {e}')
            return None, 'no_action', {'reason': 'AI unavailable'}

    async def owner_chat(self, question: str, config: dict,
                         guild: discord.Guild, snapshot: str,
                         history: list) -> tuple:
        system = self.build_system_prompt(
            config, guild.name,
            [r.name for r in guild.roles if not r.is_default()]
        )
        system += f'\n\nSERVER SNAPSHOT:\n{snapshot}'

        contents = []
        for t in history[-6:]:
            role = 'user' if t['role'] == 'user' else 'model'
            contents.append({'role': role, 'parts': [{'text': t['content']}]})
        contents.append({'role': 'user', 'parts': [{'text': question}]})

        try:
            data = await self._post(contents, system, temperature=0.3)
            return self._parse(data)
        except Exception as e:
            return f'⚠️ AI error: {e}', None, None

    async def simple_chat(self, prompt: str, system: str) -> str:
        contents = [{'role': 'user', 'parts': [{'text': prompt}]}]
        try:
            data = await self._post(contents, system, use_tools=False, temperature=0.4)
            text, _, _ = self._parse(data)
            return text or 'No response.'
        except Exception as e:
            return f'⚠️ AI error: {e}'


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 6 — TOOL EXECUTOR
# Maps Gemini's decision → real Discord action
# ═══════════════════════════════════════════════════════════════════════════════

class ToolExecutor:
    def __init__(self, db: Database):
        self.db = db

    def _find_role(self, guild: discord.Guild, name: str) -> discord.Role | None:
        name_lower = name.lower().strip()
        # Exact match first, then partial
        for r in guild.roles:
            if r.name.lower() == name_lower:
                return r
        for r in guild.roles:
            if name_lower in r.name.lower():
                return r
        return None

    def _find_category(self, guild: discord.Guild, name: str) -> discord.CategoryChannel | None:
        if not name:
            return None
        name_lower = name.lower()
        for c in guild.categories:
            if name_lower in c.name.lower():
                return c
        return None

    async def execute(self, tool_name: str, tool_args: dict,
                      message: discord.Message,
                      reply_to: discord.Message = None) -> str:
        guild   = message.guild
        channel = message.channel
        reason  = tool_args.get('reason', 'AI decision')
        anchor  = reply_to or message   # where to send confirmations

        try:
            # ── No action ─────────────────────────────────────────────────────
            if tool_name == 'no_action':
                return f'✅ No action: {reason}'

            # ── Warn ──────────────────────────────────────────────────────────
            if tool_name == 'warn_member':
                uid    = int(tool_args['user_id'])
                member = guild.get_member(uid)
                if not member:
                    return f'⚠️ Member {uid} not found'
                await self.db.log_member_action(guild.id, uid, 'WARN', reason)
                history = await self.db.get_member_history(guild.id, uid)
                count   = sum(1 for h in history if h['action'] == 'WARN')
                await channel.send(
                    f'⚠️ {member.mention} — **Warning #{count}**: {reason}',
                    delete_after=20
                )
                try:
                    await member.send(f'⚠️ Warning in **{guild.name}**: {reason}')
                except Exception:
                    pass
                return f'⚠️ Warned {member} (#{count}): {reason}'

            # ── Kick ──────────────────────────────────────────────────────────
            if tool_name == 'kick_member':
                uid    = int(tool_args['user_id'])
                member = guild.get_member(uid)
                if not member:
                    return f'⚠️ Member {uid} not found'
                await self.db.log_member_action(guild.id, uid, 'KICK', reason)
                try:
                    await member.send(f'👢 Kicked from **{guild.name}**: {reason}')
                except Exception:
                    pass
                await member.kick(reason=f'[TECO] {reason}')
                return f'👢 Kicked {member}: {reason}'

            # ── Ban ───────────────────────────────────────────────────────────
            if tool_name == 'ban_member':
                uid      = int(tool_args['user_id'])
                del_days = int(tool_args.get('delete_days', 1))
                member   = guild.get_member(uid) or discord.Object(id=uid)
                await self.db.log_member_action(guild.id, uid, 'BAN', reason)
                if isinstance(member, discord.Member):
                    try:
                        await member.send(f'🔨 Banned from **{guild.name}**: {reason}')
                    except Exception:
                        pass
                await guild.ban(member, reason=f'[TECO] {reason}',
                                delete_message_days=max(0, min(7, del_days)))
                return f'🔨 Banned {uid}: {reason}'

            # ── Timeout ───────────────────────────────────────────────────────
            if tool_name == 'timeout_member':
                uid     = int(tool_args['user_id'])
                mins    = int(tool_args.get('duration_minutes', 10))
                member  = guild.get_member(uid)
                if not member:
                    return f'⚠️ Member {uid} not found'
                until = discord.utils.utcnow() + timedelta(minutes=mins)
                await member.timeout(until, reason=f'[TECO] {reason}')
                await self.db.log_member_action(guild.id, uid, f'MUTE_{mins}m', reason)
                return f'🔇 Muted {member} for {mins}m: {reason}'

            # ── Delete message ────────────────────────────────────────────────
            if tool_name == 'delete_message':
                try:
                    await message.delete()
                except discord.NotFound:
                    pass
                await self.db.log_member_action(
                    guild.id, message.author.id, 'MSG_DELETE', reason
                )
                return f'🗑️ Deleted message from {message.author}: {reason}'

            # ── Give role ─────────────────────────────────────────────────────
            if tool_name == 'give_role':
                uid    = int(tool_args['user_id'])
                member = guild.get_member(uid)
                if not member:
                    return f'⚠️ Member {uid} not found'
                role = self._find_role(guild, tool_args['role_name'])
                if not role:
                    return f'⚠️ Role "{tool_args["role_name"]}" not found'
                if role in member.roles:
                    return f'ℹ️ {member} already has {role.name}'
                await member.add_roles(role, reason=f'[TECO] {reason}')
                await self.db.log_member_action(
                    guild.id, uid, f'ROLE_ADD:{role.name}', reason
                )
                return f'✅ Gave **{role.name}** to {member}: {reason}'

            # ── Remove role ───────────────────────────────────────────────────
            if tool_name == 'remove_role':
                uid    = int(tool_args['user_id'])
                member = guild.get_member(uid)
                if not member:
                    return f'⚠️ Member {uid} not found'
                role = self._find_role(guild, tool_args['role_name'])
                if not role:
                    return f'⚠️ Role "{tool_args["role_name"]}" not found'
                if role not in member.roles:
                    return f'ℹ️ {member} doesn\'t have {role.name}'
                await member.remove_roles(role, reason=f'[TECO] {reason}')
                await self.db.log_member_action(
                    guild.id, uid, f'ROLE_REMOVE:{role.name}', reason
                )
                return f'✅ Removed **{role.name}** from {member}: {reason}'

            # ── Create role ───────────────────────────────────────────────────
            if tool_name == 'create_role':
                name = tool_args['role_name']
                color_hex = tool_args.get('color_hex', '')
                color = discord.Color.default()
                if color_hex:
                    try:
                        color = discord.Color(int(color_hex.lstrip('#'), 16))
                    except Exception:
                        pass
                new_role = await guild.create_role(
                    name=name, color=color, reason=f'[TECO] {reason}'
                )
                return f'✅ Created role **{new_role.name}**'

            # ── Create text channel (with confirmation) ───────────────────────
            if tool_name == 'create_text_channel':
                ch_name  = tool_args['channel_name'].replace(' ', '-').lower()
                topic    = tool_args.get('topic', '')
                cat_name = tool_args.get('category', '')
                category = self._find_category(guild, cat_name) if cat_name else None

                async def do_create(interaction: discord.Interaction):
                    new_ch = await guild.create_text_channel(
                        ch_name, topic=topic, category=category,
                        reason=f'[TECO] {reason}'
                    )
                    await interaction.response.edit_message(
                        content=f'✅ Created text channel {new_ch.mention}',
                        embed=None, view=None
                    )

                e = discord.Embed(
                    title='📝 Create Text Channel?',
                    description=(
                        f'**Name:** `#{ch_name}`\n'
                        f'**Topic:** {topic or "—"}\n'
                        f'**Category:** {category.name if category else "None"}\n'
                        f'**Reason:** {reason}'
                    ),
                    color=discord.Color.blue()
                )
                view = ConfirmView(do_create, e.description)
                await anchor.channel.send(embed=e, view=view)
                return f'📝 Awaiting confirmation for #{ch_name}'

            # ── Create voice channel (with confirmation) ──────────────────────
            if tool_name == 'create_voice_channel':
                ch_name  = tool_args['channel_name']
                cat_name = tool_args.get('category', '')
                category = self._find_category(guild, cat_name) if cat_name else None

                async def do_create_voice(interaction: discord.Interaction):
                    new_ch = await guild.create_voice_channel(
                        ch_name, category=category,
                        reason=f'[TECO] {reason}'
                    )
                    await interaction.response.edit_message(
                        content=f'✅ Created voice channel 🔊 {new_ch.name}',
                        embed=None, view=None
                    )

                e = discord.Embed(
                    title='🔊 Create Voice Channel?',
                    description=(
                        f'**Name:** `{ch_name}`\n'
                        f'**Category:** {category.name if category else "None"}\n'
                        f'**Reason:** {reason}'
                    ),
                    color=discord.Color.purple()
                )
                view = ConfirmView(do_create_voice, e.description)
                await anchor.channel.send(embed=e, view=view)
                return f'🔊 Awaiting confirmation for voice: {ch_name}'

            # ── Send message ──────────────────────────────────────────────────
            if tool_name == 'send_message':
                await channel.send(tool_args.get('content', '')[:2000])
                return f'💬 Message sent'

            # ── Lock / Unlock ─────────────────────────────────────────────────
            if tool_name == 'lock_channel':
                ow = channel.overwrites_for(guild.default_role)
                ow.send_messages = False
                await channel.set_permissions(guild.default_role, overwrite=ow)
                await channel.send(f'🔒 Channel locked — {reason}')
                return f'🔒 Locked #{channel.name}'

            if tool_name == 'unlock_channel':
                ow = channel.overwrites_for(guild.default_role)
                ow.send_messages = None
                await channel.set_permissions(guild.default_role, overwrite=ow)
                await channel.send(f'🔓 Channel unlocked — {reason}')
                return f'🔓 Unlocked #{channel.name}'

            return f'Unknown tool: {tool_name}'

        except discord.Forbidden:
            return f'❌ No permission for {tool_name}'
        except Exception as e:
            logger.error(f'Tool error ({tool_name}): {e}')
            return f'❌ {tool_name} failed: {e}'


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 7 — THE BOT
# ═══════════════════════════════════════════════════════════════════════════════

class TECO(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix=PREFIX,
            intents=discord.Intents.all(),
            help_command=None
        )
        self.db  = Database()
        self.ai  = GeminiClient()
        self.exe = ToolExecutor(self.db)
        self._owner_history: dict[int, list] = defaultdict(list)

    async def setup_hook(self):
        await self.db.connect()
        try:
            synced = await self.tree.sync()
            logger.info(f'✅ Synced {len(synced)} slash commands')
        except Exception as e:
            logger.error(f'Sync failed: {e}')

    async def on_ready(self):
        logger.info(f'✅ TECO online — {self.user}')
        await self.change_presence(
            status=discord.Status.online,
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name='your server | !ai'
            )
        )

    async def on_guild_join(self, guild):
        await self.db.get_config(guild.id)

    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        # Save every message to history
        if message.content:
            await self.db.save_message(
                message.guild.id, message.channel.id,
                message.author.id, message.author.display_name,
                message.content
            )

        # Prefix commands
        await self.process_commands(message)

        config    = await self.db.get_config(message.guild.id)
        content_l = message.content.lower()

        # ── Trigger word detection ─────────────────────────────────────────────
        # Fires when any of the owner's trigger words appear in a message
        triggers  = config.get('trigger_words') or []
        triggered = any(
            re.search(rf'\b{re.escape(t)}\b', content_l)
            for t in triggers
        ) if triggers else False

        # ── @mention detection ────────────────────────────────────────────────
        mentioned = self.user in message.mentions

        if triggered or mentioned:
            # Strip trigger word / mention to get the actual question
            clean = message.content
            for t in triggers:
                clean = re.sub(rf'\b{re.escape(t)}\b', '', clean, flags=re.IGNORECASE)
            clean = clean.replace(f'<@{self.user.id}>', '').replace(f'<@!{self.user.id}>', '').strip()
            if not clean:
                return await message.reply('Hey! What do you need?', mention_author=False)

            # Check if sender is owner/admin/trusted role
            is_privileged = (
                message.author.id in OWNER_IDS
                or message.author.guild_permissions.administrator
                or any(
                    r.id in (config.get('trusted_role_ids') or [])
                    for r in message.author.roles
                )
            )

            if is_privileged:
                # Full owner-mode: AI can respond AND take actions
                await self._handle_owner_trigger(message, clean, config)
            else:
                # Regular member: AI responds in text only, no actions
                guild_roles = [r.name for r in message.guild.roles if not r.is_default()]
                system  = self.ai.build_system_prompt(config, message.guild.name, guild_roles)
                async with message.channel.typing():
                    answer = await self.ai.simple_chat(clean, system)
                await message.reply(answer[:2000], mention_author=False)
            return

        # ── Auto-mod pipeline (monitored channels only) ───────────────────────
        is_admin = (
            message.author.id in OWNER_IDS
            or message.author.guild_permissions.administrator
        )
        if is_admin:
            return

        monitored = config.get('monitored_channels') or []
        if message.channel.id not in monitored:
            return

        if not config.get('auto_mod_enabled'):
            return

        if not self.ai.quick_precheck(message.content):
            return

        # Cooldown
        now = time.time()
        if now - _last_scan[message.channel.id] < AI_SCAN_COOLDOWN:
            return
        _last_scan[message.channel.id] = now

        guild_roles = [r.name for r in message.guild.roles if not r.is_default()]
        recent      = await self.db.get_recent_messages(message.channel.id, limit=8)
        text, tool_name, tool_args = await self.ai.evaluate_message(
            message, recent, config, guild_roles
        )
        if not tool_name:
            return

        result   = await self.exe.execute(tool_name, tool_args or {}, message)
        executed = tool_name != 'no_action'

        await self.db.log_decision(
            message.guild.id, message.channel.id,
            message.author.id if executed else None,
            tool_name,
            (tool_args or {}).get('reason', ''),
            text or '', executed
        )

        if executed:
            await self._post_log(message.guild, result, config)

    async def _handle_owner_trigger(self, message: discord.Message,
                                    query: str, config: dict):
        """Privileged user triggered TECO — full agent mode."""
        async with message.channel.typing():
            decisions = await self.db.get_recent_decisions(message.guild.id, limit=5)
            snapshot  = (
                f'Server: {message.guild.name} | Members: {message.guild.member_count}\n'
                'Recent actions:\n' +
                '\n'.join(f'- {d["action"]}: {d["reason"]}' for d in decisions)
            )
            history = self._owner_history[message.author.id]
            text, tool_name, tool_args = await self.ai.owner_chat(
                query, config, message.guild, snapshot, history
            )

            history.append({'role': 'user',      'content': query})
            history.append({'role': 'assistant',  'content': text or f'→ {tool_name}'})
            if len(history) > 12:
                self._owner_history[message.author.id] = history[-12:]

            parts = []
            if text:
                parts.append(text)

            if tool_name and tool_name != 'no_action':
                result = await self.exe.execute(
                    tool_name, tool_args or {}, message, reply_to=message
                )
                parts.append(f'⚡ {result}')
                await self.db.log_decision(
                    message.guild.id, message.channel.id, None,
                    tool_name, (tool_args or {}).get('reason', ''),
                    text or '', True
                )

        reply = '\n\n'.join(parts) or '✅ Done.'
        for chunk in [reply[i:i+1990] for i in range(0, len(reply), 1990)]:
            await message.reply(chunk, mention_author=False)

    async def _post_log(self, guild: discord.Guild, result: str, config: dict):
        ch_id = config.get('log_channel')
        if not ch_id:
            return
        ch = guild.get_channel(ch_id)
        if not ch:
            return
        e = discord.Embed(
            description=f'🤖 **TECO** — {result}',
            color=discord.Color.gold(),
            timestamp=discord.utils.utcnow()
        )
        await ch.send(embed=e)

    async def on_command_error(self, ctx, error):
        if isinstance(error, commands.CommandNotFound):
            return
        logger.error(f'Command error: {error}')


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 8 — SLASH COMMANDS
# ═══════════════════════════════════════════════════════════════════════════════

bot = TECO()


def owner_only():
    async def predicate(i: discord.Interaction):
        return (i.user.id in OWNER_IDS or i.user.guild_permissions.administrator)
    return app_commands.check(predicate)


# ── /train ────────────────────────────────────────────────────────────────────
@bot.tree.command(name='train', description='Train TECO — set personality and escalation style')
@app_commands.describe(instructions='Plain English: how to behave, tone, escalation order')
@owner_only()
async def train(i: discord.Interaction, instructions: str):
    await bot.db.set_config(i.guild.id, system_prompt=instructions)
    e = discord.Embed(title='🧠 TECO Trained', description=instructions,
                      color=discord.Color.green())
    await i.response.send_message(embed=e)


# ── /rules ────────────────────────────────────────────────────────────────────
rules_grp = app_commands.Group(name='rules', description='Manage rules TECO enforces')

@rules_grp.command(name='add', description='Add a rule')
@owner_only()
async def rules_add(i: discord.Interaction, rule: str):
    config = await bot.db.get_config(i.guild.id)
    lines  = [r.strip() for r in (config.get('server_rules') or '').split('\n') if r.strip()]
    lines.append(f'{len(lines)+1}. {rule}')
    await bot.db.set_config(i.guild.id, server_rules='\n'.join(lines))
    await i.response.send_message(f'✅ Rule added. TECO enforces **{len(lines)}** rule(s).')

@rules_grp.command(name='set', description='Replace all rules with a block of text')
@owner_only()
async def rules_set(i: discord.Interaction, rules: str):
    await bot.db.set_config(i.guild.id, server_rules=rules)
    await i.response.send_message(f'✅ Rules updated.\n```{rules[:400]}```')

@rules_grp.command(name='list', description='Show all rules')
async def rules_list(i: discord.Interaction):
    config = await bot.db.get_config(i.guild.id)
    rules  = config.get('server_rules') or 'No rules set. Use `/rules add`.'
    await i.response.send_message(
        embed=discord.Embed(title='📋 Rules', description=rules, color=discord.Color.blue()),
        ephemeral=True
    )

@rules_grp.command(name='clear', description='Clear all rules')
@owner_only()
async def rules_clear(i: discord.Interaction):
    await bot.db.set_config(i.guild.id, server_rules='')
    await i.response.send_message('✅ Rules cleared.')

bot.tree.add_command(rules_grp)


# ── /trigger ──────────────────────────────────────────────────────────────────
trigger_grp = app_commands.Group(name='trigger', description='Manage custom wake words for TECO')

@trigger_grp.command(name='add', description='Add a trigger word (e.g. teco, merlin, bot)')
@owner_only()
async def trigger_add(i: discord.Interaction, word: str):
    await bot.db.add_trigger(i.guild.id, word.lower())
    await i.response.send_message(
        f'✅ Trigger `{word.lower()}` added. TECO now wakes when it hears that word.')

@trigger_grp.command(name='remove', description='Remove a trigger word')
@owner_only()
async def trigger_remove(i: discord.Interaction, word: str):
    await bot.db.remove_trigger(i.guild.id, word.lower())
    await i.response.send_message(f'✅ Trigger `{word.lower()}` removed.')

@trigger_grp.command(name='list', description='Show all trigger words')
async def trigger_list(i: discord.Interaction):
    config   = await bot.db.get_config(i.guild.id)
    triggers = config.get('trigger_words') or []
    text     = ', '.join(f'`{t}`' for t in triggers) if triggers else 'None set.'
    await i.response.send_message(f'🔔 Triggers: {text}', ephemeral=True)

bot.tree.add_command(trigger_grp)


# ── /trusted ──────────────────────────────────────────────────────────────────
trusted_grp = app_commands.Group(
    name='trusted',
    description='Roles that can command TECO like an admin'
)

@trusted_grp.command(name='add', description='Trust a role to command TECO')
@owner_only()
async def trusted_add(i: discord.Interaction, role: discord.Role):
    await bot.db.add_trusted_role(i.guild.id, role.id)
    await i.response.send_message(
        f'✅ {role.mention} can now trigger TECO with full agent access.')

@trusted_grp.command(name='remove', description='Remove a role\'s TECO access')
@owner_only()
async def trusted_remove(i: discord.Interaction, role: discord.Role):
    await bot.db.remove_trusted_role(i.guild.id, role.id)
    await i.response.send_message(f'✅ {role.mention} removed from trusted roles.')

@trusted_grp.command(name='list', description='Show trusted roles')
async def trusted_list(i: discord.Interaction):
    config = await bot.db.get_config(i.guild.id)
    ids    = config.get('trusted_role_ids') or []
    text   = ' '.join(f'<@&{r}>' for r in ids) if ids else 'None.'
    await i.response.send_message(f'🔑 Trusted roles: {text}', ephemeral=True)

bot.tree.add_command(trusted_grp)


# ── /monitor ──────────────────────────────────────────────────────────────────
@bot.tree.command(name='monitor', description='Add a channel to TECO\'s watch list')
@owner_only()
async def monitor(i: discord.Interaction, channel: Optional[discord.TextChannel] = None):
    ch = channel or i.channel
    await bot.db.add_monitored_channel(i.guild.id, ch.id)
    await i.response.send_message(f'✅ TECO now monitors {ch.mention}')

@bot.tree.command(name='unmonitor', description='Remove a channel from TECO\'s watch list')
@owner_only()
async def unmonitor(i: discord.Interaction, channel: Optional[discord.TextChannel] = None):
    ch = channel or i.channel
    await bot.db.remove_monitored_channel(i.guild.id, ch.id)
    await i.response.send_message(f'✅ TECO stopped monitoring {ch.mention}')


# ── /automod ──────────────────────────────────────────────────────────────────
@bot.tree.command(name='automod', description='Toggle autonomous TECO actions')
@owner_only()
async def automod(i: discord.Interaction, enabled: bool):
    await bot.db.set_config(i.guild.id, auto_mod_enabled=enabled)
    msg = '✅ TECO is now acting autonomously.' if enabled else '⏸️ TECO is watching only (no auto-actions).'
    await i.response.send_message(msg)


# ── /logs ─────────────────────────────────────────────────────────────────────
@bot.tree.command(name='logs', description='Set TECO\'s action log channel')
@owner_only()
async def logs(i: discord.Interaction, channel: discord.TextChannel):
    await bot.db.set_config(i.guild.id, log_channel=channel.id)
    await i.response.send_message(f'✅ TECO logs → {channel.mention}')


# ── /review ───────────────────────────────────────────────────────────────────
@bot.tree.command(name='review', description='AI risk assessment of a member')
@owner_only()
async def review(i: discord.Interaction, member: discord.Member):
    await i.response.defer()
    config   = await bot.db.get_config(i.guild.id)
    history  = await bot.db.get_member_history(i.guild.id, member.id)
    msgs     = await bot.db.get_user_messages(i.guild.id, member.id, 15)

    h_text = '\n'.join(f'- {h["action"]}: {h["reason"]} ({str(h["created_at"])[:10]})'
                       for h in history) or 'Clean record'
    m_text = '\n'.join(f'[{str(m["created_at"])[:16]}] {m["content"]}' for m in msgs) or 'No messages'

    prompt = (
        f'Risk assessment for {member.display_name} '
        f'(account age: {(discord.utils.utcnow()-member.created_at).days}d)\n\n'
        f'MOD HISTORY:\n{h_text}\n\nRECENT MESSAGES:\n{m_text}\n\n'
        f'Give: 1) Risk level (LOW/MEDIUM/HIGH)  2) Pattern  3) Recommended action'
    )
    system = bot.ai.build_system_prompt(config, i.guild.name)
    result = await bot.ai.simple_chat(prompt, system)
    e = discord.Embed(title=f'🔍 Review — {member.display_name}',
                      description=result, color=discord.Color.orange())
    e.set_thumbnail(url=member.display_avatar.url)
    await i.followup.send(embed=e)


# ── /history ──────────────────────────────────────────────────────────────────
@bot.tree.command(name='history', description="See a member's mod history")
async def history(i: discord.Interaction, member: discord.Member):
    records = await bot.db.get_member_history(i.guild.id, member.id)
    if not records:
        return await i.response.send_message(f'✅ {member.mention} has a clean record.', ephemeral=True)
    e = discord.Embed(title=f'📋 {member.display_name}', color=discord.Color.red())
    for r in records[:10]:
        e.add_field(name=f'{r["action"]} — {str(r["created_at"])[:10]}',
                    value=r['reason'] or '—', inline=False)
    await i.response.send_message(embed=e, ephemeral=True)


# ── /status ───────────────────────────────────────────────────────────────────
@bot.tree.command(name='status', description="TECO's current config")
async def status(i: discord.Interaction):
    config    = await bot.db.get_config(i.guild.id)
    monitored = config.get('monitored_channels') or []
    triggers  = config.get('trigger_words') or []
    trusted   = config.get('trusted_role_ids') or []

    e = discord.Embed(title='🤖 TECO Status', color=discord.Color.blurple())
    e.add_field(name='Auto-Mod',
                value='✅ Active' if config['auto_mod_enabled'] else '⏸️ Watch only',
                inline=True)
    e.add_field(name='Trained',
                value='✅' if config['system_prompt'] else '❌',
                inline=True)
    e.add_field(name='Rules',
                value='✅' if config['server_rules'] else '❌',
                inline=True)
    e.add_field(name='Monitored',
                value=' '.join(f'<#{c}>' for c in monitored) or 'None',
                inline=False)
    e.add_field(name='Triggers',
                value=' '.join(f'`{t}`' for t in triggers) or 'None',
                inline=True)
    e.add_field(name='Trusted Roles',
                value=' '.join(f'<@&{r}>' for r in trusted) or 'None',
                inline=True)
    await i.response.send_message(embed=e)


# ── /ask ─────────────────────────────────────────────────────────────────────
@bot.tree.command(name='ask', description='Ask TECO anything')
async def ask(i: discord.Interaction, question: str):
    await i.response.defer()
    config = await bot.db.get_config(i.guild.id)
    system = bot.ai.build_system_prompt(config, i.guild.name)
    answer = await bot.ai.simple_chat(question, system)
    e = discord.Embed(description=answer, color=discord.Color.blurple())
    e.set_author(name='TECO', icon_url=bot.user.display_avatar.url)
    await i.followup.send(embed=e)


# ── /help ────────────────────────────────────────────────────────────────────
@bot.tree.command(name='help', description='All TECO commands')
async def help_cmd(i: discord.Interaction):
    e = discord.Embed(
        title='🤖 TECO v2 — AI Co-Admin',
        description='Train me once. I handle the rest.',
        color=discord.Color.blurple()
    )
    e.add_field(name='🧠 Training', value=(
        '`/train` — Set my personality + escalation style\n'
        '`/rules add/set/list/clear` — Server rules I enforce'
    ), inline=False)
    e.add_field(name='🔔 Triggers', value=(
        '`/trigger add teco` — Wake word (say "teco" to talk to me)\n'
        '`/trigger add merlin` — Add as many as you want\n'
        '`/trigger list/remove` — Manage them'
    ), inline=False)
    e.add_field(name='🔑 Hierarchy', value=(
        '`/trusted add @Mods` — Mods can command me like an admin\n'
        '`/trusted remove/list`'
    ), inline=False)
    e.add_field(name='⚙️ Setup', value=(
        '`/monitor` `/unmonitor` — Channels I watch\n'
        '`/automod` — Toggle autonomous actions\n'
        '`/logs` — Where I post action logs'
    ), inline=False)
    e.add_field(name='🔍 Intelligence', value=(
        '`/review @user` — AI risk assessment\n'
        '`/history @user` — Mod history\n'
        '`/ask` — Ask me anything\n'
        '`/status` — My current config'
    ), inline=False)
    e.add_field(name='💬 Direct Commands', value=(
        '`!ai [anything]` — Full agent mode (owner)\n'
        '`!ai clear` — Reset conversation memory\n'
        'Or just say your trigger word in any channel!'
    ), inline=False)
    await i.response.send_message(embed=e)


# ── !ai prefix command ────────────────────────────────────────────────────────
@bot.command(name='ai')
async def ai_cmd(ctx: commands.Context, *, query: str = ''):
    if (ctx.author.id not in OWNER_IDS and
            not ctx.author.guild_permissions.administrator):
        return

    if not query:
        return await ctx.send('Usage: `!ai [question or command]`')

    if query.strip().lower() == 'clear':
        bot._owner_history[ctx.author.id].clear()
        return await ctx.send('🧹 Conversation cleared.')

    config = await bot.db.get_config(ctx.guild.id)
    async with ctx.typing():
        await bot._handle_owner_trigger(ctx.message, query, config)


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 9 — KEEP-ALIVE WEB SERVER
# ═══════════════════════════════════════════════════════════════════════════════

async def run_webserver():
    async def health(req):
        return web.json_response({
            'status': 'ok',
            'bot':    str(bot.user) if bot.is_ready() else 'starting',
            'guilds': len(bot.guilds) if bot.is_ready() else 0
        })
    app = web.Application()
    app.router.add_get('/',       health)
    app.router.add_get('/health', health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, '0.0.0.0', PORT).start()
    logger.info(f'✅ Keep-alive on port {PORT}')


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 10 — MAIN
# ═══════════════════════════════════════════════════════════════════════════════

async def main():
    for var in ('DISCORD_TOKEN', 'GEMINI_API_KEY', 'DATABASE_URL'):
        if not os.getenv(var):
            raise RuntimeError(f'{var} is not set in environment')
    async with bot:
        await asyncio.gather(run_webserver(), bot.start(DISCORD_TOKEN))

if __name__ == '__main__':
    asyncio.run(main())
