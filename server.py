import csv
import html
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

import requests


ROOT = Path(__file__).resolve().parent
CACHE_TTL_SECONDS = 60
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

NASDAQ_NDX_URL = "https://indexes.nasdaq.com/Index/Overview/NDX"
SPDJI_SPX_URL = "https://www.spglobal.com/spdji/en/indices/equity/sp-500/"
YAHOO_SPX_URL = "https://finance.yahoo.com/quote/%5EGSPC"
YAHOO_NDX_URL = "https://finance.yahoo.com/quote/%5ENDX"
YAHOO_VIX_URL = "https://finance.yahoo.com/quote/%5EVIX"
FEAR_GREED_URL = "https://www.finhacker.cz/en/fear-and-greed-index-historical-data-and-chart/"
NAAIM_URL = "https://naaim.org/programs/naaim-exposure-index/"
AAII_URL = "https://www.aaii.com/sentimentsurvey"
SCRIPT_PATTERN = re.compile(
    r'<script type="application/json" data-sveltekit-fetched[^>]*data-url="([^"]+)"[^>]*>(.*?)</script>',
    re.S,
)

FRED_FALLBACKS = {
    "EFFR": {"date": "2026-04-23", "value": "3.64"},
    "DGS10": {"date": "2026-04-23", "value": "4.34"},
    "T10Y2Y": {"date": "2026-04-24", "value": "0.53"},
    "UNRATE": {"date": "2026-03", "value": "4.3"},
    "SAHMREALTIME": {"date": "2026-03", "value": "0.20"},
}
NASDAQ_NDX_FALLBACK = {
    "last": "27,303.67",
    "net_change": "521.04",
    "net_change_pct": "1.95%",
    "day_high": "27,314.21",
    "day_low": "26,986.39",
    "previous_close": "27,303.67",
    "base_value": "125.00",
}
FEAR_GREED_FALLBACK = {"value": 68.0, "label": "Greed", "asOf": "2026-04-27"}
AAII_FALLBACK = {
    "bullish": 46.0,
    "neutral": 19.5,
    "bearish": 34.4,
    "asOf": "2026-04-22",
    "sourceNote": "AAII官网本地抓取受保护，当前使用能找到的较新转载缓存；需以AAII官网为最终准绳。"
}
NAAIM_FALLBACK_RECENT = [
    {"date": "2026-03-04", "value": 79.29},
    {"date": "2026-03-11", "value": 66.99},
    {"date": "2026-03-18", "value": 60.24},
    {"date": "2026-03-25", "value": 68.52},
    {"date": "2026-04-01", "value": 68.36},
    {"date": "2026-04-08", "value": 69.38},
    {"date": "2026-04-15", "value": 79.49},
    {"date": "2026-04-22", "value": 94.15},
]
YAHOO_INDEX_FALLBACKS = {
    "^GSPC": {
        "price": 7165.08,
        "previous_close": None,
        "day_high": None,
        "day_low": None,
        "change": None,
        "change_pct": None,
        "fifty_two_week_high": 7168.66,
        "drawdown": -0.05,
        "two_hundred_day_average": 6705.63,
        "distance_200dma": 6.85,
        "trailing_pe": None,
        "chart_points": 250,
        "source_url": YAHOO_SPX_URL,
    },
    "^VIX": {
        "price": 18.71,
        "previous_close": None,
        "day_high": None,
        "day_low": None,
        "change": None,
        "change_pct": None,
        "fifty_two_week_high": None,
        "drawdown": None,
        "two_hundred_day_average": None,
        "distance_200dma": None,
        "trailing_pe": None,
        "chart_points": 250,
        "source_url": YAHOO_VIX_URL,
    },
}


@dataclass
class CacheState:
    payload: dict | None = None
    expires_at: float = 0.0


CACHE = CacheState()
CACHE_LOCK = threading.Lock()


def request_text(url: str, timeout: int = 20) -> str:
    response = requests.get(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
        timeout=timeout,
    )
    response.raise_for_status()
    return response.text


def local_time() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")


def clean_text(value: str) -> str:
    value = html.unescape(value)
    value = re.sub(r"<[^>]+>", "", value)
    value = value.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def number_or_dash(value: str | None) -> str:
    if not value:
        return "官方源暂不可用"
    return value


def raw_or_value(value):
    if isinstance(value, dict):
        return value.get("raw")
    return value


def extract_embedded_payload(page_html: str, matcher) -> dict:
    for match in SCRIPT_PATTERN.finditer(page_html):
        data_url = html.unescape(match.group(1))
        if matcher(data_url):
            wrapper = json.loads(match.group(2))
            return json.loads(wrapper["body"])
    raise ValueError("target embedded payload not found")


def fetch_yahoo_chart_metrics(symbol: str) -> dict:
    symbol_token = quote(symbol, safe="")
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol_token}?interval=1d&range=1y"
    data = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=20).json()
    result = data["chart"]["result"][0]
    quote_data = result["indicators"]["quote"][0]
    closes = [value for value in quote_data.get("close", []) if value is not None]
    highs = [value for value in quote_data.get("high", []) if value is not None]

    chart_high = max(highs) if highs else None
    chart_200dma = sum(closes[-200:]) / 200 if len(closes) >= 200 else None
    return {
        "chart_52w_high": chart_high,
        "chart_200dma": chart_200dma,
        "chart_points": len(closes),
    }


