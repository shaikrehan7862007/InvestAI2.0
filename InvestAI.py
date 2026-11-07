"""Stock Research Assistant: an educational tool, not financial advice.

Run locally:  streamlit run app.py
All market data comes from Yahoo Finance through the unofficial yfinance library.
"""

import datetime as dt
import email.utils
import gzip
import html
import json
import os
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

import numpy as np
import pandas as pd
import streamlit as st
import yfinance as yf

# ----------------------------------------------------------------------------
# Settings
# ----------------------------------------------------------------------------

PERIODS = {"1 month": "1mo", "6 months": "6mo", "1 year": "1y", "5 years": "5y"}
EXAMPLES = ["AAPL", "MSFT", "RELIANCE.NS"]
SAMPLE_TEN = "AAPL, MSFT, GOOGL, AMZN, NVDA, JPM, XOM, JNJ, RELIANCE.NS, TCS.NS"
MAX_TICKERS = 20  # most stocks analysed in one go, to keep requests to Yahoo reasonable

BUY, HOLD, AVOID = "Potential buy candidate", "Hold/watch", "Avoid for now"

# Ranking criteria: (key, label, maximum points, how points are earned).
# A criterion with no data earns 0 points, the same as the weakest reading,
# so missing information can never lift a stock's score or rank.
CRITERIA = [
    ("return", "Return over the period", 1,
     "1 point if the closing price rose over the selected period."),
    ("trend", "Price vs trend average", 1,
     "1 point if the latest close is above its 50-day average (its 20-day average when "
     "there are fewer than 50 trading days)."),
    ("cross", "20-day vs 50-day average", 1,
     "1 point if the 20-day average is above the 50-day average. Needs 50 trading days."),
    ("volatility", "Annualized volatility", 2,
     "2 points below 25%, 1 point from 25% to 40%, 0 points at 40% or more."),
    ("drawdown", "Maximum drawdown", 2,
     "2 points if the deepest fall from a peak was less than 15%, 1 point if less than 30%, "
     "otherwise 0."),
    ("margin", "Profit margin", 2,
     "2 points above 10%, 1 point above 0%, 0 points at or below 0%."),
    ("growth", "Revenue growth (year on year)", 2,
     "2 points above 10%, 1 point above 0%, 0 points at or below 0%."),
]
TOTAL_POINTS = sum(c[2] for c in CRITERIA)

RESEARCH_NOTE = (
    "These are research suggestions based on the data fetched for the chosen period. They are "
    "not guarantees or predictions and not personalised financial advice. The app does not "
    "place trades and does not tell you what you should buy."
)
TRADING_DAYS = 252  # trading days per year, used to annualize volatility

DISCLAIMER = (
    "**Educational research tool, not financial advice.** Market data comes from an "
    "unofficial source (Yahoo Finance via yfinance) and may be delayed, incomplete or "
    "incorrect. Technical indicators describe the past and do not predict the future. "
    "Verify everything with authoritative sources, such as the company's filings and "
    "your exchange, before making any decision."
)

# Plain-language help for each indicator, shown in the app.
INDICATOR_HELP = {
    "Period return": (
        "How much the price changed from the first to the last day in the selected "
        "period. It uses the closing price and ignores dividends.",
        "A single start and end date can flatter or hurt a stock. Pick another period "
        "and the answer can change a lot.",
    ),
    "20-day average": (
        "The average closing price of the last 20 trading days, a rough line for the "
        "recent trend.",
        "It lags the price, so it reacts after a move has already happened.",
    ),
    "50-day average": (
        "The average closing price of the last 50 trading days, a slower trend line. "
        "Needs at least 50 trading days in the selected period.",
        "It lags even more than the 20-day average and often gives false signals when "
        "prices move sideways.",
    ),
    "RSI (14-day)": (
        "Relative Strength Index, scaled 0 to 100. It compares recent gains with recent "
        "losses. Readings above 70 are often called 'overbought' and below 30 "
        "'oversold'.",
        "A strong stock can stay above 70 for weeks. The labels are conventions, not "
        "forecasts.",
    ),
    "Annualized volatility": (
        "How much daily returns vary, scaled to a one-year figure. Higher means larger "
        "typical swings up and down.",
        "It is based on past moves only and says nothing about direction.",
    ),
    "Maximum drawdown": (
        "The biggest fall from a peak to a later low inside the selected period.",
        "It depends on the window you pick and does not show how long recovery took.",
    ),
}


# ----------------------------------------------------------------------------
# Small formatting helpers
# ----------------------------------------------------------------------------

def is_missing(x):
    """True for None and NaN."""
    return x is None or (isinstance(x, float) and np.isnan(x))


def fmt_pct(x, signed=False):
    if is_missing(x):
        return "unavailable"
    return f"{x * 100:+.1f}%" if signed else f"{x * 100:.1f}%"


def fmt_num(x, digits=2):
    if is_missing(x):
        return "unavailable"
    return f"{x:,.{digits}f}"


def fmt_big(x, currency=""):
    """Format large money amounts such as 2.95T or 410.2B."""
    if is_missing(x):
        return "unavailable"
    prefix = f"{currency} " if currency else ""
    for limit, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M")):
        if abs(x) >= limit:
            return f"{prefix}{x / limit:,.2f}{suffix}"
    return f"{prefix}{x:,.0f}"


def clean_ticker(raw):
    """Return an upper-case ticker, or None if it looks invalid."""
    ticker = (raw or "").strip().upper()
    if re.fullmatch(r"[A-Z0-9.\-^=]{1,15}", ticker):
        return ticker
    return None


# ----------------------------------------------------------------------------
# Indicators (plain pandas and numpy, so each one is easy to follow)
# ----------------------------------------------------------------------------

def rsi_series(close, window=14):
    """Relative Strength Index using Wilder's smoothing.

    The first value needs `window` price changes (so window + 1 prices).
    It starts from a simple average of the first `window` gains and losses,
    then smooths each new day into that average.
    """
    values = np.full(len(close), np.nan)
    if len(close) <= window:
        return pd.Series(values, index=close.index)

    change = np.diff(close.to_numpy(dtype=float), prepend=np.nan)
    gains = np.where(change > 0, change, 0.0)
    losses = np.where(change < 0, -change, 0.0)

    avg_gain = gains[1 : window + 1].mean()
    avg_loss = losses[1 : window + 1].mean()
    for i in range(window, len(close)):
        if i > window:
            avg_gain = (avg_gain * (window - 1) + gains[i]) / window
            avg_loss = (avg_loss * (window - 1) + losses[i]) / window
        if avg_loss > 0:
            values[i] = 100 - 100 / (1 + avg_gain / avg_loss)
        elif avg_gain > 0:
            values[i] = 100.0  # no losses at all in the window
        # if there were no gains and no losses the RSI is undefined, so it stays NaN
    return pd.Series(values, index=close.index)


def last_valid(series):
    """Last non-NaN value of a series, or None."""
    series = series.dropna()
    return float(series.iloc[-1]) if len(series) else None


def compute_indicators(close):
    """Return a dict of indicators. A value is None when there is not enough data."""
    close = close.dropna()
    daily_returns = close.pct_change().dropna()
    n = len(close)

    out = {
        "period_return": None,
        "ma20": None,
        "ma50": None,
        "rsi14": None,
        "volatility": None,
        "max_drawdown": None,
    }
    if n >= 2:
        out["period_return"] = float(close.iloc[-1] / close.iloc[0] - 1)
        out["max_drawdown"] = float((close / close.cummax() - 1).min())
    if n >= 20:
        out["ma20"] = float(close.rolling(20).mean().iloc[-1])
    if n >= 50:
        out["ma50"] = float(close.rolling(50).mean().iloc[-1])
    if n >= 15:
        out["rsi14"] = last_valid(rsi_series(close))
    if len(daily_returns) >= 2:
        out["volatility"] = float(daily_returns.std() * np.sqrt(TRADING_DAYS))
    return out


# ----------------------------------------------------------------------------
# Data loading (cached so many visitors do not hammer Yahoo Finance)
# ----------------------------------------------------------------------------

@st.cache_data(ttl=900, show_spinner=False)
def load_history(ticker, period):
    """Daily price history. Raises on network problems; returns an empty frame for bad tickers."""
    return yf.Ticker(ticker).history(period=period, interval="1d", auto_adjust=False)


NUMERIC_INFO_KEYS = (
    "marketCap", "trailingPE", "forwardPE", "totalRevenue", "revenueGrowth", "profitMargins",
    "trailingAnnualDividendYield", "dividendRate", "fiftyTwoWeekHigh", "fiftyTwoWeekLow",
    "trailingEps",
)


def to_number(x):
    """A finite float, or None. Yahoo sometimes sends text such as 'Infinity' instead of a number."""
    try:
        value = float(x)
    except (TypeError, ValueError):
        return None
    return value if np.isfinite(value) else None


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_info(ticker):
    # Errors are not cached by Streamlit, so a temporary failure is retried next time.
    info = dict(yf.Ticker(ticker).info or {})
    for key in NUMERIC_INFO_KEYS:
        if key in info:
            info[key] = to_number(info[key])
    return info


def load_info(ticker):
    """Company details. Returns {} if Yahoo does not provide them."""
    try:
        return fetch_info(ticker)
    except Exception:
        return {}


def load_news(ticker):
    """Recent headlines as a list of dicts. Returns [] if none can be fetched."""
    try:
        return fetch_news(ticker)
    except Exception:
        return []


@st.cache_data(ttl=900, show_spinner=False)
def fetch_news(ticker):
    items = yf.Ticker(ticker).news or []
    headlines = []
    for item in items[:8]:
        # Newer yfinance versions nest the details under "content".
        body = item.get("content") or item
        title = body.get("title")
        url = (
            (body.get("canonicalUrl") or {}).get("url")
            or (body.get("clickThroughUrl") or {}).get("url")
            or body.get("link")
        )
        published = body.get("pubDate") or body.get("displayTime")
        if not published and item.get("providerPublishTime"):
            published = dt.datetime.fromtimestamp(
                item["providerPublishTime"], tz=dt.timezone.utc
            ).isoformat()
        source = (body.get("provider") or {}).get("displayName") or body.get("publisher")
        if title and url:
            headlines.append(
                {
                    "title": title,
                    "url": url,
                    "date": str(published)[:10] if published else "date not provided",
                    "source": source or "source not provided",
                }
            )
    return headlines


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_currency(ticker):
    return yf.Ticker(ticker).fast_info.get("currency")


def get_currency(ticker, info):
    """Currency code from company info, falling back to fast_info."""
    code = info.get("currency")
    if not code:
        try:
            code = fetch_currency(ticker)
        except Exception:
            code = None
    return code or "unknown"


# ----------------------------------------------------------------------------
# Fundamentals
# ----------------------------------------------------------------------------

def build_fundamentals(info, price, currency):
    """Rows of (label, text). Anything Yahoo does not provide is marked unavailable."""
    yield_value = info.get("trailingAnnualDividendYield")
    if is_missing(yield_value) and info.get("dividendRate") and price:
        yield_value = info["dividendRate"] / price

    return [
        ("Market capitalization", fmt_big(info.get("marketCap"), currency)),
        ("P/E (trailing)", fmt_num(info.get("trailingPE"))),
        ("P/E (forward)", fmt_num(info.get("forwardPE"))),
        ("Revenue (trailing 12 months)", fmt_big(info.get("totalRevenue"), currency)),
        ("Revenue growth (latest quarter, year on year)", fmt_pct(info.get("revenueGrowth"), True)),
        ("Profit margin", fmt_pct(info.get("profitMargins"))),
        ("Dividend yield (trailing)", fmt_pct(yield_value)),
        ("52-week high", fmt_num(info.get("fiftyTwoWeekHigh"))),
        ("52-week low", fmt_num(info.get("fiftyTwoWeekLow"))),
    ]


# ----------------------------------------------------------------------------
# Analysis (rule based and fully visible: every sentence comes from fetched numbers)
# ----------------------------------------------------------------------------

