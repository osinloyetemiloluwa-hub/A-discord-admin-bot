"""
utils/keepalive.py
Hugging Face Docker Spaces expect *something* bound to a port (default 7860)
to consider a Space "Running" — a pure background worker like a Discord bot
with no HTTP server gets stuck in "Starting" forever. This module runs a
tiny aiohttp web server alongside the bot to satisfy that requirement, and
doubles as a health-check endpoint an external uptime pinger can hit to stop
the free-tier Space from sleeping after 48h of inactivity.
"""

import os
import time
import logging
from aiohttp import web

logger = logging.getLogger('CoAdminBot.KeepAlive')

START_TIME = time.time()


def create_app(bot) -> web.Application:
    async def health(request):
        uptime = int(time.time() - START_TIME)
        ready = bot.is_ready()
        return web.json_response({
            'status': 'ok' if ready else 'starting',
            'bot_user': str(bot.user) if ready else None,
            'guilds': len(bot.guilds) if ready else 0,
            'uptime_seconds': uptime,
        })

    app = web.Application()
    app.router.add_get('/', health)
    app.router.add_get('/health', health)
    return app


async def run_webserver(bot):
    port = int(os.getenv('PORT', 7860))   # read here so Render's PORT=10000 is always picked up
    app = create_app(bot)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    logger.info(f'✅ Keep-alive web server listening on 0.0.0.0:{port}')