def extract_nasdaq_summary_value(page_html: str, label: str) -> str | None:
    pattern = re.compile(
        rf"<td[^>]*>\s*{re.escape(label)}\s*</td>\s*<td[^>]*>(.*?)</td>",
        re.I | re.S,
    )
    match = pattern.search(page_html)
    return clean_text(match.group(1)) if match else None


def fetch_nasdaq_100_official() -> tuple[dict, dict]:
    status = "Nasdaq 官方"
    summary = "指数点位和日内摘要来自 Nasdaq 官方指数页面，不使用 QQQ 或其他跟踪产品数据。"
    try:
        page_html = request_text(NASDAQ_NDX_URL, timeout=30)
        fields = {
            "last": extract_nasdaq_summary_value(page_html, "Last"),
            "net_change": extract_nasdaq_summary_value(page_html, "Net Change"),
            "net_change_pct": extract_nasdaq_summary_value(page_html, "Net Change(%)"),
            "day_high": extract_nasdaq_summary_value(page_html, "Day High"),
            "day_low": extract_nasdaq_summary_value(page_html, "Day Low"),
            "previous_close": extract_nasdaq_summary_value(page_html, "Previous Close"),
            "base_value": extract_nasdaq_summary_value(page_html, "Base Value"),
        }
    except Exception:
        fields = NASDAQ_NDX_FALLBACK.copy()
        status = "Nasdaq 官方缓存"
        summary = "Nasdaq 官方页面本次读取失败，当前展示最近一次已核验的 Nasdaq 官方缓存值。"

    highlights = [
        {"label": "官方指数点位", "value": number_or_dash(fields["last"]), "asOf": local_time(), "status": status},
        {"label": "涨跌点数", "value": number_or_dash(fields["net_change"]), "asOf": local_time(), "status": status},
        {"label": "涨跌幅", "value": number_or_dash(fields["net_change_pct"]), "asOf": local_time(), "status": status},
        {"label": "日内高点", "value": number_or_dash(fields["day_high"]), "asOf": local_time(), "status": status},
        {"label": "日内低点", "value": number_or_dash(fields["day_low"]), "asOf": local_time(), "status": status},
        {"label": "前收盘", "value": number_or_dash(fields["previous_close"]), "asOf": local_time(), "status": status},
    ]

    index_card = {
        "symbol": "NDX",
        "title": "Nasdaq-100 Index",
        "summary": summary,
        "sourceLabel": "Nasdaq Indexes",
        "sourceUrl": NASDAQ_NDX_URL,
        "highlights": highlights,
    }
    return index_card, fields


def fetch_sp500_official() -> tuple[dict, dict]:
    fields = {
        "last": None,
        "net_change": None,
        "net_change_pct": None,
        "day_high": None,
        "day_low": None,
        "previous_close": None,
        "base_value": None,
        "fetch_status": "S&P DJI 官网当前阻止本地自动抓取，未使用第三方替代。",
    }

    try:
        page_html = request_text(SPDJI_SPX_URL, timeout=15)
        text = clean_text(page_html)
        if "S&P 500" in text:
            fields["fetch_status"] = "已连接 S&P DJI 官方页面，但未发现稳定公开指数值结构。"
    except Exception as exc:
        fields["fetch_status"] = f"S&P DJI 官方页面自动抓取失败：{type(exc).__name__}"

    highlights = [
        {"label": "官方指数点位", "value": "官方源暂不可用", "asOf": local_time(), "status": "S&P DJI 官方未开放"},
        {"label": "涨跌点数", "value": "官方源暂不可用", "asOf": local_time(), "status": "S&P DJI 官方未开放"},
        {"label": "涨跌幅", "value": "官方源暂不可用", "asOf": local_time(), "status": "S&P DJI 官方未开放"},
        {"label": "日内高点", "value": "官方源暂不可用", "asOf": local_time(), "status": "S&P DJI 官方未开放"},
        {"label": "日内低点", "value": "官方源暂不可用", "asOf": local_time(), "status": "S&P DJI 官方未开放"},
        {"label": "前收盘", "value": "官方源暂不可用", "asOf": local_time(), "status": "S&P DJI 官方未开放"},
    ]

    index_card = {
        "symbol": "SPX",
        "title": "S&P 500 Index",
        "summary": fields["fetch_status"],
        "sourceLabel": "S&P Dow Jones Indices",
        "sourceUrl": SPDJI_SPX_URL,
        "highlights": highlights,
    }
    return index_card, fields


