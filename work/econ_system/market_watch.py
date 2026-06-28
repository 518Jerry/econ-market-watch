#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import email.utils
import html
import json
import math
import os
import re
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent.parent
CONFIG_PATH = ROOT / "config.json"
DATA_DIR = ROOT / "data"
HISTORY_DIR = DATA_DIR / "history"
OUTPUT_DIR = Path(os.environ.get("ECON_OUTPUT_DIR", PROJECT_ROOT / "outputs"))

LATEST_JSON = DATA_DIR / "latest_snapshot.json"
HISTORY_JSONL = DATA_DIR / "history.jsonl"
REPORT_PATH = OUTPUT_DIR / "latest_market_brief.md"
OUTPUT_SNAPSHOT_PATH = OUTPUT_DIR / "latest_snapshot.json"
SNAPSHOT_JS_PATH = OUTPUT_DIR / "market_data.js"
DASHBOARD_PATH = OUTPUT_DIR / "econ_dashboard.html"
INDEX_PATH = OUTPUT_DIR / "index.html"
ENTRY_PROMPT_PATH = OUTPUT_DIR / "chatgpt_entry_prompt.md"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0 Safari/537.36 CodexMarketWatch/1.0"
)

POSITIVE_WORDS = {
    "gain",
    "gains",
    "rally",
    "rises",
    "rose",
    "higher",
    "rebound",
    "rebounds",
    "recovery",
    "eases",
    "cooling",
    "cuts",
    "cut",
    "dovish",
    "stimulus",
    "support",
    "inflows",
    "approval",
    "approved",
    "resilient",
    "beats",
    "record",
    "soft landing",
    "demand",
    "outperform",
    "bullish",
    "上涨",
    "上升",
    "反弹",
    "走强",
    "利好",
    "刺激",
    "降息",
    "流入",
    "复苏",
    "超预期",
    "支持",
}

NEGATIVE_WORDS = {
    "fall",
    "falls",
    "fell",
    "lower",
    "selloff",
    "sell-off",
    "tumbles",
    "slump",
    "weak",
    "misses",
    "miss",
    "hawkish",
    "higher yields",
    "inflation",
    "recession",
    "default",
    "crackdown",
    "regulation",
    "sanctions",
    "war",
    "geopolitical",
    "risk",
    "volatility",
    "outflows",
    "bearish",
    "下跌",
    "走弱",
    "回落",
    "暴跌",
    "风险",
    "监管",
    "制裁",
    "衰退",
    "通胀",
    "鹰派",
    "流出",
    "低迷",
}


def now_local() -> dt.datetime:
    return dt.datetime.now().astimezone()


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def load_config() -> Dict[str, Any]:
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def pct(value: Optional[float]) -> str:
    if value is None or not math.isfinite(value):
        return "-"
    sign = "+" if value > 0 else ""
    return f"{sign}{value:.2f}%"


def num(value: Optional[float]) -> str:
    if value is None or not math.isfinite(value):
        return "-"
    if abs(value) >= 1000:
        return f"{value:,.2f}"
    if abs(value) >= 10:
        return f"{value:.2f}"
    return f"{value:.4f}"


def safe_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        value = float(value)
        if math.isnan(value) or math.isinf(value):
            return None
        return value
    except (TypeError, ValueError):
        return None


