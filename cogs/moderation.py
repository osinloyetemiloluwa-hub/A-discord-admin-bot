"""
cogs/moderation.py
Full moderation suite — kick, ban, mute, warn, purge, lock, role, userinfo, etc.
"""

import discord
from discord.ext import commands
from discord import app_commands
import asyncio
from datetime import datetime, timedelta
import re
from typing import Optional


# ─── Helpers ──────────────────────────────────────────────────────────────────

def parse_duration(duration_str: str) -> Optional[int]:
    """Parse '1d12h30m10s' → total seconds. Returns None on bad input."""
    pattern = r'(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?'
    match = re.fullmatch(pattern, duration_str.strip())
    if not match or not any(match.groups()):
        return None
    d, h, m, s = (int(x or 0) for x in match.groups())
    return d * 86400 + h * 3600 + m * 60 + s


def format_duration(seconds: int) -> str:
    if seconds < 60:
        return f'{seconds}s'
    elif seconds < 3600:
        return f'{seconds // 60}m {seconds % 60}s'
    elif seconds < 86400:
        return f'{seconds // 3600}h {(seconds % 3600) // 60}m'
    else:
        return f'{seconds // 86400}d {(seconds % 86400) // 3600}h'


def embed(title: str, description: str, color: discord.Color,
          fields: dict = None, footer: str = None) -> discord.Embed:
    e = discord.Embed(title=title, description=description,
                      color=color, timestamp=discord.utils.utcnow())
    if fields:
        for k, v in fields.items():
            e.add_field(name=k, value=str(v), inline=True)
    if footer:
        e.set_footer(text=footer)
    return e


# ─── Cog ──────────────────────────────────────────────────────────────────────