def fetch_yahoo_index_snapshot(symbol: str, page_url: str) -> dict:
    page_html = request_text(page_url, timeout=30)
    symbol_token = quote(symbol, safe="")
    quote_body = extract_embedded_payload(
        page_html,
        lambda data_url: "/v7/finance/quote?" in data_url
        and f"symbols={symbol_token}" in data_url
        and "regularMarketPrice" in data_url,
    )
    quote_data = quote_body["quoteResponse"]["result"][0]
    chart_metrics = fetch_yahoo_chart_metrics(symbol)

    summary = {}
    try:
        summary_body = extract_embedded_payload(
            page_html,
            lambda data_url: f"/v10/finance/quoteSummary/{symbol_token}" in data_url,
        )
        summary = summary_body["quoteSummary"]["result"][0].get("summaryDetail", {})
    except Exception:
        summary = {}

    price = float(raw_or_value(quote_data.get("regularMarketPrice")))
    previous_close = raw_or_value(quote_data.get("regularMarketPreviousClose"))
    day_high = raw_or_value(quote_data.get("regularMarketDayHigh"))
    day_low = raw_or_value(quote_data.get("regularMarketDayLow"))
    change = raw_or_value(quote_data.get("regularMarketChange"))
    change_pct = raw_or_value(quote_data.get("regularMarketChangePercent"))
    market_time = raw_or_value(quote_data.get("regularMarketTime"))
    fifty_two_week_high = (
        raw_or_value(summary.get("fiftyTwoWeekHigh"))
        or raw_or_value(quote_data.get("fiftyTwoWeekHigh"))
        or chart_metrics["chart_52w_high"]
    )
    two_hundred_day_average = raw_or_value(summary.get("twoHundredDayAverage")) or chart_metrics["chart_200dma"]
    trailing_pe = raw_or_value(summary.get("trailingPE"))

    drawdown = None
    if fifty_two_week_high:
        drawdown = (price / float(fifty_two_week_high) - 1) * 100

    distance_200dma = None
    if two_hundred_day_average:
        distance_200dma = (price / float(two_hundred_day_average) - 1) * 100

    return {
        "symbol": symbol,
        "price": price,
        "previous_close": previous_close,
        "day_high": day_high,
        "day_low": day_low,
        "change": change,
        "change_pct": change_pct,
        "market_time": market_time,
        "fifty_two_week_high": fifty_two_week_high,
        "drawdown": drawdown,
        "two_hundred_day_average": two_hundred_day_average,
        "distance_200dma": distance_200dma,
        "trailing_pe": trailing_pe,
        "chart_points": chart_metrics["chart_points"],
        "source_url": page_url,
    }


def safe_fetch_yahoo_index_snapshot(symbol: str, page_url: str) -> dict:
    try:
        snapshot = fetch_yahoo_index_snapshot(symbol, page_url)
        snapshot["status"] = "民间源在线"
        return snapshot
    except Exception:
        fallback = YAHOO_INDEX_FALLBACKS[symbol].copy()
        fallback["symbol"] = symbol
        fallback["status"] = "民间源缓存"
        return fallback


def pct_text(value) -> str:
    return "源无值" if value is None else f"{float(value):.2f}%"


def num_text(value) -> str:
    return "源无值" if value is None else f"{float(value):,.2f}"


