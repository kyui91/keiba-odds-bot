"""ヘルスチェック用Webサーバー（Render Free Tier対応）

Renderの無料Web Serviceはヘルスチェックが必要。
Bot起動と同時に軽量HTTPサーバーを立てる。
"""
import os
from aiohttp import web


async def health(request):
    return web.Response(text="OK")


def start_server(loop):
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)

    port = int(os.getenv("PORT", "10000"))
    runner = web.AppRunner(app)
    loop.run_until_complete(runner.setup())
    site = web.TCPSite(runner, "0.0.0.0", port)
    loop.run_until_complete(site.start())
