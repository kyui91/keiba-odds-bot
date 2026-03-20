"""オッズ急変検知エンジン"""
import logging
from dataclasses import dataclass
from datetime import datetime

from scraper import RaceInfo, HorseOdds
from config import config

logger = logging.getLogger(__name__)


@dataclass
class OddsAlert:
    race: RaceInfo
    horse_number: int
    horse_name: str
    old_odds: float
    new_odds: float
    change_pct: float
    is_surge: bool  # True=急騰, False=急落
    timestamp: datetime

    @property
    def emoji(self) -> str:
        return "🔺" if self.is_surge else "🔻"

    @property
    def direction(self) -> str:
        return "急騰" if self.is_surge else "急落"

    def format_message(self) -> str:
        sign = "+" if self.change_pct > 0 else ""
        return (
            f"{self.emoji} {self.direction}　"
            f"{self.race.display_name} "
            f"[{self.horse_number:02d}番] {self.horse_name}　"
            f"{self.old_odds:.1f} → {self.new_odds:.1f} "
            f"({sign}{self.change_pct:.1f}%)"
        )


class OddsDetector:
    def __init__(self):
        # {race_id: {horse_number: HorseOdds}} - 前回のオッズ
        self._previous: dict[str, dict[int, HorseOdds]] = {}
        # 直近のアラート（重複防止用）
        self._recent_alerts: dict[str, datetime] = {}

    def check(self, race: RaceInfo, current_odds: list[HorseOdds]) -> list[OddsAlert]:
        """オッズ変動をチェックしてアラートを返す"""
        alerts = []
        race_id = race.race_id

        # 前回データがない場合は保存のみ
        if race_id not in self._previous:
            self._previous[race_id] = {h.number: h for h in current_odds}
            logger.debug(f"Initial odds saved: {race.display_name} ({len(current_odds)} horses)")
            return alerts

        prev_map = self._previous[race_id]
        now = datetime.now()

        for horse in current_odds:
            prev = prev_map.get(horse.number)
            if not prev or prev.odds <= 0:
                continue

            # 変動率を計算
            change_pct = ((horse.odds - prev.odds) / prev.odds) * 100

            # 閾値判定（前回オッズ基準）
            threshold = config.get_threshold(prev.odds)

            # 絶対変動幅チェック（ノイズ除去）
            abs_change = abs(horse.odds - prev.odds)
            if abs_change < config.min_absolute_change:
                continue

            if abs(change_pct) >= threshold:
                # 重複防止: 同じ馬について短時間で連続アラートしない（5分）
                alert_key = f"{race_id}_{horse.number}"
                if alert_key in self._recent_alerts:
                    last_alert = self._recent_alerts[alert_key]
                    if (now - last_alert).total_seconds() < 300:
                        continue

                alert = OddsAlert(
                    race=race,
                    horse_number=horse.number,
                    horse_name=horse.name,
                    old_odds=prev.odds,
                    new_odds=horse.odds,
                    change_pct=change_pct,
                    is_surge=change_pct > 0,
                    timestamp=now,
                )
                alerts.append(alert)
                self._recent_alerts[alert_key] = now

                logger.info(f"ALERT: {alert.format_message()}")

        # 現在のオッズを保存（次回比較用）
        self._previous[race_id] = {h.number: h for h in current_odds}

        return alerts

    def clear_race(self, race_id: str):
        """レース終了後にデータをクリア"""
        self._previous.pop(race_id, None)
        # 関連するアラートキーも削除
        keys_to_remove = [k for k in self._recent_alerts if k.startswith(race_id)]
        for k in keys_to_remove:
            del self._recent_alerts[k]

    def cleanup_old_alerts(self, max_age_seconds: int = 3600):
        """古いアラート履歴をクリーンアップ"""
        now = datetime.now()
        keys_to_remove = [
            k for k, t in self._recent_alerts.items()
            if (now - t).total_seconds() > max_age_seconds
        ]
        for k in keys_to_remove:
            del self._recent_alerts[k]