def parse_percent_text(value: str | None) -> float | None:
    if not value:
        return None
    cleaned = value.replace("%", "").replace(",", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return None


def clamp(value: int, low: int = 0, high: int = 100) -> int:
    return max(low, min(high, value))


def fetch_fear_greed() -> dict:
    try:
        page_html = request_text(FEAR_GREED_URL, timeout=20)
        match = re.search(
            r"current value of the Fear & Greed Index as of ([^<]+?) is\s*<strong><mark>(\d+(?:\.\d+)?)\s*-\s*([^<]+)</mark>",
            page_html,
            re.I,
        )
        if not match:
            raise ValueError("fear greed value not found")
        return {
            "value": float(match.group(2)),
            "label": clean_text(match.group(3)).title(),
            "asOf": clean_text(match.group(1)),
            "status": "民间源在线",
            "sourceLabel": "Finhacker / CNN Fear & Greed mirror",
            "sourceUrl": FEAR_GREED_URL,
        }
    except Exception:
        return {
            **FEAR_GREED_FALLBACK,
            "status": "民间源缓存",
            "sourceLabel": "Finhacker / CNN Fear & Greed mirror",
            "sourceUrl": FEAR_GREED_URL,
        }


def fetch_naaim_exposure() -> dict:
    status = "官方在线"
    try:
        page_html = request_text(NAAIM_URL, timeout=30)
        match = re.search(r"data.addRows\(\[(.*?)\]\);", page_html, re.S)
        if not match:
            raise ValueError("NAAIM chart rows not found")
        rows = re.findall(r"\[new Date\((\d+),\s*(\d+),\s*(\d+)\),\s*([\d.-]+)\]", match.group(1))
        if not rows:
            raise ValueError("NAAIM numeric rows not found")

        series = []
        seen = set()
        for year, month, day, value in rows:
            date_key = (year, month, day)
            if date_key in seen:
                continue
            seen.add(date_key)
            # JavaScript Date months are zero-based.
            date_text = f"{int(year):04d}-{int(month) + 1:02d}-{int(day):02d}"
            series.append({"date": date_text, "value": float(value)})
    except Exception:
        series = NAAIM_FALLBACK_RECENT.copy()
        status = "官方缓存"

    recent = series[-12:]
    latest = recent[-1]
    avg_4w = sum(item["value"] for item in recent[-4:]) / min(4, len(recent))
    avg_12w = sum(item["value"] for item in recent) / len(recent)
    return {
        "value": latest["value"],
        "asOf": latest["date"],
        "avg4w": avg_4w,
        "avg12w": avg_12w,
        "recent": recent,
        "status": status,
        "sourceLabel": "NAAIM Exposure Index",
        "sourceUrl": NAAIM_URL,
    }


def fetch_aaii_sentiment() -> dict:
    try:
        page_html = request_text(AAII_URL, timeout=15)
        text = clean_text(page_html)
        bullish = re.search(r"Bullish[^0-9]*(\d+(?:\.\d+)?)%", text, re.I)
        neutral = re.search(r"Neutral[^0-9]*(\d+(?:\.\d+)?)%", text, re.I)
        bearish = re.search(r"Bearish[^0-9]*(\d+(?:\.\d+)?)%", text, re.I)
        if not (bullish and neutral and bearish):
            raise ValueError("AAII values not found")
        bullish_value = float(bullish.group(1))
        neutral_value = float(neutral.group(1))
        bearish_value = float(bearish.group(1))
        total = bullish_value + neutral_value + bearish_value
        if not (95 <= total <= 105) or bearish_value <= 0 or bullish_value <= 0:
            raise ValueError("AAII values failed sanity check")
        return {
            "bullish": bullish_value,
            "neutral": neutral_value,
            "bearish": bearish_value,
            "asOf": local_time(),
            "status": "官方在线",
            "sourceLabel": "AAII Sentiment Survey",
            "sourceUrl": AAII_URL,
            "sourceNote": "AAII官网在线解析。",
        }
    except Exception:
        return {
            **AAII_FALLBACK,
            "status": "民间转载缓存",
            "sourceLabel": "AAII Sentiment Survey",
            "sourceUrl": AAII_URL,
        }


def contrarian_score_fear_greed(value: float) -> int:
    if value <= 20:
        return 90
    if value <= 35:
        return 76
    if value <= 55:
        return 55
    if value <= 75:
        return 35
    return 18


def contrarian_score_naaim(value: float, avg12w: float) -> int:
    if value <= 35:
        base = 85
    elif value <= 55:
        base = 68
    elif value <= 75:
        base = 48
    elif value <= 90:
        base = 30
    else:
        base = 18

    if value > avg12w + 12:
        base -= 8
    elif value < avg12w - 12:
        base += 8
    return clamp(round(base))


def contrarian_score_aaii(bullish: float, bearish: float) -> int:
    spread = bullish - bearish
    if spread <= -20:
        return 82
    if spread <= -10:
        return 70
    if spread <= 5:
        return 55
    if spread <= 20:
        return 38
    return 22


def build_sentiment_score_module(fear_greed: dict, naaim: dict, aaii: dict) -> dict:
    fear_score = contrarian_score_fear_greed(fear_greed["value"])
    naaim_score = contrarian_score_naaim(naaim["value"], naaim["avg12w"])
    aaii_score = contrarian_score_aaii(aaii["bullish"], aaii["bearish"])
    total_score = round(fear_score * 0.4 + naaim_score * 0.35 + aaii_score * 0.25)

    if total_score >= 75:
        action = "情绪逆向强加仓"
        allocation = "情绪层支持额外分批加仓，但仍需要和价格回撤、宏观风险一起确认。"
    elif total_score >= 60:
        action = "情绪层支持加强定投"
        allocation = "可以把下一期定投提高20%到40%，但不建议只凭情绪一次性重仓。"
    elif total_score >= 45:
        action = "情绪层中性"
        allocation = "维持原定投节奏，等待恐慌或仓位降温后再提高强度。"
    else:
        action = "情绪层建议克制"
        allocation = "市场情绪和仓位偏热，额外加仓赔率不足，保留基础定投即可。"

    spread = aaii["bullish"] - aaii["bearish"]
    analysis = [
        f"恐贪指数为 {fear_greed['value']:.0f}，处于 {fear_greed['label']} 区间，逆向加仓分被压低。",
        f"NAAIM基金经理仓位为 {naaim['value']:.2f}，近12周均值约 {naaim['avg12w']:.2f}，说明主动管理人仓位不低。",
        f"AAII散户看多 {aaii['bullish']:.1f}%，看空 {aaii['bearish']:.1f}%，多空差为 {spread:.1f} 个百分点。",
    ]

    if fear_greed["value"] >= 60 and naaim["value"] >= 80:
        analysis.append("恐贪和基金经理仓位同时偏热，情绪层不支持激进追买。")
    if spread <= -10:
        analysis.append("散户明显偏悲观，这一点提供了一定逆向加分，但不足以抵消仓位偏热。")

    return {
        "title": "情绪加权评分",
        "action": action,
        "score": total_score,
        "allocation": allocation,
        "asOf": local_time(),
        "weights": [
            {"name": "恐贪指数", "weight": "40%", "score": fear_score, "value": f"{fear_greed['value']:.0f}", "status": fear_greed["status"]},
            {"name": "基金经理仓位 NAAIM", "weight": "35%", "score": naaim_score, "value": f"{naaim['value']:.2f}", "status": naaim["status"]},
            {"name": "散户多空 AAII", "weight": "25%", "score": aaii_score, "value": f"{spread:.1f} 点", "status": aaii["status"]},
        ],
        "analysis": analysis,
        "sources": [
            {"label": fear_greed["sourceLabel"], "url": fear_greed["sourceUrl"], "asOf": fear_greed["asOf"]},
            {"label": naaim["sourceLabel"], "url": naaim["sourceUrl"], "asOf": naaim["asOf"]},
            {"label": aaii["sourceLabel"], "url": aaii["sourceUrl"], "asOf": aaii["asOf"]},
        ],
        "recentNaaim": naaim["recent"][-8:],
        "note": "该模块是情绪层评分，和上方价格/宏观策略模块相互补充，不单独构成买卖建议。",
    }


def make_spx_card(spx: dict) -> dict:
    now = local_time()
    status = spx.get("status", "民间源")
    return {
        "symbol": "SPX",
        "title": "S&P 500 Index",
        "summary": "标普500官方自动抓取受限，当前按你的要求使用 Yahoo Finance 民间行情源，并在页面中明确标注。",
        "sourceLabel": "Yahoo Finance",
        "sourceUrl": YAHOO_SPX_URL,
        "highlights": [
            {"label": "指数点位", "value": num_text(spx["price"]), "asOf": now, "status": status},
            {"label": "涨跌点数", "value": num_text(spx["change"]), "asOf": now, "status": status},
            {"label": "涨跌幅", "value": pct_text(spx["change_pct"]), "asOf": now, "status": status},
            {"label": "52周回撤", "value": pct_text(spx["drawdown"]), "asOf": now, "status": f"{status}复算"},
            {"label": "距200日均线", "value": pct_text(spx["distance_200dma"]), "asOf": now, "status": f"{status}复算"},
            {"label": "TTM 市盈率", "value": num_text(spx["trailing_pe"]), "asOf": now, "status": status},
        ],
    }


def latest_fred_value(series_id: str) -> tuple[str, str]:
    start_year = datetime.now().year - 3
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}&cosd={start_year}-01-01"
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=5) as response:
        text = response.read().decode("utf-8")
    reader = csv.DictReader(text.splitlines())
    rows = [row for row in reader if row[series_id]]
    latest = rows[-1]
    return latest["observation_date"], latest[series_id]


