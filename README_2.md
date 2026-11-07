# Stock Research Assistant

A Streamlit app that helps you research stocks. Chat with it in plain language, or use the dashboard to look at one stock or rank up to 20 at once. It combines market data from Yahoo Finance with a free web search for news and company filings, and it explains every suggestion with its evidence, risks, missing data and confidence level.

> **Disclaimer.** This is an educational research tool, not financial advice. Market data comes from Yahoo Finance through the unofficial `yfinance` library and may be delayed, incomplete or incorrect. Web results come from third-party services and are not verified by this app. Indicators describe the past and do not predict the future. Suggestions and rankings are research aids, not guarantees, predictions or personalised advice. The app does not place trades, and every decision is yours. Verify information with authoritative sources before making any decision.

## What it shows

**Chat assistant (opens first)**
- Answers in plain language, remembers the conversation, and handles follow-ups such as "Why did you rate it that way?" or "What about MSFT?"
- Market data, live web findings and the app's own analysis, each under its own label in every answer
- News, SEC filings and other links with the publisher and date, and primary sources listed first
- Comparisons, risks and next steps, sector peers, and a market summary for the latest session

**Dashboard (switch with **Mode** in the sidebar)**
- Company name, ticker, sector, latest close, the date of that close and the currency
- A price chart with 20-day and 50-day averages (periods: 1 month, 6 months, 1 year, 5 years)
- Period return, 20-day and 50-day averages, 14-day RSI, annualized volatility and maximum drawdown, each with a plain-language explanation and its limits
- Market cap, P/E, revenue, revenue growth, profit margin, dividend yield and 52-week range. Anything Yahoo does not provide is shown as "unavailable"
- Recent headlines with source and date, when the data source returns them
- A research suggestion with its evidence, risks, missing data and confidence level, plus a rule-based analysis
- For several stocks (up to 20): a summary of the strongest research candidates, a ranked comparison table and a card for each stock
- Downloads: price data with indicators (CSV), the comparison table (CSV), and text reports

The app never places trades, connects to a broker, needs an API key or stores what you type.

## Chat with the assistant

Open the app and ask a question in the box at the bottom. The **Mode** switch in the sidebar moves between the chat and the dashboard. **Clear chat** starts a new conversation, and the history is kept for the current browser session only.

**Example questions**
- "Tell me about AAPL"
- "Compare AAPL, MSFT and NVDA and explain which looks more promising"
- "What recent news could affect Reliance?"
- "Why did you rate it a potential buy candidate?"
- "What are the main risks and what should I research next?"
- "Show Tesla's latest SEC filings"
- "Find companies in the technology sector and compare them"
- "What changed in the market today?"
- "How is AAPL doing over the last 6 months?"
- "How do you score stocks?" and "What is RSI?"

**How it understands you.** The assistant is rule-based. It is not a generative AI model, and it needs no AI service or key. It recognises the question types above, and it says so and asks you to rephrase when it does not understand.
- *Which stock.* Use a ticker in capitals (`AAPL`), a ticker with an exchange suffix (`RELIANCE.NS`, any case), or a company name. A short list of well-known names is built in (Apple, Microsoft, Alphabet, Amazon, Nvidia, Tesla, Reliance, TCS, Infosys and others). Any other capitalised name is looked up on Yahoo Finance and accepted only when the search clearly finds a listed company with that name. Lowercase words are never guessed as tickers.
- *Follow-ups.* If your question names no stock, it uses the stock or stocks from earlier in the chat and tells you so. "Compare it with MSFT" keeps the earlier stock. "These stocks" or "my list" uses the sidebar list when it has two or more tickers and there is no earlier context.
- *Time period.* The sidebar period applies. Say "over the last 6 months" (or 1 month, 1 year, 5 years) to override it for one question.
- *Unclear questions.* If it cannot tell which stock you mean, or cannot match a company name, it asks one short question instead of guessing.
- *Limits per question.* It analyses at most 6 stocks per question and runs the web search for the first 3 (for a comparison, the 3 highest-ranked), to stay within free service limits. It names any stocks it skipped.

