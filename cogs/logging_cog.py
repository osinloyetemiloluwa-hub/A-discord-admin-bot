"""
cogs/logging_cog.py
Server event logging: message edits/deletes, member updates, role changes,
channel changes, voice activity — all to a configurable log channel.
"""

import discord
from discord.ext import commands
from discord import app_commands


class LoggingCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = bot.db

    async def _get_log_channel(self, guild: discord.Guild):
        config = await self.db.get_guild_config(guild.id)
        ch_id = config.get('log_channel')
        return guild.get_channel(ch_id) if ch_id else None

    # ─── /logs setchannel & disable ─────────────────────────────────────────────

    logs_group = app_commands.Group(name='logs', description='Configure server event logging')

    @logs_group.command(name='setchannel', description='Set the channel for event logs')
    @app_commands.checks.has_permissions(administrator=True)
    async def setchannel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await self.db.update_guild_config(interaction.guild.id, log_channel=channel.id)
        await interaction.response.send_message(
            embed=discord.Embed(description=f'✅ Logs will now be sent to {channel.mention}.',
                                color=discord.Color.green()))

    @logs_group.command(name='disable', description='Disable event logging')
    @app_commands.checks.has_permissions(administrator=True)
    async def disable(self, interaction: discord.Interaction):
        await self.db.update_guild_config(interaction.guild.id, log_channel=None)
        await interaction.response.send_message(
            embed=discord.Embed(description='✅ Logging disabled.', color=discord.Color.green()))

    # ─── Message Events ─────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        channel = await self._get_log_channel(message.guild)
        if not channel:
            return

        e = discord.Embed(
            title='🗑️ Message Deleted',
            description=message.content[:1000] if message.content else '*[No text content]*',
            color=discord.Color.red(), timestamp=discord.utils.utcnow()
        )
        e.set_author(name=str(message.author), icon_url=message.author.display_avatar.url)
        e.add_field(name='Channel', value=message.channel.mention, inline=True)
        e.add_field(name='Author ID', value=f'`{message.author.id}`', inline=True)
        if message.attachments:
            e.add_field(name='Attachments', value=str(len(message.attachments)), inline=True)
        await channel.send(embed=e)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if before.author.bot or not before.guild or before.content == after.content:
            return
        channel = await self._get_log_channel(before.guild)
        if not channel:
            return

        e = discord.Embed(title='✏️ Message Edited', color=discord.Color.orange(),
                          timestamp=discord.utils.utcnow())
        e.set_author(name=str(before.author), icon_url=before.author.display_avatar.url)
        e.add_field(name='Before', value=before.content[:500] or '*[empty]*', inline=False)
        e.add_field(name='After', value=after.content[:500] or '*[empty]*', inline=False)
        e.add_field(name='Channel', value=before.channel.mention, inline=True)
        e.add_field(name='Jump', value=f'[Go to message]({after.jump_url})', inline=True)
        await channel.send(embed=e)

    # ─── Member Events ─────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        channel = await self._get_log_channel(member.guild)
        if not channel:
            return
        e = discord.Embed(title='📤 Member Left', color=discord.Color.dark_grey(),
                          timestamp=discord.utils.utcnow())
        e.set_author(name=str(member), icon_url=member.display_avatar.url)
        e.add_field(name='Joined At',
                    value=f'<t:{int(member.joined_at.timestamp())}:R>' if member.joined_at else 'Unknown',
                    inline=True)
        e.add_field(name='Member Count', value=str(member.guild.member_count), inline=True)
        await channel.send(embed=e)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        channel = await self._get_log_channel(before.guild)
        if not channel:
            return

        if before.nick != after.nick:
            e = discord.Embed(title='📝 Nickname Changed', color=discord.Color.blue(),
                              timestamp=discord.utils.utcnow())
            e.set_author(name=str(after), icon_url=after.display_avatar.url)
            e.add_field(name='Before', value=before.nick or before.name, inline=True)
            e.add_field(name='After', value=after.nick or after.name, inline=True)
            await channel.send(embed=e)

        if before.roles != after.roles:
            added = set(after.roles) - set(before.roles)
            removed = set(before.roles) - set(after.roles)
            if added or removed:
                e = discord.Embed(title='🎭 Roles Updated', color=discord.Color.purple(),
                                  timestamp=discord.utils.utcnow())
                e.set_author(name=str(after), icon_url=after.display_avatar.url)
                if added:
                    e.add_field(name='Added', value=', '.join(r.mention for r in added), inline=False)
                if removed:
                    e.add_field(name='Removed', value=', '.join(r.mention for r in removed), inline=False)
                await channel.send(embed=e)

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.User):
        pass  # handled in moderation.py to avoid duplicate logs

    # ─── Channel Events ─────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel):
        log_channel = await self._get_log_channel(channel.guild)
        if not log_channel:
            return
        e = discord.Embed(title='➕ Channel Created', description=f'{channel.mention} (`{channel.type}`)',
                          color=discord.Color.green(), timestamp=discord.utils.utcnow())
        await log_channel.send(embed=e)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel):
        log_channel = await self._get_log_channel(channel.guild)
        if not log_channel:
            return
        e = discord.Embed(title='➖ Channel Deleted', description=f'#{channel.name} (`{channel.type}`)',
                          color=discord.Color.red(), timestamp=discord.utils.utcnow())
        await log_channel.send(embed=e)

    # ─── Voice Events ───────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member,
                                    before: discord.VoiceState, after: discord.VoiceState):
        channel = await self._get_log_channel(member.guild)
        if not channel:
            return

        if before.channel != after.channel:
            if before.channel is None:
                desc = f'{member.mention} joined 🔊 {after.channel.name}'
                color = discord.Color.green()
            elif after.channel is None:
                desc = f'{member.mention} left 🔊 {before.channel.name}'
                color = discord.Color.red()
            else:
                desc = f'{member.mention} moved {before.channel.name} ➜ {after.channel.name}'
                color = discord.Color.blue()

            e = discord.Embed(description=desc, color=color, timestamp=discord.utils.utcnow())
            await channel.send(embed=e)

    async def cog_app_command_error(self, interaction: discord.Interaction, error):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message(
                '❌ You need Administrator permission for this.', ephemeral=True)
        else:
            raise error


async def setup(bot: commands.Bot):
    await bot.add_cog(LoggingCog(bot))
