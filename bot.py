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
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

scraper = NetkeibaScraper()
detector = OddsDetector()

# 監視中のレース
active_races: list[RaceInfo] = []
# 今日の監視開始通知済みフラグ
daily_start_notified: str = ""
# アラート送信先チャンネルIDリスト（動的に追加可能）
alert_channels: set[int] = set(config.discord_channel_ids)


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

async def send_to_all_channels(message: str):
    """全登録チャンネルにメッセージを送信"""
    dead_channels = set()
    for ch_id in alert_channels:
        channel = bot.get_channel(ch_id)
        if not channel:
            continue
        try:
            await channel.send(message)
        except discord.HTTPException as e:
            logger.error(f"Send error to {ch_id}: {e}")
            if e.status == 403:  # Forbidden = 権限なし
                dead_channels.add(ch_id)
    # 権限のないチャンネルは除外
    alert_channels.difference_update(dead_channels)


@bot.event
async def on_ready():
    logger.info(f"Bot ready: {bot.user} | Servers: {len(bot.guilds)} | Channels: {len(alert_channels)}")
    if not daily_check.is_running():
        daily_check.start()
    if not odds_monitor.is_running():
        odds_monitor.start()


@bot.event
async def on_guild_join(guild: discord.Guild):
    """新しいサーバーに参加した時、適切なチャンネルを自動登録"""
    # "odds" or "bot" or "競馬" を含むチャンネルを探す、なければ最初のテキストチャンネル
    target = None
    for ch in guild.text_channels:
        name = ch.name.lower()
        if any(kw in name for kw in ("odds", "bot", "競馬", "keiba", "アラート", "alert")):
            target = ch
            break
    if target is None:
        # 最初に書き込めるテキストチャンネル
        for ch in guild.text_channels:
            if ch.permissions_for(guild.me).send_messages:
                target = ch
                break

    if target:
        alert_channels.add(target.id)
        logger.info(f"Joined guild: {guild.name} -> channel: #{target.name} ({target.id})")
        try:
            await target.send(
                "**🏇 オッズ急変検知Bot が参加しました！**\n\n"
                "JRA中央競馬のオッズ急変（急騰・急落）をリアルタイムで検知して通知します。\n\n"
                "📋 `!help` — コマンド一覧\n"
                "📊 `!status` — 監視状況を確認\n"
                "🏇 `!odds 阪神 1` — 指定レースのオッズ表示\n\n"
                "⏱ 監視時間: 9:00〜17:00（レース開催日のみ）\n"
                f"🔔 アラート送信先: このチャンネル (#{target.name})\n\n"
                "チャンネルを変更したい場合: 希望のチャンネルで `!setchannel` を実行してください。"
            )
        except discord.HTTPException:
            pass


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

        await send_to_all_channels("\n".join(lines))


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

    if not alert_channels:
        return

    all_alerts: list[OddsAlert] = []
    now = datetime.now()

    checked = 0
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
                logger.warning(f"No odds returned: {race.display_name}")
                continue

            checked += 1
            alerts = detector.check(race, odds)
            all_alerts.extend(alerts)

            await asyncio.sleep(1.0)

        except Exception as e:
            logger.error(f"Error monitoring {race.display_name}: {e}", exc_info=True)

    if checked > 0:
        logger.info(f"Odds check: {checked} races, {len(all_alerts)} alerts")

    if all_alerts:
        msg = format_alerts(all_alerts)
        await send_to_all_channels(msg)

    detector.cleanup_old_alerts()


@odds_monitor.error
async def odds_monitor_error(error):
    logger.error(f"odds_monitor crashed: {error}", exc_info=error)


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


@bot.command(name="setchannel")
async def cmd_setchannel(ctx):
    """このチャンネルをアラート送信先に設定"""
    if not ctx.guild:
        await ctx.send("サーバー内で実行してください")
        return

    # このサーバーの既存チャンネルを除去して新しいチャンネルを登録
    guild_channel_ids = {ch.id for ch in ctx.guild.text_channels}
    alert_channels.difference_update(guild_channel_ids)
    alert_channels.add(ctx.channel.id)

    await ctx.send(f"✅ このチャンネル (#{ctx.channel.name}) をアラート送信先に設定しました")
    logger.info(f"Channel set: {ctx.guild.name} -> #{ctx.channel.name} ({ctx.channel.id})")


@bot.command(name="help")
async def cmd_help(ctx):
    """コマンド一覧を表示"""
    embed = discord.Embed(
        title="🏇 オッズ急変検知Bot",
        description="JRA中央競馬のオッズ急変をリアルタイム検知",
        color=0x00B894,
    )
    embed.add_field(
        name="📋 基本コマンド",
        value=(
            "`!status` — 監視状況を表示\n"
            "`!odds 阪神 1` — 指定レースの単勝オッズ\n"
            "`!refresh` — レース一覧を再取得\n"
            "`!help` — このヘルプを表示"
        ),
        inline=False,
    )
    embed.add_field(
        name="⚙️ 設定コマンド",
        value=(
            "`!threshold` — 閾値の確認\n"
            "`!threshold 15 20 25` — 閾値を変更\n"
            "`!setchannel` — アラート送信先を変更"
        ),
        inline=False,
    )
    embed.add_field(
        name="⏱ 監視仕様",
        value=(
            "**時間**: 9:00〜17:00（レース開催日のみ自動）\n"
            "**頻度**: 発走60〜10分前は60秒間隔 / 10分前〜発走は15秒間隔\n"
            "**検知**: オッズ帯別の閾値で急騰🔺・急落🔻を判定"
        ),
        inline=False,
    )
    embed.set_footer(text="開発: @kyui91 | GitHub: kyui91/keiba-odds-bot")
    await ctx.send(embed=embed)


@bot.command(name="invite")
async def cmd_invite(ctx):
    """Botの招待リンクを表示"""
    if bot.user:
        url = discord.utils.oauth_url(
            bot.user.id,
            permissions=discord.Permissions(
                send_messages=True,
                read_messages=True,
                embed_links=True,
                read_message_history=True,
            ),
        )
        await ctx.send(f"**🔗 Bot招待リンク**\n{url}")


async def start_bot():
    """Botを非同期で起動（外部ループと共存用）"""
    if not config.discord_token:
        logger.error("DISCORD_TOKEN が設定されていません")
        return

    logger.info(f"Starting bot with {len(alert_channels)} alert channel(s)")
    await bot.start(config.discord_token)


def run():
    if not config.discord_token:
        logger.error("DISCORD_TOKEN が設定されていません")
        return

    bot.run(config.discord_token)