**What every answer contains.** Answers keep three kinds of information apart, each with its own label:
- **MARKET DATA**: prices, returns and fundamentals from Yahoo Finance, with the date of the latest close and the currency
- **LIVE WEB FINDINGS**: news and filings from the web search, with links, publishers, dates and the time of the search
- **APP ANALYSIS**: this app's rule-based reading of the data above (scores, suggestions, risks), which is not a forecast

Suggestions use the same three labels as the dashboard (*Potential buy candidate*, *Hold/watch*, *Avoid for now*) and come with the evidence, risks, missing or stale data and confidence level. When you ask "should I buy...?", it says it cannot decide for you and gives the research view instead. If your question assumes a label the data does not currently give ("Why is it a buy candidate?" for a stock rated Hold/watch), it says so. Every answer ends with a reminder that it is research, not personalised advice, and that no trades are placed.

## Web search

The chat searches the web for news, company announcements and filings. **It needs no API key, no account, no paid plan and no extra Python package.**

| Source | What it provides | Notes |
| --- | --- | --- |
| SEC EDGAR | Recent 10-K, 10-Q, 8-K, 20-F, 6-K, 40-F and proxy filings with filing dates and links, for companies that file in the US | Primary source. 8-K filings show what they cover (for example "results of operations") |
| Google News search feed (RSS) | Recent headlines and company press releases with publisher, date and link | Unofficial public feed. Links open the publisher's article |
| Yahoo Finance headlines | Extra headlines through the same `yfinance` library the app already uses | Used alongside the news search, and on its own if the news search fails |

- **Primary sources come first.** SEC filings are listed before headlines. In the headlines, items from regulators, exchanges and government sites (judged by the site address) are marked *primary source*, and press releases from the main release wires are marked *company-issued release*. If none of the headlines is a primary source, the answer says so. The company website from Yahoo Finance is shown so you can find its investor relations page.
- **Non-US companies.** SEC EDGAR covers US filers only. For symbols such as `RELIANCE.NS` the answer says so and points to the exchange's website (for example NSE or BSE in India), where you can find announcements yourself.
- **Nothing is invented.** Links, publishers and dates are shown exactly as the sources return them. A missing date shows "date not provided", and items older than 30 days are flagged.
- **Nothing is verified.** The app does not open the articles or filings. Each answer reminds you that the links come from third-party search results and should be checked at the source.
- **Failures are reported.** If a source is blocked, rate-limited or down, the answer names the source and the reason, and continues with the others. When no source returns anything, it says that no news is shown instead of filling the gap. The header of each web section lists only the sources that actually returned results.

### Optional setting for the SEC

The SEC asks automated programs to identify themselves. The app sends a generic name, `StockResearchAssistant/1.0`, and no personal details. If the SEC ever refuses requests (the answer will say "refused the request (HTTP 403)"), set `SEC_USER_AGENT` to your own name and contact, for example `Your Name your.email@example.com`. The app uses it only for requests to the SEC.

- **On your computer (macOS / Linux):** `export SEC_USER_AGENT="Your Name your.email@example.com"` before `streamlit run app.py`
- **On your computer (Windows PowerShell):** `$env:SEC_USER_AGENT="Your Name your.email@example.com"`
- **On Streamlit Community Cloud:** add `SEC_USER_AGENT = "Your Name your.email@example.com"` in the app's secrets settings. Streamlit also makes root-level secrets available as environment variables.

## Analyze several stocks at once (dashboard)

1. Set **Mode** to **Research dashboard**.
2. In the sidebar, type or paste up to 20 ticker symbols into the **Ticker symbols** box. Separate them with commas, spaces or new lines, for example `AAPL, MSFT, RELIANCE.NS, TCS.NS`.
3. Press Ctrl+Enter or click outside the box to load. The **10-stock sample** button fills the box with ten example tickers.
4. Pick a time period.
5. Read the summary, then the ranking table. Open the "How the score, suggestion and confidence are worked out" section for the rules, and open any stock's card for its evidence, risks, missing data and confidence.
6. Use **Show full detail for** to see the chart, indicators, fundamentals and headlines for one stock.

