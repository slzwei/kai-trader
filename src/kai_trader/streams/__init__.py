"""WebSocket stream workers.

Push-based replacements for REST polling where it makes sense. Phase 5c
ships the TradingStream worker for real-time fill / cancel / expire
events. Other streams (account equity for the drawdown breaker, per
position option-quote streams for roll triggers) are explicitly out of
scope and remain on REST polling.
"""
