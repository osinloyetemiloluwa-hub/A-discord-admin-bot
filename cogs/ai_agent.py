"""
cogs/ai_agent.py
Advanced AI Co-Admin with Groq API - Natural Language Command Processing

This cog provides powerful AI-driven Discord server management:
- Natural language command interpretation ("delete nsfw messages")
- Message analysis and content filtering
- Advanced chat with context memory
- Command parsing and execution

Powered by Groq's fast inference API with Llama/Mixtral models.
"""

import discord
from discord.ext import commands
from discord import app_commands
from collections import defaultdict, deque
import asyncio
import re
from utils.groq_client import GroqClient


class AIAgent(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = bot.db
        self.ai = GroqClient()
        # Per-user rolling conversation context (in-memory, last 8 turns)
        self.conversations: dict[int, deque] = defaultdict(lambda: deque(maxlen=8))

    # ─── /ask — Direct AI Question ─────────────────────────────────────────────

    @app_commands.command(name='ask', description='Ask the AI co-admin a question')
    @app_commands.describe(question='What do you want to ask?')
    async def ask(self, interaction: discord.Interaction, question: str):
        await interaction.response.defer()
        history = self.conversations[interaction.user.id]
        answer = await self.ai.chat(question, context=list(history))

        history.append({'role': 'user', 'content': question})
        history.append({'role': 'assistant', 'content': answer})

        e = discord.Embed(
            title='🤖 AI Co-Admin',
            description=answer[:4096],
            color=discord.Color.blurple(),
            timestamp=discord.utils.utcnow()
        )
        e.set_footer(text=f'Asked by {interaction.user}')
        await interaction.followup.send(embed=e)

    # ─── /teco — Natural Language Command Handler ──────────────────────────────
    # This is your main advanced command - interpret natural language and execute actions

    @app_commands.command(
        name='teco',
        description='[ADVANCED] Natural language command - do anything on your server!'
    )
    @app_commands.describe(command='Your natural language command (e.g., "show last 10 messages")')
    async def teco(self, interaction: discord.Interaction, command: str):
        """Advanced natural language command processor."""
        await interaction.response.defer()

        # Build context info for better parsing
        context_info = {
            'server_name': interaction.guild.name if interaction.guild else 'DM',
            'channel_name': interaction.channel.name if interaction.channel else 'Unknown',
            'user_roles': ', '.join([r.name for r in interaction.user.roles]) if interaction.guild else 'N/A'
        }

        # Parse the natural language command
        parsed = await self.ai.parse_command(command, context_info)
        action = parsed.get('action', 'chat')
        params = parsed.get('params', {})
        explanation = parsed.get('explanation', '')
        requires_confirmation = parsed.get('requires_confirmation', False)

        # Log the parsed command
        self.bot.logger.info(f"[TECO] User: {interaction.user} | Command: '{command}' | Action: {action}")

        # Handle different actions
        if action == 'chat':
            # Regular chat response
            answer = await self.ai.chat(command, context=list(self.conversations[interaction.user.id]))
            self.conversations[interaction.user.id].append({'role': 'user', 'content': command})
            self.conversations[interaction.user.id].append({'role': 'assistant', 'content': answer})

            e = discord.Embed(
                title='🤖 TECO - AI Co-Admin',
                description=answer[:4096],
                color=discord.Color.green(),
                timestamp=discord.utils.utcnow()
            )
            e.set_footer(text=f"Interpreted: {explanation} | {interaction.user}")
            await interaction.followup.send(embed=e)

        elif action == 'list_messages':
            # Show recent messages
            amount = params.get('amount', 10)
            await self._list_messages(interaction, amount)

        elif action == 'search_messages':
            # Search for specific messages
            keyword = params.get('keyword', command)
            amount = params.get('amount', 50)
            await self._search_messages(interaction, keyword, amount)

        elif action == 'summarize':
            # Summarize channel
            amount = params.get('amount', 30)
            await self._summarize_channel(interaction, amount)

        elif action == 'purge':
            # Delete messages with optional filters
            await self._purge_filtered(interaction, params)

        elif action == 'info':
            # Get server/user info
            target = params.get('target', 'server')
            if target == 'user':
                await self._user_info(interaction, params)
            else:
                await self._server_info(interaction)

        else:
            # Unknown action - respond as chat
            answer = await self.ai.chat(
                f"The user asked: '{command}'. They wanted to: {explanation}. "
                f"I couldn't execute that as a Discord command. Explain what you understood and suggest alternatives.",
                context=list(self.conversations[interaction.user.id])
            )

            e = discord.Embed(
                title='🤖 TECO - Command Interpretation',
                description=answer[:4096],
                color=discord.Color.orange(),
                timestamp=discord.utils.utcnow()
            )
            e.add_field(name='Parsed Action', value=action, inline=True)
            e.add_field(name='Parameters', value=str(params)[:1024], inline=True)
            await interaction.followup.send(embed=e)

    # ─── /purgecontent — Delete messages by keyword/content ───────────────────

    @app_commands.command(
        name='purgecontent',
        description='Delete messages containing specific content or violations'
    )
    @app_commands.describe(
        amount='Number of messages to check (max 100)',
        keyword='Keyword to search for (optional)',
        filter_type='Content filter: nsfw, spam, harassment, all (optional)'
    )
    @app_commands.checks.has_permissions(manage_messages=True)
    async def purgecontent(
        self,
        interaction: discord.Interaction,
        amount: int = 10,
        keyword: str = None,
        filter_type: str = 'all'
    ):
        """Delete messages containing specific content or AI-detected violations."""
        amount = max(1, min(100, amount))

        # Validate filter_type
        valid_filters = ['nsfw', 'spam', 'harassment', 'all']
        if filter_type not in valid_filters:
            filter_type = 'all'

        await interaction.response.defer(ephemeral=True)

        messages = [m async for m in interaction.channel.history(limit=amount)]
        messages.reverse()  # Oldest first

        deleted_count = 0
        deleted_keywords = []
        ai_violations = []

        for msg in messages:
            if not msg.content or msg.author.bot:
                continue

            should_delete = False
            reason = ""

            # Check keyword match
            if keyword and keyword.lower() in msg.content.lower():
                should_delete = True
                reason = f"Contains keyword: '{keyword}'"
                deleted_keywords.append(msg.id)

            # Check AI content analysis
            if filter_type != 'all' and not should_delete:
                analysis = await self.ai.analyze_message_content(msg.content, filter_type)
                if analysis.get('violation', False):
                    should_delete = True
                    reason = f"AI detected {filter_type}: {analysis.get('reason', 'Unknown')}"
                    ai_violations.append({
                        'author': str(msg.author),
                        'content': msg.content[:100],
                        'reason': reason
                    })

            if should_delete:
                try:
                    await msg.delete()
                    deleted_count += 1
                    await asyncio.sleep(0.3)  # Rate limit protection
                except discord.Forbidden:
                    pass
                except discord.NotFound:
                    pass

        # Create result embed
        e = discord.Embed(
            title='🗑️ Content Purge Complete',
            color=discord.Color.blue(),
            timestamp=discord.utils.utcnow()
        )
        e.add_field(name='Messages Deleted', value=str(deleted_count), inline=True)
        e.add_field(name='Total Checked', value=str(len(messages)), inline=True)

        if keyword:
            e.add_field(name='Keyword Matches', value=str(len(deleted_keywords)), inline=True)

        if ai_violations:
            violation_text = '\n'.join([
                f"• {v['author']}: {v['reason']}"
                for v in ai_violations[:5]
            ])
            e.add_field(name='AI Violations Found', value=violation_text or 'None', inline=False)

        await interaction.followup.send(embed=e, ephemeral=True)

    # ─── /audit — AI-powered server audit ──────────────────────────────────────

    @app_commands.command(
        name='audit',
        description='[ADMIN] Run an AI-powered audit of recent server activity'
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def audit(self, interaction: discord.Interaction, message_count: int = 50):
        """Run a comprehensive audit of server activity using AI analysis."""
        message_count = max(10, min(200, message_count))
        await interaction.response.defer()

        # Collect recent messages from multiple channels
        channels = interaction.guild.text_channels[:5]  # Check first 5 channels
        all_messages = []

        for channel in channels:
            try:
                messages = await channel.history(limit=message_count // len(channels)).flatten()
                all_messages.extend(messages)
            except discord.Forbidden:
                continue

        # Format messages for analysis
        message_text = '\n'.join([
            f"[{channel.name}] {m.author.display_name}: {m.content}"
            for m in sorted(all_messages, key=lambda x: x.created_at, reverse=True)[:100]
            if m.content and not m.author.bot
        ])

        if not message_text:
            return await interaction.followup.send(
                embed=discord.Embed(
                    description='⚠️ No messages found to analyze.',
                    color=discord.Color.red()
                )
            )

        # Generate audit report
        audit_prompt = f"""Perform a comprehensive Discord server audit based on these recent messages.
Server: {interaction.guild.name}
Total messages analyzed: {len(all_messages)}

Messages:
{message_text[:5000]}

Provide an audit report with:
1. Overall server health (1-10)
2. Common issues detected
3. User behavior patterns
4. Recommendations for improvement
5. Any red flags (toxicity, spam, conflicts)

Format your response clearly with sections."""

        report = await self.ai.chat(audit_prompt)

        e = discord.Embed(
            title=f'📊 Server Audit Report - {interaction.guild.name}',
            description=report[:4096],
            color=discord.Color.purple(),
            timestamp=discord.utils.utcnow()
        )
        e.set_footer(text=f'Audit of {len(all_messages)} messages | {interaction.user}')

        await interaction.followup.send(embed=e)

    # ─── /scan — Scan users/messages for issues ─────────────────────────────────

    @app_commands.command(
        name='scan',
        description='[ADMIN] Scan server for potential issues (new accounts, no avatar, etc.)'
    )
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.choices(scan_type=[
        app_commands.Choice(name='New Accounts', value='new_accounts'),
        app_commands.Choice(name='No Avatar', value='no_avatar'),
        app_commands.Choice(name='Incomplete Profiles', value='incomplete'),
        app_commands.Choice(name='Bot Accounts', value='bots'),
        app_commands.Choice(name='Inactive Members', value='inactive'),
    ])
    async def scan(
        self,
        interaction: discord.Interaction,
        scan_type: str,
        days_threshold: int = 7
    ):
        """Scan server members for various issues."""
        await interaction.response.defer(ephemeral=True)

        members = [m for m in interaction.guild.members if not m.bot]
        results = []

        if scan_type == 'new_accounts':
            for member in members:
                age = (discord.utils.utcnow() - member.created_at).days
                if age <= days_threshold:
                    results.append(f"{member.mention} - Account age: {age} days")

        elif scan_type == 'no_avatar':
            for member in members:
                if member.display_avatar.is_default():
                    results.append(f"{member.mention} - No custom avatar")

        elif scan_type == 'incomplete':
            for member in members:
                flags = []
                if member.display_avatar.is_default():
                    flags.append('No avatar')
                if not member.bio:
                    flags.append('No bio')
                if flags:
                    results.append(f"{member.mention} - {', '.join(flags)}")

        elif scan_type == 'bots':
            bots = [m for m in interaction.guild.members if m.bot]
            results = [f"{bot.mention} (Added: {bot.created_at.strftime('%Y-%m-%d')})"
                      for bot in bots[:20]]

        # Create results embed
        e = discord.Embed(
            title=f'🔍 Scan Results: {scan_type.replace("_", " ").title()}',
            color=discord.Color.teal(),
            timestamp=discord.utils.utcnow()
        )

        if results:
            # Paginate if many results
            page_size = 20
            total_pages = (len(results) + page_size - 1) // page_size

            if total_pages == 1:
                e.description = '\n'.join(results[:50])
            else:
                e.description = '\n'.join(results[:20])
                e.set_footer(text=f'Showing 1-{min(20, len(results))} of {len(results)} | Page 1/{total_pages}')
        else:
            e.description = '✅ No issues found!'

        await interaction.followup.send(embed=e, ephemeral=True)

    # ─── /search — Search messages with AI assistance ─────────────────────────

    @app_commands.command(
        name='search',
        description='Search recent messages with AI-powered filtering'
    )
    @app_commands.describe(
        query='Search query or keyword',
        amount='Number of messages to search (max 100)'
    )
    async def search(self, interaction: discord.Interaction, query: str, amount: int = 50):
        """Search recent messages with keyword and optional AI analysis."""
        amount = max(10, min(100, amount))
        await interaction.response.defer()

        messages = [m async for m in interaction.channel.history(limit=amount)]
        matches = []

        for msg in messages:
            if not msg.content or msg.author.bot:
                continue

            # Simple keyword matching
            if query.lower() in msg.content.lower():
                matches.append(msg)

        # Create results embed
        e = discord.Embed(
            title=f'🔍 Search Results for "{query}"',
            description=f'Found {len(matches)} matching messages in the last {amount} messages.',
            color=discord.Color.blue(),
            timestamp=discord.utils.utcnow()
        )

        if matches:
            # Show first 10 matches
            for msg in matches[:10]:
                timestamp = msg.created_at.strftime('%m/%d %H:%M')
                content = msg.content[:100] + ('...' if len(msg.content) > 100 else '')
                e.add_field(
                    name=f'{msg.author.display_name} • {timestamp}',
                    value=content,
                    inline=False
                )

            if len(matches) > 10:
                e.set_footer(text=f'Showing 10 of {len(matches)} matches')
        else:
            e.description = f'⚠️ No messages found matching "{query}"'

        await interaction.followup.send(embed=e)

    # ─── /toxicityscan — AI scan for toxic content ─────────────────────────────

    @app_commands.command(
        name='toxicityscan',
        description='[MOD] AI scan channel for toxic/inappropriate content'
    )
    @app_commands.describe(amount='Number of messages to scan (max 100)')
    @app_commands.checks.has_permissions(manage_messages=True)
    async def toxicityscan(self, interaction: discord.Interaction, amount: int = 50):
        """Scan recent messages for toxic content using AI."""
        amount = max(10, min(100, amount))
        await interaction.response.defer()

        messages = [m async for m in interaction.channel.history(limit=amount)]
        results = []
        clean_count = 0

        for msg in messages:
            if not msg.content or msg.author.bot:
                continue

            toxicity = await self.ai.check_toxicity(msg.content)

            if toxicity.get('is_toxic', False) or toxicity.get('score', 0) > 0.6:
                results.append({
                    'author': msg.author.mention,
                    'content': msg.content[:150],
                    'score': toxicity.get('score', 0),
                    'label': toxicity.get('label', 'unknown'),
                    'reason': toxicity.get('reason', '')
                })
            else:
                clean_count += 1

        # Create results embed
        e = discord.Embed(
            title='🔍 Toxicity Scan Results',
            description=f'Scanned {len(messages)} messages in #{interaction.channel.name}',
            color=discord.Color.red() if results else discord.Color.green(),
            timestamp=discord.utils.utcnow()
        )

        e.add_field(name='Clean Messages', value=str(clean_count), inline=True)
        e.add_field(name='Flagged Messages', value=str(len(results)), inline=True)

        if results:
            flagged_text = '\n'.join([
                f"**{r['author']}** (Score: {r['score']:.0%}) - {r['reason'][:80]}"
                for r in results[:10]
            ])
            e.add_field(name='Flagged Content', value=flagged_text or 'None', inline=False)

        await interaction.followup.send(embed=e)

    # ─── /clearcontext ─────────────────────────────────────────────────────────

    @app_commands.command(name='clearcontext', description='Clear your AI conversation memory')
    async def clearcontext(self, interaction: discord.Interaction):
        self.conversations[interaction.user.id].clear()
        await interaction.response.send_message(
            embed=discord.Embed(
                description='🧹 Your AI conversation context has been cleared.',
                color=discord.Color.green()
            ),
            ephemeral=True
        )

    # ─── /toxcheck — Manual toxicity check ────────────────────────────────────

    @app_commands.command(name='toxcheck', description='Check a message for toxicity using AI')
    @app_commands.describe(text='Text to analyze')
    @app_commands.checks.has_permissions(manage_messages=True)
    async def toxcheck(self, interaction: discord.Interaction, text: str):
        await interaction.response.defer(ephemeral=True)
        result = await self.ai.check_toxicity(text)

        color = discord.Color.red() if result['is_toxic'] else discord.Color.green()
        e = discord.Embed(
            title='🔍 Toxicity Analysis',
            color=color,
            timestamp=discord.utils.utcnow()
        )
        e.add_field(name='Text', value=f'```{text[:500]}```', inline=False)
        e.add_field(name='Classification', value=result['label'], inline=True)
        e.add_field(name='Confidence', value=f"{result['score'] * 100:.1f}%", inline=True)
        e.add_field(name='Verdict', value='⚠️ Toxic' if result['is_toxic'] else '✅ Clean', inline=True)
        if result.get('reason'):
            e.add_field(name='Reason', value=result['reason'], inline=False)
        await interaction.followup.send(embed=e, ephemeral=True)

    # ─── /summarize — Summarize recent channel activity ────────────────────────

    @app_commands.command(name='summarize', description='AI-summarize recent messages in this channel')
    @app_commands.describe(amount='Number of recent messages to summarize (max 100)')
    async def summarize(self, interaction: discord.Interaction, amount: int = 30):
        await self._summarize_channel(interaction, amount)

    # ─── /moderate — AI judges if text breaks rules ────────────────────────────

    @app_commands.command(name='moderate', description='Ask the AI if a message would violate server rules')
    @app_commands.describe(text='Text to evaluate')
    @app_commands.checks.has_permissions(manage_messages=True)
    async def moderate(self, interaction: discord.Interaction, text: str):
        await interaction.response.defer(ephemeral=True)
        verdict = await self.ai.moderation_verdict(text)
        tox = await self.ai.check_toxicity(text)

        e = discord.Embed(
            title='⚖️ AI Moderation Review',
            color=discord.Color.gold(),
            timestamp=discord.utils.utcnow()
        )
        e.add_field(name='Message', value=f'```{text[:500]}```', inline=False)
        e.add_field(name='AI Verdict', value=verdict, inline=False)
        e.add_field(name='Toxicity Score', value=f"{tox['label']} ({tox['score']*100:.1f}%)", inline=False)
        await interaction.followup.send(embed=e, ephemeral=True)

    # ─── @Mention Conversational Handler ──────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        if self.bot.user not in message.mentions:
            return

        content = message.content.replace(f'<@{self.bot.user.id}>', '').strip()
        content = content.replace(f'<@!{self.bot.user.id}>', '').strip()
        if not content:
            return await message.reply(
                "Hi! Ask me something, e.g. `@bot what's the rule on spam?`\n"
                "Or use `/teco` for advanced commands like `/teco delete last 5 messages containing nsfw`",
                mention_author=False
            )

        async with message.channel.typing():
            history = self.conversations[message.author.id]
            answer = await self.ai.chat(content, context=list(history))
            history.append({'role': 'user', 'content': content})
            history.append({'role': 'assistant', 'content': answer})

        await message.reply(answer[:2000], mention_author=False)

    # ─── Helper Methods ───────────────────────────────────────────────────────

    async def _list_messages(self, interaction: discord.Interaction, amount: int):
        """List recent messages from a channel."""
        amount = max(5, min(50, amount))
        messages = [m async for m in interaction.channel.history(limit=amount)]
        messages.reverse()

        e = discord.Embed(
            title=f'📜 Last {len(messages)} Messages in #{interaction.channel.name}',
            color=discord.Color.blurple(),
            timestamp=discord.utils.utcnow()
        )

        for msg in messages:
            if msg.content:
                timestamp = msg.created_at.strftime('%m/%d %H:%M')
                content = msg.content[:200] + ('...' if len(msg.content) > 200 else '')
                e.add_field(
                    name=f'{msg.author.display_name} • {timestamp}',
                    value=content,
                    inline=False
                )

        await interaction.followup.send(embed=e)

    async def _search_messages(self, interaction: discord.Interaction, keyword: str, amount: int):
        """Search for messages containing a keyword."""
        amount = max(10, min(100, amount))

        query_result = await self.ai.generate_search_query(keyword)
        search_keyword = query_result.get('keyword', keyword)

        messages = [m async for m in interaction.channel.history(limit=amount)]
        matches = [m for m in messages if search_keyword.lower() in m.content.lower()]

        e = discord.Embed(
            title=f'🔍 Search: "{search_keyword}"',
            description=f'Found {len(matches)} messages matching "{search_keyword}"',
            color=discord.Color.blue(),
            timestamp=discord.utils.utcnow()
        )

        if matches:
            for msg in matches[:15]:
                timestamp = msg.created_at.strftime('%m/%d %H:%M')
                content = msg.content[:150] + ('...' if len(msg.content) > 150 else '')
                e.add_field(
                    name=f'{msg.author.display_name} • {timestamp}',
                    value=content,
                    inline=False
                )
        else:
            e.description = f'⚠️ No messages found matching "{search_keyword}"'

        await interaction.followup.send(embed=e)

    async def _summarize_channel(self, interaction: discord.Interaction, amount: int):
        """Summarize recent channel activity."""
        amount = max(5, min(100, amount))
        messages = [m async for m in interaction.channel.history(limit=amount)]
        messages.reverse()

        text_block = '\n'.join(
            f'{m.author.display_name}: {m.content}'
            for m in messages if m.content and not m.author.bot
        )

        if not text_block.strip():
            return await interaction.followup.send(
                embed=discord.Embed(
                    description='⚠️ No text content found to summarize.',
                    color=discord.Color.red()
                )
            )

        summary = await self.ai.summarize(text_block)
        e = discord.Embed(
            title=f'📋 Summary of Last {amount} Messages',
            description=summary,
            color=discord.Color.teal(),
            timestamp=discord.utils.utcnow()
        )
        e.set_footer(text=f'Channel: #{interaction.channel.name}')
        await interaction.followup.send(embed=e)

    async def _purge_filtered(self, interaction: discord.Interaction, params: dict):
        """Handle natural language purge requests."""
        keyword = params.get('keyword')
        amount = params.get('amount', 10)
        member_id = params.get('member_id')

        if not interaction.user.guild_permissions.manage_messages:
            return await interaction.followup.send(
                embed=discord.Embed(
                    description='❌ You need "Manage Messages" permission for this.',
                    color=discord.Color.red()
                ),
                ephemeral=True
            )

        await interaction.followup.send(
            embed=discord.Embed(
                description=f'🔄 Processing purge request...\n'
                           f'Keyword: `{keyword or "None"}`\n'
                           f'Amount: {amount}',
                color=discord.Color.orange()
            ),
            ephemeral=True
        )

        # Redirect to proper purge
        if keyword:
            await self.purgecontent(
                interaction,
                amount=amount,
                keyword=keyword,
                filter_type='all'
            )
        else:
            await self.purgecontent(
                interaction,
                amount=amount,
                keyword=None,
                filter_type='all'
            )

    async def _user_info(self, interaction: discord.Interaction, params: dict):
        """Get user information."""
        # This would need member_id parsing - simplified for now
        e = discord.Embed(
            title='👤 User Info',
            description='Use `/userinfo` command to get detailed user information.',
            color=discord.Color.blurple()
        )
        await interaction.followup.send(embed=e)

    async def _server_info(self, interaction: discord.Interaction):
        """Get server information."""
        if not interaction.guild:
            return

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
        e.add_field(name='Members', value=f'👥 {g.member_count - bots} | 🤖 {bots}', inline=True)
        e.add_field(name='Channels', value=f'💬 {len(g.text_channels)} | 🔊 {len(g.voice_channels)}', inline=True)
        e.add_field(name='Roles', value=str(len(g.roles)), inline=True)
        await interaction.followup.send(embed=e)

    # ─── Error Handler ───────────────────────────────────────────────────────

    async def cog_app_command_error(self, interaction: discord.Interaction, error):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message(
                embed=discord.Embed(
                    description='❌ Missing permissions for this command.',
                    color=discord.Color.red()
                ),
                ephemeral=True
            )
        else:
            raise error


async def setup(bot: commands.Bot):
    await bot.add_cog(AIAgent(bot))