Each stock is loaded separately. An invalid ticker, a data error or a stock with no data is reported by name and left out of the ranking, and every other stock is analysed normally. Entries that do not look like ticker symbols are skipped with a notice. If you enter more than 20, the first 20 are used. Entering a single ticker shows the detailed view for that stock, with its suggestion at the top of the Analysis tab. The chat uses the same scoring, so its comparisons match the dashboard.

## How suggestions and rankings are generated

Everything below uses only data fetched for the chosen period. Nothing is estimated, and nothing is filled in when data is missing. The web search is not part of the score.

**Score.** Each stock is scored out of 11 points on 7 criteria:

| Criterion | Max points | How points are earned |
| --- | --- | --- |
| Return over the period | 1 | 1 if the closing price rose over the period |
| Price vs trend average | 1 | 1 if the latest close is above its 50-day average (20-day average when there are fewer than 50 trading days) |
| 20-day vs 50-day average | 1 | 1 if the 20-day average is above the 50-day average (needs 50 trading days) |
| Annualized volatility | 2 | 2 below 25%, 1 from 25% to 40%, 0 at 40% or more |
| Maximum drawdown | 2 | 2 if the deepest fall from a peak was under 15%, 1 if under 30%, otherwise 0 |
| Profit margin | 2 | 2 above 10%, 1 above 0%, 0 at or below 0% |
| Revenue growth (year on year) | 2 | 2 above 10%, 1 above 0%, 0 at or below 0% |

**Missing data is never a positive signal.** A criterion with no data earns 0 points, the same as the weakest possible reading, so a stock can never gain score or rank from information that is missing. Each stock shows how many of the 7 criteria had data.

**Ranking.** Highest score first. Ties go to the stock with data for more criteria, then to alphabetical ticker order, which has no analytical meaning. Ranks compare only the stocks you entered, so a high rank means stronger evidence than the others in the list, not a good investment.

**Research suggestion.**

| Suggestion | Rule |
| --- | --- |
| Potential buy candidate | At least 8 of 11 points, the price above its trend average, and data for at least 5 criteria |
| Avoid for now | Data for at least 4 criteria, and 35% or less of the points available from that data. Missing data alone never produces this label |
| Hold/watch | Everything else, including stocks with data for fewer than 4 criteria |

Stocks without fundamentals can score at most 7 points, so they cannot be a buy candidate however strong their price trend.

**Confidence.**
- *Low*: the latest price is more than 5 days old, fewer than 4 criteria have data, or there are fewer than 60 trading days (the 1 month period)
- *High*: at least 6 criteria have data, there are at least 120 trading days, and the signals clearly agree (70% or more, or 30% or less, of the available points)
- *Medium*: everything else

**Explanations.** Every suggestion lists the evidence for each criterion with the points it earned, the data-specific risks, the missing or limited data (including anything counted as 0 points, a missing currency or a stale price), and the reasons for its confidence level.

**Not scored.** Valuation (P/E), dividends, news, filings and sector or industry context. They are shown where available but do not change the score. The thresholds above are rules of thumb, not industry standards.

The suggestion labels describe how the data fits these rules. They do not say what you personally should buy, and the app does not place trades.

## Run it on your computer

You need Python 3.9 or newer.

```bash
cd stock-research-agent
python -m venv .venv

# macOS / Linux
source .venv/bin/activate
# Windows (PowerShell)
# .venv\Scripts\Activate.ps1

pip install -r requirements.txt
streamlit run app.py
```

