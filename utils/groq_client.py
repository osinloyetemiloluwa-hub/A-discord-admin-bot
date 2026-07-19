"""
utils/groq_client.py
Async wrapper around the Groq API (api.groq.com),
using a key from console.groq.com. Handles chat, summarization,
toxicity/moderation judgment, and natural language command interpretation.

Replaces the previous Gemini client with full Groq API integration.
Supports Llama, Mixtral, and other Groq-hosted models.
"""

import aiohttp
import os
import json
import logging
import re
from typing import Optional

logger = logging.getLogger('CoAdminBot.Groq')

GROQ_API_KEY = os.getenv('GROQ_API_KEY')
GROQ_MODEL = os.getenv('GROQ_MODEL', 'llama-3.3-70b-versatile')
GROQ_API_BASE = 'https://api.groq.com/openai/v1'

# System prompt for the AI co-admin
SYSTEM_PROMPT = """You are an advanced AI co-admin assistant for a Discord server. You have full access to moderation tools and can interpret natural language commands to perform server management actions.

Your capabilities include:
- Viewing recent messages and chat history
- Deleting messages (with content filtering like NSFW, spam, etc.)
- Moderation actions (kick, ban, mute, warn)
- Server information queries
- User management (roles, nicknames, etc.)
- Auto-moderation analysis

When users ask you to perform actions, you should:
1. First understand what they want in natural language
2. Parse their request into a structured command
3. Provide clear feedback about what you're doing

Always be helpful, concise, and enforce community guidelines.
Keep responses under 200 words unless asked for detail.
You can analyze message content for violations, summarize conversations, and more.

IMPORTANT: When users ask you to delete messages containing certain content:
1. Use the /purge command with appropriate filters
2. Or use /purgecontent to delete messages with specific keywords

Format your responses clearly with Discord-friendly formatting.
"""


