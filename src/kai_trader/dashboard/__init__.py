"""Read-only web dashboard (Phase D1).

A separate Render free-tier web service that renders account stats,
the latest position book, recent orders, and AI decisions from
Postgres alone. It authenticates as the ``kai_chat_ro`` read-only role
(``DATABASE_URL_RO``) and holds no broker keys, no Telegram token, and
no write-capable database credentials, so nothing reachable from this
service can place, modify, or close a trade. Access requires the
``DASHBOARD_TOKEN`` secret; with the token unset the app serves only
its health check and a setup notice, never data.
"""
