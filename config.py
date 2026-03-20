"""設定管理"""
import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    # Discord
    discord_token: str = os.getenv("DISCORD_TOKEN", "")
    discord_channel_id: int = int(os.getenv("DISCORD_CHANNEL_ID", "0"))

    # スクレイピング間隔（秒）
    poll_interval: int = int(os.getenv("POLL_INTERVAL", "60"))

    # オッズ急変検知の閾値（%）- オッズ帯別
    # 低オッズ（1.0〜5.0倍）: 小さな変動でも重要
    threshold_low: float = float(os.getenv("THRESHOLD_LOW", "15"))
    # 中オッズ（5.0〜20.0倍）
    threshold_mid: float = float(os.getenv("THRESHOLD_MID", "20"))
    # 高オッズ（20.0倍以上）
    threshold_high: float = float(os.getenv("THRESHOLD_HIGH", "25"))

    # 最小オッズ変動幅（絶対値）- ノイズ除去
    min_absolute_change: float = float(os.getenv("MIN_ABSOLUTE_CHANGE", "1.0"))

    # 監視対象
    watch_jra: bool = os.getenv("WATCH_JRA", "true").lower() == "true"
    watch_nar: bool = os.getenv("WATCH_NAR", "false").lower() == "true"

    # スクレイピング設定
    request_timeout: int = 15
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    )

    def get_threshold(self, odds: float) -> float:
        """オッズ帯に応じた閾値を返す"""
        if odds < 5.0:
            return self.threshold_low
        elif odds < 20.0:
            return self.threshold_mid
        else:
            return self.threshold_high


config = Config()