def build_analysis(ind, price, info, start, end, has_news):
    facts, positives, risks, changes = [], [], [], []
    signals = []  # (description, +1 / 0 / -1)

    # --- Trend signals -------------------------------------------------------
    r = ind["period_return"]
    if r is not None:
        facts.append(
            f"The price moved {fmt_pct(r, True)} between {start} and {end} (closing prices, "
            "dividends excluded)."
        )
        if r > 0.01:
            signals.append(("Positive return over the period", 1))
            positives.append(f"The price rose {fmt_pct(r)} over the selected period.")
        elif r < -0.01:
            signals.append(("Negative return over the period", -1))
            risks.append(f"The price fell {fmt_pct(abs(r))} over the selected period.")
        else:
            signals.append(("Roughly flat return over the period", 0))

    ma20, ma50 = ind["ma20"], ind["ma50"]
    reference, label = (ma50, "50-day") if ma50 is not None else (ma20, "20-day")
    if reference is not None:
        above = price > reference
        facts.append(
            f"The latest close ({fmt_num(price)}) is {'above' if above else 'below'} its "
            f"{label} average ({fmt_num(reference)})."
        )
        signals.append((f"Price vs {label} average", 1 if above else -1))
        if above:
            positives.append(f"The price is trading above its {label} average.")
        else:
            risks.append(f"The price is trading below its {label} average.")
    if ma20 is not None and ma50 is not None:
        signals.append(("20-day vs 50-day average", 1 if ma20 > ma50 else -1))
        facts.append(
            f"The 20-day average ({fmt_num(ma20)}) is "
            f"{'above' if ma20 > ma50 else 'below'} the 50-day average ({fmt_num(ma50)})."
        )

    rsi = ind["rsi14"]
    if rsi is not None:
        facts.append(f"The 14-day RSI is {rsi:.0f}.")
        if rsi >= 70:
            risks.append(
                f"RSI of {rsi:.0f} is in the zone often called overbought, so recent gains may "
                "be stretched."
            )
        elif rsi <= 30:
            risks.append(
                f"RSI of {rsi:.0f} is in the zone often called oversold, which can mean heavy "
                "selling pressure."
            )

    vol = ind["volatility"]
    if vol is not None:
        facts.append(f"Annualized volatility is {fmt_pct(vol)}.")
        if vol > 0.40:
            risks.append(
                f"Annualized volatility of {fmt_pct(vol)} means large price swings have been common."
            )

    dd = ind["max_drawdown"]
    if dd is not None:
        facts.append(f"The largest peak-to-low fall in the period was {fmt_pct(dd)}.")
        if dd <= -0.20:
            risks.append(f"The price fell as much as {fmt_pct(abs(dd))} from a peak inside this period.")

    # --- Fundamentals (only when Yahoo provides them) --------------------------
    eps = info.get("trailingEps")
    if not is_missing(eps) and eps < 0:
        risks.append("Trailing earnings per share is negative, so the company was not profitable over the last 12 months.")
    margin = info.get("profitMargins")
    if not is_missing(margin):
        if margin > 0:
            positives.append(f"The company reports a positive profit margin ({fmt_pct(margin)}).")
        else:
            risks.append(f"The reported profit margin is negative ({fmt_pct(margin)}).")
    growth = info.get("revenueGrowth")
    if not is_missing(growth):
        if growth > 0:
            positives.append(f"Revenue grew {fmt_pct(growth)} year on year in the latest reported quarter.")
        else:
            risks.append(f"Revenue changed {fmt_pct(growth, True)} year on year in the latest reported quarter.")
    div = info.get("trailingAnnualDividendYield")
    if not is_missing(div) and div > 0:
        positives.append(
            f"The company paid dividends (trailing yield {fmt_pct(div)}). Dividends can be cut or stopped."
        )
    high = info.get("fiftyTwoWeekHigh")
    if not is_missing(high) and high > 0:
        facts.append(f"The latest close is {fmt_pct(price / high - 1, True)} versus the 52-week high.")

    if not positives:
        positives.append("The available data shows no clear positive factor.")
    if not risks:
        risks.append("The available data shows no standout risk, but absence of data is not absence of risk.")
    specific_risks = list(risks)  # the data-driven risks, before the general ones below
    risks.append(
        "Indicators look backward. News, earnings, policy and market-wide moves can change the picture quickly."
    )
    risks.append("Yahoo Finance data is unofficial and can be delayed or wrong.")

    # --- What could change the assessment -------------------------------------
    if reference is not None:
        changes.append(
            f"A sustained move to the other side of the {label} average ({fmt_num(reference)}) "
            "would flip the trend signals."
        )
    changes.append("New quarterly results, guidance changes or other company announcements.")
    changes.append("Sector or market-wide moves, interest-rate news and, for foreign listings, currency changes.")
    changes.append("A different time window. Check at least two periods before drawing a conclusion.")
    if has_news:
        changes.append("Anything in the recent headlines tab that you find credible after reading the source.")

    # --- Evidence summary ----------------------------------------------------
    total = sum(v for _, v in signals)
    if len(signals) < 2:
        label_text = "Mixed"
        reasoning = "Too few indicators could be calculated from this data to lean either way."
    else:
        positive_count = sum(1 for _, v in signals if v > 0)
        negative_count = sum(1 for _, v in signals if v < 0)
        label_text = "Positive" if total >= 2 else "Negative" if total <= -2 else "Mixed"
        reasoning = (
            f"{positive_count} of {len(signals)} trend signals were positive and "
            f"{negative_count} were negative. This summary covers price-trend evidence in the "
            "selected window only. It does not judge business quality or valuation."
        )

    questions = [
        "What does the company earn money from, and how steady is that income?",
        "How does the valuation compare with similar companies and with its own history?",
        "What do the latest earnings report and official filings say about the outlook?",
        "What would make you change your mind about this stock?",
    ]

    return {
        "facts": facts,
        "positives": positives,
        "risks": risks,
        "risks_specific": specific_risks,
        "changes": changes,
        "questions": questions,
        "signals": signals,
        "summary_label": label_text,
        "summary_reasoning": reasoning,
    }


# ----------------------------------------------------------------------------
# Research suggestion, score and ranking (rule based, uses only fetched data)
# ----------------------------------------------------------------------------

def score_stock(ind, price, info, days, stale):
    """Score one stock against CRITERIA and turn the score into a research suggestion.

    A criterion with no data earns 0 points, so missing information never helps.
    """
    ma20, ma50 = ind["ma20"], ind["ma50"]
    ref, ref_name = (ma50, "50-day") if ma50 is not None else (ma20, "20-day")
    r, vol, dd = ind["period_return"], ind["volatility"], ind["max_drawdown"]
    margin, growth = info.get("profitMargins"), info.get("revenueGrowth")

    # Points per criterion. None means the data is unavailable.
    earned = {
        "return": None if r is None else int(r > 0),
        "trend": None if ref is None else int(price > ref),
        "cross": None if ma20 is None or ma50 is None else int(ma20 > ma50),
        "volatility": None if vol is None else (2 if vol < 0.25 else 1 if vol < 0.40 else 0),
        "drawdown": None if dd is None else (2 if dd > -0.15 else 1 if dd > -0.30 else 0),
        "margin": None if is_missing(margin) else (2 if margin > 0.10 else 1 if margin > 0 else 0),
        "growth": None if is_missing(growth) else (2 if growth > 0.10 else 1 if growth > 0 else 0),
    }
    readings = {
        "return": fmt_pct(r, True),
        "trend": f"{fmt_num(price)} vs {ref_name} average {fmt_num(ref)}",
        "cross": f"{fmt_num(ma20)} vs {fmt_num(ma50)}",
        "volatility": fmt_pct(vol),
        "drawdown": fmt_pct(dd),
        "margin": fmt_pct(margin),
        "growth": fmt_pct(growth, True),
    }
    phrases = {  # what to say when a criterion earns full points
        "return": f"the price rose {fmt_pct(r)} over the period",
        "trend": f"it trades above its {ref_name} average",
        "cross": "its 20-day average is above its 50-day average",
        "volatility": f"volatility is relatively low ({fmt_pct(vol)} a year)",
        "drawdown": f"the deepest fall from a peak was shallow ({fmt_pct(dd)})",
        "margin": f"the profit margin is {fmt_pct(margin)}",
        "growth": f"revenue grew {fmt_pct(growth)} year on year",
    }

    rows, missing, strengths = [], [], []
    for key, label, max_points, _rule in CRITERIA:
        got = earned[key]
        if got is None:
            rows.append({"Criterion": label, "Reading": "unavailable", "Points": f"0 of {max_points}, data missing"})
            missing.append(f"{label} is unavailable, so it earns 0 of {max_points} points.")
        else:
            rows.append({"Criterion": label, "Reading": readings[key], "Points": f"{got} of {max_points}"})
            if got == max_points:
                strengths.append(phrases[key])
    if not info:
        missing.append("Yahoo returned no company details, so fundamentals are unavailable.")

    points = sum(v for v in earned.values() if v is not None)
    coverage = sum(1 for v in earned.values() if v is not None)
    available_max = sum(c[2] for c in CRITERIA if earned[c[0]] is not None)
    share = points / available_max if available_max else None

    # --- Suggestion ------------------------------------------------------------
    if coverage < 4:
        label = HOLD
        why = "Too little data is available to lean either way, so this is a watch rather than a call."
    elif points >= 8 and coverage >= 5 and earned["trend"] == 1:
        label = BUY
        why = (
            f"It scored {points} of {TOTAL_POINTS} points, trades above its {ref_name} average, "
            f"and {coverage} of {len(CRITERIA)} criteria had data."
        )
    elif share is not None and share <= 0.35:
        label = AVOID
        why = (
            f"It earned only {points} of the {available_max} points available from the data we "
            f"have ({share * 100:.0f}%)."
        )
    else:
        label = HOLD
        if points >= 8:
            gaps = []
            if earned["trend"] != 1:
                gaps.append("the price is not confirmed above its trend average")
            if coverage < 5:
                gaps.append("fewer than 5 criteria had data")
            why = "The score is high, but " + " and ".join(gaps) + ", so it does not qualify as a buy candidate."
        elif share >= 0.7:
            why = (
                f"The data we have looks strong ({points} of {available_max} available points), "
                f"but {len(CRITERIA) - coverage} criteria had no data and earn nothing, so it "
                f"cannot reach the {BUY} bar of 8 points."
            )
        else:
            why = (
                f"The evidence is mixed: it earned {points} of the {available_max} points "
                f"available from the data we have ({share * 100:.0f}%)."
            )

    # --- Confidence ------------------------------------------------------------
    clear = share is not None and (share >= 0.7 or share <= 0.3)
    reasons = [
        f"{coverage} of {len(CRITERIA)} criteria had data.",
        f"{days} trading days of price history were used.",
    ]
    if stale:
        reasons.append("The latest price is more than 5 days old.")
    if share is not None:
        reasons.append("The signals mostly agree." if clear else "The signals are mixed, which limits confidence.")
    if days < 60:
        reasons.append("Windows shorter than about 60 trading days (such as 1 month) give noisy signals.")
    if stale or coverage < 4 or days < 60:
        confidence = "Low"
    elif coverage >= 6 and days >= 120 and clear:
        confidence = "High"
    else:
        confidence = "Medium"

    return {
        "label": label, "why": why, "points": points, "available_max": available_max,
        "coverage": coverage, "share": share, "confidence": confidence,
        "confidence_reasons": reasons, "rows": rows, "missing": missing,
        "strengths": strengths, "earned": earned,
    }


def rank_results(results):
    """Highest score first. Ties go to the stock with more data, then alphabetical order."""
    ranked = sorted(
        results, key=lambda x: (-x["suggestion"]["points"], -x["suggestion"]["coverage"], x["ticker"])
    )
    for position, item in enumerate(ranked, start=1):
        item["rank"] = position
    return ranked


def build_comparison_frame(ranked):
    rows = []
    for r in ranked:
        s, ind, info = r["suggestion"], r["ind"], r["info"]
        rows.append({
            "Rank": r["rank"],
            "Ticker": r["ticker"],
            "Name": r["name"],
            "Research suggestion": s["label"],
            f"Score (max {TOTAL_POINTS})": s["points"],
            "Confidence": s["confidence"],
            "Data coverage": f"{s['coverage']} of {len(CRITERIA)}",
            "Latest close": f"{fmt_num(r['price'])} {r['currency']}",
            "Period return": fmt_pct(ind["period_return"], True),
            "Volatility": fmt_pct(ind["volatility"]),
            "Max drawdown": fmt_pct(ind["max_drawdown"]),
            "P/E (trailing)": fmt_num(info.get("trailingPE")),
            "Profit margin": fmt_pct(info.get("profitMargins")),
            "Revenue growth": fmt_pct(info.get("revenueGrowth"), True),
        })
    return pd.DataFrame(rows)


