"""エントリーポイント: Bot + ヘルスチェックサーバー同時起動"""
import asyncio
import os
import logging

from aiohttp import web

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def health(request):
    return web.Response(text="OK")


async def main():
    # ヘルスチェック用Webサーバー
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)

    port = int(os.getenv("PORT", "10000"))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Health server started on port {port}")

    # Discord Bot起動
    from bot import start_bot
    await start_bot()


if __name__ == "__main__":
    asyncio.run(main())