def fred_metric(series_id: str) -> tuple[str, str, str]:
    try:
        date, value = latest_fred_value(series_id)
        return date, value, "官方在线"
    except Exception:
        fallback = FRED_FALLBACKS[series_id]
        return fallback["date"], fallback["value"], "官方缓存"


def make_index_metric(name: str, ndx_value: str | None, spx_value: str | None, note: str) -> dict:
    return {
        "name": name,
        "ndx": number_or_dash(ndx_value),
        "spx": number_or_dash(spx_value),
        "sourceLabel": "Nasdaq / S&P DJI 官方指数页面",
        "sourceUrlNdx": NASDAQ_NDX_URL,
        "sourceUrlSpx": SPDJI_SPX_URL,
        "asOf": local_time(),
        "status": "官方指数口径",
        "note": note,
    }


def make_mixed_index_metric(name: str, ndx_value: str | None, spx_value: str | None, spx_status: str, note: str) -> dict:
    return {
        "name": name,
        "ndx": number_or_dash(ndx_value),
        "spx": number_or_dash(spx_value),
        "sourceLabel": "NDX: Nasdaq 官方 / SPX: Yahoo Finance 民间源",
        "sourceUrlNdx": NASDAQ_NDX_URL,
        "sourceUrlSpx": YAHOO_SPX_URL,
        "asOf": local_time(),
        "status": spx_status,
        "note": note,
    }


