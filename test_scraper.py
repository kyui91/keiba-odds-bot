"""スクレイパーの動作テスト"""
import asyncio
from scraper import NetkeibaScraper


async def main():
    scraper = NetkeibaScraper()

    # 明日(土曜)のレース一覧を取得
    print("=== レース一覧取得テスト (2026-03-21 土曜) ===")
    races = await scraper.get_today_races("20260321")
    print(f"取得レース数: {len(races)}")

    if not races:
        # スクショの日付で試す
        print("\n2026-03-14 (土曜) を試します...")
        races = await scraper.get_today_races("20260314")
        print(f"取得レース数: {len(races)}")

    venues = set()
    for race in races:
        venues.add(race.venue)
    print(f"開催場: {', '.join(sorted(venues))}")

    for race in races[:12]:
        print(f"  {race.display_name:8s} {race.post_time:6s} {race.race_name:15s} ID={race.race_id}")

    # オッズ取得テスト
    if races:
        target = races[0]
        print(f"\n=== オッズ取得テスト: {target.display_name} ===")
        odds = await scraper.get_odds(target)
        print(f"取得馬数: {len(odds)}")
        if odds:
            for h in sorted(odds, key=lambda x: x.odds):
                print(f"  {h.number:2d}番 {h.name:12s} {h.odds:>7.1f}倍  {h.popularity}番人気")
        else:
            print("  ※ 発売前のためオッズなし（レース当日に取得可能）")

    await scraper.close()


asyncio.run(main())
