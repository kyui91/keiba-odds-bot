"""Discord Bot - オッズ急変検知 & 通知"""
import asyncio
import logging
from datetime import datetime, time as dtime, timedelta

import discord
from discord.ext import commands, tasks

from config import config
from scraper import NetkeibaScraper, RaceInfo
from detector import OddsDetector, OddsAlert

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

scraper = NetkeibaScraper()
detector = OddsDetector()

# 監視中のレース
active_races: list[RaceInfo] = []
# 今日の監視開始通知済みフラグ
daily_start_notified: str = ""


# ========================
#  レース時間帯判定
# ========================

# JRA: 通常 9:30発売開始 〜 最終レース16:30頃
MONITOR_START = dtime(9, 0)    # 9:00から監視（発売前の準備）
MONITOR_END = dtime(17, 0)     # 17:00で監視終了

def is_race_hours() -> bool:
    """現在がレース監視時間帯かどうか"""
    now = datetime.now().time()
    return MONITOR_START <= now <= MONITOR_END


def is_race_day() -> bool:
    """今日がレース開催日かどうか（レース一覧があればTrue）"""
    return len(active_races) > 0


# ========================
#  メインループ
# ========================

@bot.event
async def on_ready():
    logger.info(f"Bot ready: {bot.user}")
    if not daily_check.is_running():
        daily_check.start()
    if not odds_monitor.is_running():
        odds_monitor.start()


@tasks.loop(minutes=10)
async def daily_check():
    """10分ごと: レース一覧更新 & 監視開始通知"""
    global daily_start_notified

    try:
        today = datetime.now().strftime("%Y%m%d")

        # 日付が変わったらリセット
        if daily_start_notified and not daily_start_notified.startswith(today):
            daily_start_notified = ""
            detector._previous.clear()
            detector._recent_alerts.clear()

        # 9:00〜17:00 の間だけレース一覧を更新
        if not is_race_hours():
            return

        await load_races()
    except Exception as e:
        logger.error(f"daily_check error: {e}")
        return

    # 開催日の初回のみ「監視開始」通知を送信
    if active_races and daily_start_notified != today:
        daily_start_notified = today
        channel = bot.get_channel(config.discord_channel_id)
        if channel:
            venues = {}
            for race in active_races:
                venues.setdefault(race.venue, []).append(race)

            lines = [f"**🏇 本日の競馬オッズ監視を開始します** `{datetime.now().strftime('%Y/%m/%d')}`", ""]
            for venue, races_list in venues.items():
                first = min(races_list, key=lambda r: r.post_time or "99:99")
                last = max(races_list, key=lambda r: r.post_time or "00:00")
                lines.append(
                    f"📍 **{venue}** {len(races_list)}R "
                    f"({first.post_time}〜{last.post_time})"
                )

            lines.append("")
            lines.append(f"⏱ 監視間隔: {config.poll_interval}秒")
            lines.append(f"📊 閾値: 低{config.threshold_low}% / 中{config.threshold_mid}% / 高{config.threshold_high}%")

            try:
                await channel.send("\n".join(lines))
            except discord.HTTPException as e:
                logger.error(f"Discord send error: {e}")


@daily_check.before_loop
async def before_daily():
    await bot.wait_until_ready()


@tasks.loop(seconds=15)
async def odds_monitor():
    """メインループ: レース時間帯のみオッズを監視

    15秒間隔で回るが、各レースの監視頻度は発走までの残り時間で変える:
    - 60〜10分前: 通常間隔（60秒に1回）
    - 10分前〜発走: 高頻度（15秒に1回）= 急変が最も起きやすい
    - 発走後5分: 監視終了
    """
    if not is_race_hours() or not active_races:
        return

    channel = bot.get_channel(config.discord_channel_id)
    if not channel:
        logger.error(f"Channel not found: {config.discord_channel_id}")
        return

    all_alerts: list[OddsAlert] = []
    now = datetime.now()

    for race in active_races:
        priority = get_monitor_priority(race, now)
        if priority == "skip":
            continue

        # 通常モード（60分〜10分前）: 60秒に1回 → 15秒ループ4回に1回
        if priority == "normal" and now.second > 15:
            continue

        try:
            odds = await scraper.get_odds(race)
            if not odds:
                continue

            alerts = detector.check(race, odds)
            all_alerts.extend(alerts)

            await asyncio.sleep(1.0)

        except Exception as e:
            logger.error(f"Error monitoring {race.display_name}: {e}")

    if all_alerts:
        msg = format_alerts(all_alerts)
        try:
            await channel.send(msg)
        except discord.HTTPException as e:
            logger.error(f"Discord send error: {e}")

    detector.cleanup_old_alerts()


@odds_monitor.before_loop
async def before_monitor():
    await bot.wait_until_ready()


def get_minutes_to_post(race: RaceInfo, now: datetime) -> float | None:
    """発走までの残り分数を返す（発走後はマイナス）"""
    if not race.post_time:
        return None
    try:
        h, m = map(int, race.post_time.strip().split(":"))
        post = now.replace(hour=h, minute=m, second=0, microsecond=0)
        return (post - now).total_seconds() / 60
    except (ValueError, TypeError):
        return None