def build_overall_summary(ranked, failed_count=0):
    """Markdown lines describing the strongest research candidates and why."""
    candidates = [r for r in ranked if r["suggestion"]["label"] == BUY]
    counts = {label: sum(1 for r in ranked if r["suggestion"]["label"] == label) for label in (BUY, HOLD, AVOID)}

    def describe(r):
        s = r["suggestion"]
        shown = s["strengths"][:4]
        strengths = "; ".join(shown) if shown else "no criterion earned full points"
        if len(s["strengths"]) > len(shown):
            strengths += f"; plus {len(s['strengths']) - len(shown)} more"
        line = (
            f"- **#{r['rank']} {r['ticker']}** ({r['name']}): {s['points']} of {TOTAL_POINTS} "
            f"points, {s['confidence'].lower()} confidence. Strengths: {strengths}. "
            f"Watch-out: {r['analysis']['risks_specific'][0]}"
        )
        if s["missing"]:
            line += f" Data gaps: {len(s['missing'])}."
        return line

    if candidates:
        lines = [
            f"**{len(candidates)} of {len(ranked)} stocks met the criteria for \"{BUY}\".** "
            "The strongest, in rank order:"
        ]
        lines += [describe(r) for r in candidates[:3]]
    else:
        lines = [
            f"**No stock met the criteria for \"{BUY}\".** The highest-scoring stocks are "
            "listed for further research, but none has strong enough evidence in this data:"
        ]
        lines += [describe(r) for r in ranked[:3]]
    tally = f"Overall: {counts[BUY]} {BUY}, {counts[HOLD]} {HOLD}, {counts[AVOID]} {AVOID}"
    if failed_count:
        tally += f", {failed_count} could not be analysed"
    lines.append(tally + ". Ranks compare only the stocks you entered.")
    return lines


def suggestion_lines(r):
    """Plain-text version of the suggestion block, used in downloadable reports."""
    s = r["suggestion"]
    lines = [
        f"RESEARCH SUGGESTION: {s['label']} (score {s['points']} of {TOTAL_POINTS}, confidence {s['confidence']})",
        s["why"],
        "",
        "Evidence by criterion:",
    ]
    lines += [f"- {row['Criterion']}: {row['Reading']} ({row['Points']})" for row in s["rows"]]
    lines += ["", "Risks:"] + [f"- {t}" for t in r["analysis"]["risks_specific"]]
    lines += ["", "Missing or limited data:"]
    lines += [f"- {t}" for t in s["missing"]] or ["- Every scored criterion had data."]
    lines += ["", f"Confidence: {s['confidence']}"] + [f"- {t}" for t in s["confidence_reasons"]]
    lines += ["", RESEARCH_NOTE]
    return lines


# ----------------------------------------------------------------------------
# Text report for download
# ----------------------------------------------------------------------------

def build_report(ctx, ind, fundamentals, analysis, headlines):
    lines = [
        f"STOCK RESEARCH REPORT: {ctx['name']} ({ctx['ticker']})",
        f"Generated: {dt.datetime.now(dt.timezone.utc):%Y-%m-%d %H:%M} UTC",
        "",
        "DISCLAIMER: " + DISCLAIMER.replace("**", ""),
        "",
        "DATA",
        f"Sector: {ctx['sector']}",
        f"Latest close: {fmt_num(ctx['price'])} {ctx['currency']} (as of {ctx['end']})",
        f"Period shown: {ctx['start']} to {ctx['end']} ({ctx['period_label']}, {ctx['days']} trading days)",
        "Source: Yahoo Finance via yfinance (unofficial; may be delayed or incomplete)",
        "",
        "INDICATORS",
        f"Period return: {fmt_pct(ind['period_return'], True)}",
        f"20-day average: {fmt_num(ind['ma20'])}",
        f"50-day average: {fmt_num(ind['ma50'])}",
        f"RSI (14-day): {fmt_num(ind['rsi14'], 0)}",
        f"Annualized volatility: {fmt_pct(ind['volatility'])}",
        f"Maximum drawdown: {fmt_pct(ind['max_drawdown'])}",
        "",
        "FUNDAMENTALS",
    ]
    lines += [f"{label}: {value}" for label, value in fundamentals]
    if "suggestion" in ctx:
        lines += [""] + suggestion_lines(ctx)
    lines += ["", f"TREND EVIDENCE SUMMARY: {analysis['summary_label']}", analysis["summary_reasoning"]]
    for title, key in (
        ("WHAT THE DATA SHOWS", "facts"),
        ("POTENTIAL POSITIVE FACTORS", "positives"),
        ("RISKS AND UNCERTAINTIES", "risks"),
        ("WHAT COULD CHANGE THE ASSESSMENT", "changes"),
        ("QUESTIONS TO INVESTIGATE", "questions"),
    ):
        lines += ["", title] + [f"- {text}" for text in analysis[key]]
    lines += ["", "RECENT HEADLINES"]
    if headlines:
        lines += [f"- {h['date']} | {h['source']} | {h['title']} | {h['url']}" for h in headlines]
    elif headlines is None:
        lines.append("- Not loaded in the multi-stock report. Open the stock on its own to see them.")
    else:
        lines.append("- Could not be fetched from this data source.")
    return "\n".join(lines)


def build_multi_report(period_label, ranked, failed, summary_lines):
    lines = [
        "MULTI-STOCK RESEARCH REPORT",
        f"Generated: {dt.datetime.now(dt.timezone.utc):%Y-%m-%d %H:%M} UTC",
        f"Period: {period_label}",
        "",
        "DISCLAIMER: " + DISCLAIMER.replace("**", ""),
        RESEARCH_NOTE,
        "",
        "SUMMARY",
    ]
    lines += [line.replace("**", "") for line in summary_lines]
    lines += ["", f"RANKING (score out of {TOTAL_POINTS}; a criterion with missing data earns 0 points)"]
    for r in ranked:
        s = r["suggestion"]
        lines.append(
            f"{r['rank']}. {r['ticker']} | {s['label']} | {s['points']} of {TOTAL_POINTS} | "
            f"confidence {s['confidence']} | data for {s['coverage']} of {len(CRITERIA)} criteria"
        )
    if failed:
        lines += ["", "COULD NOT BE ANALYSED"] + [f"- {f['ticker']}: {f['short_error']}" for f in failed]
    for r in ranked:
        lines += ["", "=" * 70, build_report(r, r["ind"], r["fundamentals"], r["analysis"], None)]
    return "\n".join(lines)


# ----------------------------------------------------------------------------
# Web search (free, no API key): Google News RSS, SEC EDGAR and Yahoo headlines
# ----------------------------------------------------------------------------

HTTP_TIMEOUT = 8  # seconds per request
MAX_DOWNLOAD = 8_000_000  # bytes; the SEC ticker list is the largest file we read
WEB_AGENT = "Mozilla/5.0 (compatible; StockResearchAssistant/1.0; educational research app)"
ALLOWED_HOSTS = ("news.google.com", "www.sec.gov", "data.sec.gov")
GOOGLE_NEWS_URL = "https://news.google.com/rss/search"
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
SEC_DOCUMENT_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{document}"
SEC_FORMS = ("10-K", "10-Q", "8-K", "20-F", "6-K", "40-F", "DEF 14A")
SEC_8K_ITEMS = {
    "1.01": "entry into a material agreement",
    "1.02": "end of a material agreement",
    "2.01": "completed acquisition or disposal",
    "2.02": "results of operations (earnings release)",
    "2.03": "new financial obligation",
    "3.02": "unregistered sale of shares",
    "4.02": "financial statements should no longer be relied on",
    "5.02": "director or officer changes",
    "5.07": "shareholder vote results",
    "7.01": "Regulation FD disclosure",
    "8.01": "other events",
}
# Google News edition by exchange suffix: (language, country, edition id)
NEWS_EDITIONS = {
    "": ("en-US", "US", "US:en"),
    ".NS": ("en-IN", "IN", "IN:en"),
    ".BO": ("en-IN", "IN", "IN:en"),
    ".L": ("en-GB", "GB", "GB:en"),
    ".TO": ("en-CA", "CA", "CA:en"),
    ".AX": ("en-AU", "AU", "AU:en"),
}
# Regulators, exchanges and governments count as primary sources.
PRIMARY_HOSTS = (
    "sec.gov", "nseindia.com", "bseindia.com", "sebi.gov.in", "rbi.org.in", "federalreserve.gov",
    "bls.gov", "bea.gov", "treasury.gov", "londonstockexchange.com", "hkexnews.hk",
)
RELEASE_WIRES = ("businesswire.com", "prnewswire.com", "globenewswire.com", "accesswire.com")
MARKET_INDICES = [
    ("^GSPC", "S&P 500"), ("^DJI", "Dow Jones"), ("^IXIC", "Nasdaq Composite"),
    ("^NSEI", "Nifty 50"), ("^BSESN", "BSE Sensex"),
]


def safe_url(url):
    """The URL if it is a plain http(s) link, made safe for a markdown link. Otherwise None."""
    url = (url or "").strip()
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    for old, new in ((" ", "%20"), ("(", "%28"), (")", "%29"), ("<", "%3C"), (">", "%3E")):
        url = url.replace(old, new)
    return url


def md_escape(text):
    """Make untrusted text safe to place inside markdown (no formatting, math or tables)."""
    text = re.sub(r"\s+", " ", str(text if text is not None else "")).strip()
    return re.sub(r"([\\`*_\[\]<>|#$~])", r"\\\1", text)


def md_table(headers, rows):
    lines = [
        "| " + " | ".join(md_escape(h) for h in headers) + " |",
        "|" + "|".join(["---"] * len(headers)) + "|",
    ]
    lines += ["| " + " | ".join(md_escape(c) for c in row) + " |" for row in rows]
    return "\n".join(lines)


def http_get(url, headers=None):
    """Download a small file from one of the few hosts this app uses."""
    host = urllib.parse.urlparse(url).hostname or ""
    if host not in ALLOWED_HOSTS:
        raise ValueError("host not allowed")
    request = urllib.request.Request(url, headers={"User-Agent": WEB_AGENT, **(headers or {})})
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
        data = response.read(MAX_DOWNLOAD + 1)
        encoding = (response.headers.get("Content-Encoding") or "").lower()
    if len(data) > MAX_DOWNLOAD:
        raise ValueError("response too large")
    if encoding == "gzip":
        data = gzip.decompress(data)
    return data


def describe_error(exc):
    """A short, plain reason for a failed web request."""
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code == 429:
            return "the service is rate-limiting this app (HTTP 429)"
        if exc.code in (401, 403):
            return f"the service refused the request (HTTP {exc.code})"
        return f"the service returned an error (HTTP {exc.code})"
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return "the request timed out"
    if isinstance(exc, urllib.error.URLError):
        return "the service could not be reached"
    if isinstance(exc, (ValueError, KeyError, TypeError, ET.ParseError)):
        return "the response was not in the expected format"
    return type(exc).__name__


def source_kind(url):
    """'primary' for regulators, exchanges and government sites, 'company release' for
    press-release wires, otherwise None. A company's own investor page cannot be told apart
    from a look-alike by its address, so it is not guessed; the company website is shown instead."""
    host = (urllib.parse.urlparse(url or "").hostname or "").lower()
    if not host:
        return None
    if (re.search(r"(?:^|\.)gov(?:\.[a-z]{2})?$", host)
            or any(host == h or host.endswith("." + h) for h in PRIMARY_HOSTS)):
        return "primary"
    if any(host == h or host.endswith("." + h) for h in RELEASE_WIRES):
        return "company release"
    return None


def parse_news_date(text):
    """(YYYY-MM-DD, timestamp) from an RFC 822 date, or (None, None)."""
    try:
        moment = email.utils.parsedate_to_datetime((text or "").strip())
    except (TypeError, ValueError, IndexError):
        return None, None
    if moment is None:
        return None, None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    moment = moment.astimezone(dt.timezone.utc)
    return moment.date().isoformat(), moment.timestamp()


def parse_google_news(xml_bytes, limit=10):
    """Items from a Google News RSS feed. Only fields present in the feed are used."""
    upper = xml_bytes.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise ValueError("unexpected XML content")
    root = ET.fromstring(xml_bytes)
    if root.tag != "rss":
        raise ValueError("not an RSS feed")
    items = []
    for node in root.iter("item"):
        title = html.unescape((node.findtext("title") or "").strip())
        link = safe_url(node.findtext("link"))
        source_node = node.find("source")
        source = (source_node.text or "").strip() if source_node is not None else ""
        source_url = (source_node.get("url") or "") if source_node is not None else ""
        if source and title.endswith(" - " + source):
            title = title[: -len(source) - 3].strip()
        date, stamp = parse_news_date(node.findtext("pubDate"))
        if title and link:
            items.append({
                "title": title, "url": link, "source": source or None, "date": date, "ts": stamp,
                "kind": source_kind(source_url), "origin": "Google News",
            })
        if len(items) >= limit:
            break
    return items


