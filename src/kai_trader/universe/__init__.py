"""Weekly universe review (Phase U1): machine-proposed, human-ratified.

The wheel's risk surface is WHICH names it may be assigned; this
package keeps that surface fresh without ever letting the bot rewrite
it unilaterally. A weekly run screens a curated candidate pool plus the
current whitelists deterministically, asks the AI underwriter which
screened names deserve a place (and which incumbents have
deteriorated), applies mechanical guardrails, and files any change as
an ordinary ``pending_changes`` watchlist_edit proposal. Nothing
changes until the owner taps Approve, on Telegram or the dashboard;
retired names keep being managed to close because the strategy only
consults the whitelist for NEW entries.
"""

from kai_trader.universe.review import run_universe_review as run_universe_review