def get_monitor_priority(race: RaceInfo, now: datetime) -> str:
    """レースの監視優先度を返す

    Returns:
        "hot"    : 発走10分前〜発走 → 15秒間隔（急変ゾーン）
        "normal" : 発走60分前〜10分前 → 約60秒間隔
        "skip"   : 監視対象外
    """
    mins = get_minutes_to_post(race, now)
    if mins is None:
        return "normal"  # 時刻不明は通常監視

    if mins < -5:
        return "skip"     # 発走後5分以上 → 終了
    elif mins <= 10:
        return "hot"      # 10分前〜発走後5分 → 高頻度
    elif mins <= 60:
        return "normal"   # 60分前〜10分前 → 通常
    else:
        return "skip"     # まだ早い


async def load_races():
    """今日のレース一覧を取得"""
    global active_races
    try:
        races = await scraper.get_today_races()
        active_races = races
        logger.info(f"Loaded {len(races)} races for today")

        if races:
            venues = set(r.venue for r in races)
            logger.info(f"Venues: {', '.join(venues)}")
    except Exception as e:
        logger.error(f"Failed to load races: {e}")


def format_alerts(alerts: list[OddsAlert]) -> str:
    """アラートをDiscordメッセージに整形"""
    now = datetime.now().strftime("%H:%M:%S")
    lines = [f"**⚡ オッズ急変検知** `{now}`", "```"]

    surges = [a for a in alerts if a.is_surge]
    drops = [a for a in alerts if not a.is_surge]

    for alert in surges + drops:
        lines.append(alert.format_message())

    lines.append("```")
    return "\n".join(lines)


# ========================
#  コマンド
# ========================

@bot.command(name="status")
async def cmd_status(ctx):
    """監視状況を表示"""
    now = datetime.now()
    monitoring = is_race_hours()

    if not active_races:
        await ctx.send("📋 今日は開催なし（またはレース一覧未取得）")
        return

    # 現在監視中のレース数
    active_count = sum(1 for r in active_races if get_monitor_priority(r, now) != "skip")

    venues = {}
    for race in active_races:
        venues.setdefault(race.venue, []).append(race.race_number)

    lines = [f"**📋 監視状況** {'🟢 監視中' if monitoring else '🔴 時間外'}"]
    for venue, nums in venues.items():
        nums_str = ", ".join(f"{n}R" for n in sorted(nums))
        lines.append(f"　🏇 {venue}: {nums_str}")

    lines.append(f"\n⏱ ポーリング間隔: {config.poll_interval}秒")
    lines.append(f"📊 閾値: 低{config.threshold_low}% / 中{config.threshold_mid}% / 高{config.threshold_high}%")
    # hotレース（直前）を表示
    hot_races = [r for r in active_races if get_monitor_priority(r, now) == "hot"]
    lines.append(f"🎯 アクティブ: {active_count}/{len(active_races)}レース")
    if hot_races:
        hot_names = ", ".join(f"{r.display_name}({r.post_time})" for r in hot_races)
        lines.append(f"🔥 直前監視中（15秒間隔）: {hot_names}")
    await ctx.send("\n".join(lines))


@bot.command(name="odds")
async def cmd_odds(ctx, venue: str = "", race_num: str = ""):
    """指定レースの現在オッズを表示"""
    if not venue or not race_num:
        await ctx.send("使い方: `!odds 阪神 1`")
        return

    try:
        num = int(race_num)
    except ValueError:
        await ctx.send("レース番号は数字で指定してください")
        return

    target = None
    for race in active_races:
        if venue in race.venue and race.race_number == num:
            target = race
            break

    if not target:
        await ctx.send(f"❌ {venue}{num}R が見つかりません")
        return

    odds = await scraper.get_odds(target)
    if not odds:
        await ctx.send(f"❌ {target.display_name} のオッズを取得できませんでした")
        return

    odds.sort(key=lambda h: h.odds)

    lines = [f"**🏇 {target.display_name} {target.race_name}** 単勝オッズ", "```"]
    for h in odds:
        lines.append(f"  {h.number:2d}番 {h.name:<10s}  {h.odds:>7.1f}倍")
    lines.append("```")

    await ctx.send("\n".join(lines))


@bot.command(name="threshold")
async def cmd_threshold(ctx, low: str = "", mid: str = "", high: str = ""):
    """閾値を変更: !threshold 15 20 25"""
    if not low:
        await ctx.send(
            f"現在の閾値: 低オッズ {config.threshold_low}% / "
            f"中オッズ {config.threshold_mid}% / 高オッズ {config.threshold_high}%\n"
            f"変更: `!threshold 15 20 25`"
        )
        return

    try:
        config.threshold_low = float(low)
        if mid:
            config.threshold_mid = float(mid)
        if high:
            config.threshold_high = float(high)
        await ctx.send(
            f"✅ 閾値更新: 低 {config.threshold_low}% / "
            f"中 {config.threshold_mid}% / 高 {config.threshold_high}%"
        )
    except ValueError:
        await ctx.send("数値で指定してください")


@bot.command(name="refresh")
async def cmd_refresh(ctx):
    """レース一覧を再取得"""
    await load_races()
    venues = set(r.venue for r in active_races)
    await ctx.send(f"🔄 レース一覧を更新しました（{len(active_races)}レース: {', '.join(venues) or 'なし'}）")


async def start_bot():
    """Botを非同期で起動（外部ループと共存用）"""
    if not config.discord_token:
        logger.error("DISCORD_TOKEN が設定されていません")
        return
    if not config.discord_channel_id:
        logger.error("DISCORD_CHANNEL_ID が設定されていません")
        return

    await bot.start(config.discord_token)


def run():
    if not config.discord_token:
        logger.error("DISCORD_TOKEN が設定されていません")
        return
    if not config.discord_channel_id:
        logger.error("DISCORD_CHANNEL_ID が設定されていません")
        return

    bot.run(config.discord_token)