Streamlit prints a local address (usually http://localhost:8501) and opens it in your browser. The chat and web search add no new packages, so `requirements.txt` is unchanged, and you need an internet connection for market data and web search.

## Get a link you can share (free)

Streamlit Community Cloud hosts Streamlit apps from a GitHub repository and gives each app a public web address.

1. Create a free account at [github.com](https://github.com) and make a new **public** repository, for example `stock-research-agent`.
2. In the repository, choose **Add file > Upload files** and upload `app.py`, `requirements.txt` and `README.md`. Commit them to the `main` branch.
3. Go to [share.streamlit.io](https://share.streamlit.io) and sign in with GitHub. Allow access to your repository when asked.
4. Click **Create app** and choose your repository. Set the branch to `main` and the main file path to `app.py`. You can pick your own app address if the form offers it.
5. Click **Deploy**. The first start takes a few minutes while the libraries install.
6. Copy the address of the running app (it ends in `.streamlit.app`) and send it to anyone.

The button names can change slightly over time. Streamlit's own guide is at [docs.streamlit.io](https://docs.streamlit.io/deploy/streamlit-community-cloud). Free apps may go to sleep when nobody has used them for a while and wake up when someone opens the link, so the first visit can be slow.

To change the app later, edit `app.py` on GitHub and commit. The deployed app updates itself. If you already deployed an earlier version, replace `app.py` and `README.md` in your repository with the new files. Everyone who opens the link shares the same free server, so heavy use can trigger rate limits from Yahoo, Google or the SEC.

## Limits and troubleshooting

- **The assistant is rule-based.** It can misread an unusual question. Rephrase it, use a ticker symbol, or type "help" to see the supported questions. It does not know why a price moved and does not read the articles or filings it links to.
- **Data source.** `yfinance` is not an official Yahoo product. Fields can be missing, change names or stop working without notice.
- **Web search sources.** The Google News feed is public but unofficial, and its format, limits and availability can change. Headlines are keyword matches, so some may be off topic, duplicated across publishers or out of date. The SEC may refuse the default identification (see the optional setting above). If web results do not appear, the answer names the source and the reason. Wait a few minutes and ask again, or set `SEC_USER_AGENT` if the SEC refuses.
- **Company names.** Name lookup relies on Yahoo's search. If a name is not matched, give the ticker symbol, with an exchange suffix for non-US stocks.
- **Sector lists.** "Find companies in the X sector" uses Yahoo Finance's list of the largest companies in a sector (at most 5 are compared). It is a starting point, not a screen of the whole market, and it can lean towards US-listed companies. If Yahoo does not return a list, the assistant says so and asks you to name tickers.
- **Market summary.** It shows the latest session available for each index, with its date, because markets differ by time zone and holidays. It does not explain why the market moved.
- **Many tickers.** Each stock needs its own requests to Yahoo, so a first load of 20 stocks takes longer than one, and is more likely to be rate limited. Results are cached for 15 minutes (company details for an hour). Stocks that fail are listed by name and the rest still load.
- **Rate limiting.** Yahoo may block requests from shared cloud servers, which can make a hosted copy fail for a few minutes. If you see a data error, wait a few minutes and try again. If it keeps failing, update the library by changing the `yfinance` line in `requirements.txt`.
- **Short periods.** The 50-day average needs at least 50 trading days, so it is unavailable for the 1 month period. RSI needs at least 15 prices. Short periods also lower the confidence level.
- **Prices.** Closing prices are adjusted for stock splits but not for dividends, so returns exclude dividends. Prices in comparison tables are in each stock's own currency, so compare scores and percentages, not prices.
- **Fundamentals.** Funds, indexes and some foreign listings often have no profit margin or revenue growth, which limits their score and confidence.
- **Analysis.** The score, suggestion and trend evidence summary describe past data and simple rules. They do not judge business quality or valuation in depth and they are not recommendations.
- **Chat history.** It lasts for the current browser session (the last 40 messages) and is gone after a refresh or when you press **Clear chat**.
- **Privacy.** Ticker symbols and company names you ask about are sent to Yahoo Finance, to Google News and, for US companies, to the SEC. The app does not store your questions, but the hosting service may keep standard access logs.