@st.cache_data(ttl=900, show_spinner=False)
def fetch_google_news(query, edition):
    language, country, edition_id = NEWS_EDITIONS.get(edition, NEWS_EDITIONS[""])
    params = urllib.parse.urlencode({"q": query, "hl": language, "gl": country, "ceid": edition_id})
    return parse_google_news(http_get(f"{GOOGLE_NEWS_URL}?{params}"))


def sec_headers():
    # The SEC asks automated clients to identify themselves. Set SEC_USER_AGENT to your own
    # name and contact if the SEC refuses the default.
    agent = (os.environ.get("SEC_USER_AGENT") or "").strip().replace("\n", " ")
    return {"User-Agent": agent or "StockResearchAssistant/1.0 (educational research app)",
            "Accept": "application/json"}


@st.cache_data(ttl=86400, show_spinner=False)
def fetch_sec_ticker_map():
    data = json.loads(http_get(SEC_TICKERS_URL, sec_headers()))
    return {str(v["ticker"]).upper(): (int(v["cik_str"]), str(v.get("title", ""))) for v in data.values()}


def parse_sec_filings(data, cik, limit=8):
    """Recent 10-K, 10-Q, 8-K and similar filings from an EDGAR submissions file."""
    recent = (data.get("filings") or {}).get("recent") or {}
    forms = recent.get("form") or []

    def column(name):
        values = list(recent.get(name) or [])
        return values + [""] * (len(forms) - len(values))

    dates, accessions = column("filingDate"), column("accessionNumber")
    documents, descriptions, items = column("primaryDocument"), column("primaryDocDescription"), column("items")
    filings = []
    for i, form in enumerate(forms):  # the columns are parallel: same index, same filing
        if not str(form).startswith(SEC_FORMS):
            continue
        accession = str(accessions[i]).replace("-", "")
        if not accession:
            continue
        document = str(documents[i])
        base = f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/"
        topics = []
        if str(form).startswith("8-K"):
            for code in str(items[i]).split(","):
                if code.strip() in SEC_8K_ITEMS:
                    topics.append(SEC_8K_ITEMS[code.strip()])
        date = str(dates[i])
        filings.append({
            "form": str(form),
            "date": date if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date) else None,
            "description": str(descriptions[i]) or str(form),
            "topics": topics,
            "url": SEC_DOCUMENT_URL.format(cik=cik, accession=accession, document=document) if document else base,
        })
        if len(filings) >= limit:
            break
    return filings


@st.cache_data(ttl=1800, show_spinner=False)
def fetch_sec_filings(cik):
    data = json.loads(http_get(SEC_SUBMISSIONS_URL.format(cik=cik), sec_headers()))
    return parse_sec_filings(data, cik)


def sec_filings_for(ticker):
    """{'status', 'filings', 'error'}. Never raises. Status: ok, not_applicable, not_listed, error."""
    if not re.fullmatch(r"[A-Z0-9\-]{1,10}", ticker or ""):
        return {"status": "not_applicable", "filings": [], "error": ""}
    try:
        entry = fetch_sec_ticker_map().get(ticker.upper())
        if entry is None:
            return {"status": "not_listed", "filings": [], "error": ""}
        return {"status": "ok", "filings": fetch_sec_filings(entry[0]), "error": ""}
    except Exception as exc:
        return {"status": "error", "filings": [], "error": describe_error(exc)}


def clean_company_name(name, ticker):
    text = re.sub(
        r"\b(incorporated|inc|corporation|corp|company|co|limited|ltd|plc|holdings|class [a-c]|the)\b\.?",
        " ", name or "", flags=re.I,
    )
    text = re.sub(r"\s+", " ", re.sub(r"[^\w&\- ]", " ", text)).strip()
    base = ticker.split(".")[0]
    return text if text and text.lower() != ticker.lower() else base


def news_item_from_yahoo(item):
    date = item.get("date") if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(item.get("date"))) else None
    stamp = None
    if date:
        stamp = dt.datetime.fromisoformat(date).replace(tzinfo=dt.timezone.utc).timestamp()
    return {
        "title": item["title"], "url": safe_url(item["url"]), "source": item.get("source"),
        "date": date, "ts": stamp, "kind": source_kind(item["url"]), "origin": "Yahoo Finance",
    }


def merge_news(*lists, limit=8):
    """Remove duplicate headlines. Primary sources first, then newest first."""
    seen, merged = set(), []
    for items in lists:
        for item in items:
            key = re.sub(r"\W+", " ", item["title"].lower()).strip()[:80]
            if key and item.get("url") and key not in seen:
                seen.add(key)
                merged.append(item)
    order = {"primary": 0, "company release": 1}
    merged.sort(key=lambda x: (order.get(x["kind"], 2), -(x["ts"] or 0)))
    return merged[:limit]


def gather_web(ticker, name, info):
    """Everything the web search can find for one stock. Each source fails on its own."""
    web = {
        "ticker": ticker, "filings": [], "filings_status": "", "filings_error": "", "news": [],
        "errors": [], "website": safe_url((info or {}).get("website")),
        "retrieved": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M"),
    }
    sec = sec_filings_for(ticker)
    web["filings"], web["filings_status"], web["filings_error"] = sec["filings"], sec["status"], sec["error"]

    suffix = ticker[ticker.rfind("."):].upper() if "." in ticker else ""
    company = clean_company_name(name, ticker)
    queries = [
        f'"{company}" (stock OR shares OR earnings) when:14d',
        f'"{company}" (announces OR "press release" OR filing OR results) when:30d',
    ]
    found = []
    for query in queries:
        try:
            found += fetch_google_news(query, suffix)
        except Exception as exc:
            web["errors"].append(f"news search: {describe_error(exc)}")
            break
    yahoo = []
    for item in load_news(ticker):
        if safe_url(item.get("url")):
            yahoo.append(news_item_from_yahoo(item))
    web["news"] = merge_news(found, yahoo)
    return web


@st.cache_data(ttl=600, show_spinner=False)
def fetch_index(symbol):
    return yf.Ticker(symbol).history(period="5d", interval="1d")


def market_snapshot():
    """Latest session versus the previous one for a few major indexes. Failures are listed."""
    rows, failed = [], []
    for symbol, label in MARKET_INDICES:
        try:
            close = fetch_index(symbol)["Close"].dropna()
            if len(close) < 2:
                raise ValueError("not enough data")
            last, previous = float(close.iloc[-1]), float(close.iloc[-2])
            rows.append({"name": label, "last": last, "change": last / previous - 1,
                         "date": close.index[-1].date().isoformat()})
        except Exception:
            failed.append(label)
    return rows, failed


SECTORS = {
    "technology": r"tech|technology|software|semiconductors?|chips?",
    "healthcare": r"health ?care|pharma\w*|biotech|medical|hospitals?",
    "financial-services": r"financials?|financial services|banks?|banking|insurance|insurers|fintech",
    "energy": r"energy|oil|gas|petroleum",
    "consumer-cyclical": r"consumer cyclical|consumer discretionary|retail(?:ers)?|automobiles?|autos?|carmakers?",
    "consumer-defensive": r"consumer defensive|consumer staples|fmcg|groceries|grocery",
    "industrials": r"industrials?|aerospace|defen[cs]e|machinery|logistics",
    "utilities": r"utilit(?:y|ies)",
    "real-estate": r"real estate|reits?|property developers?",
    "basic-materials": r"basic materials|materials|mining|metals|steel|chemicals",
    "communication-services": r"communication services?|telecom\w*|media|entertainment|social media",
}


def find_sector(text):
    lower = (text or "").lower()
    for key, pattern in SECTORS.items():
        if re.search(rf"\b(?:{pattern})\b", lower):
            return key
    return None


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_sector_companies(key, region):
    """[(symbol, name)] of the largest companies in a Yahoo Finance sector."""
    try:
        sector = yf.Sector(key, region=region) if region else yf.Sector(key)
    except TypeError:  # older yfinance versions have no region option
        if region:
            raise
        sector = yf.Sector(key)
    top = sector.top_companies
    if top is None or len(top) == 0:
        return []
    has_names = "name" in top.columns
    return [(str(symbol), str(top["name"].iloc[i]) if has_names else "")
            for i, symbol in enumerate(list(top.index)[:10])]


# ----------------------------------------------------------------------------
# Chat assistant: reads the question, fetches data and web results, answers in plain language
# ----------------------------------------------------------------------------

CHAT_MAX_STOCKS = 6  # most stocks analysed for one question
WEB_MAX_STOCKS = 3  # most stocks that get a web search for one question
MAX_CHAT_MESSAGES = 40

CHAT_NOTE = (
    "*Research only, not personalised financial advice. This tool does not place trades, and "
    "every decision is yours.*"
)
WEB_NOTE = (
    "Links come from third-party search results. This app has not verified them, so open the "
    "source to confirm the details and the date before relying on them."
)

# Well-known names. Words that are also common English words must be capitalised to count.
KNOWN_COMPANIES = {
    "apple": "AAPL", "microsoft": "MSFT", "alphabet": "GOOGL", "google": "GOOGL", "amazon": "AMZN",
    "nvidia": "NVDA", "tesla": "TSLA", "meta": "META", "netflix": "NFLX", "jpmorgan": "JPM",
    "jp morgan": "JPM", "exxon": "XOM", "exxonmobil": "XOM", "walmart": "WMT", "intel": "INTC",
    "johnson & johnson": "JNJ", "reliance": "RELIANCE.NS", "tcs": "TCS.NS", "infosys": "INFY.NS",
    "hdfc bank": "HDFCBANK.NS", "icici bank": "ICICIBANK.NS",
}
NEEDS_CAPITAL = {"meta", "intel", "reliance"}
EXCHANGE_SUFFIXES = "NS|BO|L|TO|HK|T|AX|DE|SS|SZ|PA|MI|SI|KS"
# All-capital words that look like tickers but are not.
CAPS_STOP = set("""
AI IT US USA UK EU PE EPS RSI ETF IPO CEO CFO SEC FDA GDP CPI FED FOMC NSE BSE NYSE NASDAQ VS AND OR
THE FOR BUY SELL HOLD NEWS WHY WHAT HOW WHO ALL NOW NEW CAN ARE YOU ANY TOP OK DO IS OF ON IN TO AT AS BE
BY IF MY NO SO UP WE AM AN ME TV PM Q1 Q2 Q3 Q4 FY YOY QOQ ROE ROI EBITDA SIP ESG DCF TTM YTD AVOID WATCH
RISK TODAY EDGAR DEF NIFTY SENSEX DOW EV
""".split())
# Capitalised words that are not company names.
CHAT_STOPWORDS = set("""
what why how which who when where is are was were do does did can could should would will tell show give
find compare explain list rank analyze analyse research check look the an and or vs versus about on for of
in to with from any all news stock stocks share shares market markets today now latest recent risk risks
sector industry companies company buy sell hold avoid candidate potential thanks thank hello hi hey please
also then next more other another same these those them it its this that here there yes no ok okay sure
filing filings earnings price prices rating rated score suggestion main best top better promising indian
i i'm my me we you monday tuesday wednesday thursday friday saturday sunday january february march april
may june july august september october november december
""".split())

