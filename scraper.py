"""netkeiba スクレイパー - レース一覧 & オッズ取得

データ取得方式:
- レース一覧: race_list_sub.html (AJAX sub-page)
- オッズ: /api/api_get_jra_odds.html (JSON API, data にHTMLフラグメント)
"""
import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime

import aiohttp
from bs4 import BeautifulSoup

from config import config

logger = logging.getLogger(__name__)

# netkeiba URL
RACE_LIST_SUB = "https://race.netkeiba.com/top/race_list_sub.html"
ODDS_API = "https://race.netkeiba.com/api/api_get_jra_odds.html"
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
        self._session: aiohttp.ClientSession | None = None
        self._headers = {
            "User-Agent": config.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ja,en-US;q=0.7,en;q=0.3",
            "Referer": "https://race.netkeiba.com/",
        }

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers=self._headers,
                timeout=aiohttp.ClientTimeout(total=config.request_timeout),
            )
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def _fetch_bytes(self, url: str, params: dict | None = None) -> bytes | None:
        """バイト列を取得（リトライ付き）"""
        session = await self._get_session()
        for attempt in range(3):
            try:
                async with session.get(url, params=params) as resp:
                    if resp.status == 200:
                        return await resp.read()
                    logger.warning(f"HTTP {resp.status}: {url}")
            except Exception as e:
                logger.warning(f"Fetch error (attempt {attempt + 1}): {e}")
                if attempt < 2:
                    await asyncio.sleep(2 * (attempt + 1))
        return None

    async def _fetch_html(self, url: str, params: dict | None = None) -> str | None:
        """HTMLをデコードして取得"""
        raw = await self._fetch_bytes(url, params)
        if not raw:
            return None
        for encoding in ("utf-8", "euc-jp", "shift_jis"):
            try:
                return raw.decode(encoding)
            except (UnicodeDecodeError, LookupError):
                continue
        return raw.decode("utf-8", errors="replace")

    async def _fetch_json(self, url: str, params: dict | None = None) -> dict | None:
        """JSONレスポンスを取得"""
        session = await self._get_session()
        for attempt in range(3):
            try:
                async with session.get(url, params=params) as resp:
                    if resp.status == 200:
                        return await resp.json(content_type=None)
                    logger.warning(f"HTTP {resp.status}: {url}")
            except Exception as e:
                logger.warning(f"JSON fetch error (attempt {attempt + 1}): {e}")
                if attempt < 2:
                    await asyncio.sleep(2 * (attempt + 1))
        return None

    # ========================
    #  レース一覧取得
    # ========================

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

            # レース番号
            num_el = item.select_one(".Race_Num")
            race_num = 0
            if num_el:
                num_match = re.search(r"(\d+)", num_el.get_text())
                if num_match:
                    race_num = int(num_match.group(1))
            if race_num == 0:
                race_num = int(race_id[-2:])

            # 場名（race_idの5-6桁目が場コード）
            venue_code = race_id[4:6]
            venue = JRA_VENUE.get(venue_code, f"場{venue_code}")

            # レース名
            title_el = item.select_one(".ItemTitle")
            race_name = title_el.get_text(strip=True) if title_el else f"{race_num}R"

            # 発走時刻
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
    #  オッズ取得
    # ========================

    async def get_odds(self, race: RaceInfo) -> list[HorseOdds]:
        """レースの単勝オッズを取得

        方式1: JSON API（api_get_jra_odds.html）
        方式2: オッズページ直接パース（APIが使えない場合のフォールバック）
        """
        # 方式1: API
        odds = await self._get_odds_api(race.race_id)
        if odds:
            return odds

        # 方式2: ページ直接パース
        odds = await self._get_odds_page(race.race_id)
        return odds

    async def _get_odds_api(self, race_id: str) -> list[HorseOdds]:
        """JSON APIからオッズ取得"""
        params = {
            "race_id": race_id,
            "type": "b1",
            "compress": "false",
        }
        resp = await self._fetch_json(ODDS_API, params=params)
        if not resp:
            return []

        status = resp.get("status", "")
        data_html = resp.get("data", "")

        if not data_html:
            logger.debug(f"Odds API empty for {race_id} (status={status}, reason={resp.get('reason', '')})")
            return []

        return self._parse_odds_html(data_html)

    def _parse_odds_html(self, html: str) -> list[HorseOdds]:
        """APIレスポンスのHTMLフラグメントからオッズをパース

        HTML内の要素: id="odds-1_{umaban}" が単勝オッズ
        """
        soup = BeautifulSoup(html, "html.parser")
        horses = []

        # 方式A: odds-1_{umaban} IDから取得
        odds_spans = soup.select("[id^='odds-1_']")
        if odds_spans:
            for span in odds_spans:
                try:
                    span_id = span.get("id", "")
                    umaban = int(span_id.split("_")[1])
                    odds_text = span.get_text(strip=True).replace(",", "")
                    if not re.match(r"[\d.]+", odds_text):
                        continue
                    odds_val = float(odds_text)

                    # 馬名を探す（同じ行内）
                    parent_row = span.find_parent("tr") or span.find_parent("div")
                    name = f"{umaban}番"
                    if parent_row:
                        name_el = parent_row.select_one("a[href*='horse']")
                        if name_el:
                            name = name_el.get_text(strip=True)

                    # 人気を探す
                    popularity = 0
                    pop_el = soup.select_one(f"[id='ninki-rank_{umaban}']")
                    if pop_el:
                        pop_text = pop_el.get_text(strip=True)
                        if pop_text.isdigit():
                            popularity = int(pop_text)

                    horses.append(HorseOdds(
                        number=umaban,
                        name=name,
                        odds=odds_val,
                        popularity=popularity,
                    ))
                except (ValueError, IndexError):
                    continue
            return horses

        # 方式B: テーブル行から取得（フォールバック）
        return self._parse_odds_table(soup)

    async def _get_odds_page(self, race_id: str) -> list[HorseOdds]:
        """オッズページを直接パース（フォールバック）"""
        params = {"type": "b1", "race_id": race_id}
        html = await self._fetch_html(ODDS_PAGE, params=params)
        if not html:
            return []

        soup = BeautifulSoup(html, "html.parser")

        # ページ内のテーブルからオッズを取得
        return self._parse_odds_table(soup)

    def _parse_odds_table(self, soup: BeautifulSoup) -> list[HorseOdds]:
        """テーブル形式のオッズをパース"""
        horses = []
        rows = soup.select("tr")

        for row in rows:
            try:
                # 馬番を含むセルを探す
                cells = row.select("td")
                if len(cells) < 2:
                    continue

                # 馬番（通常最初のtdの数字）
                num_text = cells[0].get_text(strip=True)
                if not num_text.isdigit():
                    continue
                number = int(num_text)

                # 馬名
                name_el = row.select_one("a[href*='horse']")
                name = name_el.get_text(strip=True) if name_el else f"{number}番"

                # オッズ（数値を含むtd）
                odds_val = None
                for cell in cells[1:]:
                    text = cell.get_text(strip=True).replace(",", "")
                    if re.match(r"^\d+\.\d+$", text):
                        odds_val = float(text)
                        break

                if odds_val is None:
                    continue

                # 人気
                popularity = 0
                pop_el = row.select_one(".Popular, .Ninki")
                if pop_el:
                    pop_text = pop_el.get_text(strip=True)
                    if pop_text.isdigit():
                        popularity = int(pop_text)

                horses.append(HorseOdds(
                    number=number,
                    name=name,
                    odds=odds_val,
                    popularity=popularity,
                ))
            except (ValueError, AttributeError):
                continue

        return horses
