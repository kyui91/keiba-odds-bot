"""エントリーポイント: Bot + ヘルスチェックサーバー同時起動"""
import asyncio
from bot import run
from server import start_server


if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # ヘルスチェック用Webサーバー起動（Render Free Tier用）
    start_server(loop)

    # Discord Bot起動
    run()
