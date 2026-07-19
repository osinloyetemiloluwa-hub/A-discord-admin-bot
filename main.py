"""
╔══════════════════════════════════════════════════════════════════╗
║          TECO - AI CO-ADMIN AGENT BOT                          ║
║          Language: Python | AI: Groq (Llama/Mixtral)            ║
║          Deploy: Render/Railway/Heroku                          ║
╚══════════════════════════════════════════════════════════════════╝

Advanced AI-powered Discord server management bot with natural language
command processing. Powered by Groq's fast inference API.
"""

import discord
from discord.ext import commands
from discord import app_commands
import os
import asyncio
import logging
from dotenv import load_dotenv
from utils.database import Database
from utils.keepalive import run_webserver

load_dotenv()

# ─── Logging Setup ────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger('TECO_Bot')

# ─── Bot Intents ──────────────────────────────────────────────────────────────
INTENTS = discord.Intents.all()


class TecoBot(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix=os.getenv('PREFIX', '!'),
            intents=INTENTS,
            help_command=None,
            description='TECO - Advanced AI Co-Admin Agent for Discord'
        )
        self.db = Database()
        self.logger = logger
        raw_ids = os.getenv('OWNER_IDS', '')
        self.owner_ids = set(int(i) for i in raw_ids.split(',') if i.strip().isdigit())

    async def setup_hook(self):
        """Called once before the bot starts — loads cogs and syncs commands."""
        await self.db.initialize()

        cogs = [
            'cogs.moderation',
            'cogs.ai_agent',
            'cogs.automod',
            'cogs.logging_cog',
            'cogs.welcome',
        ]

        for cog in cogs:
            try:
                await self.load_extension(cog)
                logger.info(f'✅ Loaded: {cog}')
            except Exception as e:
                logger.error(f'❌ Failed to load {cog}: {e}')

        try:
            synced = await self.tree.sync()
            logger.info(f'✅ Synced {len(synced)} slash command(s)')
        except Exception as e:
            logger.error(f'❌ Slash command sync failed: {e}')

    async def on_ready(self):
        total_members = sum(g.member_count for g in self.guilds)
        logger.info(f'✅ Logged in as {self.user} (ID: {self.user.id})')
        logger.info(f'📊 Servers: {len(self.guilds)} | Members: {total_members}')

        await self.change_presence(
            status=discord.Status.online,
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name=f'{len(self.guilds)} servers | /help'
            )
        )

    async def on_guild_join(self, guild: discord.Guild):
        await self.db.initialize_guild(guild.id)
        logger.info(f'Joined: {guild.name} ({guild.id})')

    async def on_command_error(self, ctx, error):
        if isinstance(error, commands.MissingPermissions):
            await ctx.send(embed=discord.Embed(
                description='❌ You lack the permissions for this command.',
                color=discord.Color.red()
            ), delete_after=8)
        elif isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(embed=discord.Embed(
                description=f'❌ Missing argument: `{error.param.name}`',
                color=discord.Color.red()
            ), delete_after=8)
        elif isinstance(error, commands.CommandNotFound):
            pass
        else:
            logger.error(f'Unhandled command error: {error}')


# ─── Bot Instance ─────────────────────────────────────────────────────────────
bot = TecoBot()


# ─── Owner-only sync command ──────────────────────────────────────────────────
@bot.command(name='sync')
@commands.is_owner()
async def sync_commands(ctx):
    """Manually re-sync slash commands (owner only)."""
    synced = await bot.tree.sync()
    await ctx.send(f'✅ Synced {len(synced)} slash command(s).')


# ─── /help ────────────────────────────────────────────────────────────────────
@bot.tree.command(name='help', description='Show all commands and features')
async def help_command(interaction: discord.Interaction):
    embed = discord.Embed(
        title='TECO - AI Co-Admin Bot',
        description='Your intelligent Discord server management assistant, powered by Groq AI (Llama/Mixtral).',
        color=discord.Color.blurple(),
        timestamp=discord.utils.utcnow()
    )

    embed.add_field(
        name='🚀 TECO - Advanced AI Commands',
        value=(
            '`/teco` - Natural language commands (e.g., "delete last 5 messages with nsfw")\n'
            '`/ask` - Ask the AI anything\n'
            '`/search` - Search messages with AI\n'
            '`/toxicityscan` - Scan for toxic content\n'
            '`/audit` - Full server audit with AI\n'
            '`/scan` - Scan members for issues\n'
            '`/purgecontent` - Delete messages by keyword/AI filter'
        ),
        inline=False
    )

    embed.add_field(
        name='🛡️ Moderation',
        value=(
            '`/kick` `/ban` `/tempban` `/unban`\n'
            '`/mute` `/unmute` `/warn` `/warnings`\n'
            '`/clearwarns` `/purge` `/slowmode`\n'
            '`/lock` `/unlock` `/role` `/nick`\n'
            '`/userinfo` `/serverinfo` `/note` `/notes`'
        ),
        inline=False
    )

    embed.add_field(
        name='🤖 AI Analysis',
        value='`/toxcheck` `/summarize` `/moderate` `/clearcontext`\n@mention me to chat!',
        inline=False
    )

    embed.add_field(
        name='🔧 Auto-Mod',
        value=(
            '`/automod toggle` `/automod status`\n'
            '`/automod antispam` `/automod links` `/automod caps`\n'
            '`/badword add` `/badword remove` `/badword list`\n'
            '`/antiraid`'
        ),
        inline=False
    )

    embed.add_field(
        name='📋 Logging',
        value='`/logs setchannel` `/logs disable`',
        inline=False
    )

    embed.add_field(
        name='👋 Welcome',
        value='`/welcome setchannel` `/welcome setmessage` `/welcome setfarewell` `/welcome test` `/autorole`',
        inline=False
    )

    embed.set_footer(text='TECO Bot • Powered by Groq AI | Use /teco for natural language commands!')

    await interaction.response.send_message(embed=embed)


# ─── Entry Point ──────────────────────────────────────────────────────────────
async def main():
    token = os.getenv('DISCORD_TOKEN')
    if not token:
        logger.critical('❌ DISCORD_TOKEN not found in environment variables!')
        exit(1)

    async with bot:
        # Web server runs alongside the bot so Hugging Face Docker Spaces
        # (which expect a bound port to mark the Space as "Running") work
        # correctly. Harmless extra service if deploying elsewhere (e.g. Railway).
        await asyncio.gather(
            run_webserver(bot),
            bot.start(token),
        )


if __name__ == '__main__':
    asyncio.run(main())