class GroqClient:
    def __init__(self):
        if not GROQ_API_KEY:
            logger.warning('GROQ_API_KEY is not set — AI commands will fail.')
        self.headers = {
            'Authorization': f'Bearer {GROQ_API_KEY}',
            'Content-Type': 'application/json'
        }

    async def _chat_completion(
        self,
        messages: list,
        model: str = None,
        max_tokens: int = 400,
        temperature: float = 0.7
    ) -> str:
        """Make a chat completion request to the Groq API."""
        if model is None:
            model = GROQ_MODEL

        url = f'{GROQ_API_BASE}/chat/completions'
        payload = {
            'model': model,
            'messages': messages,
            'max_tokens': max_tokens,
            'temperature': temperature
        }

        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(
                    url,
                    headers=self.headers,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=30)
                ) as resp:
                    data = await resp.json()

                    if resp.status != 200:
                        error_msg = data.get('error', {}).get('message', str(data))
                        logger.error(f'Groq API error ({resp.status}): {error_msg}')
                        raise RuntimeError(error_msg)

                    try:
                        return data['choices'][0]['message']['content'].strip()
                    except (KeyError, IndexError):
                        logger.error(f'Unexpected Groq response shape: {data}')
                        return "I couldn't generate a response for that."

            except aiohttp.ClientError as e:
                logger.error(f'Groq API connection error: {e}')
                raise RuntimeError(f"Connection error: {e}")

    # ─── Chat ─────────────────────────────────────────────────────────────────

    async def chat(self, prompt: str, context: list = None, max_tokens: int = 300) -> str:
        """
        `context` is a list of {'role': 'user'|'assistant', 'content': str}.
        Groq uses standard OpenAI-style roles.
        """
        messages = [{'role': 'system', 'content': SYSTEM_PROMPT}]

        if context:
            for turn in context[-8:]:  # Keep last 8 turns for better context
                messages.append({
                    'role': turn['role'],
                    'content': turn['content']
                })

        messages.append({'role': 'user', 'content': prompt})

        try:
            return await self._chat_completion(messages, max_tokens=max_tokens)
        except RuntimeError as e:
            return f'⚠️ AI error: {e}'

    # ─── Summarization ────────────────────────────────────────────────────────

    async def summarize(self, text: str, max_words: int = 120) -> str:
        if len(text.split()) < 30:
            return text

        prompt = (
            f"Summarize the following Discord chat log in no more than {max_words} words. "
            f"Focus on topics discussed, key decisions, and any action items. "
            f"Do not include usernames unless essential for context.\n\n{text[:6000]}"
        )

        messages = [
            {'role': 'system', 'content': 'You are a concise summarizer. Provide brief, accurate summaries.'},
            {'role': 'user', 'content': prompt}
        ]

        try:
            return await self._chat_completion(messages, max_tokens=300, temperature=0.3)
        except RuntimeError as e:
            return f'⚠️ Summarization error: {e}'

    # ─── Toxicity / Moderation Judgment ─────────────────────────────────────────

    async def check_toxicity(self, text: str) -> dict:
        """
        Asks Groq to classify text for Discord community guideline violations.
        Returns {'label': str, 'score': float, 'is_toxic': bool, 'reason': str}.
        """
        prompt = (
            'Analyze this Discord message for community guideline violations. '
            'Consider: harassment, hate speech, threats, sexual content, severe profanity, spam. '
            'Respond with ONLY a valid JSON object, no markdown, no extra text:\n'
            '{"label": "clean|toxic", "score": 0.0_to_1.0, "reason": "brief explanation"}\n\n'
            f'Message: "{text}"'
        )

        messages = [
            {'role': 'system', 'content': 'You analyze messages for toxicity. Always respond with valid JSON only.'},
            {'role': 'user', 'content': prompt}
        ]

        try:
            raw = await self._chat_completion(messages, max_tokens=100, temperature=0.0)

            # Clean the response
            cleaned = raw.strip()
            # Remove markdown code blocks if present
            cleaned = re.sub(r'^```json\s*', '', cleaned, flags=re.IGNORECASE)
            cleaned = re.sub(r'^```\s*', '', cleaned)
            cleaned = re.sub(r'\s*```$', '', cleaned)
            cleaned = cleaned.strip()

            parsed = json.loads(cleaned)
            label = parsed.get('label', 'unknown')
            score = float(parsed.get('score', 0.0))

            return {
                'label': label,
                'score': round(score, 3),
                'is_toxic': label == 'toxic' and score > 0.5,
                'reason': parsed.get('reason', '')
            }
        except (RuntimeError, json.JSONDecodeError, ValueError, KeyError) as e:
            logger.error(f'Toxicity check failed to parse: {e} | Raw: {raw if "raw" in dir() else "N/A"}')
            return {'label': 'error', 'score': 0.0, 'is_toxic': False, 'reason': 'Analysis failed'}

    # ─── Moderation Verdict ─────────────────────────────────────────────────────

    async def moderation_verdict(self, text: str) -> str:
        prompt = (
            f'A Discord moderator wants to know if this message violates typical '
            f'community guidelines. Consider harassment, hate speech, spam, NSFW content, threats. '
            f'Message: "{text}"\n'
            f'Respond with a clear verdict (SAFE/BORDERLINE/VIOLATION) and a one-sentence reason.'
        )

        messages = [
            {'role': 'system', 'content': 'You are a Discord moderation expert. Be clear and decisive.'},
            {'role': 'user', 'content': prompt}
        ]

        try:
            return await self._chat_completion(messages, max_tokens=80, temperature=0.0)
        except RuntimeError as e:
            return f'⚠️ AI error: {e}'

    # ─── Natural Language Command Parser ─────────────────────────────────────────

    async def parse_command(self, user_input: str, context_info: dict = None) -> dict:
        """
        Parse natural language input into structured bot commands.
        Returns {'action': str, 'params': dict, 'explanation': str}
        """
        context_prompt = ""
        if context_info:
            context_prompt = f"\n\nAdditional context available:\n"
            if 'recent_messages' in context_info:
                context_prompt += f"- Recent messages: {context_info['recent_messages'][:500]}...\n"
            if 'server_name' in context_info:
                context_prompt += f"- Server: {context_info['server_name']}\n"
            if 'channel_name' in context_info:
                context_prompt += f"- Current channel: {context_info['channel_name']}\n"
            if 'user_roles' in context_info:
                context_prompt += f"- Your roles: {context_info['user_roles']}\n"

        prompt = f"""Analyze this Discord command and determine what action the user wants.
{context_prompt}

User input: "{user_input}"

Respond with ONLY valid JSON (no markdown):
{{
    "action": "action_name or 'chat'",
    "params": {{"param1": "value1", ...}},
    "explanation": "Brief explanation of what you understood",
    "requires_confirmation": true_or_false
}}

Available actions:
- purge: Delete messages (params: amount, member, keyword, channel)
- kick: Kick a member (params: member_id, reason)
- ban: Ban a member (params: member_id, reason, delete_days)
- mute: Mute a member (params: member_id, duration, reason)
- warn: Warn a member (params: member_id, reason)
- role: Manage roles (params: member_id, role_action, role_name)
- nick: Change nickname (params: member_id, new_nickname)
- info: Get information (params: target: 'user'|'server'|'channel')
- list_messages: Get recent messages (params: amount, channel)
- summarize: Summarize channel (params: amount)
- chat: General conversation (no action needed)
- search_messages: Search for messages (params: keyword, amount)

If the user is just chatting or asking questions, use action "chat" with no params.
"""

        messages = [
            {'role': 'system', 'content': 'You parse Discord commands into structured JSON. Always respond with valid JSON only.'},
            {'role': 'user', 'content': prompt}
        ]

        try:
            raw = await self._chat_completion(messages, max_tokens=200, temperature=0.0)

            # Clean the response
            cleaned = raw.strip()
            cleaned = re.sub(r'^```json\s*', '', cleaned, flags=re.IGNORECASE)
            cleaned = re.sub(r'^```\s*', '', cleaned)
            cleaned = re.sub(r'\s*```$', '', cleaned)
            cleaned = cleaned.strip()

            return json.loads(cleaned)
        except (RuntimeError, json.JSONDecodeError) as e:
            logger.error(f'Command parsing failed: {e}')
            return {
                'action': 'chat',
                'params': {},
                'explanation': 'I had trouble understanding that command.',
                'requires_confirmation': False
            }

    # ─── Message Content Analysis ───────────────────────────────────────────────

    async def analyze_message_content(self, text: str, filter_type: str = 'all') -> dict:
        """
        Analyze message content for specific filter types.
        filter_type: 'nsfw', 'spam', 'harassment', 'all'
        """
        filter_prompts = {
            'nsfw': 'NSFW/inappropriate content including adult content, gore, or explicit material',
            'spam': 'Spam including repeated messages, promotional content, or fake links',
            'harassment': 'Harassment, bullying, hate speech, or threatening language',
            'all': 'Any content that violates Discord Terms of Service or community guidelines'
        }

        prompt = (
            f'Analyze this Discord message for {filter_prompts.get(filter_type, filter_prompts["all"])}.\n'
            'Respond with ONLY valid JSON:\n'
            '{"violation": true_or_false, "category": "category_name", "severity": "low|medium|high", "reason": "explanation"}\n\n'
            f'Message: "{text}"'
        )

        messages = [
            {'role': 'system', 'content': 'You analyze messages for content violations. Always respond with valid JSON only.'},
            {'role': 'user', 'content': prompt}
        ]

        try:
            raw = await self._chat_completion(messages, max_tokens=100, temperature=0.0)

            cleaned = raw.strip()
            cleaned = re.sub(r'^```json\s*', '', cleaned, flags=re.IGNORECASE)
            cleaned = re.sub(r'^```\s*', '', cleaned)
            cleaned = re.sub(r'\s*```$', '', cleaned)

            return json.loads(cleaned)
        except (RuntimeError, json.JSONDecodeError) as e:
            logger.error(f'Message analysis failed: {e}')
            return {'violation': False, 'category': 'unknown', 'severity': 'unknown', 'reason': 'Analysis failed'}

    # ─── Search Messages Query ───────────────────────────────────────────────────

    async def generate_search_query(self, user_input: str) -> dict:
        """
        Generate a structured query for searching messages based on natural language.
        """
        prompt = (
            f'Based on this user request to search messages, generate a search query.\n'
            f'User: "{user_input}"\n\n'
            'Respond with ONLY valid JSON:\n'
            '{"keyword": "search_term", "filters": {{"member": "username or null", "days": number_or_null}}, "amount": number}'
        )

        messages = [
            {'role': 'system', 'content': 'You generate search queries for Discord message history.'},
            {'role': 'user', 'content': prompt}
        ]

        try:
            raw = await self._chat_completion(messages, max_tokens=100, temperature=0.0)

            cleaned = raw.strip()
            cleaned = re.sub(r'^```json\s*', '', cleaned, flags=re.IGNORECASE)
            cleaned = re.sub(r'^```\s*', '', cleaned)
            cleaned = re.sub(r'\s*```$', '', cleaned)

            return json.loads(cleaned)
        except (RuntimeError, json.JSONDecodeError) as e:
            logger.error(f'Search query generation failed: {e}')
            return {'keyword': user_input, 'filters': {}, 'amount': 50}