def fetch_bytes(url: str, timeout: int = 12, max_bytes: int = 5_000_000) -> bytes:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json,text/xml,application/xml,text/html;q=0.9,*/*;q=0.8",
            "Accept-Encoding": "identity",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read(max_bytes)


def yahoo_url(symbol: str, chart_range: str = "6mo", interval: str = "1d") -> str:
    encoded = urllib.parse.quote(symbol, safe="")
    params = urllib.parse.urlencode(
        {
            "range": chart_range,
            "interval": interval,
            "includePrePost": "false",
            "events": "div,splits",
        }
    )
    return f"https://query1.finance.yahoo.com/v8/finance/chart/{encoded}?{params}"


def market_scale(symbol: str) -> Tuple[float, str]:
    if symbol == "^TNX":
        return 1.0, "%"
    return 1.0, ""


def fetch_chart(asset: Dict[str, Any]) -> Dict[str, Any]:
    symbol = asset["symbol"]
    url = yahoo_url(symbol)
    started = time.time()
    try:
        payload = fetch_bytes(url, timeout=16)
        data = json.loads(payload.decode("utf-8"))
        chart = data.get("chart", {})
        errors = chart.get("error")
        if errors:
            raise RuntimeError(str(errors))
        result = (chart.get("result") or [None])[0]
        if not result:
            raise RuntimeError("empty Yahoo Finance chart result")

        meta = result.get("meta") or {}
        timestamps = result.get("timestamp") or []
        quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
        scale, display_unit = market_scale(symbol)

        opens = quote.get("open") or []
        highs = quote.get("high") or []
        lows = quote.get("low") or []
        closes = quote.get("close") or []
        volumes = quote.get("volume") or []
        points: List[Dict[str, Any]] = []
        for i, ts in enumerate(timestamps):
            close = safe_float(closes[i] if i < len(closes) else None)
            if close is None:
                continue
            open_value = safe_float(opens[i] if i < len(opens) else None)
            high_value = safe_float(highs[i] if i < len(highs) else None)
            low_value = safe_float(lows[i] if i < len(lows) else None)
            volume_value = safe_float(volumes[i] if i < len(volumes) else None)
            when = dt.datetime.fromtimestamp(int(ts), tz=dt.timezone.utc)
            points.append(
                {
                    "date": when.date().isoformat(),
                    "time_utc": when.isoformat(),
                    "open": None if open_value is None else open_value * scale,
                    "high": None if high_value is None else high_value * scale,
                    "low": None if low_value is None else low_value * scale,
                    "close": close * scale,
                    "volume": volume_value,
                }
            )

        if not points:
            raise RuntimeError("no valid close prices")

        indicators = compute_indicators(points)
        price = indicators.get("latest")
        market_time = meta.get("regularMarketTime") or meta.get("firstTradeDate")
        market_time_iso = None
        if market_time:
            market_time_iso = dt.datetime.fromtimestamp(int(market_time), tz=dt.timezone.utc).isoformat()

        return {
            "symbol": symbol,
            "name": asset.get("name", symbol),
            "category": asset.get("category", "其他"),
            "role": asset.get("role", ""),
            "status": "ok",
            "source": "Yahoo Finance chart API",
            "source_url": url,
            "currency": meta.get("currency") or "",
            "exchange": meta.get("exchangeName") or meta.get("fullExchangeName") or "",
            "display_unit": display_unit,
            "market_time_utc": market_time_iso,
            "regular_market_price": price,
            "points": points,
            "indicators": indicators,
            "fetch_seconds": round(time.time() - started, 2),
        }
    except Exception as exc:  # noqa: BLE001 - keep one asset failure isolated.
        return {
            "symbol": symbol,
            "name": asset.get("name", symbol),
            "category": asset.get("category", "其他"),
            "role": asset.get("role", ""),
            "status": "error",
            "source": "Yahoo Finance chart API",
            "source_url": url,
            "error": f"{type(exc).__name__}: {exc}",
            "points": [],
            "indicators": {},
            "fetch_seconds": round(time.time() - started, 2),
        }


def compute_indicators(points: List[Dict[str, Any]]) -> Dict[str, Any]:
    closes = [safe_float(p.get("close")) for p in points if safe_float(p.get("close")) is not None]
    if not closes:
        return {}

    latest = closes[-1]
    previous = closes[-2] if len(closes) >= 2 else None

    def ret(days: int) -> Optional[float]:
        if len(closes) <= days:
            return None
        base = closes[-1 - days]
        if not base:
            return None
        return (latest / base - 1.0) * 100.0

    def abs_change(days: int) -> Optional[float]:
        if len(closes) <= days:
            return None
        return latest - closes[-1 - days]

    def sma(days: int) -> Optional[float]:
        if len(closes) < days:
            return None
        return sum(closes[-days:]) / days

    daily_returns = []
    for i in range(1, len(closes)):
        base = closes[i - 1]
        if base:
            daily_returns.append(closes[i] / base - 1.0)
    vol_20 = None
    if len(daily_returns) >= 10:
        sample = daily_returns[-20:]
        avg = sum(sample) / len(sample)
        variance = sum((x - avg) ** 2 for x in sample) / max(len(sample) - 1, 1)
        vol_20 = math.sqrt(variance) * math.sqrt(252) * 100.0

    rsi_14 = compute_rsi(closes, 14)
    ma20 = sma(20)
    ma50 = sma(50)
    ma100 = sma(100)
    recent_60 = closes[-60:] if len(closes) >= 60 else closes
    support_60 = min(recent_60)
    resistance_60 = max(recent_60)
    high_60 = resistance_60
    drawdown = (latest / high_60 - 1.0) * 100.0 if high_60 else None

    trend_score = 0.0
    signals: List[str] = []

    r5 = ret(5)
    r21 = ret(21)
    if r5 is not None:
        if r5 > 1:
            trend_score += 0.8
            signals.append(f"5日涨幅 {pct(r5)}")
        elif r5 < -1:
            trend_score -= 0.8
            signals.append(f"5日跌幅 {pct(r5)}")
    if r21 is not None:
        if r21 > 3:
            trend_score += 1.0
            signals.append(f"约1个月涨幅 {pct(r21)}")
        elif r21 < -3:
            trend_score -= 1.0
            signals.append(f"约1个月跌幅 {pct(r21)}")

    if ma20:
        if latest > ma20:
            trend_score += 0.7
            signals.append("价格在20日均线之上")
        else:
            trend_score -= 0.7
            signals.append("价格在20日均线之下")
    if ma20 and ma50:
        if ma20 > ma50:
            trend_score += 0.6
            signals.append("20日均线高于50日均线")
        else:
            trend_score -= 0.6
            signals.append("20日均线低于50日均线")
    if rsi_14 is not None:
        if rsi_14 >= 72:
            trend_score -= 0.4
            signals.append(f"RSI {rsi_14:.1f}，短线偏热")
        elif rsi_14 <= 30:
            trend_score += 0.2
            signals.append(f"RSI {rsi_14:.1f}，存在超跌修复可能")

    return {
        "latest": latest,
        "previous": previous,
        "change_1d": None if previous in (None, 0) else latest - previous,
        "return_1d": ret(1),
        "return_5d": r5,
        "return_1m": r21,
        "return_3m": ret(63),
        "return_6m": None if len(closes) < 2 or not closes[0] else (latest / closes[0] - 1.0) * 100.0,
        "change_5d_abs": abs_change(5),
        "change_1m_abs": abs_change(21),
        "sma_20": ma20,
        "sma_50": ma50,
        "sma_100": ma100,
        "rsi_14": rsi_14,
        "volatility_20d_ann": vol_20,
        "support_60d": support_60,
        "resistance_60d": resistance_60,
        "drawdown_from_60d_high": drawdown,
        "trend_score": round(trend_score, 2),
        "trend_signals": signals[:6],
        "point_count": len(closes),
        "last_date": points[-1].get("date"),
    }


def compute_rsi(values: List[float], window: int = 14) -> Optional[float]:
    if len(values) <= window:
        return None
    changes = [values[i] - values[i - 1] for i in range(1, len(values))]
    sample = changes[-window:]
    gains = [x for x in sample if x > 0]
    losses = [-x for x in sample if x < 0]
    avg_gain = sum(gains) / window
    avg_loss = sum(losses) / window
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def fetch_all_assets(assets: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    max_workers = min(8, max(1, len(assets)))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {executor.submit(fetch_chart, asset): asset for asset in assets}
        for future in as_completed(future_map):
            results.append(future.result())
    order = {asset["symbol"]: idx for idx, asset in enumerate(assets)}
    results.sort(key=lambda item: order.get(item["symbol"], 999))
    return results


def clean_text(value: Optional[str]) -> str:
    if not value:
        return ""
    value = html.unescape(value)
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def article_sentiment(title: str) -> int:
    lower = title.lower()
    score = 0
    for word in POSITIVE_WORDS:
        if word.lower() in lower or word in title:
            score += 1
    for word in NEGATIVE_WORDS:
        if word.lower() in lower or word in title:
            score -= 1
    return max(-3, min(3, score))


def parse_rss_date(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc).isoformat()
    except Exception:
        return None


def fetch_news_topic(topic: Dict[str, str], max_items: int = 10) -> Dict[str, Any]:
    query = topic["query"]
    params = urllib.parse.urlencode(
        {
            "q": query,
            "hl": "en-US",
            "gl": "US",
            "ceid": "US:en",
        }
    )
    url = f"https://news.google.com/rss/search?{params}"
    started = time.time()
    try:
        payload = fetch_bytes(url, timeout=12, max_bytes=1_000_000)
        root = ET.fromstring(payload)
        items = []
        for item in root.findall("./channel/item")[:max_items]:
            title = clean_text(item.findtext("title"))
            link = clean_text(item.findtext("link"))
            pub = parse_rss_date(item.findtext("pubDate"))
            source = ""
            source_url = ""
            for child in list(item):
                if child.tag.endswith("source"):
                    source = clean_text(child.text)
                    source_url = child.attrib.get("url", "")
                    break
            items.append(
                {
                    "title": title,
                    "link": link,
                    "published_utc": pub,
                    "source": source,
                    "source_url": source_url,
                    "sentiment": article_sentiment(title),
                }
            )
        average = None
        if items:
            average = sum(item["sentiment"] for item in items) / len(items)
        return {
            "name": topic["name"],
            "query": query,
            "status": "ok",
            "source": "Google News RSS",
            "source_url": url,
            "sentiment_average": average,
            "article_count": len(items),
            "articles": items,
            "fetch_seconds": round(time.time() - started, 2),
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "name": topic["name"],
            "query": query,
            "status": "error",
            "source": "Google News RSS",
            "source_url": url,
            "error": f"{type(exc).__name__}: {exc}",
            "sentiment_average": None,
            "article_count": 0,
            "articles": [],
            "fetch_seconds": round(time.time() - started, 2),
        }


def fetch_all_news(news_queries: List[Dict[str, str]]) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    max_workers = min(6, max(1, len(news_queries)))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {executor.submit(fetch_news_topic, topic): topic for topic in news_queries}
        for future in as_completed(future_map):
            results.append(future.result())
    order = {topic["name"]: idx for idx, topic in enumerate(news_queries)}
    results.sort(key=lambda item: order.get(item["name"], 999))
    return results


def get_asset(assets: List[Dict[str, Any]], symbol: str) -> Optional[Dict[str, Any]]:
    for asset in assets:
        if asset.get("symbol") == symbol:
            return asset
    return None


def get_indicator(assets: List[Dict[str, Any]], symbol: str, key: str) -> Optional[float]:
    asset = get_asset(assets, symbol)
    if not asset:
        return None
    return safe_float((asset.get("indicators") or {}).get(key))


def news_topic(news: List[Dict[str, Any]], name: str) -> Optional[Dict[str, Any]]:
    for item in news:
        if item.get("name") == name:
            return item
    return None


def weighted_news_score(news: List[Dict[str, Any]], names: Iterable[Tuple[str, float]]) -> float:
    total = 0.0
    weight_sum = 0.0
    for name, weight in names:
        topic = news_topic(news, name)
        avg = safe_float((topic or {}).get("sentiment_average"))
        if avg is None:
            continue
        total += avg * weight
        weight_sum += abs(weight)
    if weight_sum == 0:
        return 0.0
    return total / weight_sum


def category_news_score(category: str, news: List[Dict[str, Any]]) -> float:
    if category == "美股":
        return weighted_news_score(news, [("美股", 0.65), ("全球宏观", 0.35)])
    if category == "A股/港股":
        return weighted_news_score(news, [("A股/中国", 0.8), ("全球宏观", 0.2)])
    if category == "虚拟货币":
        return weighted_news_score(news, [("虚拟货币", 0.8), ("全球宏观", 0.2)])
    if category == "黄金/贵金属":
        return weighted_news_score(news, [("黄金/贵金属", 0.75), ("全球宏观", 0.25)])
    if category == "商品":
        return weighted_news_score(news, [("商品", 0.75), ("全球宏观", 0.25)])
    return weighted_news_score(news, [("全球宏观", 1.0)])


def macro_context(assets: List[Dict[str, Any]]) -> Dict[str, Any]:
    dxy_5d = get_indicator(assets, "DX-Y.NYB", "return_5d")
    dxy_1m = get_indicator(assets, "DX-Y.NYB", "return_1m")
    tnx = get_indicator(assets, "^TNX", "latest")
    tnx_5d_abs = get_indicator(assets, "^TNX", "change_5d_abs")
    vix = get_indicator(assets, "^VIX", "latest")
    cny_5d = get_indicator(assets, "CNY=X", "return_5d")
    spx_5d = get_indicator(assets, "^GSPC", "return_5d")
    btc_5d = get_indicator(assets, "BTC-USD", "return_5d")

    notes: List[str] = []
    if dxy_5d is not None:
        notes.append(f"美元指数5日 {pct(dxy_5d)}")
    if tnx is not None:
        suffix = ""
        if tnx_5d_abs is not None:
            suffix = f"，5日变化 {tnx_5d_abs:+.2f} 个百分点"
        notes.append(f"美国10年期收益率 {tnx:.2f}%{suffix}")
    if vix is not None:
        notes.append(f"VIX {vix:.2f}")
    if cny_5d is not None:
        notes.append(f"美元/人民币5日 {pct(cny_5d)}")

    risk_regime = "中性"
    if vix is not None and vix >= 25:
        risk_regime = "避险升温"
    elif spx_5d is not None and btc_5d is not None and spx_5d > 1 and btc_5d > 3:
        risk_regime = "风险偏好较强"
    elif spx_5d is not None and spx_5d < -2:
        risk_regime = "风险偏好降温"

    return {
        "dxy_5d": dxy_5d,
        "dxy_1m": dxy_1m,
        "tnx": tnx,
        "tnx_5d_abs": tnx_5d_abs,
        "vix": vix,
        "cny_5d": cny_5d,
        "spx_5d": spx_5d,
        "btc_5d": btc_5d,
        "risk_regime": risk_regime,
        "notes": notes,
    }


def bias_label(score: float) -> str:
    if score >= 2.6:
        return "短线偏强"
    if score >= 1.0:
        return "中性偏强"
    if score > -1.0:
        return "震荡/中性"
    if score > -2.6:
        return "中性偏弱"
    return "短线偏弱"


def confidence_label(value: float) -> str:
    if value >= 68:
        return "较高"
    if value >= 52:
        return "中等"
    return "偏低"


def action_note(asset: Dict[str, Any], score: float, macro: Dict[str, Any]) -> str:
    category = asset.get("category")
    name = asset.get("name", asset.get("symbol"))
    if category == "黄金/贵金属":
        if score >= 1:
            return (
                "黄金类资产可作为避险和分散配置继续观察；更稳妥的做法是分批、轻仓，"
                "并把美元指数和美债收益率重新走强作为短线失效条件。"
            )
        if score <= -1:
            return (
                "黄金短线承压信号偏多，适合等待美元/美债收益率回落或价格重新站上20日均线后再评估。"
            )
        return "黄金处在观察区间，适合看触发条件，不适合只因避险叙事一次性重仓。"
    if category == "虚拟货币":
        return "虚拟货币波动大，适合把仓位上限、止损和持有周期先定清楚；不宜用短线新闻追涨。"
    if category == "美股":
        return "美股判断重点看盈利预期、利率和VIX；指数偏强时也要防止高估值板块回撤。"
    if category == "A股/港股":
        return "中国资产重点看政策、地产信用、人民币汇率和成交量；缺少量能时反弹更容易反复。"
    if category == "宏观变量":
        return f"{name}是解释其他资产的重要变量，重点看方向变化，不直接等同于买卖信号。"
    return "适合把它作为组合里的观察变量，结合趋势、宏观和新闻触发条件再行动。"


def generate_outlooks(assets: List[Dict[str, Any]], news: List[Dict[str, Any]]) -> Dict[str, Any]:
    macro = macro_context(assets)
    outlooks: Dict[str, Any] = {}
    for asset in assets:
        indicators = asset.get("indicators") or {}
        if asset.get("status") != "ok" or not indicators:
            outlooks[asset["symbol"]] = {
                "bias": "数据不足",
                "score": None,
                "confidence": "偏低",
                "why": [asset.get("error", "行情数据不足")],
                "watch": [],
                "action_note": "等待数据恢复后再判断。",
            }
            continue

        category = asset.get("category", "")
        trend_score = safe_float(indicators.get("trend_score")) or 0.0
        news_score = category_news_score(category, news)
        score = trend_score + news_score * 0.6
        why = list(indicators.get("trend_signals") or [])[:3]

        dxy_5d = safe_float(macro.get("dxy_5d"))
        tnx_5d_abs = safe_float(macro.get("tnx_5d_abs"))
        vix = safe_float(macro.get("vix"))
        cny_5d = safe_float(macro.get("cny_5d"))

        if category in {"美股", "虚拟货币"}:
            if vix is not None and vix >= 25:
                score -= 0.9
                why.append("VIX偏高，风险资产折价压力增加")
            if tnx_5d_abs is not None and tnx_5d_abs > 0.12:
                score -= 0.5
                why.append("美债收益率短线抬升")
            if dxy_5d is not None and dxy_5d > 0.8:
                score -= 0.4
                why.append("美元走强压制全球流动性")
        if category == "A股/港股":
            if cny_5d is not None and cny_5d > 0.4:
                score -= 0.6
                why.append("人民币短线承压")
            if dxy_5d is not None and dxy_5d > 0.8:
                score -= 0.3
                why.append("强美元环境不利于外资风险偏好")
        if category == "黄金/贵金属":
            if dxy_5d is not None and dxy_5d > 0.8:
                score -= 0.8
                why.append("美元指数走强压制黄金")
            elif dxy_5d is not None and dxy_5d < -0.6:
                score += 0.6
                why.append("美元走弱有利于黄金")
            if tnx_5d_abs is not None and tnx_5d_abs > 0.12:
                score -= 0.8
                why.append("美债收益率上行压制无息资产")
            elif tnx_5d_abs is not None and tnx_5d_abs < -0.10:
                score += 0.6
                why.append("美债收益率回落支撑黄金")

        confidence = 55.0
        if indicators.get("point_count", 0) >= 60:
            confidence += 7
        if abs(news_score) >= 0.6:
            confidence += 5
        vol = safe_float(indicators.get("volatility_20d_ann"))
        if vol is not None and vol > 55:
            confidence -= 9
        rsi = safe_float(indicators.get("rsi_14"))
        if rsi is not None and (rsi >= 75 or rsi <= 25):
            confidence -= 5
        confidence = max(35.0, min(75.0, confidence))

        watch = []
        support = safe_float(indicators.get("support_60d"))
        resistance = safe_float(indicators.get("resistance_60d"))
        ma20 = safe_float(indicators.get("sma_20"))
        if support is not None:
            watch.append(f"60日支撑附近 {num(support)}")
        if resistance is not None:
            watch.append(f"60日压力附近 {num(resistance)}")
        if ma20 is not None:
            watch.append(f"20日均线 {num(ma20)}")

        outlooks[asset["symbol"]] = {
            "bias": bias_label(score),
            "score": round(score, 2),
            "confidence": confidence_label(confidence),
            "confidence_value": round(confidence, 1),
            "news_score": round(news_score, 2),
            "why": why[:5],
            "watch": watch,
            "action_note": action_note(asset, score, macro),
        }
    return {"macro": macro, "assets": outlooks}


def category_summary(assets: List[Dict[str, Any]], outlooks: Dict[str, Any]) -> List[Dict[str, Any]]:
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for asset in assets:
        grouped.setdefault(asset.get("category", "其他"), []).append(asset)

    summaries = []
    for category, items in grouped.items():
        ok_items = [item for item in items if item.get("status") == "ok"]
        if not ok_items:
            summaries.append(
                {
                    "category": category,
                    "bias": "数据不足",
                    "average_score": None,
                    "leaders": [],
                    "laggards": [],
                    "note": "暂无可用行情。",
                }
            )
            continue
        scores = []
        for item in ok_items:
            score = safe_float((outlooks.get(item["symbol"]) or {}).get("score"))
            if score is not None:
                scores.append(score)
        avg_score = sum(scores) / len(scores) if scores else 0.0
        ranked = sorted(
            ok_items,
            key=lambda item: safe_float((item.get("indicators") or {}).get("return_5d")) or -9999,
            reverse=True,
        )
        summaries.append(
            {
                "category": category,
                "bias": bias_label(avg_score),
                "average_score": round(avg_score, 2),
                "leaders": [item.get("name", item["symbol"]) for item in ranked[:2]],
                "laggards": [item.get("name", item["symbol"]) for item in ranked[-2:]],
                "note": category_note(category, avg_score),
            }
        )
    return summaries


def category_note(category: str, score: float) -> str:
    if category == "黄金/贵金属":
        return "黄金最需要同时看美元、美债收益率和避险新闻；趋势强但利率上行时容易震荡。"
    if category == "美股":
        return "美股短线由盈利、利率和风险偏好共同驱动，VIX和10年期收益率是关键风向标。"
    if category == "A股/港股":
        return "中国资产需要确认政策预期、人民币汇率和成交量，单日反弹不等于趋势反转。"
    if category == "虚拟货币":
        return "加密资产受流动性和监管新闻影响大，趋势信号要配合严格仓位控制。"
    if category == "宏观变量":
        return "宏观变量本身是解释器，方向变化比单点数值更重要。"
    return "保持跨资产对照，避免只看单一产品价格。"


def build_snapshot() -> Dict[str, Any]:
    ensure_dirs()
    config = load_config()
    generated_at = now_local()
    assets = fetch_all_assets(config["assets"])
    news = fetch_all_news(config["news_queries"])
    outlook_bundle = generate_outlooks(assets, news)
    summaries = category_summary(assets, outlook_bundle["assets"])

    snapshot = {
        "version": "1.0",
        "generated_at": generated_at.isoformat(),
        "generated_at_readable": generated_at.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "data_sources": [
            {
                "name": "Yahoo Finance chart API",
                "url": "https://query1.finance.yahoo.com/v8/finance/chart/",
                "use": "行情时间序列",
            },
            {
                "name": "Google News RSS",
                "url": "https://news.google.com/rss/search",
                "use": "新闻标题、来源和发布时间",
            },
        ],
        "risk_note": config.get("risk_note", ""),
        "assets": assets,
        "news": news,
        "outlook": outlook_bundle,
        "category_summary": summaries,
    }
    write_snapshot(snapshot)
    return snapshot


def write_snapshot(snapshot: Dict[str, Any]) -> None:
    ensure_dirs()
    stamp = now_local().strftime("%Y%m%d_%H%M%S")
    snapshot_path = HISTORY_DIR / f"snapshot_{stamp}.json"
    payload = json.dumps(snapshot, ensure_ascii=False, indent=2)
    LATEST_JSON.write_text(payload, encoding="utf-8")
    snapshot_path.write_text(payload, encoding="utf-8")
    with HISTORY_JSONL.open("a", encoding="utf-8") as f:
        compact = {
            "generated_at": snapshot["generated_at"],
            "category_summary": snapshot.get("category_summary", []),
            "macro": (snapshot.get("outlook") or {}).get("macro", {}),
        }
        f.write(json.dumps(compact, ensure_ascii=False) + "\n")
    write_outputs(snapshot)


def load_latest_snapshot() -> Optional[Dict[str, Any]]:
    if not LATEST_JSON.exists():
        return None
    try:
        return json.loads(LATEST_JSON.read_text(encoding="utf-8"))
    except Exception:
        return None


def snapshot_age_minutes(snapshot: Dict[str, Any]) -> Optional[float]:
    try:
        generated = dt.datetime.fromisoformat(snapshot["generated_at"])
        if generated.tzinfo is None:
            generated = generated.replace(tzinfo=dt.timezone.utc)
        delta = now_local() - generated.astimezone()
        return delta.total_seconds() / 60.0
    except Exception:
        return None


def ensure_recent_snapshot(max_age_minutes: int = 15, force: bool = False) -> Dict[str, Any]:
    latest = load_latest_snapshot()
    age = snapshot_age_minutes(latest) if latest else None
    if force or latest is None or age is None or age > max_age_minutes:
        return build_snapshot()
    return latest


def write_outputs(snapshot: Dict[str, Any]) -> None:
    OUTPUT_SNAPSHOT_PATH.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    REPORT_PATH.write_text(render_report(snapshot), encoding="utf-8")
    SNAPSHOT_JS_PATH.write_text(
        "window.MARKET_SNAPSHOT = "
        + json.dumps(snapshot, ensure_ascii=False, indent=2)
        + ";\n",
        encoding="utf-8",
    )
    dashboard = render_dashboard_html()
    DASHBOARD_PATH.write_text(dashboard, encoding="utf-8")
    INDEX_PATH.write_text(dashboard, encoding="utf-8")
    ENTRY_PROMPT_PATH.write_text(render_entry_prompt(), encoding="utf-8")


def render_report(snapshot: Dict[str, Any]) -> str:
    generated = snapshot.get("generated_at_readable") or snapshot.get("generated_at")
    risk_note = snapshot.get("risk_note", "")
    macro = (snapshot.get("outlook") or {}).get("macro", {})
    assets = snapshot.get("assets") or []
    outlooks = ((snapshot.get("outlook") or {}).get("assets") or {})
    category_items = snapshot.get("category_summary") or []
    news_items = snapshot.get("news") or []

    lines: List[str] = []
    lines.append("# 实时经济走势系统简报")
    lines.append("")
    lines.append(f"- 更新时间：{generated}")
    lines.append(f"- 风险状态：{macro.get('risk_regime', '中性')}")
    if risk_note:
        lines.append(f"- 风险提示：{risk_note}")
    lines.append("")
    lines.append("## 关键宏观读数")
    notes = macro.get("notes") or []
    if notes:
        for note in notes:
            lines.append(f"- {note}")
    else:
        lines.append("- 暂无宏观变量数据。")
    lines.append("")

    lines.append("## 跨资产概览")
    lines.append("| 类别 | 推演 | 强势观察 | 弱势观察 | 核心提示 |")
    lines.append("| --- | --- | --- | --- | --- |")
    for item in category_items:
        leaders = "、".join(item.get("leaders") or []) or "-"
        laggards = "、".join(item.get("laggards") or []) or "-"
        lines.append(
            f"| {item.get('category', '-')} | {item.get('bias', '-')} | "
            f"{leaders} | {laggards} | {item.get('note', '-')} |"
        )
    lines.append("")

    lines.append("## 重点产品走势")
    lines.append("| 类别 | 产品 | 最新 | 1日 | 5日 | 1月 | 波动率 | 推演 | 置信度 |")
    lines.append("| --- | --- | ---: | ---: | ---: | ---: | ---: | --- | --- |")
    for asset in assets:
        indicators = asset.get("indicators") or {}
        outlook = outlooks.get(asset.get("symbol"), {})
        latest = indicators.get("latest")
        unit = asset.get("display_unit") or ""
        lines.append(
            f"| {asset.get('category', '-')} | {asset.get('name', asset.get('symbol'))} | "
            f"{num(latest)}{unit} | {pct(indicators.get('return_1d'))} | "
            f"{pct(indicators.get('return_5d'))} | {pct(indicators.get('return_1m'))} | "
            f"{pct(indicators.get('volatility_20d_ann'))} | "
            f"{outlook.get('bias', '-')} | {outlook.get('confidence', '-')} |"
        )
    lines.append("")

    gold = get_asset(assets, "GC=F") or get_asset(assets, "GLD")
    if gold:
        gold_outlook = outlooks.get(gold.get("symbol"), {})
        lines.append("## 黄金观察结论")
        lines.append(
            f"- 当前判断：{gold.get('name')} 为 **{gold_outlook.get('bias', '数据不足')}**，"
            f"置信度 {gold_outlook.get('confidence', '偏低')}。"
        )
        for reason in gold_outlook.get("why") or []:
            lines.append(f"- 依据：{reason}")
        for watch in gold_outlook.get("watch") or []:
            lines.append(f"- 观察位：{watch}")
        if gold_outlook.get("action_note"):
            lines.append(f"- 操作含义：{gold_outlook['action_note']}")
        lines.append("")

    lines.append("## 新闻线索")
    lines.append("| 主题 | 情绪均值 | 代表标题 | 来源 |")
    lines.append("| --- | ---: | --- | --- |")
    for topic in news_items:
        articles = topic.get("articles") or []
        first = articles[0] if articles else {}
        title = (first.get("title") or topic.get("error") or "-").replace("|", "/")
        source = first.get("source") or topic.get("source") or "-"
        avg = topic.get("sentiment_average")
        avg_text = "-" if avg is None else f"{avg:.2f}"
        lines.append(f"| {topic.get('name', '-')} | {avg_text} | {title} | {source} |")
    lines.append("")

    lines.append("## 下一步看什么")
    lines.append("- 黄金：美元指数、美债收益率、地缘风险和央行购金新闻是否同向支持。")
    lines.append("- 美股：指数创新高是否伴随VIX下降、10年期收益率稳定和盈利预期上修。")
    lines.append("- A股/港股：政策新闻能否转化为成交量和人民币汇率企稳。")
    lines.append("- 虚拟货币：ETF/监管新闻和美元流动性变化，优先控制仓位和回撤。")
    lines.append("")
    lines.append("## 数据源")
    for source in snapshot.get("data_sources", []):
        lines.append(f"- [{source['name']}]({source['url']})：{source['use']}")
    lines.append("")
    return "\n".join(lines)


def render_entry_prompt() -> str:
    return """# ChatGPT 入口提示词

你是我的实时经济走势系统入口。请按以下步骤工作：

1. 先运行 `python work/econ_system/market_watch.py --refresh` 获取最新行情和新闻。
2. 读取 `outputs/latest_market_brief.md` 和 `work/econ_system/data/latest_snapshot.json`。
3. 用中文回答我当前最关心的问题，例如“现在黄金能不能买”“A股和美股哪个更强”“加密货币风险是否升温”。
4. 回答必须包括：当前走势、关键新闻、未来1-4周情景推演、触发条件、失效条件、风险提示。
5. 不要把结论写成确定性预测，也不要给出替我下单的指令；用“观察、分批、仓位、止损、等待确认”这样的决策语言。

长期记忆位置：

- 最新快照：`work/econ_system/data/latest_snapshot.json`
- 历史摘要：`work/econ_system/data/history.jsonl`
- 最新简报：`outputs/latest_market_brief.md`
- 图表面板：`outputs/econ_dashboard.html` 或本地服务 `http://127.0.0.1:8765/`
"""


def render_dashboard_html() -> str:
    return r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>实时经济走势系统</title>
  <script src="market_data.js"></script>
  <style>
    :root {
      --bg: #f5f7fa;
      --panel: #ffffff;
      --ink: #17202a;
      --muted: #667085;
      --line: #d7dde6;
      --blue: #2458d3;
      --green: #168a5b;
      --red: #c33b3b;
      --amber: #b7791f;
      --teal: #157a7e;
      --shadow: 0 6px 20px rgba(20, 32, 50, 0.08);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", Arial, sans-serif;
      letter-spacing: 0;
    }
    header {
      background: #fff;
      border-bottom: 1px solid var(--line);
      position: sticky;
      top: 0;
      z-index: 10;
    }
    .topbar {
      max-width: 1480px;
      margin: 0 auto;
      padding: 14px 18px;
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 14px;
      align-items: center;
    }
    h1 {
      font-size: 22px;
      margin: 0;
      font-weight: 760;
    }
    .meta {
      color: var(--muted);
      font-size: 13px;
      margin-top: 4px;
    }
    .toolbar {
      display: flex;
      gap: 8px;
      align-items: center;
      flex-wrap: wrap;
      justify-content: flex-end;
    }
    button, select {
      border: 1px solid var(--line);
      background: #fff;
      color: var(--ink);
      min-height: 34px;
      padding: 7px 10px;
      border-radius: 7px;
      font-size: 14px;
    }
    button {
      cursor: pointer;
      font-weight: 650;
    }
    button.primary {
      background: var(--blue);
      color: #fff;
      border-color: var(--blue);
    }
    main {
      max-width: 1480px;
      margin: 0 auto;
      padding: 18px;
    }
    .summary-grid {
      display: grid;
      grid-template-columns: repeat(6, minmax(150px, 1fr));
      gap: 10px;
      margin-bottom: 14px;
    }
    .summary {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 12px;
      min-height: 106px;
      box-shadow: var(--shadow);
    }
    .summary .label {
      color: var(--muted);
      font-size: 12px;
    }
    .summary .value {
      font-size: 20px;
      font-weight: 760;
      margin: 7px 0;
    }
    .summary .note {
      color: var(--muted);
      font-size: 12px;
      line-height: 1.35;
    }
    .layout {
      display: grid;
      grid-template-columns: minmax(420px, 1.7fr) minmax(320px, 1fr);
      gap: 14px;
      align-items: start;
    }
    .panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: var(--shadow);
    }
    .panel-head {
      padding: 12px 14px;
      border-bottom: 1px solid var(--line);
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: center;
    }
    .panel-head h2 {
      margin: 0;
      font-size: 16px;
    }
    .asset-table {
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
    }
    .asset-table th, .asset-table td {
      border-bottom: 1px solid #edf0f4;
      padding: 8px 10px;
      text-align: right;
      vertical-align: middle;
      white-space: nowrap;
    }
    .asset-table th:first-child, .asset-table td:first-child,
    .asset-table th:nth-child(2), .asset-table td:nth-child(2) {
      text-align: left;
    }
    .asset-table tbody tr {
      cursor: pointer;
    }
    .asset-table tbody tr:hover {
      background: #f0f4ff;
    }
    .asset-table tbody tr.active {
      background: #e9f0ff;
    }
    .chip {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-width: 84px;
      min-height: 24px;
      border-radius: 999px;
      padding: 3px 8px;
      font-size: 12px;
      font-weight: 720;
      border: 1px solid var(--line);
      background: #fff;
    }
    .pos { color: var(--green); }
    .neg { color: var(--red); }
    .flat { color: var(--muted); }
    .bias-strong { color: var(--green); background: #e9f8f0; border-color: #c4ebd5; }
    .bias-weak { color: var(--red); background: #fff0f0; border-color: #f3caca; }
    .bias-neutral { color: var(--amber); background: #fff7e6; border-color: #ead7a8; }
    .chart-wrap {
      padding: 12px 14px 14px;
    }
    canvas {
      width: 100%;
      height: 300px;
      display: block;
      border: 1px solid #edf0f4;
      border-radius: 8px;
      background: linear-gradient(#fff, #fbfcfe);
    }
    .detail-grid {
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      gap: 8px;
      margin-top: 10px;
    }
    .metric {
      border: 1px solid #edf0f4;
      border-radius: 7px;
      padding: 9px;
      min-height: 64px;
    }
    .metric .label {
      color: var(--muted);
      font-size: 12px;
    }
    .metric .value {
      font-size: 16px;
      font-weight: 760;
      margin-top: 5px;
    }
    .news-list {
      padding: 0;
      margin: 0;
      list-style: none;
    }
    .news-list li {
      padding: 10px 14px;
      border-bottom: 1px solid #edf0f4;
    }
    .news-list a {
      color: var(--ink);
      text-decoration: none;
      font-weight: 650;
      line-height: 1.35;
    }
    .news-list a:hover { color: var(--blue); }
    .news-meta {
      color: var(--muted);
      font-size: 12px;
      margin-top: 4px;
    }
    .reason-list {
      margin: 10px 0 0;
      padding-left: 18px;
      color: var(--muted);
      line-height: 1.45;
      font-size: 13px;
    }
    .action {
      margin-top: 10px;
      border-left: 3px solid var(--teal);
      background: #eefafa;
      padding: 9px 10px;
      color: #164d52;
      font-size: 13px;
      line-height: 1.45;
    }
    .status {
      color: var(--muted);
      font-size: 12px;
      min-width: 160px;
      text-align: right;
    }
    @media (max-width: 1050px) {
      .summary-grid { grid-template-columns: repeat(3, minmax(150px, 1fr)); }
      .layout { grid-template-columns: 1fr; }
    }
    @media (max-width: 720px) {
      .topbar { grid-template-columns: 1fr; }
      .toolbar { justify-content: flex-start; }
      main { padding: 12px; }
      .summary-grid { grid-template-columns: 1fr 1fr; }
      .asset-table th:nth-child(4), .asset-table td:nth-child(4),
      .asset-table th:nth-child(5), .asset-table td:nth-child(5) { display: none; }
      .detail-grid { grid-template-columns: 1fr; }
      canvas { height: 250px; }
    }
  </style>
</head>
<body>
  <header>
    <div class="topbar">
      <div>
        <h1>实时经济走势系统</h1>
        <div class="meta" id="generatedAt">等待数据</div>
      </div>
      <div class="toolbar">
        <select id="categoryFilter" aria-label="类别筛选"></select>
        <button id="refreshBtn" class="primary">刷新</button>
        <button id="openReportBtn">简报</button>
        <div class="status" id="statusText">本地快照</div>
      </div>
    </div>
  </header>
  <main>
    <section class="summary-grid" id="summaryGrid"></section>
    <section class="layout">
      <div class="panel">
        <div class="panel-head">
          <h2>资产走势</h2>
          <span class="meta" id="riskRegime">风险状态</span>
        </div>
        <div style="overflow:auto;">
          <table class="asset-table">
            <thead>
              <tr>
                <th>类别</th>
                <th>产品</th>
                <th>最新</th>
                <th>1日</th>
                <th>5日</th>
                <th>1月</th>
                <th>推演</th>
              </tr>
            </thead>
            <tbody id="assetRows"></tbody>
          </table>
        </div>
      </div>
      <aside class="panel">
        <div class="panel-head">
          <h2 id="detailTitle">详情</h2>
          <span class="chip" id="detailBias">-</span>
        </div>
        <div class="chart-wrap">
          <canvas id="priceChart" width="900" height="360"></canvas>
          <div class="detail-grid" id="detailMetrics"></div>
          <ul class="reason-list" id="reasonList"></ul>
          <div class="action" id="actionNote"></div>
        </div>
      </aside>
    </section>
    <section class="panel" style="margin-top:14px;">
      <div class="panel-head">
        <h2>新闻线索</h2>
        <span class="meta">标题情绪只作线索，不等同于事实定价</span>
      </div>
      <ul class="news-list" id="newsList"></ul>
    </section>
  </main>

  <script>
    let snapshot = window.MARKET_SNAPSHOT || null;
    let selectedSymbol = null;

    const fmtNum = value => {
      if (value === null || value === undefined || !Number.isFinite(Number(value))) return "-";
      const n = Number(value);
      if (Math.abs(n) >= 1000) return n.toLocaleString(undefined, { maximumFractionDigits: 2 });
      if (Math.abs(n) >= 10) return n.toFixed(2);
      return n.toFixed(4);
    };
    const fmtPct = value => {
      if (value === null || value === undefined || !Number.isFinite(Number(value))) return "-";
      const n = Number(value);
      return `${n > 0 ? "+" : ""}${n.toFixed(2)}%`;
    };
    const clsForPct = value => {
      const n = Number(value);
      if (!Number.isFinite(n) || Math.abs(n) < 0.01) return "flat";
      return n > 0 ? "pos" : "neg";
    };
    const biasClass = bias => {
      if (!bias) return "bias-neutral";
      if (bias.includes("偏强")) return "bias-strong";
      if (bias.includes("偏弱")) return "bias-weak";
      return "bias-neutral";
    };

    async function tryFetchJson(path) {
      const res = await fetch(path, { cache: "no-store" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      return await res.json();
    }

    async function refresh(force = false) {
      const status = document.getElementById("statusText");
      status.textContent = force ? "刷新中..." : "读取中...";
      try {
        snapshot = await tryFetchJson(force ? "/api/refresh" : "/api/snapshot");
        status.textContent = "服务实时数据";
      } catch (err) {
        if (!snapshot) status.textContent = "读取本地快照失败";
        else status.textContent = "本地快照";
      }
      render();
    }

    function categories() {
      const names = Array.from(new Set((snapshot.assets || []).map(a => a.category || "其他")));
      return ["全部"].concat(names);
    }

    function renderFilters() {
      const select = document.getElementById("categoryFilter");
      const current = select.value || "全部";
      select.innerHTML = "";
      for (const name of categories()) {
        const option = document.createElement("option");
        option.value = name;
        option.textContent = name;
        select.appendChild(option);
      }
      select.value = categories().includes(current) ? current : "全部";
    }

    function renderSummary() {
      const grid = document.getElementById("summaryGrid");
      grid.innerHTML = "";
      for (const item of snapshot.category_summary || []) {
        const div = document.createElement("div");
        div.className = "summary";
        div.innerHTML = `
          <div class="label">${item.category}</div>
          <div class="value ${biasClass(item.bias)}">${item.bias}</div>
          <div class="note">${item.note || ""}</div>
        `;
        grid.appendChild(div);
      }
    }

    function visibleAssets() {
      const category = document.getElementById("categoryFilter").value || "全部";
      const assets = snapshot.assets || [];
      return category === "全部" ? assets : assets.filter(a => a.category === category);
    }

    function renderRows() {
      const tbody = document.getElementById("assetRows");
      tbody.innerHTML = "";
      const assets = visibleAssets();
      if (!selectedSymbol && assets.length) selectedSymbol = assets[0].symbol;
      if (!assets.some(a => a.symbol === selectedSymbol) && assets.length) selectedSymbol = assets[0].symbol;
      const outlooks = (snapshot.outlook || {}).assets || {};
      for (const asset of assets) {
        const ind = asset.indicators || {};
        const outlook = outlooks[asset.symbol] || {};
        const tr = document.createElement("tr");
        tr.className = asset.symbol === selectedSymbol ? "active" : "";
        tr.innerHTML = `
          <td>${asset.category || "-"}</td>
          <td><strong>${asset.name || asset.symbol}</strong><div class="meta">${asset.symbol}</div></td>
          <td>${fmtNum(ind.latest)}${asset.display_unit || ""}</td>
          <td class="${clsForPct(ind.return_1d)}">${fmtPct(ind.return_1d)}</td>
          <td class="${clsForPct(ind.return_5d)}">${fmtPct(ind.return_5d)}</td>
          <td class="${clsForPct(ind.return_1m)}">${fmtPct(ind.return_1m)}</td>
          <td><span class="chip ${biasClass(outlook.bias)}">${outlook.bias || "数据不足"}</span></td>
        `;
        tr.addEventListener("click", () => {
          selectedSymbol = asset.symbol;
          renderRows();
          renderDetail();
        });
        tbody.appendChild(tr);
      }
    }

    function renderDetail() {
      const asset = (snapshot.assets || []).find(a => a.symbol === selectedSymbol) || (snapshot.assets || [])[0];
      if (!asset) return;
      const ind = asset.indicators || {};
      const outlook = ((snapshot.outlook || {}).assets || {})[asset.symbol] || {};
      document.getElementById("detailTitle").textContent = `${asset.name || asset.symbol} · ${asset.symbol}`;
      const bias = document.getElementById("detailBias");
      bias.textContent = outlook.bias || "数据不足";
      bias.className = `chip ${biasClass(outlook.bias)}`;
      const metrics = [
        ["最新", `${fmtNum(ind.latest)}${asset.display_unit || ""}`],
        ["20日均线", fmtNum(ind.sma_20)],
        ["50日均线", fmtNum(ind.sma_50)],
        ["RSI", fmtNum(ind.rsi_14)],
        ["20日年化波动", fmtPct(ind.volatility_20d_ann)],
        ["60日回撤", fmtPct(ind.drawdown_from_60d_high)]
      ];
      document.getElementById("detailMetrics").innerHTML = metrics.map(m => `
        <div class="metric"><div class="label">${m[0]}</div><div class="value">${m[1]}</div></div>
      `).join("");
      document.getElementById("reasonList").innerHTML = (outlook.why || []).map(x => `<li>${x}</li>`).join("");
      document.getElementById("actionNote").textContent = outlook.action_note || "";
      drawChart(asset);
    }

    function drawChart(asset) {
      const canvas = document.getElementById("priceChart");
      const ctx = canvas.getContext("2d");
      const w = canvas.width;
      const h = canvas.height;
      ctx.clearRect(0, 0, w, h);
      const points = (asset.points || []).filter(p => Number.isFinite(Number(p.close)));
      if (points.length < 2) {
        ctx.fillStyle = "#667085";
        ctx.font = "16px sans-serif";
        ctx.fillText("暂无足够图表数据", 24, 40);
        return;
      }
      const values = points.map(p => Number(p.close));
      const min = Math.min(...values);
      const max = Math.max(...values);
      const pad = 36;
      const span = max - min || Math.abs(max) || 1;
      ctx.strokeStyle = "#d7dde6";
      ctx.lineWidth = 1;
      ctx.beginPath();
      for (let i = 0; i < 5; i++) {
        const y = pad + (h - pad * 2) * i / 4;
        ctx.moveTo(pad, y);
        ctx.lineTo(w - pad, y);
      }
      ctx.stroke();
      ctx.fillStyle = "#667085";
      ctx.font = "12px sans-serif";
      ctx.fillText(fmtNum(max), 8, pad + 4);
      ctx.fillText(fmtNum(min), 8, h - pad + 4);
      ctx.beginPath();
      points.forEach((p, i) => {
        const x = pad + (w - pad * 2) * i / (points.length - 1);
        const y = h - pad - ((Number(p.close) - min) / span) * (h - pad * 2);
        if (i === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      });
      const first = values[0];
      const last = values[values.length - 1];
      ctx.strokeStyle = last >= first ? "#168a5b" : "#c33b3b";
      ctx.lineWidth = 2.5;
      ctx.stroke();
      ctx.fillStyle = "#17202a";
      ctx.font = "13px sans-serif";
      ctx.fillText(points[0].date, pad, h - 12);
      ctx.textAlign = "right";
      ctx.fillText(points[points.length - 1].date, w - pad, h - 12);
      ctx.textAlign = "left";
    }

    function renderNews() {
      const list = document.getElementById("newsList");
      const items = [];
      for (const topic of snapshot.news || []) {
        for (const article of (topic.articles || []).slice(0, 3)) {
          items.push({ topic: topic.name, ...article });
        }
      }
      list.innerHTML = items.slice(0, 18).map(item => `
        <li>
          <a href="${item.link || "#"}" target="_blank" rel="noreferrer">${item.title || "-"}</a>
          <div class="news-meta">${item.topic || "-"} · ${item.source || "Google News"} · 情绪 ${item.sentiment ?? "-"}</div>
        </li>
      `).join("");
    }

    function render() {
      if (!snapshot) return;
      document.getElementById("generatedAt").textContent = `更新时间：${snapshot.generated_at_readable || snapshot.generated_at || "-"}`;
      document.getElementById("riskRegime").textContent = `风险状态：${((snapshot.outlook || {}).macro || {}).risk_regime || "中性"}`;
      renderFilters();
      renderSummary();
      renderRows();
      renderDetail();
      renderNews();
    }

    document.getElementById("categoryFilter").addEventListener("change", () => {
      selectedSymbol = null;
      renderRows();
      renderDetail();
    });
    document.getElementById("refreshBtn").addEventListener("click", () => refresh(true));
    document.getElementById("openReportBtn").addEventListener("click", () => {
      const reportUrl = window.location.protocol === "file:" ? "latest_market_brief.md" : "/latest_market_brief.md";
      window.open(reportUrl, "_blank");
    });

    render();
    refresh(false);
    window.setInterval(() => refresh(false), 5 * 60 * 1000);
  </script>
</body>
</html>
"""


class MarketWatchHandler(BaseHTTPRequestHandler):
    server_version = "MarketWatchHTTP/1.0"
    snapshot_lock = threading.Lock()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        try:
            if path in {"/", "/dashboard", "/index.html"}:
                self.serve_text(render_dashboard_html(), "text/html; charset=utf-8")
                return
            if path == "/market_data.js":
                snapshot = ensure_recent_snapshot(max_age_minutes=15)
                body = "window.MARKET_SNAPSHOT = " + json.dumps(snapshot, ensure_ascii=False, indent=2) + ";\n"
                self.serve_text(body, "application/javascript; charset=utf-8")
                return
            if path == "/api/snapshot":
                with self.snapshot_lock:
                    snapshot = ensure_recent_snapshot(max_age_minutes=15)
                self.serve_json(snapshot)
                return
            if path == "/api/refresh":
                with self.snapshot_lock:
                    snapshot = ensure_recent_snapshot(force=True)
                self.serve_json(snapshot)
                return
            if path == "/latest_market_brief.md":
                snapshot = ensure_recent_snapshot(max_age_minutes=15)
                self.serve_text(render_report(snapshot), "text/markdown; charset=utf-8")
                return
            self.send_error(404, "Not found")
        except Exception as exc:  # noqa: BLE001
            self.send_error(500, f"{type(exc).__name__}: {exc}")

    def log_message(self, fmt: str, *args: Any) -> None:
        timestamp = now_local().strftime("%H:%M:%S")
        print(f"[{timestamp}] {self.address_string()} {fmt % args}")

    def serve_json(self, data: Dict[str, Any]) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def serve_text(self, text: str, content_type: str) -> None:
        body = text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve(port: int) -> None:
    ensure_dirs()
    ensure_recent_snapshot(max_age_minutes=15)
    server = ThreadingHTTPServer(("127.0.0.1", port), MarketWatchHandler)
    print(f"实时经济走势系统已启动：http://127.0.0.1:{port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("正在关闭服务...")
    finally:
        server.server_close()


def main() -> int:
    parser = argparse.ArgumentParser(description="实时经济走势系统")
    parser.add_argument("--refresh", action="store_true", help="抓取最新行情和新闻并写入输出文件")
    parser.add_argument("--serve", action="store_true", help="启动本地HTTP仪表盘")
    parser.add_argument("--port", type=int, default=8765, help="本地服务端口")
    parser.add_argument("--print-summary", action="store_true", help="在终端输出简短摘要")
    args = parser.parse_args()

    if args.refresh or not args.serve:
        snapshot = build_snapshot()
        if args.print_summary:
            print(render_terminal_summary(snapshot))
    if args.serve:
        serve(args.port)
    return 0


def render_terminal_summary(snapshot: Dict[str, Any]) -> str:
    lines = [f"更新时间：{snapshot.get('generated_at_readable')}"]
    macro = (snapshot.get("outlook") or {}).get("macro", {})
    lines.append(f"风险状态：{macro.get('risk_regime', '中性')}")
    for item in snapshot.get("category_summary") or []:
        lines.append(f"- {item.get('category')}: {item.get('bias')} ({item.get('note')})")
    lines.append(f"简报：{REPORT_PATH}")
    lines.append(f"面板：{DASHBOARD_PATH}")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