def score_strategy(ndx: dict, spx: dict, vix: dict, macro: dict) -> dict:
    reasons = []
    analysis = []
    risk_notes = []
    factor_scores = []

    ndx_change_pct = parse_percent_text(ndx.get("net_change_pct"))
    spx_drawdown = spx.get("drawdown")
    spx_200dma = spx.get("distance_200dma")
    spx_trailing_pe = spx.get("trailing_pe")
    vix_level = vix.get("price")
    dgs10 = float(macro["dgs10"])
    sahm = float(macro["sahm"])
    unrate = float(macro["unrate"])

    price_score = 50
    if spx_drawdown is not None:
        if spx_drawdown <= -15:
            price_score = 88
            reasons.append("标普500回撤超过15%，价格位置进入强加仓观察区。")
        elif spx_drawdown <= -10:
            price_score = 75
            reasons.append("标普500回撤超过10%，适合提高定投强度。")
        elif spx_drawdown <= -5:
            price_score = 62
            reasons.append("标普500有一定回撤，但还不是深度便宜。")
        elif spx_drawdown > -2:
            price_score = 40
            reasons.append("标普500距离52周高点很近，价格没有提供明显安全边际。")
        analysis.append(f"价格位置：标普500当前距离52周高点约 {spx_drawdown:.2f}%。")
    else:
        analysis.append("价格位置：当前缺少标普500 52周回撤数据，价格吸引力只能降权处理。")
    factor_scores.append({"name": "价格位置", "score": price_score, "comment": "以标普500相对52周高点的回撤衡量买入安全边际。"})

    trend_score = 50
    if spx_200dma is not None:
        if spx_200dma < -3:
            trend_score = 72
            reasons.append("标普500低于200日均线，趋势层支持分批加大买入。")
            analysis.append(f"趋势位置：标普500低于200日均线 {abs(spx_200dma):.2f}%，属于回落后的观察区。")
        elif spx_200dma > 8:
            trend_score = 32
            reasons.append("标普500显著高于200日均线，追加强度应保守。")
            analysis.append(f"趋势位置：标普500高于200日均线 {spx_200dma:.2f}%，短期追加强度不宜太激进。")
        elif spx_200dma > 0:
            trend_score = 46
            analysis.append(f"趋势位置：标普500高于200日均线 {spx_200dma:.2f}%，趋势健康但不便宜。")
        else:
            trend_score = 58
            analysis.append(f"趋势位置：标普500略低于200日均线 {abs(spx_200dma):.2f}%，可小幅增强定投。")
    else:
        analysis.append("趋势位置：当前缺少200日均线偏离数据。")
    factor_scores.append({"name": "趋势位置", "score": trend_score, "comment": "以标普500相对200日均线偏离衡量追高或回落状态。"})

    sentiment_score = 50
    if vix_level is not None:
        if vix_level >= 30:
            sentiment_score = 85
            reasons.append("VIX高于30，市场恐慌升温，适合逆向分批加仓。")
        elif vix_level >= 22:
            sentiment_score = 68
            reasons.append("VIX处于偏紧张区间，可适度增加定投。")
        elif vix_level < 16:
            sentiment_score = 40
            reasons.append("VIX偏低，情绪不恐慌，额外加仓的赔率一般。")
        else:
            sentiment_score = 52
        analysis.append(f"情绪层：VIX 当前约 {vix_level:.2f}，没有出现明显恐慌溢价。" if vix_level < 22 else f"情绪层：VIX 当前约 {vix_level:.2f}，市场压力上升。")
    else:
        analysis.append("情绪层：当前缺少VIX数据。")
    factor_scores.append({"name": "市场情绪", "score": sentiment_score, "comment": "以VIX衡量是否存在逆向加仓的恐慌环境。"})

    macro_score = 55
    if sahm >= 0.5:
        macro_score -= 25
        reasons.append("Sahm Rule达到衰退警戒区，应优先控制加仓节奏。")
        risk_notes.append("Sahm Rule 已触发衰退警戒，策略应避免一次性重仓。")
    elif sahm >= 0.3:
        macro_score -= 12
        reasons.append("Sahm Rule接近警戒区，宏观风险权重上升。")
        risk_notes.append("Sahm Rule 接近警戒区，需要观察就业是否继续走弱。")

    if dgs10 >= 4.5:
        macro_score -= 12
        reasons.append("10年期美债收益率偏高，对成长股估值有压制。")
        risk_notes.append("10年期美债收益率偏高，纳指估值扩张空间受限。")
    elif dgs10 <= 3.8:
        macro_score += 6
        reasons.append("10年期美债收益率相对温和，有利于估值修复。")

    if unrate >= 4.5:
        macro_score -= 8
        reasons.append("失业率偏高，需要避免一次性重仓。")
        risk_notes.append("失业率偏高，若继续上行可能压制盈利预期。")

    macro_score = clamp(macro_score)
    analysis.append(f"宏观层：10年期美债 {dgs10:.2f}%，失业率 {unrate:.1f}%，Sahm Rule {sahm:.2f}。")
    factor_scores.append({"name": "宏观风险", "score": macro_score, "comment": "综合利率、失业率和Sahm Rule判断系统性风险。"})

    valuation_score = 50
    if spx_trailing_pe is not None:
        if spx_trailing_pe >= 28:
            valuation_score = 35
            reasons.append("标普500 TTM 市盈率偏高，估值层不支持激进加仓。")
        elif spx_trailing_pe >= 24:
            valuation_score = 45
            reasons.append("标普500估值不低，适合维持或小幅增强，不适合重仓。")
        elif spx_trailing_pe <= 18:
            valuation_score = 70
            reasons.append("标普500估值回到较有吸引力区间。")
        analysis.append(f"估值层：标普500 TTM 市盈率约 {spx_trailing_pe:.2f}，用于辅助判断买入赔率。")
    else:
        analysis.append("估值层：当前缺少标普500 TTM 市盈率数据。")
    factor_scores.append({"name": "估值压力", "score": valuation_score, "comment": "以民间源可得的标普500 TTM 市盈率做辅助。"})

    breadth_proxy_score = 50
    if ndx_change_pct is not None and spx.get("change_pct") is not None:
        spx_change_pct = float(spx["change_pct"])
        if ndx_change_pct - spx_change_pct > 1.2:
            breadth_proxy_score = 42
            reasons.append("纳指涨幅明显强于标普，行情可能更依赖成长风格。")
            analysis.append("结构层：纳指明显强于标普，说明成长风格占优，但广度未必充分。")
        elif spx_change_pct - ndx_change_pct > 1.0:
            breadth_proxy_score = 58
            analysis.append("结构层：标普强于纳指，行情相对更均衡。")
        else:
            breadth_proxy_score = 52
            analysis.append("结构层：纳指和标普表现接近，没有明显风格背离。")
    else:
        analysis.append("结构层：缺少纳指和标普的可比涨跌幅，暂不判断风格背离。")
    factor_scores.append({"name": "结构广度", "score": breadth_proxy_score, "comment": "用NDX与SPX当日涨跌差做粗略风格代理。"})

    score = round(
        price_score * 0.25
        + trend_score * 0.18
        + sentiment_score * 0.16
        + macro_score * 0.18
        + valuation_score * 0.15
        + breadth_proxy_score * 0.08
    )
    score = clamp(score)
    if score >= 75:
        action = "强力加仓"
        allocation = "额外加仓资金可分3到5笔执行，同时保留后续下跌弹药。"
        execution_plan = [
            "额外资金分3到5笔执行，不一次性打满。",
            "若继续下跌5%以上，保留下一档加仓预算。",
            "如果VIX继续上升但Sahm Rule未触发，可继续按计划分批。",
        ]
    elif score >= 60:
        action = "加强定投"
        allocation = "把下一期定投金额提高30%到50%，不建议一次性打满。"
        execution_plan = [
            "下一期定投金额提高30%到50%。",
            "额外加仓只做小批量，不追日内上涨。",
            "若回撤扩大到10%以上，再进入更高强度档位。",
        ]
    elif score >= 45:
        action = "正常定投"
        allocation = "维持原定投金额，等待更明显的回撤或恐慌信号。"
        execution_plan = [
            "维持原计划定投金额。",
            "不做额外大额加仓。",
            "等待回撤、VIX或200DMA信号给出更明确的赔率。",
        ]
    else:
        action = "减少追加"
        allocation = "保留基础定投，暂停额外加仓或降低追加金额。"
        execution_plan = [
            "保留基础定投，不中断长期计划。",
            "暂停额外追加或降低追加金额。",
            "等待估值、利率或回撤条件改善后再恢复增强定投。",
        ]

    if not reasons:
        reasons.append("当前数据没有触发明显增强或降低定投的条件。")
    if not risk_notes:
        risk_notes.append("当前没有触发明确的衰退或利率极端风险，但价格位置仍偏高。")

    return {
        "title": "今日策略建议",
        "action": action,
        "score": score,
        "allocation": allocation,
        "asOf": local_time(),
        "factorScores": factor_scores,
        "analysis": analysis,
        "reasons": reasons,
        "executionPlan": execution_plan,
        "riskNotes": risk_notes,
        "dataSnapshot": [
            {"label": "SPX 52周回撤", "value": pct_text(spx_drawdown)},
            {"label": "SPX 距200DMA", "value": pct_text(spx_200dma)},
            {"label": "VIX", "value": num_text(vix_level)},
            {"label": "10年期美债", "value": f"{dgs10:.2f}%"},
            {"label": "Sahm Rule", "value": f"{sahm:.2f}"},
            {"label": "失业率", "value": f"{unrate:.1f}%"},
        ],
        "disclaimer": "这是基于当前面板数据的规则化建议，不构成投资顾问意见。",
    }


