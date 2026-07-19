"""
cogs/welcome.py
Welcome/farewell messages with placeholder support, plus auto-role assignment
for new members.
"""

import discord
from discord.ext import commands
from discord import app_commands
from typing import Optional


DEFAULT_WELCOME = "👋 Welcome {mention} to **{server}**! You're member #{count}."
DEFAULT_FAREWELL = "👋 **{user}** has left **{server}**. We now have {count} members."


def render(template: str, member: discord.Member) -> str:
    return (template
            .replace('{mention}', member.mention)
            .replace('{user}', str(member))
            .replace('{name}', member.display_name)
            .replace('{server}', member.guild.name)
            .replace('{count}', str(member.guild.member_count)))


class Welcome(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = bot.db

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        config = await self.db.get_guild_config(member.guild.id)

        # Auto-role
        role_id = config.get('auto_role')
        if role_id:
            role = member.guild.get_role(role_id)
            if role:
                try:
                    await member.add_roles(role, reason='Auto-role on join')
                except discord.Forbidden:
                    pass

        # Welcome message
        ch_id = config.get('welcome_channel')
        if not ch_id:
            return
        channel = member.guild.get_channel(ch_id)
        if not channel:
            return

        template = config.get('welcome_message') or DEFAULT_WELCOME
        text = render(template, member)

        e = discord.Embed(description=text, color=discord.Color.green(),
                          timestamp=discord.utils.utcnow())
        e.set_thumbnail(url=member.display_avatar.url)
        e.set_footer(text=f'Member #{member.guild.member_count}')
        await channel.send(embed=e)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        config = await self.db.get_guild_config(member.guild.id)
        ch_id = config.get('welcome_channel')
        if not ch_id:
            return
        channel = member.guild.get_channel(ch_id)
        if not channel:
            return

        template = config.get('farewell_message') or DEFAULT_FAREWELL
        text = render(template, member)

        e = discord.Embed(description=text, color=discord.Color.dark_grey(),
                          timestamp=discord.utils.utcnow())
        e.set_thumbnail(url=member.display_avatar.url)
        await channel.send(embed=e)

    # ─── Slash Commands ─────────────────────────────────────────────────────────

    welcome_group = app_commands.Group(name='welcome', description='Configure welcome/farewell messages')

    @welcome_group.command(name='setchannel', description='Set the welcome/farewell channel')
    @app_commands.checks.has_permissions(administrator=True)
    async def setchannel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await self.db.update_guild_config(interaction.guild.id, welcome_channel=channel.id)
        await interaction.response.send_message(f'✅ Welcome channel set to {channel.mention}.')

    @welcome_group.command(name='setmessage', description='Set the welcome message template')
    @app_commands.describe(template='Use {mention} {name} {server} {count} as placeholders')
    @app_commands.checks.has_permissions(administrator=True)
    async def setmessage(self, interaction: discord.Interaction, template: str):
        await self.db.update_guild_config(interaction.guild.id, welcome_message=template)
        await interaction.response.send_message(
            embed=discord.Embed(title='✅ Welcome Message Set',
                                description=f'Preview:\n{render(template, interaction.user)}',
                                color=discord.Color.green()))

    @welcome_group.command(name='setfarewell', description='Set the farewell message template')
    @app_commands.describe(template='Use {user} {name} {server} {count} as placeholders')
    @app_commands.checks.has_permissions(administrator=True)
    async def setfarewell(self, interaction: discord.Interaction, template: str):
        await self.db.update_guild_config(interaction.guild.id, farewell_message=template)
        await interaction.response.send_message(
            embed=discord.Embed(title='✅ Farewell Message Set',
                                description=f'Preview:\n{render(template, interaction.user)}',
                                color=discord.Color.green()))

    @welcome_group.command(name='test', description='Preview the welcome message')
    async def test(self, interaction: discord.Interaction):
        config = await self.db.get_guild_config(interaction.guild.id)
        template = config.get('welcome_message') or DEFAULT_WELCOME
        await interaction.response.send_message(
            embed=discord.Embed(description=render(template, interaction.user),
                                color=discord.Color.green()))

    @app_commands.command(name='autorole', description='Set a role to auto-assign to new members')
    @app_commands.describe(role='Role to assign (omit to disable)')
    @app_commands.checks.has_permissions(administrator=True)
    async def autorole(self, interaction: discord.Interaction, role: Optional[discord.Role] = None):
        await self.db.update_guild_config(interaction.guild.id, auto_role=role.id if role else None)
        msg = f'✅ Auto-role set to {role.mention}.' if role else '✅ Auto-role disabled.'
        await interaction.response.send_message(msg)

    async def cog_app_command_error(self, interaction: discord.Interaction, error):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message(
                '❌ You need Administrator permission for this.', ephemeral=True)
        else:
            raise error


async def setup(bot: commands.Bot):
    await bot.add_cog(Welcome(bot))
