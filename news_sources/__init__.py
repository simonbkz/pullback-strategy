"""Free news-event calendar sources for fx-edge.

Provides scheduled event windows (NFP, FOMC, CPI, ECB, BoE, BOJ, etc.) in the
format expected by live_trader._in_news_blackout, without paid API subscriptions.

Sources:
  - FRED (St. Louis Fed) API for US macro release schedules
  - Hardcoded central bank meeting dates for 2026-2027 (compiled from official sources)
"""