def build_dashboard_payload() -> dict:
    with ThreadPoolExecutor(max_workers=10) as executor:
        ndx_future = executor.submit(fetch_nasdaq_100_official)
        spx_official_future = executor.submit(fetch_sp500_official)
        spx_yahoo_future = executor.submit(safe_fetch_yahoo_index_snapshot, "^GSPC", YAHOO_SPX_URL)
        vix_future = executor.submit(safe_fetch_yahoo_index_snapshot, "^VIX", YAHOO_VIX_URL)
        fear_greed_future = executor.submit(fetch_fear_greed)
        naaim_future = executor.submit(fetch_naaim_exposure)
        aaii_future = executor.submit(fetch_aaii_sentiment)
        effr_future = executor.submit(fred_metric, "EFFR")
        dgs10_future = executor.submit(fred_metric, "DGS10")
        t10y2y_future = executor.submit(fred_metric, "T10Y2Y")
        unrate_future = executor.submit(fred_metric, "UNRATE")
        sahm_future = executor.submit(fred_metric, "SAHMREALTIME")

        ndx_card, ndx = ndx_future.result()
        _spx_official_card, _spx_official = spx_official_future.result()
        spx = spx_yahoo_future.result()
        spx_card = make_spx_card(spx)
        vix = vix_future.result()
        fear_greed = fear_greed_future.result()
        naaim = naaim_future.result()
        aaii = aaii_future.result()
        effr_date, effr_value, effr_status = effr_future.result()
        dgs10_date, dgs10_value, dgs10_status = dgs10_future.result()
        t10y2y_date, t10y2y_value, t10y2y_status = t10y2y_future.result()
        unrate_date, unrate_value, unrate_status = unrate_future.result()
        sahm_date, sahm_value, sahm_status = sahm_future.result()

    macro_values = {
        "dgs10": dgs10_value,
        "sahm": sahm_value,
        "unrate": unrate_value,
    }
    strategy = score_strategy(ndx, spx, vix, macro_values)
    sentiment_score = build_sentiment_score_module(fear_greed, naaim, aaii)

    return {
        "meta": {
            "mode": "official-only",
            "boardStatus": "混合数据口径已启用",
            "boardSummary": "纳指100使用 Nasdaq 官方页面；标普500、VIX 和技术指标按你的要求使用民间源，并在表格中明确标注。",
            "fetchedAtLocal": local_time(),
            "refreshSeconds": CACHE_TTL_SECONDS,
        },
        "indices": {
            "ndx": ndx_card,
            "spx": spx_card,
        },
        "strategy": strategy,
        "sentimentScore": sentiment_score,
        "officialIndexMetrics": [
            make_mixed_index_metric("指数点位", ndx["last"], num_text(spx["price"]), "混合源", "NDX 为 Nasdaq 官方指数点位；SPX 为 Yahoo Finance 民间源。"),
            make_mixed_index_metric("涨跌点数", ndx["net_change"], num_text(spx["change"]), "混合源", "NDX 为 Nasdaq 官方；SPX 为民间源。"),
            make_mixed_index_metric("涨跌幅", ndx["net_change_pct"], pct_text(spx["change_pct"]), "混合源", "NDX 为 Nasdaq 官方；SPX 为民间源。"),
            make_mixed_index_metric("日内高点", ndx["day_high"], num_text(spx["day_high"]), "混合源", "NDX 为 Nasdaq 官方；SPX 为民间源。"),
            make_mixed_index_metric("日内低点", ndx["day_low"], num_text(spx["day_low"]), "混合源", "NDX 为 Nasdaq 官方；SPX 为民间源。"),
            make_mixed_index_metric("前收盘", ndx["previous_close"], num_text(spx["previous_close"]), "混合源", "NDX 为 Nasdaq 官方；SPX 为民间源。"),
            make_mixed_index_metric("TTM 市盈率", None, num_text(spx["trailing_pe"]), "民间源", "NDX 官方公开页未提供；SPX 使用 Yahoo Finance 可解析字段。"),
            make_mixed_index_metric("52周回撤", None, pct_text(spx["drawdown"]), "民间源复算", "NDX 官方公开页未提供历史序列；SPX 用民间源最新价和52周高点复算。"),
            make_mixed_index_metric("距200日均线偏离", None, pct_text(spx["distance_200dma"]), "民间源复算", "NDX 官方公开页未提供200DMA；SPX 用民间源复算。"),
            make_mixed_index_metric("VIX", None, num_text(vix["price"]), "民间源", "VIX 使用 Yahoo Finance 民间源，用作情绪层输入。"),
        ],
        "macroMetrics": [
            {
                "name": "有效联邦基金利率 EFFR",
                "value": f"{effr_value}%",
                "asOf": effr_date,
                "sourceLabel": "FRED / New York Fed",
                "sourceUrl": "https://fred.stlouisfed.org/series/EFFR",
                "status": effr_status,
                "note": "官方宏观数据，和指数口径分开展示。",
            },
            {
                "name": "10年期美债收益率",
                "value": f"{dgs10_value}%",
                "asOf": dgs10_date,
                "sourceLabel": "FRED / Federal Reserve H.15",
                "sourceUrl": "https://fred.stlouisfed.org/series/DGS10",
                "status": dgs10_status,
                "note": "官方宏观数据，和指数口径分开展示。",
            },
            {
                "name": "10Y-2Y 利差",
                "value": f"{t10y2y_value}%",
                "asOf": t10y2y_date,
                "sourceLabel": "FRED",
                "sourceUrl": "https://fred.stlouisfed.org/series/T10Y2Y",
                "status": t10y2y_status,
                "note": "官方宏观数据，和指数口径分开展示。",
            },
            {
                "name": "美国失业率",
                "value": f"{unrate_value}%",
                "asOf": unrate_date,
                "sourceLabel": "FRED / BLS",
                "sourceUrl": "https://fred.stlouisfed.org/series/UNRATE",
                "status": unrate_status,
                "note": "官方宏观数据，和指数口径分开展示。",
            },
            {
                "name": "Sahm Rule",
                "value": sahm_value,
                "asOf": sahm_date,
                "sourceLabel": "FRED / Claudia Sahm",
                "sourceUrl": "https://fred.stlouisfed.org/series/SAHMREALTIME",
                "status": sahm_status,
                "note": "官方宏观数据，和指数口径分开展示。",
            },
        ],
        "rules": [
            {
                "title": "纳指坚持官方",
                "body": "Nasdaq-100 使用 Nasdaq 官方指数页面，不使用 QQQ 或第三方纳指替代。",
            },
            {
                "title": "其他字段允许民间源",
                "body": "标普500、VIX、技术指标和估值字段允许使用民间源，但必须在表格中标注来源层级。",
            },
            {
                "title": "策略只用当前面板数据",
                "body": "今日策略建议由回撤、200DMA、VIX、10年期美债、失业率和 Sahm Rule 规则化生成。",
            },
        ],
        "sources": [
            {
                "title": "Nasdaq-100",
                "body": "指数点位来自 Nasdaq 官方 Index Overview 页面。",
            },
            {
                "title": "S&P 500",
                "body": "S&P DJI 官方自动抓取受限，当前使用 Yahoo Finance 民间源补充 SPX 点位、回撤、200DMA 和估值字段。",
            },
            {
                "title": "授权边界",
                "body": "S&P DJI 和 Nasdaq 的完整历史、权重、估值等字段通常属于授权数据服务；要保证官方口径，需要后续接官方授权接口或下载文件。",
            },
        ],
    }


def get_dashboard_payload() -> dict:
    now = time.time()
    with CACHE_LOCK:
        if CACHE.payload is not None and now < CACHE.expires_at:
            return CACHE.payload

    payload = build_dashboard_payload()

    with CACHE_LOCK:
        CACHE.payload = payload
        CACHE.expires_at = time.time() + CACHE_TTL_SECONDS
        return CACHE.payload


class DashboardHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/dashboard":
            self.serve_dashboard()
            return
        if parsed.path == "/api/health":
            self.serve_health()
            return
        if parsed.path == "/":
            self.path = "/index.html"
        super().do_GET()

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        super().end_headers()

    def serve_dashboard(self):
        try:
            payload = get_dashboard_payload()
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as exc:
            error_body = json.dumps({"error": str(exc)}, ensure_ascii=False).encode("utf-8")
            self.send_response(HTTPStatus.INTERNAL_SERVER_ERROR)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(error_body)))
            self.end_headers()
            self.wfile.write(error_body)

    def serve_health(self):
        body = json.dumps({"ok": True, "timestamp": local_time()}, ensure_ascii=False).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    host = "127.0.0.1"
    port = 8000
    server = ThreadingHTTPServer((host, port), DashboardHandler)
    print(f"Serving on http://{host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