INTENT_PATTERNS = {
    "method": [
        r"\bhow (?:do|does|are|is|did) (?:you|the app|the score|suggestions?|rankings?)\b.*\b(?:score|rate|rating|rank|ranking|suggest\w*|decide|work|calculat\w*|generat\w*|choose)",
        r"\b(?:scoring|ranking|suggestion|confidence)\b.*\b(?:method|criteria|rules?|calculated|generated)\b",
        r"\b(?:methodology|criteria)\b",
    ],
    "define": [r"\b(?:what is|what's|what does|explain|define|meaning of)\b.*\b(?:rsi|volatility|drawdown|moving averages?|p/e|pe ratio|market cap\w*)\b"],
    "market": [
        r"\b(?:market|markets|wall street|sensex|nifty|indices|indexes|s&p 500|dow|nasdaq)\b.*\b(?:today|changed|change|moving|moved|doing|now|this week|summary|wrap|update)\b",
        r"\bwhat(?:'s| is| has)? (?:changed|happening|moving)\b.*\b(?:market|markets|today)\b",
        r"\bmarket (?:today|summary|wrap|update|news)\b",
    ],
    "sector": [
        r"\b(?:sector|industry|industries|peers|competitors|rivals|similar (?:companies|stocks))\b",
        r"\bfind (?:me )?(?:some )?(?:companies|stocks)\b",
    ],
    "news": [r"\b(?:news|headlines?|announc\w+|press release|what happened|what's happening|in the news|catalysts?|could affect|might affect|latest developments?)\b"],
    "filings": [r"\b(?:filings?|10-k|10-q|8-k|20-f|6-k|edgar|sec|annual report|quarterly report|investor relations|prospectus|disclosures?)\b"],
    "why": [
        r"\bwhy\b.*\b(?:rate|rated|rating|suggest\w*|label\w*|score\w*|candidate|buy|avoid|hold|watch|rank\w*)\b",
        r"\b(?:explain|justify|reason for)\b.*\b(?:rating|suggestion|score|label|rank\w*)\b",
        r"\bhow did you (?:rate|score|rank)\b",
    ],
    "risks": [
        r"\b(?:risks?|risky|downside|concerns?|worr\w+|go wrong|research next|look into next|due diligence|red flags?|weakness\w*)\b",
        r"\bwhat should i (?:research|look|check|investigate|read)\b",
    ],
    "compare": [r"\b(?:compare|comparison|versus|vs\.?|which (?:one|of|stock|looks|is)|between|stack up)\b"],
    "suggest": [r"\b(?:buy|sell|avoid|hold|suggest\w*|recommend\w*|rate|rating|worth|outlook|analy[sz]e|analysis|opinion|good (?:stock|buy|investment)|invest\w*|should i|promising|tell me about|how is|how's|how are)\b"],
    "help": [r"\b(?:help|what can you do|how can you help|capabilities|examples?)\b", r"^\s*(?:hi|hello|hey|thanks|thank you)[!. ]*$"],
}
TOPIC_FLAGS = ("why", "risks", "news", "filings", "compare", "suggest")

CHAT_INTRO = (
    "Hi. Ask me about any listed company in plain language. I look up market data (Yahoo Finance), "
    "search the web for news and company filings, and explain what the app's rules make of it.\n\n"
    "I follow the conversation, so you can ask follow-ups such as *\"Why did you rate it that way?\"* "
    "or *\"What about MSFT?\"*. I am rule-based, not a generative AI model, so I understand the "
    "question types in the README and will ask you to rephrase otherwise."
)
CHAT_EXAMPLES = [
    "Tell me about AAPL",
    "Compare AAPL, MSFT and NVDA and explain which looks more promising",
    "What recent news could affect Reliance?",
    "Find companies in the technology sector and compare them",
    "What changed in the market today?",
]


# --- Understanding the question ------------------------------------------------

def extract_entities(text):
    """(tickers in the order they appear, capitalised words that may be company names)."""
    working = text or ""
    found = []  # (position, ticker)

    def blank(match):
        nonlocal working
        working = working[: match.start()] + " " * (match.end() - match.start()) + working[match.end():]

    for match in list(re.finditer(rf"(?<![\w.])([A-Za-z0-9\-]{{1,12}})\.({EXCHANGE_SUFFIXES})\b", working, re.I)):
        found.append((match.start(), f"{match.group(1)}.{match.group(2)}".upper()))
        blank(match)
    for match in list(re.finditer(r"(?<!\w)\$([A-Za-z][A-Za-z0-9\-]{0,9})\b", working)):
        found.append((match.start(), match.group(1).upper()))
        blank(match)
    for key in sorted(KNOWN_COMPANIES, key=len, reverse=True):
        for match in list(re.finditer(r"\b" + re.escape(key) + r"\b", working, re.I)):
            if key in NEEDS_CAPITAL and not match.group()[0].isupper():
                continue
            found.append((match.start(), KNOWN_COMPANIES[key]))
            blank(match)

    candidates, current, last_end = [], [], -10
    for match in re.finditer(r"(?<![\w.])[A-Za-z][A-Za-z0-9\-]*", working):
        word = match.group()
        is_caps = re.fullmatch(r"[A-Z]+(?:-[A-Z]{1,2})?", word) is not None
        if is_caps and 2 <= len(word) <= 6 and word not in CAPS_STOP:
            found.append((match.start(), word))
            word_is_name = False
        else:
            word_is_name = (word[0].isupper() and word.isalpha() and not word.isupper() and len(word) > 2
                            and word.lower() not in CHAT_STOPWORDS)
        if word_is_name or (is_caps and word.isalpha() and len(word) > 6 and word not in CAPS_STOP):
            if current and match.start() - last_end <= 1:
                current.append(word)
            else:
                if current:
                    candidates.append(" ".join(current))
                current = [word]
            last_end = match.end()
        elif current:
            candidates.append(" ".join(current))
            current = []
    if current:
        candidates.append(" ".join(current))

    tickers = []
    for _, ticker in sorted(found):
        if ticker not in tickers:
            tickers.append(ticker)
    return tickers, [c for i, c in enumerate(candidates) if c not in candidates[:i]]


@st.cache_data(ttl=86400, show_spinner=False)
def search_symbol(name):
    quotes = yf.Search(name, max_results=5).quotes or []
    return [(q.get("symbol"), q.get("longname") or q.get("shortname") or "", q.get("quoteType"))
            for q in quotes if q.get("symbol")]


def lookup_company(name):
    """Ticker for a company name, only when Yahoo's search clearly finds that company."""
    try:
        quotes = search_symbol(name)
    except Exception:
        return None
    needle = name.lower()
    for symbol, label, quote_type in quotes:
        if quote_type == "EQUITY" and needle in label.lower():
            return str(symbol).upper()
    return None


def resolve_message_tickers(text, context_tickers, sidebar_tickers):
    """Find the stocks a question is about. Falls back to the chat so far for follow-ups."""
    tickers, candidates = extract_entities(text)
    unresolved = []
    for candidate in candidates[:3]:
        symbol = lookup_company(candidate)
        if symbol is None:
            unresolved.append(candidate)
        elif symbol not in tickers:
            tickers.append(symbol)
    source = "message" if tickers else "none"
    lower = text.lower()
    if (len(tickers) == 1 and context_tickers and context_tickers[0] not in tickers
            and re.search(r"\b(?:compare|versus|vs\.?|against)\b.*\b(?:it|this|that|them)\b|\b(?:it|this|that)\b.*\b(?:compare|versus|vs\.?|against)\b", lower)):
        tickers = [context_tickers[0]] + tickers
    if not tickers and not unresolved:
        wants_list = re.search(r"\b(?:these|those|my list|the list|my stocks|my watchlist|all of them)\b", lower)
        if context_tickers:
            tickers, source = list(context_tickers), "context"
        elif wants_list and len(sidebar_tickers) >= 2:
            tickers, source = list(sidebar_tickers), "sidebar"
    return {"tickers": tickers, "unresolved": unresolved, "source": source}


def parse_period(text, default):
    t = (text or "").lower()
    if re.search(r"\b(?:5|five)[- ]?(?:years?|yrs?)\b", t):
        return "5 years"
    if re.search(r"\b(?:6|six)[- ]?(?:months?|mos?)\b|\bhalf[- ]a[- ]year\b", t):
        return "6 months"
    if re.search(r"\b(?:1|one|past|last)[- ]?month\b|\bthis month\b|\b30 days\b", t):
        return "1 month"
    if re.search(r"\b(?:1|one|past|last)[- ]?(?:year|yr)\b|\b12 months\b", t):
        return "1 year"
    return default


def detect_intents(text):
    t = (text or "").lower()
    return {name: any(re.search(p, t) for p in patterns) for name, patterns in INTENT_PATTERNS.items()}


def asked_label(text):
    t = (text or "").lower()
    if re.search(r"\bavoid\b", t):
        return AVOID
    if re.search(r"\b(?:hold|watch)\b", t):
        return HOLD
    if re.search(r"\b(?:buy|candidate)\b", t):
        return BUY
    return None


# --- Building the answer -----------------------------------------------------------

def tag(kind, detail=""):
    label = {
        "data": ":blue[**MARKET DATA**]",
        "web": ":orange[**LIVE WEB FINDINGS**]",
        "analysis": ":violet[**APP ANALYSIS**]",
    }[kind]
    return label + (f" · {detail}" if detail else "")


def bullets(items):
    return [f"- {md_escape(item)}" for item in items]


def stock_title(r):
    return f"#### {md_escape(r['name'])} ({md_escape(r['ticker'])})"


def data_line(r):
    return tag("data", (
        f"Yahoo Finance via yfinance, which may be delayed · latest close {fmt_num(r['price'])} "
        f"{md_escape(r['currency'])} on {r['end']} · {r['period_label']} window"
    ))


def item_age_days(item):
    if not item.get("ts"):
        return None
    return (dt.datetime.now(dt.timezone.utc).timestamp() - item["ts"]) / 86400


def news_line(item):
    title = md_escape(item["title"])
    link = safe_url(item.get("url"))
    head = f"[{title}]({link})" if link else title
    parts = [md_escape(item.get("source") or "publisher not provided"), item.get("date") or "date not provided"]
    if item.get("kind") == "primary":
        parts.append("primary source")
    elif item.get("kind") == "company release":
        parts.append("company-issued release")
    age = item_age_days(item)
    if age is not None and age > 30:
        parts.append("older than 30 days")
    return f"- {head} — " + " · ".join(parts)


def filing_line(filing):
    desc = md_escape(filing["description"])
    link = safe_url(filing["url"])
    head = f"[{desc}]({link})" if link else desc
    line = f"- **{md_escape(filing['form'])}** · filed {filing['date'] or 'date not provided'} · {head}"
    if filing["topics"]:
        line += f" · covers: {md_escape('; '.join(filing['topics']))}"
    return line


def block_web(r, web, compact=False, note=True):
    """Live web findings for one stock, clearly separated from market data and analysis."""
    if web is None:
        return [tag("web"), f"Web search was skipped for {md_escape(r['ticker'])} to stay within free service "
                "limits. Ask about it on its own to include its news and filings."]
    top = 3 if compact else 6
    # Name only the sources that actually returned something, so a failed search is never credited.
    shown_news = web["news"][:top]
    used = (["SEC EDGAR"] if web["filings_status"] == "ok" and web["filings"] else []) + [
        name for name in ("Google News", "Yahoo Finance") if any(i["origin"] == name for i in shown_news)]
    found = ("sources that returned results: " + ", ".join(used)) if used else "no source returned results"
    lines = [tag("web", f"{md_escape(r['ticker'])} · retrieved {web['retrieved']} UTC · {found}"), ""]
    status = web["filings_status"]
    if status == "ok" and web["filings"]:
        lines += ["**Recent SEC filings** (primary source, SEC EDGAR)"] + [filing_line(f) for f in web["filings"][:top]]
    elif status == "ok":
        lines.append("SEC EDGAR returned no recent 10-K, 10-Q or 8-K style filings for this company.")
    elif status == "not_applicable":
        lines.append("SEC EDGAR only covers companies that file in the US, so no filings are listed for this symbol. "
                     "For non-US companies, check the exchange's announcements page (for example "
                     "[NSE](https://www.nseindia.com) or [BSE](https://www.bseindia.com) in India).")
    elif status == "not_listed":
        lines.append("This symbol is not in the SEC's ticker list, so no filings are shown. It may be listed outside "
                     "the US or be new. Check the exchange's announcements page.")
    else:
        lines.append(f"SEC EDGAR could not be searched ({md_escape(web['filings_error'] or 'unknown problem')}), so no filings are shown.")
    if web["website"]:
        lines.append(f"Company website from Yahoo Finance: {web['website']} (look for its investor relations section).")
    lines.append("")
    if web["news"]:
        lines += ["**Recent headlines** (primary sources first, then newest)"] + [news_line(i) for i in web["news"][:top]]
        if not any(i["kind"] == "primary" for i in web["news"]):
            lines.append("None of these headlines is a primary source. Treat them as secondary reporting and "
                         "check the filings or the company's own announcements.")
    elif web["errors"]:
        lines.append("Web news search is unavailable right now (" + "; ".join(md_escape(e) for e in web["errors"])
                     + "), and Yahoo Finance returned no headlines. No news is shown.")
    else:
        lines.append("No recent headlines were found for this company in the last few weeks.")
    if web["errors"] and web["news"]:
        lines.append("Part of the news search failed (" + "; ".join(md_escape(e) for e in web["errors"])
                     + "), so some coverage may be missing.")
    if note:
        lines += ["", f"*{WEB_NOTE}*"]
    return lines


def research_next(r, web):
    s, steps = r["suggestion"], []
    if s["missing"]:
        steps.append("Fill the data gaps listed above from the company's own annual or quarterly report, because "
                     "the score gives missing figures 0 points.")
    if web and web["filings"]:
        latest = web["filings"][0]
        steps.append(f"Read the most recent filing ({latest['form']}, filed {latest['date'] or 'date not provided'}).")
    if s["confidence"] == "Low":
        steps.append("Re-run with a longer period, because confidence is Low.")
    steps += r["analysis"]["questions"][:3]
    return steps