class Moderation(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = bot.db

    # ── Shared helpers ────────────────────────────────────────────────────────

    async def _log(self, guild: discord.Guild, action: str,
                   target=None, moderator=None, reason: str = None, extra: str = None):
        config = await self.db.get_guild_config(guild.id)
        ch_id = config.get('log_channel')
        if not ch_id:
            return
        channel = guild.get_channel(ch_id)
        if not channel:
            return

        colors = {
            'KICK': discord.Color.orange(), 'BAN': discord.Color.red(),
            'TEMPBAN': discord.Color.dark_red(), 'UNBAN': discord.Color.green(),
            'MUTE': discord.Color.dark_orange(), 'UNMUTE': discord.Color.teal(),
            'WARN': discord.Color.yellow(), 'PURGE': discord.Color.blue(),
            'LOCK': discord.Color.dark_red(), 'UNLOCK': discord.Color.dark_green(),
        }
        e = discord.Embed(
            title=f'🔨 {action}',
            color=colors.get(action.upper(), discord.Color.greyple()),
            timestamp=discord.utils.utcnow()
        )
        if target:
            e.add_field(name='Target', value=f'{target.mention} (`{target.id}`)', inline=True)
        if moderator:
            e.add_field(name='Moderator', value=moderator.mention, inline=True)
        if reason:
            e.add_field(name='Reason', value=reason, inline=False)
        if extra:
            e.add_field(name='Details', value=extra, inline=False)

        await channel.send(embed=e)
        await self.db.add_mod_log(guild.id, action,
                                  getattr(target, 'id', None),
                                  getattr(moderator, 'id', None), reason)

    async def _dm(self, member: discord.Member, title: str, description: str, color: discord.Color):
        try:
            await member.send(embed=discord.Embed(title=title, description=description,
                                                  color=color, timestamp=discord.utils.utcnow()))
        except (discord.Forbidden, discord.HTTPException):
            pass

    def _hierarchy_check(self, actor: discord.Member, target: discord.Member) -> bool:
        """True = can act; False = blocked by role hierarchy."""
        if actor == actor.guild.owner:
            return True
        return target.top_role < actor.top_role

    # ─── KICK ─────────────────────────────────────────────────────────────────

    @app_commands.command(name='kick', description='Kick a member from the server')
    @app_commands.describe(member='Target member', reason='Reason for kick')
    @app_commands.checks.has_permissions(kick_members=True)
    async def kick(self, interaction: discord.Interaction,
                   member: discord.Member, reason: str = 'No reason provided'):
        if not self._hierarchy_check(interaction.user, member):
            return await interaction.response.send_message(
                embed=embed('❌ Hierarchy Error', 'You cannot kick someone with an equal or higher role.',
                            discord.Color.red()), ephemeral=True)

        await self._dm(member, f'👢 Kicked from {interaction.guild.name}',
                       f'**Reason:** {reason}', discord.Color.orange())
        await member.kick(reason=f'{reason} | Mod: {interaction.user}')

        await interaction.response.send_message(
            embed=embed('✅ Member Kicked', f'{member.mention} has been kicked.',
                        discord.Color.green(), {'Reason': reason}))
        await self._log(interaction.guild, 'KICK', member, interaction.user, reason)

    # ─── BAN ──────────────────────────────────────────────────────────────────

    @app_commands.command(name='ban', description='Permanently ban a member')
    @app_commands.describe(member='Target member', reason='Reason for ban',
                           delete_days='Days of messages to delete (0–7)')
    @app_commands.checks.has_permissions(ban_members=True)
    async def ban(self, interaction: discord.Interaction, member: discord.Member,
                  reason: str = 'No reason provided', delete_days: int = 1):
        if not self._hierarchy_check(interaction.user, member):
            return await interaction.response.send_message(
                embed=embed('❌ Hierarchy Error', 'Cannot ban someone with equal/higher role.',
                            discord.Color.red()), ephemeral=True)

        delete_days = max(0, min(7, delete_days))
        await self._dm(member, f'🔨 Banned from {interaction.guild.name}',
                       f'You have been **permanently banned**.\n**Reason:** {reason}', discord.Color.red())
        await member.ban(reason=f'{reason} | Mod: {interaction.user}',
                         delete_message_days=delete_days)

        await interaction.response.send_message(
            embed=embed('✅ Member Banned', f'{member.mention} has been banned.',
                        discord.Color.red(), {'Reason': reason, 'Msg Deleted': f'{delete_days}d'}))
        await self._log(interaction.guild, 'BAN', member, interaction.user, reason)

    # ─── TEMPBAN ──────────────────────────────────────────────────────────────

    @app_commands.command(name='tempban', description='Temporarily ban a member (e.g. 1d12h)')
    @app_commands.describe(member='Target member', duration='Duration e.g. 1d12h30m',
                           reason='Reason for ban')
    @app_commands.checks.has_permissions(ban_members=True)
    async def tempban(self, interaction: discord.Interaction, member: discord.Member,
                      duration: str, reason: str = 'No reason provided'):
        secs = parse_duration(duration)
        if not secs:
            return await interaction.response.send_message(
                embed=embed('❌ Invalid Duration', 'Use format: `1d12h30m`', discord.Color.red()),
                ephemeral=True)

        await self._dm(member, f'⏱️ Temp-Banned from {interaction.guild.name}',
                       f'Banned for **{format_duration(secs)}**.\n**Reason:** {reason}',
                       discord.Color.orange())
        await member.ban(reason=f'[TEMPBAN {format_duration(secs)}] {reason} | Mod: {interaction.user}')

        await interaction.response.send_message(
            embed=embed('✅ Temp-Banned', f'{member.mention} banned for `{format_duration(secs)}`.',
                        discord.Color.orange(), {'Reason': reason}))
        await self._log(interaction.guild, 'TEMPBAN', member, interaction.user, reason,
                        f'Duration: {format_duration(secs)}')

        await asyncio.sleep(secs)
        try:
            await interaction.guild.unban(member, reason='Temp-ban expired')
        except Exception:
            pass

    # ─── UNBAN ────────────────────────────────────────────────────────────────

    @app_commands.command(name='unban', description='Unban a user by ID')
    @app_commands.describe(user_id='Discord user ID', reason='Reason for unban')
    @app_commands.checks.has_permissions(ban_members=True)
    async def unban(self, interaction: discord.Interaction,
                    user_id: str, reason: str = 'No reason provided'):
        try:
            user = await self.bot.fetch_user(int(user_id))
            await interaction.guild.unban(user, reason=f'{reason} | Mod: {interaction.user}')
            await interaction.response.send_message(
                embed=embed('✅ User Unbanned', f'{user.mention} (`{user.id}`) has been unbanned.',
                            discord.Color.green(), {'Reason': reason}))
            await self._log(interaction.guild, 'UNBAN', user, interaction.user, reason)
        except discord.NotFound:
            await interaction.response.send_message(
                embed=embed('❌ Not Found', 'User not found or not banned.', discord.Color.red()),
                ephemeral=True)
        except ValueError:
            await interaction.response.send_message(
                embed=embed('❌ Invalid ID', 'Please provide a valid numeric user ID.', discord.Color.red()),
                ephemeral=True)

    # ─── MUTE (Discord Timeout) ───────────────────────────────────────────────

    @app_commands.command(name='mute', description='Timeout (mute) a member')
    @app_commands.describe(member='Target member', duration='Duration e.g. 1h30m (max 28d)',
                           reason='Reason for mute')
    @app_commands.checks.has_permissions(moderate_members=True)
    async def mute(self, interaction: discord.Interaction, member: discord.Member,
                   duration: str = '10m', reason: str = 'No reason provided'):
        secs = parse_duration(duration)
        if not secs or secs > 2419200:
            return await interaction.response.send_message(
                embed=embed('❌ Invalid Duration', 'Max is 28 days. Format: `1d12h30m`',
                            discord.Color.red()), ephemeral=True)

        if not self._hierarchy_check(interaction.user, member):
            return await interaction.response.send_message(
                embed=embed('❌ Hierarchy Error', 'Cannot mute someone with equal/higher role.',
                            discord.Color.red()), ephemeral=True)

        until = discord.utils.utcnow() + timedelta(seconds=secs)
        await member.timeout(until, reason=f'{reason} | Mod: {interaction.user}')
        await self._dm(member, f'🔇 Muted in {interaction.guild.name}',
                       f'Muted for **{format_duration(secs)}**.\n**Reason:** {reason}',
                       discord.Color.dark_orange())

        await interaction.response.send_message(
            embed=embed('✅ Member Muted', f'{member.mention} muted for `{format_duration(secs)}`.',
                        discord.Color.orange(), {'Reason': reason}))
        await self._log(interaction.guild, 'MUTE', member, interaction.user, reason,
                        f'Duration: {format_duration(secs)}')

    # ─── UNMUTE ───────────────────────────────────────────────────────────────

    @app_commands.command(name='unmute', description='Remove a member\'s timeout')
    @app_commands.describe(member='Target member', reason='Reason for unmute')
    @app_commands.checks.has_permissions(moderate_members=True)
    async def unmute(self, interaction: discord.Interaction, member: discord.Member,
                     reason: str = 'No reason provided'):
        if not member.is_timed_out():
            return await interaction.response.send_message(
                embed=embed('❌ Not Muted', f'{member.mention} is not currently muted.',
                            discord.Color.red()), ephemeral=True)

        await member.timeout(None, reason=f'{reason} | Mod: {interaction.user}')
        await interaction.response.send_message(
            embed=embed('✅ Member Unmuted', f'{member.mention} has been unmuted.',
                        discord.Color.green(), {'Reason': reason}))
        await self._log(interaction.guild, 'UNMUTE', member, interaction.user, reason)

    # ─── WARN ─────────────────────────────────────────────────────────────────

    @app_commands.command(name='warn', description='Warn a member (auto-escalates at 3/5/7 warns)')
    @app_commands.describe(member='Target member', reason='Reason for warning')
    @app_commands.checks.has_permissions(manage_messages=True)
    async def warn(self, interaction: discord.Interaction, member: discord.Member,
                   reason: str = 'No reason provided'):
        warn_id = await self.db.add_warning(interaction.guild.id, member.id,
                                            interaction.user.id, reason)
        count = await self.db.get_warning_count(interaction.guild.id, member.id)

        await self._dm(member, f'⚠️ Warning in {interaction.guild.name}',
                       f'**Reason:** {reason}\n**Total warnings:** {count}', discord.Color.yellow())

        # Auto-escalation
        action_note = None
        if count == 3:
            action_note = '🔇 Auto-muted 1 hour (3rd warning)'
            await member.timeout(discord.utils.utcnow() + timedelta(hours=1),
                                 reason='[AutoMod] 3 warnings reached')
        elif count == 5:
            action_note = '👢 Auto-kicked (5th warning)'
            await member.kick(reason='[AutoMod] 5 warnings reached')
        elif count >= 7:
            action_note = '🔨 Auto-banned (7+ warnings)'
            await member.ban(reason='[AutoMod] 7+ warnings reached')

        e = embed('⚠️ Member Warned',
                  f'{member.mention} warned. `(Warning #{warn_id})`',
                  discord.Color.yellow(),
                  {'Reason': reason, 'Total Warnings': count})
        if action_note:
            e.add_field(name='⚡ Auto-Action', value=action_note, inline=False)

        await interaction.response.send_message(embed=e)
        await self._log(interaction.guild, 'WARN', member, interaction.user, reason,
                        f'Warning #{warn_id} | Total: {count}')

    # ─── WARNINGS ─────────────────────────────────────────────────────────────

    @app_commands.command(name='warnings', description='View all warnings for a member')
    @app_commands.describe(member='Target member')
    @app_commands.checks.has_permissions(manage_messages=True)
    async def warnings(self, interaction: discord.Interaction, member: discord.Member):
        warns = await self.db.get_warnings(interaction.guild.id, member.id)
        if not warns:
            return await interaction.response.send_message(
                embed=embed('✅ Clean Record', f'{member.mention} has no warnings.',
                            discord.Color.green()))

        e = discord.Embed(
            title=f'⚠️ Warnings — {member.display_name}',
            description=f'**{len(warns)}** warning(s) on record',
            color=discord.Color.yellow(), timestamp=discord.utils.utcnow()
        )
        e.set_thumbnail(url=member.display_avatar.url)
        for w in warns[:10]:
            mod = interaction.guild.get_member(w['moderator_id'])
            mod_str = mod.mention if mod else f"`{w['moderator_id']}`"
            e.add_field(
                name=f'#{w["id"]} — {w["timestamp"][:10]}',
                value=f'**Reason:** {w["reason"]}\n**By:** {mod_str}',
                inline=False
            )
        if len(warns) > 10:
            e.set_footer(text=f'Showing 10 of {len(warns)}')
        await interaction.response.send_message(embed=e)

    # ─── CLEAR WARNS ──────────────────────────────────────────────────────────

    @app_commands.command(name='clearwarns', description='Clear all warnings for a member')
    @app_commands.describe(member='Target member')
    @app_commands.checks.has_permissions(administrator=True)
    async def clearwarns(self, interaction: discord.Interaction, member: discord.Member):
        count = await self.db.clear_warnings(interaction.guild.id, member.id)
        await interaction.response.send_message(
            embed=embed('✅ Warnings Cleared', f'Cleared **{count}** warning(s) for {member.mention}.',
                        discord.Color.green()))

    # ─── PURGE ────────────────────────────────────────────────────────────────

    @app_commands.command(name='purge', description='Bulk delete messages (1–100)')
    @app_commands.describe(amount='Number of messages to delete',
                           member='Only delete messages from this member (optional)')
    @app_commands.checks.has_permissions(manage_messages=True)
    async def purge(self, interaction: discord.Interaction, amount: int,
                    member: Optional[discord.Member] = None):
        amount = max(1, min(100, amount))
        await interaction.response.defer(ephemeral=True)

        check = (lambda m: m.author == member) if member else None
        deleted = await interaction.channel.purge(limit=amount, check=check)

        await interaction.followup.send(
            embed=embed('✅ Purged', f'Deleted **{len(deleted)}** message(s).',
                        discord.Color.blue(), {'Channel': interaction.channel.mention}),
            ephemeral=True)
        await self._log(interaction.guild, 'PURGE', None, interaction.user,
                        f'{len(deleted)} messages deleted in {interaction.channel.mention}')

    # ─── SLOWMODE ─────────────────────────────────────────────────────────────

    @app_commands.command(name='slowmode', description='Set channel slowmode (0 to disable)')
    @app_commands.describe(seconds='Slowmode delay in seconds (0–21600)',
                           channel='Target channel (defaults to current)')
    @app_commands.checks.has_permissions(manage_channels=True)
    async def slowmode(self, interaction: discord.Interaction, seconds: int,
                       channel: Optional[discord.TextChannel] = None):
        channel = channel or interaction.channel
        seconds = max(0, min(21600, seconds))
        await channel.edit(slowmode_delay=seconds)
        msg = f'Slowmode **disabled** in {channel.mention}.' if seconds == 0 \
            else f'Slowmode set to **{seconds}s** in {channel.mention}.'
        await interaction.response.send_message(
            embed=embed('✅ Slowmode Updated', msg, discord.Color.blue()))

    # ─── LOCK / UNLOCK ────────────────────────────────────────────────────────

    @app_commands.command(name='lock', description='Lock a channel so members cannot send messages')
    @app_commands.describe(channel='Channel to lock', reason='Reason')
    @app_commands.checks.has_permissions(manage_channels=True)
    async def lock(self, interaction: discord.Interaction,
                   channel: Optional[discord.TextChannel] = None,
                   reason: str = 'No reason provided'):
        channel = channel or interaction.channel
        ow = channel.overwrites_for(interaction.guild.default_role)
        ow.send_messages = False
        await channel.set_permissions(interaction.guild.default_role, overwrite=ow)
        await channel.send(embed=embed('🔒 Channel Locked',
                                       f'**Reason:** {reason}', discord.Color.red()))
        await interaction.response.send_message(
            embed=embed('✅ Locked', f'{channel.mention} has been locked.',
                        discord.Color.red()), ephemeral=True)
        await self._log(interaction.guild, 'LOCK', None, interaction.user, reason,
                        f'Channel: {channel.mention}')

    @app_commands.command(name='unlock', description='Unlock a previously locked channel')
    @app_commands.describe(channel='Channel to unlock', reason='Reason')
    @app_commands.checks.has_permissions(manage_channels=True)
    async def unlock(self, interaction: discord.Interaction,
                     channel: Optional[discord.TextChannel] = None,
                     reason: str = 'No reason provided'):
        channel = channel or interaction.channel
        ow = channel.overwrites_for(interaction.guild.default_role)
        ow.send_messages = None
        await channel.set_permissions(interaction.guild.default_role, overwrite=ow)
        await channel.send(embed=embed('🔓 Channel Unlocked', '', discord.Color.green()))
        await interaction.response.send_message(
            embed=embed('✅ Unlocked', f'{channel.mention} has been unlocked.',
                        discord.Color.green()), ephemeral=True)
        await self._log(interaction.guild, 'UNLOCK', None, interaction.user, reason,
                        f'Channel: {channel.mention}')

    # ─── ROLE ─────────────────────────────────────────────────────────────────

    @app_commands.command(name='role', description='Add or remove a role from a member')
    @app_commands.describe(member='Target member', role='Role to add/remove', action='add or remove')
    @app_commands.choices(action=[
        app_commands.Choice(name='Add', value='add'),
        app_commands.Choice(name='Remove', value='remove'),
    ])
    @app_commands.checks.has_permissions(manage_roles=True)
    async def role(self, interaction: discord.Interaction, member: discord.Member,
                   role: discord.Role, action: str):
        if role >= interaction.guild.me.top_role:
            return await interaction.response.send_message(
                embed=embed('❌ Error', "I can't manage a role higher than my own.",
                            discord.Color.red()), ephemeral=True)

        if action == 'add':
            if role in member.roles:
                return await interaction.response.send_message(
                    embed=embed('❌ Already Has Role', f'{member.mention} already has {role.mention}.',
                                discord.Color.red()), ephemeral=True)
            await member.add_roles(role, reason=f'By {interaction.user}')
            await interaction.response.send_message(
                embed=embed('✅ Role Added', f'Added {role.mention} to {member.mention}.',
                            discord.Color.green()))
        else:
            if role not in member.roles:
                return await interaction.response.send_message(
                    embed=embed('❌ Role Not Found', f"{member.mention} doesn't have {role.mention}.",
                                discord.Color.red()), ephemeral=True)
            await member.remove_roles(role, reason=f'By {interaction.user}')
            await interaction.response.send_message(
                embed=embed('✅ Role Removed', f'Removed {role.mention} from {member.mention}.',
                            discord.Color.green()))

    # ─── NICK ─────────────────────────────────────────────────────────────────

    @app_commands.command(name='nick', description="Change or reset a member's nickname")
    @app_commands.describe(member='Target member', nickname='New nickname (leave empty to reset)')
    @app_commands.checks.has_permissions(manage_nicknames=True)
    async def nick(self, interaction: discord.Interaction, member: discord.Member,
                   nickname: str = None):
        old = member.display_name
        await member.edit(nick=nickname)
        msg = (f'Nickname changed: `{old}` → `{nickname}`' if nickname
               else f'Nickname reset to `{member.name}`')
        await interaction.response.send_message(
            embed=embed('✅ Nickname Updated', msg, discord.Color.blue()))

    # ─── USERINFO ─────────────────────────────────────────────────────────────

    @app_commands.command(name='userinfo', description='Get detailed info about a member')
    @app_commands.describe(member='Target member (defaults to yourself)')
    async def userinfo(self, interaction: discord.Interaction,
                       member: Optional[discord.Member] = None):
        member = member or interaction.user
        warn_count = await self.db.get_warning_count(interaction.guild.id, member.id)
        roles = [r.mention for r in reversed(member.roles[1:])]

        e = discord.Embed(
            title=f'👤 {member.display_name}',
            color=member.color if member.color.value else discord.Color.blurple(),
            timestamp=discord.utils.utcnow()
        )
        e.set_thumbnail(url=member.display_avatar.url)
        e.add_field(name='Username', value=str(member), inline=True)
        e.add_field(name='ID', value=f'`{member.id}`', inline=True)
        e.add_field(name='Bot?', value='🤖 Yes' if member.bot else '👤 No', inline=True)
        e.add_field(name='Joined Server',
                    value=f'<t:{int(member.joined_at.timestamp())}:R>', inline=True)
        e.add_field(name='Account Created',
                    value=f'<t:{int(member.created_at.timestamp())}:R>', inline=True)
        e.add_field(name='⚠️ Warnings', value=str(warn_count), inline=True)
        e.add_field(name=f'Roles ({len(roles)})',
                    value=' '.join(roles[:15]) or 'None', inline=False)
        if member.is_timed_out():
            e.add_field(name='⏱️ Timed Out Until',
                        value=f'<t:{int(member.communication_disabled_until.timestamp())}:R>',
                        inline=False)
        e.set_footer(text=f'Requested by {interaction.user}')
        await interaction.response.send_message(embed=e)

    # ─── SERVERINFO ───────────────────────────────────────────────────────────

    @app_commands.command(name='serverinfo', description='Get information about this server')
    async def serverinfo(self, interaction: discord.Interaction):
        g = interaction.guild
        bots = sum(1 for m in g.members if m.bot)

        e = discord.Embed(
            title=f'📊 {g.name}',
            color=discord.Color.blurple(),
            timestamp=discord.utils.utcnow()
        )
        if g.icon:
            e.set_thumbnail(url=g.icon.url)
        e.add_field(name='Owner', value=g.owner.mention, inline=True)
        e.add_field(name='ID', value=f'`{g.id}`', inline=True)
        e.add_field(name='Created', value=f'<t:{int(g.created_at.timestamp())}:R>', inline=True)
        e.add_field(name='Members',
                    value=f'👥 {g.member_count - bots} humans | 🤖 {bots} bots', inline=True)
        e.add_field(name='Channels',
                    value=f'💬 {len(g.text_channels)} text | 🔊 {len(g.voice_channels)} voice',
                    inline=True)
        e.add_field(name='Roles', value=str(len(g.roles)), inline=True)
        e.add_field(name='Boosts',
                    value=f'Level {g.premium_tier} ({g.premium_subscription_count} boosts)',
                    inline=True)
        e.add_field(name='Verification',
                    value=str(g.verification_level).replace('_', ' ').title(), inline=True)
        e.add_field(name='Emojis', value=f'{len(g.emojis)}/{g.emoji_limit}', inline=True)
        await interaction.response.send_message(embed=e)

    # ─── NOTES ────────────────────────────────────────────────────────────────

    @app_commands.command(name='note', description='Add a private moderator note to a user')
    @app_commands.describe(member='Target member', note='Note content')
    @app_commands.checks.has_permissions(manage_messages=True)
    async def note(self, interaction: discord.Interaction, member: discord.Member, note: str):
        note_id = await self.db.add_note(interaction.guild.id, member.id, interaction.user.id, note)
        await interaction.response.send_message(
            embed=embed('📝 Note Added', f'Note #{note_id} added for {member.mention}.',
                        discord.Color.blue(), {'Note': note}), ephemeral=True)

    @app_commands.command(name='notes', description='View all moderator notes for a user')
    @app_commands.describe(member='Target member')
    @app_commands.checks.has_permissions(manage_messages=True)
    async def notes(self, interaction: discord.Interaction, member: discord.Member):
        notes = await self.db.get_notes(interaction.guild.id, member.id)
        if not notes:
            return await interaction.response.send_message(
                embed=embed('📝 No Notes', f'No notes for {member.mention}.', discord.Color.blue()),
                ephemeral=True)

        e = discord.Embed(title=f'📝 Notes — {member.display_name}',
                          color=discord.Color.blue(), timestamp=discord.utils.utcnow())
        e.set_thumbnail(url=member.display_avatar.url)
        for n in notes[:10]:
            mod = interaction.guild.get_member(n['moderator_id'])
            e.add_field(
                name=f'#{n["id"]} — {n["timestamp"][:10]}',
                value=f'{n["note"]}\n**By:** {mod.mention if mod else "Unknown"}',
                inline=False
            )
        await interaction.response.send_message(embed=e, ephemeral=True)

    # ─── Error Handler ────────────────────────────────────────────────────────

    async def cog_app_command_error(self, interaction: discord.Interaction,
                                    error: app_commands.AppCommandError):
        if isinstance(error, app_commands.MissingPermissions):
            msg = f'You need: `{", ".join(error.missing_permissions)}`'
            await interaction.response.send_message(
                embed=embed('❌ Missing Permissions', msg, discord.Color.red()), ephemeral=True)
        elif isinstance(error, app_commands.BotMissingPermissions):
            msg = f'I need: `{", ".join(error.missing_permissions)}`'
            await interaction.response.send_message(
                embed=embed('❌ Bot Missing Permissions', msg, discord.Color.red()), ephemeral=True)
        else:
            raise error


async def setup(bot: commands.Bot):
    await bot.add_cog(Moderation(bot))
