"""
cogs/automod.py
Automated moderation: anti-spam, link filtering, caps filtering,
banned word detection, and basic anti-raid (join-rate) protection.
"""

import discord
from discord.ext import commands
from discord import app_commands
from collections import defaultdict, deque
from datetime import datetime, timedelta
import re
import time


URL_PATTERN = re.compile(r'(https?://\S+|www\.\S+|discord\.gg/\S+)', re.IGNORECASE)
INVITE_PATTERN = re.compile(r'(discord\.gg|discordapp\.com/invite)/\S+', re.IGNORECASE)


class AutoMod(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = bot.db
        # message_history[(guild_id, user_id)] -> deque[timestamps]
        self.message_history: dict[tuple, deque] = defaultdict(lambda: deque(maxlen=10))
        # recent joins for anti-raid: guild_id -> deque[timestamps]
        self.join_history: dict[int, deque] = defaultdict(lambda: deque(maxlen=20))

    # ─── Message Listener — core automod pipeline ──────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        if message.author.guild_permissions.administrator:
            return

        config = await self.db.get_guild_config(message.guild.id)
        if not config.get('automod_enabled'):
            return

        # Order matters: spam check first (most common), then content checks
        if config.get('antispam_enabled') and await self._check_spam(message, config):
            return
        if config.get('anti_links') and await self._check_links(message):
            return
        if config.get('anti_caps') and await self._check_caps(message, config):
            return
        if await self._check_banned_words(message):
            return

    # ─── Spam Detection ─────────────────────────────────────────────────────────

    async def _check_spam(self, message: discord.Message, config: dict) -> bool:
        key = (message.guild.id, message.author.id)
        now = time.time()
        history = self.message_history[key]
        history.append(now)

        interval = config.get('spam_interval', 5)
        max_msgs = config.get('max_messages', 5)
        recent = [t for t in history if now - t <= interval]

        # Mention spam check
        max_mentions = config.get('max_mentions', 5)
        if len(message.mentions) >= max_mentions:
            await self._punish(message, 'Mass mention spam')
            return True

        if len(recent) >= max_msgs:
            await self._punish(message, f'Message spam ({len(recent)} msgs in {interval}s)')
            history.clear()
            return True

        return False

    # ─── Link Filtering ─────────────────────────────────────────────────────────

    async def _check_links(self, message: discord.Message) -> bool:
        if URL_PATTERN.search(message.content):
            await message.delete()
            await self._warn_user(message, '🔗 Links are not allowed in this server.')
            await self._log_action(message.guild, 'Link Filter', message.author,
                                   f'Deleted message containing a link in {message.channel.mention}')
            return True
        return False

    # ─── Caps Filtering ─────────────────────────────────────────────────────────

    async def _check_caps(self, message: discord.Message, config: dict) -> bool:
        text = message.content
        if len(text) < 10:
            return False
        letters = [c for c in text if c.isalpha()]
        if not letters:
            return False
        caps_ratio = sum(1 for c in letters if c.isupper()) / len(letters) * 100
        threshold = config.get('caps_threshold', 70)

        if caps_ratio >= threshold:
            await message.delete()
            await self._warn_user(message, '🔠 Please avoid excessive caps lock.')
            return True
        return False

    # ─── Banned Word Filtering ───────────────────────────────────────────────────

    async def _check_banned_words(self, message: discord.Message) -> bool:
        words = await self.db.get_banned_words(message.guild.id)
        if not words:
            return False
        content_lower = message.content.lower()
        for w in words:
            if re.search(rf'\b{re.escape(w)}\b', content_lower):
                await message.delete()
                await self._warn_user(message, '🚫 Your message contained a prohibited word.')
                await self._log_action(message.guild, 'Banned Word', message.author,
                                       f'Triggered word: `{w}` in {message.channel.mention}')
                return True
        return False

    # ─── Punishment Escalation ───────────────────────────────────────────────────

    async def _punish(self, message: discord.Message, reason: str):
        try:
            await message.delete()
        except discord.NotFound:
            pass

        offenses = await self.db.increment_offense(message.guild.id, message.author.id)

        if offenses == 1:
            await self._warn_user(message, f'⚠️ {reason}. This is your first notice.')
        elif offenses == 2:
            try:
                await message.author.timeout(discord.utils.utcnow() + timedelta(minutes=10),
                                            reason=f'[AutoMod] {reason}')
                await message.channel.send(
                    f'🔇 {message.author.mention} has been muted 10 minutes for repeated spam.',
                    delete_after=8)
            except discord.Forbidden:
                pass
        else:
            try:
                await message.author.kick(reason=f'[AutoMod] Repeated spam offenses ({offenses})')
                await message.channel.send(
                    f'👢 {message.author.mention} was kicked for repeated spam.', delete_after=8)
            except discord.Forbidden:
                pass

        await self._log_action(message.guild, 'AutoMod Action', message.author,
                               f'{reason} (Offense #{offenses})')

    async def _warn_user(self, message: discord.Message, text: str):
        try:
            await message.channel.send(f'{message.author.mention} {text}', delete_after=6)
        except discord.Forbidden:
            pass

    async def _log_action(self, guild: discord.Guild, action: str, member: discord.Member, detail: str):
        config = await self.db.get_guild_config(guild.id)
        ch_id = config.get('log_channel')
        if not ch_id:
            return
        channel = guild.get_channel(ch_id)
        if not channel:
            return
        e = discord.Embed(title=f'🛡️ {action}', description=detail,
                          color=discord.Color.dark_gold(), timestamp=discord.utils.utcnow())
        e.set_author(name=str(member), icon_url=member.display_avatar.url)
        await channel.send(embed=e)

    # ─── Anti-Raid: Join Rate Monitor ────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        config = await self.db.get_guild_config(member.guild.id)
        if not config.get('antiraid_enabled'):
            return

        history = self.join_history[member.guild.id]
        now = time.time()
        history.append(now)

        recent_joins = [t for t in history if now - t <= 10]  # joins in last 10s
        if len(recent_joins) >= 8:
            # Possible raid — enable verification lockdown
            try:
                await member.guild.edit(verification_level=discord.VerificationLevel.high)
                await self._log_action(
                    member.guild, '🚨 RAID DETECTED',
                    member,
                    f'{len(recent_joins)} joins in 10s. Verification level raised to HIGH automatically.'
                )
            except discord.Forbidden:
                pass

        # Flag very new accounts (possible bot/raid accounts)
        account_age = (discord.utils.utcnow() - member.created_at).days
        if account_age < 3:
            await self._log_action(
                member.guild, '⚠️ New Account Joined', member,
                f'Account is only **{account_age} day(s)** old. Created: <t:{int(member.created_at.timestamp())}:R>'
            )

    # ─── Slash Commands: /automod config ─────────────────────────────────────────

    automod_group = app_commands.Group(name='automod', description='Configure auto-moderation settings')

    @automod_group.command(name='toggle', description='Enable or disable auto-moderation')
    @app_commands.checks.has_permissions(administrator=True)
    async def automod_toggle(self, interaction: discord.Interaction, enabled: bool):
        await self.db.update_guild_config(interaction.guild.id, automod_enabled=int(enabled))
        status = '✅ enabled' if enabled else '❌ disabled'
        await interaction.response.send_message(f'Auto-moderation is now {status}.')

    @automod_group.command(name='status', description='View current automod configuration')
    async def automod_status(self, interaction: discord.Interaction):
        c = await self.db.get_guild_config(interaction.guild.id)
        e = discord.Embed(title='🛡️ AutoMod Configuration', color=discord.Color.blurple(),
                          timestamp=discord.utils.utcnow())
        e.add_field(name='AutoMod', value='✅' if c['automod_enabled'] else '❌', inline=True)
        e.add_field(name='Anti-Spam', value='✅' if c['antispam_enabled'] else '❌', inline=True)
        e.add_field(name='Anti-Links', value='✅' if c['anti_links'] else '❌', inline=True)
        e.add_field(name='Anti-Caps', value='✅' if c['anti_caps'] else '❌', inline=True)
        e.add_field(name='Anti-Raid', value='✅' if c['antiraid_enabled'] else '❌', inline=True)
        e.add_field(name='Caps Threshold', value=f"{c['caps_threshold']}%", inline=True)
        e.add_field(name='Max Mentions', value=str(c['max_mentions']), inline=True)
        e.add_field(name='Spam Limit', value=f"{c['max_messages']} msgs / {c['spam_interval']}s", inline=True)
        await interaction.response.send_message(embed=e)

    @automod_group.command(name='antispam', description='Configure anti-spam thresholds')
    @app_commands.checks.has_permissions(administrator=True)
    async def automod_antispam(self, interaction: discord.Interaction,
                               enabled: bool, max_messages: int = 5, interval_seconds: int = 5):
        await self.db.update_guild_config(
            interaction.guild.id, antispam_enabled=int(enabled),
            max_messages=max_messages, spam_interval=interval_seconds
        )
        await interaction.response.send_message(
            f'Anti-spam {"enabled" if enabled else "disabled"} — '
            f'limit: {max_messages} msgs / {interval_seconds}s.')

    @automod_group.command(name='links', description='Toggle link filtering')
    @app_commands.checks.has_permissions(administrator=True)
    async def automod_links(self, interaction: discord.Interaction, enabled: bool):
        await self.db.update_guild_config(interaction.guild.id, anti_links=int(enabled))
        await interaction.response.send_message(f'Link filtering {"enabled" if enabled else "disabled"}.')

    @automod_group.command(name='caps', description='Toggle excessive caps filtering')
    @app_commands.checks.has_permissions(administrator=True)
    async def automod_caps(self, interaction: discord.Interaction,
                           enabled: bool, threshold_percent: int = 70):
        await self.db.update_guild_config(
            interaction.guild.id, anti_caps=int(enabled), caps_threshold=threshold_percent)
        await interaction.response.send_message(
            f'Caps filter {"enabled" if enabled else "disabled"} — threshold {threshold_percent}%.')

    @app_commands.command(name='antiraid', description='Toggle anti-raid join protection')
    @app_commands.checks.has_permissions(administrator=True)
    async def antiraid(self, interaction: discord.Interaction, enabled: bool):
        await self.db.update_guild_config(interaction.guild.id, antiraid_enabled=int(enabled))
        await interaction.response.send_message(
            f'Anti-raid protection {"enabled" if enabled else "disabled"}.')

    # ─── Banned Word Management ───────────────────────────────────────────────────

    badword_group = app_commands.Group(name='badword', description='Manage the banned word list')

    @badword_group.command(name='add', description='Add a word to the banned list')
    @app_commands.checks.has_permissions(administrator=True)
    async def badword_add(self, interaction: discord.Interaction, word: str):
        await self.db.add_banned_word(interaction.guild.id, word, interaction.user.id)
        await interaction.response.send_message(f'✅ Added `{word}` to the banned word list.',
                                                 ephemeral=True)

    @badword_group.command(name='remove', description='Remove a word from the banned list')
    @app_commands.checks.has_permissions(administrator=True)
    async def badword_remove(self, interaction: discord.Interaction, word: str):
        removed = await self.db.remove_banned_word(interaction.guild.id, word)
        msg = f'✅ Removed `{word}`.' if removed else f'❌ `{word}` was not in the list.'
        await interaction.response.send_message(msg, ephemeral=True)

    @badword_group.command(name='list', description='View all banned words')
    @app_commands.checks.has_permissions(administrator=True)
    async def badword_list(self, interaction: discord.Interaction):
        words = await self.db.get_banned_words(interaction.guild.id)
        text = ', '.join(f'`{w}`' for w in words) if words else 'No banned words configured.'
        await interaction.response.send_message(
            embed=discord.Embed(title='🚫 Banned Words', description=text,
                                color=discord.Color.dark_red()), ephemeral=True)

    async def cog_app_command_error(self, interaction: discord.Interaction, error):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message(
                '❌ You need Administrator permission for this.', ephemeral=True)
        else:
            raise error


async def setup(bot: commands.Bot):
    await bot.add_cog(AutoMod(bot))