def block_overview(r, web, preface=False):
    s, ind, info = r["suggestion"], r["ind"], r["info"]
    lines = [stock_title(r)]
    if preface:
        lines += ["I can't tell you whether you should buy or sell, and the decision is yours. Here is the research "
                  "view from the data, which you can weigh against your own situation.", ""]
    lines += [data_line(r), "", md_table(
        ["Return", "Volatility", "Max drawdown", "P/E (trailing)", "Market cap", "Profit margin"],
        [[fmt_pct(ind["period_return"], True), fmt_pct(ind["volatility"]), fmt_pct(ind["max_drawdown"]),
          fmt_num(info.get("trailingPE")), fmt_big(info.get("marketCap"), r["currency"]),
          fmt_pct(info.get("profitMargins"))]]), ""]
    lines += [tag("analysis", "rule-based, from the data above; not a forecast"),
              f"**Research view: {s['label']}** · score {s['points']} of {TOTAL_POINTS} · confidence {s['confidence']}",
              md_escape(s["why"]), "", md_table(["Criterion", "Reading", "Points"],
                                               [[row["Criterion"], row["Reading"], row["Points"]] for row in s["rows"]]), ""]
    lines += ["**Strengths**"] + (bullets(s["strengths"][:4]) or ["- No criterion earned full points."])
    lines += ["", "**Risks**"] + bullets(r["analysis"]["risks_specific"])
    lines += ["", "**Missing, stale or limited data**"] + (bullets(s["missing"]) or ["- Every scored criterion had data."])
    lines += ["", f"**Confidence: {s['confidence']}**"] + bullets(s["confidence_reasons"])
    lines += ["", "**What to research next**"] + bullets(research_next(r, web))
    lines += ["", ""] + block_web(r, web, compact=True)
    return lines


def explain_label_rules(r):
    s = r["suggestion"]
    trend = s["earned"]["trend"]
    trend_text = "unavailable" if trend is None else "yes" if trend == 1 else "no"
    return [
        f"{BUY} needs all three: a score of at least 8 of {TOTAL_POINTS} (this stock: {s['points']}), the price above its "
        f"trend average (this stock: {trend_text}), and data for at least 5 of {len(CRITERIA)} criteria (this stock: {s['coverage']}).",
        f"{AVOID} needs data for at least 4 criteria and 35% or less of the points available from that data "
        f"(this stock: {'n/a' if s['share'] is None else format(s['share'] * 100, '.0f') + '%'} with {s['coverage']} criteria).",
        f"{HOLD} is everything else, including stocks with too little data to lean either way.",
    ]


def block_why(r, wanted=None):
    s = r["suggestion"]
    lines = [stock_title(r), data_line(r), "", tag("analysis", "how the rules produced this suggestion"),
             f"**{r['ticker']} is currently rated {s['label']}** · score {s['points']} of {TOTAL_POINTS} · confidence {s['confidence']}"]
    if wanted and wanted != s["label"]:
        lines.append(f"You asked about \"{wanted}\", but with the data fetched now the suggestion is \"{s['label']}\". "
                     "Suggestions can change when prices or data change.")
    lines += [md_escape(s["why"]), "", md_table(["Criterion", "Reading", "Points"],
                                               [[row["Criterion"], row["Reading"], row["Points"]] for row in s["rows"]]), "",
              "**How the label is decided**"] + bullets(explain_label_rules(r))
    lines += ["", "**Missing, stale or limited data**"] + (bullets(s["missing"]) or ["- Every scored criterion had data."])
    lines += ["", f"**Why the confidence is {s['confidence']}**"] + bullets(s["confidence_reasons"])
    lines += ["", "The suggestion uses price history and the fundamentals Yahoo provides. It does not use news, valuation "
              "(P/E), dividends or sector context, so a recent event can matter more than the score suggests."]
    return lines


def block_risks(r, web):
    s, a = r["suggestion"], r["analysis"]
    lines = [stock_title(r), data_line(r), "", tag("analysis", "rule-based, from the data above"), "**Risks and uncertainties**"]
    lines += bullets(a["risks"]) + ["", "**What could change the assessment**"] + bullets(a["changes"][:4])
    lines += ["", "**Missing, stale or limited data**"] + (bullets(s["missing"]) or ["- Every scored criterion had data."])
    lines += ["", "**What to research next**"] + bullets(research_next(r, web)) + [""]
    lines += block_web(r, web, compact=True)
    return lines


def compare_view(ranked):
    top = ranked[0]
    s = top["suggestion"]
    lines = [f"**On this app's criteria, {md_escape(top['ticker'])} has the strongest evidence of the {len(ranked)} stocks "
             f"compared:** {s['points']} of {TOTAL_POINTS} points, \"{s['label']}\", {s['confidence'].lower()} confidence."]
    second = ranked[1]
    gap = s["points"] - second["suggestion"]["points"]
    if gap == 0:
        lines.append(f"{md_escape(second['ticker'])} scored the same. The tie was broken by data coverage and then "
                     "alphabetical order, which carries no analytical meaning, so treat them as level.")
    elif gap == 1:
        lines.append(f"The gap to {md_escape(second['ticker'])} is only 1 point, so this is a close call.")
    else:
        lines.append(f"It leads {md_escape(second['ticker'])} by {gap} points.")
    if not any(r["suggestion"]["label"] == BUY for r in ranked):
        lines.append(f"No stock in this group reached \"{BUY}\", so first place only means the least weak evidence here.")
    lines.append("")
    for r in ranked:
        s = r["suggestion"]
        strengths = "; ".join(s["strengths"][:2]) or "no criterion earned full points"
        lines.append(f"- **{md_escape(r['ticker'])}** (#{r['rank']}, {s['label']}, {s['points']}/{TOTAL_POINTS}, "
                     f"{s['confidence'].lower()} confidence). Strengths: {md_escape(strengths)}. Main watch-out: "
                     f"{md_escape(r['analysis']['risks_specific'][0])} Data gaps: {len(s['missing'])}.")
    lines += ["", "This ranks evidence on the app's criteria only. It does not say which stock you should buy. It leaves out "
              "valuation, news, competition and business quality, so research those before deciding."]
    return lines


def block_compare(ranked, period_label):
    frame = build_comparison_frame(ranked)
    columns = ["Rank", "Ticker", "Research suggestion", f"Score (max {TOTAL_POINTS})", "Confidence", "Data coverage",
               "Period return", "Volatility", "Max drawdown", "Profit margin"]
    dates = sorted({r["end"] for r in ranked})
    lines = [tag("data", f"Yahoo Finance via yfinance, which may be delayed · latest closes dated {', '.join(dates)} · {period_label} window"),
             "", md_table(columns, frame[columns].values.tolist()), "",
             "Prices are in each stock's own currency, so compare scores and percentages. Ranked by score; missing data earns 0 points.", "",
             tag("analysis", "rule-based ranking, not a forecast")] + compare_view(ranked)
    return lines


def block_market():
    rows, failed = market_snapshot()
    lines = []
    if rows:
        lines += [tag("data", "Yahoo Finance via yfinance, latest session versus the one before, may be delayed"), "",
                  md_table(["Index", "Latest close", "Change", "Session date"],
                           [[row["name"], fmt_num(row["last"]), fmt_pct(row["change"], True), row["date"]] for row in rows])]
        if failed:
            lines.append(f"Could not load: {', '.join(md_escape(f) for f in failed)}.")
        lines.append("Session dates can differ between markets because of time zones and holidays. A date that is not "
                     "today means that market has not traded yet or is closed.")
    else:
        lines.append("Index data could not be loaded from Yahoo Finance right now, so there are no index figures to show.")
    news, errors, retrieved = [], [], dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M")
    for query, edition in (("stock market today when:1d", ""), ("Sensex Nifty today when:1d", ".NS")):
        try:
            news += fetch_google_news(query, edition)
        except Exception as exc:
            errors.append(describe_error(exc))
    news = merge_news(news, limit=8)
    lines += ["", tag("web", f"news search · retrieved {retrieved} UTC")]
    if news:
        lines += [news_line(i) for i in news] + ["", f"*{WEB_NOTE}*"]
    elif errors:
        lines.append(f"Web news search is unavailable right now ({md_escape(errors[0])}), so there are no headlines to show.")
    else:
        lines.append("No market headlines were found for the last day.")
    lines += ["", "This summarises what the data and headlines show. It does not explain why the market moved or predict what happens next."]
    return lines


def block_define(text):
    t = text.lower()
    entries = []
    for pattern, label in ((r"\brsi\b", "RSI (14-day)"), (r"volatil", "Annualized volatility"),
                           (r"drawdown", "Maximum drawdown"), (r"moving average", "20-day average")):
        if re.search(pattern, t):
            meaning, limit = INDICATOR_HELP[label]
            entries.append(f"**{label}.** {meaning} *Limitation:* {limit}")
    if re.search(r"p/e|pe ratio", t):
        entries.append("**P/E ratio.** The share price divided by the company's earnings per share. A higher number means investors "
                       "pay more for each unit of earnings. *Limitation:* it is not scored here, differs widely between sectors, "
                       "and is meaningless when earnings are negative.")
    if re.search(r"market cap", t):
        entries.append("**Market capitalization.** Share price times the number of shares. It measures the company's size, not "
                       "whether the stock is cheap or expensive.")
    return entries or [md_escape("Ask about RSI, volatility, drawdown, moving averages, P/E or market cap and I will explain it.")]


def block_method():
    return [
        "**How suggestions, rankings and confidence work**", "",
        method_markdown(),
    ]


CLARIFY_STOCK = ("Which stock do you mean? Give me a ticker symbol in capitals (for example AAPL, or RELIANCE.NS for India) "
                 "or a company name. I can also tell you what changed in the market today or list companies in a sector.")


def block_help():
    return [
        "**What I can do.** I research stocks with free data sources and explain the results. I am rule-based, not a generative "
        "AI model, so I answer the question types below and ask you to rephrase anything else.", "",
        "- **A research view of a stock:** \"Tell me about AAPL\"",
        "- **Compare stocks:** \"Compare AAPL, MSFT and NVDA and explain which looks more promising\"",
        "- **News and filings:** \"What recent news could affect Reliance?\", \"Show Tesla's latest SEC filings\"",
        "- **Why a rating was given:** \"Why did you rate it a potential buy candidate?\"",
        "- **Risks and next steps:** \"What are the main risks and what should I research next?\"",
        "- **Sectors:** \"Find companies in the technology sector and compare them\"",
        "- **The market:** \"What changed in the market today?\"",
        "- **Explanations:** \"How do you score stocks?\", \"What is RSI?\"", "",
        "Everything I say comes from fetched data, search results or the app's rules. I do not place trades or decide for you.",
    ]


def analyze_many(tickers, period_label):
    ok, failed = [], []
    for ticker in tickers:
        result = analyze_stock(ticker, period_label)
        (ok if result["ok"] else failed).append(result)
    return ok, failed


def failure_lines(failed):
    if not failed:
        return []
    return ["I couldn't get data for: " + "; ".join(f"**{md_escape(f['ticker'])}** ({md_escape(f['short_error'])})" for f in failed)
            + ". Check the spelling and exchange suffix (for example RELIANCE.NS)."]


def answer_sector(text, ticker_hint, period_label):
    """(markdown, tickers) for a 'find companies in this sector' question."""
    key, anchor = find_sector(text), None
    if key is None and ticker_hint:
        key = (load_info(ticker_hint[0]) or {}).get("sectorKey")
        anchor = ticker_hint[0]
    if not key or key not in SECTORS:
        names = ", ".join(k.replace("-", " ") for k in SECTORS)
        return [f"Which sector do you mean? I can list companies from these sectors: {names}. You can also name a stock and ask "
                "for companies in its sector."], []
    region = "IN" if re.search(r"\b(?:india|indian|nse|bse|nifty|sensex)\b", text.lower()) else None
    try:
        listed = fetch_sector_companies(key, region)
    except Exception as exc:
        return [f"Yahoo Finance's list for the {key.replace('-', ' ')} sector could not be loaded ({md_escape(describe_error(exc))}), "
                "so I can't list companies. Name the tickers you want compared and I will compare them."], []
    if not listed:
        return [f"Yahoo Finance returned no companies for the {key.replace('-', ' ')} sector, so there is nothing to compare. "
                "Name the tickers you want compared."], []
    symbols = [anchor] if anchor else []
    for symbol, _ in listed:
        if symbol.upper() not in symbols and len(symbols) < 5:
            symbols.append(symbol.upper())
    ok, failed = analyze_many(symbols, period_label)
    lines = [tag("data", f"Yahoo Finance via yfinance · largest companies in the {key.replace('-', ' ')} sector"
                         + (" (India)" if region else "")),
             "This list is ordered by Yahoo's market weight within the sector. It is a starting point for research, not a "
             "recommendation, and it may be mostly US-listed companies."]
    if anchor:
        lines.append(f"{md_escape(anchor)} is included first because you were discussing it.")
    lines.append("")
    if len(ok) >= 2:
        lines += block_compare(rank_results(ok), period_label)
    elif ok:
        lines.append("Only one company could be analysed, so there is nothing to compare.")
    lines += [""] + failure_lines(failed)
    return lines, [r["ticker"] for r in ok]


