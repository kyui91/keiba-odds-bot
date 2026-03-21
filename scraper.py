"""netkeiba スクレイパー - レース一覧 & オッズ取得

データ取得方式:
- レース一覧: race_list_sub.html (AJAX sub-page, cloudscraper)
- オッズ: Playwright (ヘッドレスブラウザ) でJS実行後に取得
"""
import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from functools import partial

import cloudscraper
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, Browser, BrowserContext

from config import config

logger = logging.getLogger(__name__)

# netkeiba URL
RACE_LIST_SUB = "https://race.netkeiba.com/top/race_list_sub.html"
ODDS_PAGE = "https://race.netkeiba.com/odds/index.html"

# 場コード → 場名
JRA_VENUE = {
    "01": "札幌", "02": "函館", "03": "福島", "04": "新潟",
    "05": "東京", "06": "中山", "07": "中京", "08": "京都",
    "09": "阪神", "10": "小倉",
}


@dataclass
class RaceInfo:
    race_id: str
    race_name: str
    venue: str
    race_number: int
    post_time: str

    @property
    def display_name(self) -> str:
        return f"{self.venue}{self.race_number}R"


@dataclass
class HorseOdds:
    number: int       # 馬番
    name: str         # 馬名
    odds: float       # 単勝オッズ
    popularity: int   # 人気順


class NetkeibaScraper:
    def __init__(self):
        # レース一覧用 (静的HTML)
        self._scraper = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "windows", "mobile": False},
        )
        self._scraper.headers.update({
            "Accept-Language": "ja,en-US;q=0.7,en;q=0.3",
            "Referer": "https://race.netkeiba.com/",
        })
        # Playwright (オッズ取得用)
        self._playwright = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None

    async def _ensure_browser(self):
        """Playwrightブラウザを初期化（遅延起動）"""
        if self._browser is None:
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(headless=True)
            self._context = await self._browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
                locale="ja-JP",
            )
            logger.info("Playwright browser started")

    async def close(self):
        """ブラウザを閉じる"""
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()

    # ========================
    #  レース一覧取得 (cloudscraper)
    # ========================

    def _sync_get(self, url: str, params: dict | None = None) -> bytes | None:
        """同期HTTP GET"""
        try:
            resp = self._scraper.get(url, params=params, timeout=config.request_timeout)
            if resp.status_code == 200:
                if "cf-error" in resp.text[:500] or "challenge-platform" in resp.text[:500]:
                    logger.warning(f"Cloudflare challenge detected: {url}")
                    return None
                return resp.content
            logger.warning(f"HTTP {resp.status_code}: {url}")
        except Exception as e:
            logger.warning(f"Fetch error: {e}")
        return None

    async def _fetch_html(self, url: str, params: dict | None = None) -> str | None:
        """HTMLをデコードして取得"""
        loop = asyncio.get_event_loop()
        for attempt in range(3):
            try:
                raw = await loop.run_in_executor(
                    None, partial(self._sync_get, url, params)
                )
                if raw is not None:
                    for encoding in ("utf-8", "euc-jp", "shift_jis"):
                        try:
                            return raw.decode(encoding)
                        except (UnicodeDecodeError, LookupError):
                            continue
                    return raw.decode("utf-8", errors="replace")
            except Exception as e:
                logger.warning(f"Fetch error (attempt {attempt + 1}): {e}")
            if attempt < 2:
                await asyncio.sleep(2 * (attempt + 1))
        return None

    async def get_today_races(self, date: str | None = None) -> list[RaceInfo]:
        """今日のJRAレース一覧を取得"""
        if date is None:
            date = datetime.now().strftime("%Y%m%d")

        html = await self._fetch_html(RACE_LIST_SUB, params={"kaisai_date": date})
        if not html:
            logger.error("Failed to fetch race list")
            return []

        return self._parse_race_list(html)

    def _parse_race_list(self, html: str) -> list[RaceInfo]:
        """レース一覧HTMLをパース"""
        soup = BeautifulSoup(html, "html.parser")
        races = []
        seen_ids = set()

        items = soup.select("li.RaceList_DataItem")
        for item in items:
            link = item.select_one("a[href*='race_id=']")
            if not link:
                continue

            href = link.get("href", "")
            match = re.search(r"race_id=(\d+)", href)
            if not match:
                continue

            race_id = match.group(1)
            if race_id in seen_ids:
                continue
            seen_ids.add(race_id)

            num_el = item.select_one(".Race_Num")
            race_num = 0
            if num_el:
                num_match = re.search(r"(\d+)", num_el.get_text())
                if num_match:
                    race_num = int(num_match.group(1))
            if race_num == 0:
                race_num = int(race_id[-2:])

            venue_code = race_id[4:6]
            venue = JRA_VENUE.get(venue_code, f"場{venue_code}")

            title_el = item.select_one(".ItemTitle")
            race_name = title_el.get_text(strip=True) if title_el else f"{race_num}R"

            time_el = item.select_one(".RaceList_Itemtime")
            post_time = time_el.get_text(strip=True) if time_el else ""

            races.append(RaceInfo(
                race_id=race_id,
                race_name=race_name,
                venue=venue,
                race_number=race_num,
                post_time=post_time,
            ))

        logger.info(f"Parsed {len(races)} races")
        return races

    # ========================
    #  オッズ取得 (Playwright)
    # ========================

    async def get_odds(self, race: RaceInfo) -> list[HorseOdds]:
        """Playwrightでオッズページを開いてJS実行後のオッズを取得"""
        await self._ensure_browser()

        url = f"{ODDS_PAGE}?type=b1&race_id={race.race_id}"

        try:
            page = await self._context.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=20000)

                # オッズがJS で読み込まれるまで待機
                try:
                    await page.wait_for_function(
                        """() => {
                            const el = document.getElementById('odds-1_01');
                            return el && el.textContent.trim() !== '---.-';
                        }""",
                        timeout=10000,
                    )
                except Exception:
                    logger.warning(f"Odds not loaded within 10s: {race.display_name}")
                    return []

                # オッズを抽出
                odds_data = await page.evaluate("""() => {
                    const results = [];
                    const spans = document.querySelectorAll('[id^="odds-1_"]');
                    spans.forEach(span => {
                        const id = span.id;
                        const umaban = parseInt(id.split('_')[1]);
                        const oddsText = span.textContent.trim();
                        const oddsVal = parseFloat(oddsText);
                        if (isNaN(oddsVal)) return;

                        // 馬名
                        const row = span.closest('tr');
                        let name = umaban + '番';
                        if (row) {
                            const nameEl = row.querySelector('.Horse_Name') || row.querySelector('a[href*="horse"]');
                            if (nameEl) name = nameEl.textContent.trim();
                        }

                        // 人気順
                        const popEl = document.getElementById('ninki-rank_' + String(umaban).padStart(2, '0'));
                        let popularity = 0;
                        if (popEl) {
                            const p = parseInt(popEl.textContent.trim());
                            if (!isNaN(p)) popularity = p;
                        }

                        results.push({number: umaban, name, odds: oddsVal, popularity});
                    });
                    return results;
                }""")

                horses = [
                    HorseOdds(
                        number=d["number"],
                        name=d["name"],
                        odds=d["odds"],
                        popularity=d.get("popularity", 0),
                    )
                    for d in odds_data
                ]

                if horses:
                    logger.info(f"Odds: {race.display_name} -> {len(horses)} horses")
                else:
                    logger.warning(f"No odds parsed: {race.display_name}")

                return horses

            finally:
                await page.close()

        except Exception as e:
            logger.error(f"Playwright error for {race.display_name}: {e}")
            return []