def answer_question(question, state, period_label, sidebar_tickers=()):
    """(markdown answer, new conversation state). Uses only fetched data, search results and the app's rules."""
    state = dict(state or {})
    text = (question or "").strip()
    period_label = parse_period(text, period_label)
    flags = detect_intents(text)
    topics = [k for k in TOPIC_FLAGS if flags[k]]
    resolved = resolve_message_tickers(text, state.get("tickers", []), list(sidebar_tickers))
    tickers, unresolved, source = resolved["tickers"], resolved["unresolved"], resolved["source"]

    def done(lines, new_tickers=None, new_topics=None):
        new_state = {"tickers": new_tickers if new_tickers is not None else state.get("tickers", []),
                     "flags": new_topics if new_topics is not None else state.get("flags", [])}
        return "\n".join(lines + ["", CHAT_NOTE]), new_state

    if flags["method"]:
        return done(block_method())
    if flags["define"] and source != "message":
        return done(block_define(text))
    if flags["market"] and source != "message" and not unresolved:
        return done(block_market())
    if flags["help"] and not topics and source != "message" and not unresolved and not flags["sector"] and not find_sector(text):
        return done(block_help())
    sector_request = flags["sector"] or bool(
        source != "message" and not unresolved and find_sector(text)
        and re.search(r"\b(?:find|list|show|top|best|biggest|largest|leading|compare)\b", text.lower())
        and not (flags["news"] or flags["filings"] or flags["why"])
    )
    if sector_request:
        lines, new_tickers = answer_sector(text, tickers[:1], period_label)
        return done(lines, new_tickers or None, ["compare"])

    if not topics and re.match(r"\s*(?:and|what about|how about|same for)\b", text, re.I) and state.get("flags"):
        topics = list(state["flags"])  # "What about MSFT?" repeats the previous kind of question
        flags = {**flags, **{k: True for k in topics}}
    if not tickers:
        if unresolved:
            names = ", ".join(f"**{md_escape(n)}**" for n in unresolved)
            return done([f"I couldn't match {names} to a listed company. Could you give me its ticker symbol in capitals (for "
                         "example AAPL, or RELIANCE.NS for India)?"])
        return done([CLARIFY_STOCK])

    notes = []
    if source == "context":
        notes.append(f"*Using the stock(s) from earlier in this chat: {', '.join(tickers)}.*")
    elif source == "sidebar":
        notes.append(f"*Using the list from the sidebar: {', '.join(tickers)}.*")
    if unresolved:
        notes.append("I couldn't match " + ", ".join(f"\"{md_escape(n)}\"" for n in unresolved) + " to a listed company, so I skipped it.")
    if len(tickers) > CHAT_MAX_STOCKS:
        notes.append(f"I analyse at most {CHAT_MAX_STOCKS} stocks per question, so I skipped: {', '.join(tickers[CHAT_MAX_STOCKS:])}.")
        tickers = tickers[:CHAT_MAX_STOCKS]

    ok, failed = analyze_many(tickers, period_label)
    if not ok:
        return done(notes + failure_lines(failed))

    compare_mode = flags["compare"] or (len(ok) >= 2 and not any(flags[k] for k in ("news", "filings", "risks", "why")))
    if flags["compare"] and len(tickers) == 1:
        return done(notes + [f"I have only **{md_escape(ok[0]['ticker'])}** so far. Which other stocks should I compare it with?"],
                    [ok[0]["ticker"]])
    show_compare = compare_mode and len(ok) >= 2
    per_stock = bool(flags["risks"] or flags["news"] or flags["filings"] or not show_compare)
    ranked = rank_results(ok) if show_compare else ok
    compare_web = show_compare and not per_stock and not flags["why"]  # a plain comparison also gets headlines
    wants_web = (per_stock or compare_web) and not flags["why"]
    web = {}
    if wants_web:
        for r in ranked[:WEB_MAX_STOCKS]:  # for a comparison these are the highest-ranked stocks
            web[r["ticker"]] = gather_web(r["ticker"], r["name"], r["info"])

    lines = notes + ([""] if notes else [])
    if flags["why"]:
        wanted = asked_label(text)
        for r in rank_results(ok):
            lines += block_why(r, wanted) + ["", "---", ""]
    else:
        if show_compare:
            lines += block_compare(ranked, period_label)
            if compare_web:
                lines += ["", "**What the web search found for the highest-ranked stocks**", ""]
                for r in ranked[:WEB_MAX_STOCKS]:
                    lines += [stock_title(r)] + block_web(r, web.get(r["ticker"]), compact=True, note=False) + [""]
                if len(ranked) > WEB_MAX_STOCKS:
                    left = ", ".join(r["ticker"] for r in ranked[WEB_MAX_STOCKS:])
                    lines.append(f"Web search covered the {WEB_MAX_STOCKS} highest-ranked stocks only, to stay within free service "
                                 f"limits. Ask about {left} on its own to include its news and filings.")
                lines.append(f"*{WEB_NOTE}*")
            lines += ["", "---", ""]
        if per_stock:
            preface = re.search(r"\b(?:should i|shall i|what should i (?:buy|sell))\b", text.lower()) is not None
            for r in ok:
                w = web.get(r["ticker"])
                if flags["risks"]:
                    lines += block_risks(r, w)
                elif flags["news"] or flags["filings"]:
                    lines += [stock_title(r)] + block_web(r, w, compact=False)
                else:
                    lines += block_overview(r, w, preface)
                lines += ["", "---", ""]
    while lines and lines[-1] in ("", "---"):
        lines.pop()
    lines += [""] + failure_lines(failed)
    return done(lines, [r["ticker"] for r in ok], topics or state.get("flags", []))


# --- The chat page ------------------------------------------------------------------------

def render_chat(period_label, sidebar_tickers):
    st.subheader("Chat assistant")
    if "messages" not in st.session_state:
        st.session_state.messages = []
    if "chat_state" not in st.session_state:
        st.session_state.chat_state = {}
    messages = st.session_state.messages

    def ask(text):
        st.session_state.pending_question = text

    def clear():
        st.session_state.messages = []
        st.session_state.chat_state = {}

    if messages:
        st.button("Clear chat", on_click=clear, key="chat_clear")
    else:
        with st.chat_message("assistant"):
            st.markdown(CHAT_INTRO)
        columns = st.columns(2)
        for i, text in enumerate(CHAT_EXAMPLES):
            columns[i % 2].button(text, on_click=ask, args=(text,), key=f"chat_example_{i}")

    for message in messages:
        with st.chat_message(message["role"]):
            st.markdown(md_escape(message["content"]) if message["role"] == "user" else message["content"])

    typed = st.chat_input("Ask about a stock, compare companies, or ask what changed in the market")
    question = typed or st.session_state.pop("pending_question", None)
    if question:
        question = question.strip()[:1000]
        messages.append({"role": "user", "content": question})
        with st.chat_message("user"):
            st.markdown(md_escape(question))
        with st.chat_message("assistant"):
            with st.spinner("Researching with market data and web search..."):
                try:
                    answer, new_state = answer_question(question, st.session_state.chat_state, period_label, sidebar_tickers)
                except Exception as exc:  # never lose the conversation because of one bad lookup
                    answer = (f"Something went wrong while researching that ({type(exc).__name__}). Please try again, or name a "
                              "ticker symbol directly.")
                    new_state = st.session_state.chat_state
            st.markdown(answer)
        messages.append({"role": "assistant", "content": answer})
        st.session_state.chat_state = new_state
        del messages[:-MAX_CHAT_MESSAGES]


# ----------------------------------------------------------------------------
# The app
# ----------------------------------------------------------------------------

def parse_tickers(raw):
    """Split the input box into (valid tickers, invalid entries, valid tickers over the limit)."""
    valid, invalid, extra = [], [], []
    for entry in re.split(r"[,\s;]+", raw or ""):
        if not entry:
            continue
        ticker = clean_ticker(entry)
        if ticker is None:
            shown = entry[:20].replace("`", "")
            if shown not in invalid:
                invalid.append(shown)
        elif ticker not in valid and ticker not in extra:
            (valid if len(valid) < MAX_TICKERS else extra).append(ticker)
    return valid, invalid, extra


def analyze_stock(ticker, period_label):
    """Load and analyse one stock. Never raises: problems come back as ok=False with a message."""
    try:
        hist = load_history(ticker, PERIODS[period_label])
    except Exception as exc:  # network errors, rate limits, library errors
        return {
            "ok": False, "ticker": ticker,
            "short_error": f"data source error ({type(exc).__name__})",
            "error": (
                f"Could not reach the data source for {ticker} ({type(exc).__name__}). Yahoo "
                "Finance may be rate-limiting this app or temporarily down. Wait a minute and "
                "try again."
            ),
        }

    if hist is None or hist.empty or "Close" not in hist.columns or hist["Close"].dropna().empty:
        return {
            "ok": False, "ticker": ticker,
            "short_error": "no price data came back (check the spelling and exchange suffix)",
            "error": (
                f"No price data came back for **{ticker}**. Check the spelling and the exchange "
                "suffix (for example `RELIANCE.NS`, not `RELIANCE`), or try another symbol. If "
                "symbols that normally work also fail, Yahoo Finance may be rate-limiting this "
                "app, so try again in a few minutes."
            ),
        }

    try:
        hist = hist.dropna(subset=["Close"])
        close = hist["Close"]
        info = load_info(ticker)
        currency = get_currency(ticker, info)
        price = float(close.iloc[-1])
        start = hist.index[0].date().isoformat()
        end = hist.index[-1].date().isoformat()
        stale = (dt.date.today() - hist.index[-1].date()).days > 5

        ind = compute_indicators(close)
        suggestion = score_stock(ind, price, info, len(close), stale)
        if currency == "unknown":
            suggestion["missing"].append("Yahoo did not report a currency, so prices are shown without one.")
        if stale:
            suggestion["missing"].append(f"The latest price is from {end}, more than 5 days old.")
        return {
            "ok": True, "ticker": ticker, "period_label": period_label,
            "name": info.get("longName") or info.get("shortName") or ticker,
            "sector": info.get("sector") or "unavailable",
            "currency": currency, "price": price, "start": start, "end": end,
            "days": len(close), "stale": stale, "hist": hist, "close": close, "info": info,
            "ind": ind,
            "fundamentals": build_fundamentals(info, price, currency),
            "analysis": build_analysis(ind, price, info, start, end, False),
            "suggestion": suggestion,
        }
    except Exception as exc:  # unexpected data shapes for this one symbol
        return {
            "ok": False, "ticker": ticker,
            "short_error": f"unexpected data problem ({type(exc).__name__})",
            "error": (
                f"The data for {ticker} could not be analysed ({type(exc).__name__}). Try another "
                "period or symbol."
            ),
        }


def render_suggestion(r):
    """Research suggestion with its evidence, risks, missing data and confidence."""
    s = r["suggestion"]
    colour = {BUY: "green", HOLD: "orange", AVOID: "red"}[s["label"]]
    st.markdown(
        f"**Research suggestion: :{colour}[{s['label']}]** · score {s['points']} of "
        f"{TOTAL_POINTS} · confidence {s['confidence']}"
    )
    st.write(s["why"])
    st.markdown("**Evidence**")
    st.dataframe(pd.DataFrame(s["rows"]), hide_index=True)
    st.markdown("**Risks**")
    st.markdown("\n".join(f"- {t}" for t in r["analysis"]["risks_specific"]))
    st.markdown("**Missing or limited data**")
    st.markdown("\n".join(f"- {t}" for t in s["missing"]) or "- Every scored criterion had data.")
    st.markdown(f"**Confidence: {s['confidence']}**")
    st.markdown("\n".join(f"- {t}" for t in s["confidence_reasons"]))
    st.caption("Not scored: valuation (P/E), dividends, headlines and sector context. " + RESEARCH_NOTE)


def method_markdown():
    """How scores, suggestions and confidence work, as markdown (used by the dashboard and the chat)."""
    table = md_table(["Criterion", "Max points", "How points are earned"],
                     [[label, m, rule] for _, label, m, rule in CRITERIA])
    return (
        f"**Score.** Each stock is scored out of {TOTAL_POINTS} points on {len(CRITERIA)} criteria, "
        "using only the data fetched for the chosen period. **A criterion with missing data earns "
        "0 points**, the same as the weakest reading, so missing information never helps a stock.\n\n"
        f"{table}\n\n"
        "**Ranking.** Highest score first. Ties go to the stock with data for more criteria, then "
        "to alphabetical ticker order, which has no analytical meaning.\n\n"
        "**Suggestion**\n"
        f"- *{BUY}*: at least 8 of {TOTAL_POINTS} points, the price above its trend average, and "
        "data for at least 5 criteria.\n"
        f"- *{AVOID}*: data for at least 4 criteria, and 35% or less of the points available "
        "from that data. Missing data alone never produces this label.\n"
        f"- *{HOLD}*: everything else, including stocks with data for fewer than 4 criteria.\n\n"
        "**Confidence.** *Low* if the latest price is more than 5 days old, fewer than 4 criteria "
        "have data, or there are fewer than 60 trading days. *High* if at least 6 criteria have "
        "data, there are at least 120 trading days, and the signals clearly agree (70% or more, or "
        "30% or less, of the available points). *Medium* otherwise.\n\n"
        "**Not scored:** valuation (P/E), dividends, headlines and sector or industry context. The "
        "thresholds are rules of thumb, not industry standards, and ranks compare only the stocks "
        "you entered."
    )


def render_method():
    st.markdown(method_markdown())


def render_detail(r, include_suggestion=True):
    """Chart, indicators, fundamentals, headlines and downloads for one stock."""
    ticker, name, currency = r["ticker"], r["name"], r["currency"]
    price, start, end = r["price"], r["start"], r["end"]
    hist, close, info, ind = r["hist"], r["close"], r["info"], r["ind"]
    period_label, fundamentals = r["period_label"], r["fundamentals"]

    headlines = load_news(ticker)
    analysis = build_analysis(ind, price, info, start, end, bool(headlines))

    # --- Header: company, price, caveats -----------------------------------------
    st.subheader(f"{name} ({ticker})")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Latest close", f"{price:,.2f} {currency}")
    c2.metric("As of", end)
    c3.metric(f"Return ({period_label})", fmt_pct(ind["period_return"], True))
    c4.metric("Sector", r["sector"])
    st.caption(
        f"Showing {start} to {end} ({len(close)} trading days), prices in {currency}. "
        "Source: Yahoo Finance via yfinance, which is unofficial and may be delayed."
    )
    if r["stale"]:
        st.warning(
            f"The latest price is from {end}. The data may be stale, or trading may be halted "
            "or the stock delisted."
        )
    if currency == "unknown":
        st.info("Yahoo did not report a currency for this symbol, so prices are shown without one.")

    # --- Chart --------------------------------------------------------------
    chart = pd.DataFrame({"Close": close})
    chart["20-day average"] = close.rolling(20).mean()
    chart["50-day average"] = close.rolling(50).mean()
    chart.index = chart.index.tz_localize(None) if chart.index.tz is not None else chart.index
    st.line_chart(chart.dropna(axis=1, how="all"))

    # --- Tabs ---------------------------------------------------------------
    tab_analysis, tab_ind, tab_fund, tab_news = st.tabs(
        ["Analysis", "Indicators", "Fundamentals", "Headlines"]
    )

    with tab_analysis:
        if include_suggestion:
            render_suggestion(r)
            st.divider()
        st.markdown(f"### Trend evidence summary: {analysis['summary_label']}")
        st.write(analysis["summary_reasoning"])
        if analysis["signals"]:
            st.dataframe(
                pd.DataFrame(
                    [
                        {"Signal": text, "Reading": {1: "Positive", 0: "Neutral", -1: "Negative"}[v]}
                        for text, v in analysis["signals"]
                    ]
                ),
                hide_index=True,
            )
        for title, key in (
            ("What the data shows", "facts"),
            ("Potential positive factors", "positives"),
            ("Risks and uncertainties", "risks"),
            ("What could change the assessment", "changes"),
            ("Questions to investigate yourself", "questions"),
        ):
            st.markdown(f"**{title}**")
            st.markdown("\n".join(f"- {text}" for text in analysis[key]))
        st.caption(RESEARCH_NOTE)

    with tab_ind:
        values = {
            "Period return": fmt_pct(ind["period_return"], True),
            "20-day average": fmt_num(ind["ma20"]),
            "50-day average": fmt_num(ind["ma50"]),
            "RSI (14-day)": fmt_num(ind["rsi14"], 0),
            "Annualized volatility": fmt_pct(ind["volatility"]),
            "Maximum drawdown": fmt_pct(ind["max_drawdown"]),
        }
        for label, value in values.items():
            meaning, limit = INDICATOR_HELP[label]
            st.markdown(f"**{label}: {value}**")
            st.markdown(f"{meaning}  \n*Limitation:* {limit}")

    with tab_fund:
        st.dataframe(pd.DataFrame(fundamentals, columns=["Measure", "Value"]), hide_index=True)
        st.caption("Values come from Yahoo Finance. 'unavailable' means Yahoo did not provide the figure; nothing is estimated.")

    with tab_news:
        if headlines:
            for h in headlines:
                st.markdown(f"- [{h['title']}]({h['url']})  \n  {h['source']}, {h['date']}")
            st.caption("Headlines are shown as provided by the data source and are not analysed or verified here.")
        else:
            st.info("Headlines could not be fetched reliably for this symbol, so none are shown. The rest of the analysis does not depend on them.")

    # --- Downloads ----------------------------------------------------------
    st.divider()
    csv_frame = hist[[c for c in ["Open", "High", "Low", "Close", "Volume"] if c in hist.columns]].copy()
    csv_frame["MA20"] = close.rolling(20).mean()
    csv_frame["MA50"] = close.rolling(50).mean()
    csv_frame["RSI14"] = rsi_series(close)
    csv_frame.index = csv_frame.index.strftime("%Y-%m-%d")
    csv_frame.index.name = "Date"

    report = build_report(r, ind, fundamentals, analysis, headlines)
    d1, d2 = st.columns(2)
    d1.download_button("Download price data (CSV)", csv_frame.to_csv().encode("utf-8"),
                       file_name=f"{ticker}_{period_label.replace(' ', '')}.csv", mime="text/csv",
                       key=f"csv_{ticker}")
    d2.download_button("Download report (text)", report.encode("utf-8"),
                       file_name=f"{ticker}_report.txt", mime="text/plain", key=f"txt_{ticker}")


def render_multi(ok, failed, period_label):
    """Summary, ranking table and per-stock explanations for several stocks."""
    if failed:
        st.warning(
            "These tickers could not be analysed and are left out of the ranking. The others "
            "were analysed normally:\n\n"
            + "\n".join(f"- **{f['ticker']}**: {f['short_error']}" for f in failed)
            + "\n\nCheck the spelling and exchange suffix. If valid symbols fail, Yahoo may be "
            "rate-limiting this app, so try again in a few minutes."
        )
    if not ok:
        st.error("None of the tickers could be analysed, so there is nothing to rank.")
        st.stop()

    ranked = rank_results(ok)
    summary = build_overall_summary(ranked, len(failed))

    st.subheader("Strongest research candidates")
    for line in summary:
        st.markdown(line)
    st.caption(RESEARCH_NOTE)

    st.subheader("Comparison and ranking")
    frame = build_comparison_frame(ranked)
    st.dataframe(frame, hide_index=True)
    st.caption(
        f"Ranked by score out of {TOTAL_POINTS}. Missing data earns 0 points. Prices are in each "
        "stock's own currency, so compare the scores and percentages, not the prices."
    )
    with st.expander("How the score, suggestion and confidence are worked out"):
        render_method()

    d1, d2 = st.columns(2)
    d1.download_button("Download comparison (CSV)", frame.to_csv(index=False).encode("utf-8"),
                       file_name=f"comparison_{period_label.replace(' ', '')}.csv", mime="text/csv",
                       key="cmp_csv")
    d2.download_button("Download all reports (text)",
                       build_multi_report(period_label, ranked, failed, summary).encode("utf-8"),
                       file_name="multi_stock_report.txt", mime="text/plain", key="cmp_txt")

    st.subheader("Suggestion for each stock")
    for r in ranked:
        s = r["suggestion"]
        title = f"#{r['rank']} {r['ticker']} · {r['name']} · {s['label']} · {s['confidence']} confidence"
        with st.expander(title):
            render_suggestion(r)

    st.subheader("Chart, indicators, fundamentals and headlines")
    names = {r["ticker"]: r["name"] for r in ranked}
    choice = st.selectbox(
        "Show full detail for", [r["ticker"] for r in ranked],
        format_func=lambda t: f"{t} ({names[t]})",
    )
    render_detail(next(r for r in ranked if r["ticker"] == choice), include_suggestion=False)


def main():
    st.set_page_config(page_title="Stock Research Assistant", page_icon="📈", layout="wide")

    st.title("Stock Research Assistant")
    st.warning(DISCLAIMER)

    # --- Sidebar inputs -----------------------------------------------------
    if "tickers" not in st.session_state:
        st.session_state.tickers = "AAPL"

    def pick_example(text):
        st.session_state.tickers = text

    with st.sidebar:
        mode = st.radio("Mode", ["Chat assistant", "Research dashboard"], key="mode")
        st.header("Choose stocks")
        st.text_area(
            f"Ticker symbols (1 to {MAX_TICKERS})",
            key="tickers",
            height=110,
            help="Separate symbols with commas, spaces or new lines. Press Ctrl+Enter or click "
                 "outside the box to load. Add an exchange suffix for non-US stocks, such as "
                 "RELIANCE.NS. Several symbols give a ranked comparison. In chat mode this list "
                 "is what \"these stocks\" refers to, and the time period applies to the chat.",
        )
        st.caption("Examples")
        for symbol in EXAMPLES:
            st.button(symbol, on_click=pick_example, args=(symbol,), key=f"ex_{symbol}")
        st.button("10-stock sample", on_click=pick_example, args=(SAMPLE_TEN,), key="ex_ten")
        period_label = st.selectbox("Time period", list(PERIODS), index=2)
        with st.expander("Exchange suffixes"):
            st.markdown(
                "- US stocks: no suffix (`AAPL`)\n"
                "- NSE India: `.NS` (`RELIANCE.NS`)\n"
                "- BSE India: `.BO`\n"
                "- London: `.L`\n"
                "- Toronto: `.TO`\n"
                "- Hong Kong: `.HK`\n"
                "- Tokyo: `.T`\n"
                "- Australia: `.AX`\n"
                "- Germany (Xetra): `.DE`"
            )
        st.caption(
            "Ticker lookups are sent to Yahoo Finance through yfinance. This app does not "
            "store them, though the hosting service may keep standard access logs."
        )

    valid, invalid, extra = parse_tickers(st.session_state.tickers)
    if mode == "Chat assistant":
        render_chat(period_label, valid)
        st.caption(DISCLAIMER)
        return
    if invalid:
        st.warning(
            "Skipped entries that do not look like ticker symbols: "
            + ", ".join(f"`{e}`" for e in invalid[:10])
            + ". Use letters and numbers, plus an optional suffix such as `.NS`."
        )
    if extra:
        st.warning(f"Only the first {MAX_TICKERS} tickers are analysed. Skipped: {', '.join(extra)}.")
    if not valid:
        st.error("Enter at least one valid ticker, for example `AAPL` or `RELIANCE.NS`.")
        st.stop()

    # --- Load and analyse each stock separately, so one failure never stops the rest ----
    results = []
    progress = st.progress(0.0, text="Loading data...")
    for i, ticker in enumerate(valid, start=1):
        progress.progress((i - 1) / len(valid), text=f"Loading {ticker} ({i} of {len(valid)})...")
        results.append(analyze_stock(ticker, period_label))
    progress.empty()

    ok = [r for r in results if r["ok"]]
    failed = [r for r in results if not r["ok"]]

    if len(valid) == 1:
        if failed:
            st.error(failed[0]["error"])
            st.stop()
        render_detail(ok[0], include_suggestion=True)
    else:
        render_multi(ok, failed, period_label)

    st.caption(DISCLAIMER)


if __name__ == "__main__":
    main()
