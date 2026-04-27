"""Tool surface for Kai's chat handler.

The Anthropic API drives a tool loop: Claude returns ``tool_use`` blocks,
this module routes each to a handler, and the result is sent back as a
``tool_result`` block. Every tool here is read-only with one deliberate
exception: ``propose_change`` writes to ``pending_changes``, which is the
only path Kai has to mutate state (and even that change does not take
effect until Shawn taps Approve).

All tool handlers are async, return JSON-serialisable Python values, and
fail with a string error message rather than raising. A failure inside a
tool should not take down the whole turn.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

from kai_trader.broker.alpaca import get_account, list_positions
from kai_trader.broker.market_data import get_latest_quote, get_latest_trade
from kai_trader.broker.options_data import get_chain
from kai_trader.db import events as events_db
from kai_trader.db import pending_changes as pending_changes_db
from kai_trader.db.decision_log import recent_decisions
from kai_trader.db.readonly import (
    ReadOnlyConfigError,
    ReadOnlyQueryError,
    run_readonly_select,
)
from kai_trader.db.sleeve_config import get_all_sleeves
from kai_trader.logging import get_logger

_log = get_logger(__name__)

# Repo root is computed once at import. Tests can override via
# REPO_ROOT_OVERRIDE for a tmp_path-based sandbox.
_REPO_ROOT = Path(__file__).resolve().parents[3]


def _repo_root() -> Path:
    return _REPO_ROOT


_DEFAULT_GREP_INCLUDES = ("*.py", "*.sql", "*.md", "*.toml", "*.yaml", "*.yml")
_GREP_OUTPUT_CAP = 8000  # bytes
_READ_FILE_CAP = 200_000  # bytes


def _safe_path(rel: str) -> Path:
    """Resolve ``rel`` against the repo root and reject escapes."""
    if not rel:
        raise ValueError("path must not be empty")
    candidate = (_repo_root() / rel).resolve()
    if not str(candidate).startswith(str(_repo_root())):
        raise ValueError("path resolves outside the repository")
    return candidate


# ---------- repo tools ----------


async def _read_file(path: str) -> dict[str, Any]:
    target = _safe_path(path)
    if not target.exists():
        return {"error": f"not found: {path}"}
    if target.is_dir():
        return {"error": f"is a directory: {path}"}
    size = target.stat().st_size
    if size > _READ_FILE_CAP:
        return {
            "error": (
                f"file is {size} bytes; limit is {_READ_FILE_CAP}. "
                "Use grep_repo to narrow the search instead."
            )
        }
    text = target.read_text(encoding="utf-8", errors="replace")
    return {"path": path, "size_bytes": size, "content": text}


async def _list_dir(path: str) -> dict[str, Any]:
    target = _safe_path(path or ".")
    if not target.exists():
        return {"error": f"not found: {path}"}
    if not target.is_dir():
        return {"error": f"not a directory: {path}"}
    entries: list[dict[str, str]] = []
    for child in sorted(target.iterdir()):
        entries.append(
            {
                "name": child.name,
                "type": "dir" if child.is_dir() else "file",
            }
        )
    return {"path": path or ".", "entries": entries}


async def _grep_repo(
    pattern: str,
    path_glob: str | None = None,
) -> dict[str, Any]:
    if not pattern:
        return {"error": "pattern must not be empty"}
    cmd = [
        "grep",
        "-rIn",
        "--exclude-dir=.git",
        "--exclude-dir=.venv",
        "--exclude-dir=__pycache__",
        "--exclude-dir=node_modules",
    ]
    if path_glob:
        # path_glob restricts to a directory; combine with the default
        # extension whitelist via --include filters.
        target = _safe_path(path_glob)
        for include in _DEFAULT_GREP_INCLUDES:
            cmd.append(f"--include={include}")
        cmd.extend(["--", pattern, str(target)])
    else:
        for include in _DEFAULT_GREP_INCLUDES:
            cmd.append(f"--include={include}")
        cmd.extend(["--", pattern, str(_repo_root())])

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, _stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return {"error": "grep timed out"}

    text = stdout.decode("utf-8", errors="replace")
    truncated = False
    if len(text) > _GREP_OUTPUT_CAP:
        text = text[:_GREP_OUTPUT_CAP]
        truncated = True
    matches = [_strip_repo_root(line) for line in text.splitlines() if line]
    return {"matches": matches, "truncated": truncated}


def _strip_repo_root(line: str) -> str:
    root = str(_repo_root())
    if line.startswith(root + "/"):
        return line[len(root) + 1 :]
    return line


# ---------- supabase tools ----------


def _to_jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _to_jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_to_jsonable(v) for v in value]
    return value


async def _query_supabase(sql: str, max_rows: int = 200) -> dict[str, Any]:
    try:
        rows = await run_readonly_select(sql, max_rows=max_rows)
    except ReadOnlyConfigError as exc:
        return {"error": f"chat read-only DB is not configured: {exc}"}
    except ReadOnlyQueryError as exc:
        return {"error": f"query rejected: {exc}"}
    except Exception as exc:
        _log.error("chat.query_supabase.failed", error=str(exc))
        return {"error": f"query failed: {type(exc).__name__}: {exc}"}
    return {"rows": [_to_jsonable(r) for r in rows], "row_count": len(rows)}


# ---------- alpaca tools ----------


_ALPACA_ENDPOINTS = {
    "account",
    "positions",
    "latest_quote",
    "latest_trade",
    "options_chain",
}


async def _alpaca_read(endpoint: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    params = params or {}
    if endpoint not in _ALPACA_ENDPOINTS:
        return {"error": f"unknown endpoint: {endpoint}"}
    try:
        if endpoint == "account":
            snap = await get_account()
            return {
                "equity": str(snap.equity),
                "cash": str(snap.cash),
                "buying_power": str(snap.buying_power),
                "portfolio_value": str(snap.portfolio_value),
                "day_pl": str(snap.day_pl),
                "status": snap.status,
                "paper": snap.paper,
            }
        if endpoint == "positions":
            positions = await list_positions()
            return {
                "positions": [
                    {
                        "symbol": p.symbol,
                        "qty": str(p.qty),
                        "avg_entry_price": str(p.avg_entry_price)
                        if p.avg_entry_price is not None
                        else None,
                        "current_price": str(p.current_price)
                        if p.current_price is not None
                        else None,
                        "unrealized_pl": str(p.unrealized_pl)
                        if p.unrealized_pl is not None
                        else None,
                        "side": p.side,
                    }
                    for p in positions
                ],
            }
        if endpoint == "latest_quote":
            symbol = str(params.get("symbol", "")).upper()
            if not symbol:
                return {"error": "symbol param required"}
            quote = await get_latest_quote(symbol)
            return {
                "symbol": symbol,
                "bid": str(quote.bid_price),
                "ask": str(quote.ask_price),
                "mid": str(quote.mid),
                "spread": str(quote.spread),
                "timestamp": quote.timestamp.isoformat(),
            }
        if endpoint == "latest_trade":
            symbol = str(params.get("symbol", "")).upper()
            if not symbol:
                return {"error": "symbol param required"}
            trade = await get_latest_trade(symbol)
            return {
                "symbol": symbol,
                "price": str(trade.price),
                "size": str(trade.size),
                "timestamp": trade.timestamp.isoformat(),
            }
        # options_chain
        symbol = str(params.get("symbol", "")).upper()
        if not symbol:
            return {"error": "symbol param required"}
        expiration_param = params.get("expiration")
        expiration: date | None = None
        if expiration_param:
            try:
                expiration = date.fromisoformat(str(expiration_param))
            except ValueError:
                return {"error": "expiration must be YYYY-MM-DD"}
        contracts = await get_chain(symbol, expiration)
        # Hard-cap to 80 contracts so a 12k-row chain does not blow up the turn.
        head = contracts[:80]
        return {
            "symbol": symbol,
            "total_contracts": len(contracts),
            "returned": len(head),
            "contracts": [
                {
                    "symbol": c.symbol,
                    "type": c.option_type,
                    "strike": str(c.strike),
                    "expiration": c.expiration.isoformat(),
                    "bid": str(c.bid) if c.bid is not None else None,
                    "ask": str(c.ask) if c.ask is not None else None,
                    "delta": str(c.delta) if c.delta is not None else None,
                    "iv": str(c.implied_volatility)
                    if c.implied_volatility is not None
                    else None,
                }
                for c in head
            ],
        }
    except Exception as exc:
        _log.error("chat.alpaca_read.failed", endpoint=endpoint, error=str(exc))
        return {"error": f"{type(exc).__name__}: {exc}"}


# ---------- recent decisions / git log / sleeves ----------


async def _recent_decisions(n: int = 20) -> dict[str, Any]:
    try:
        rows = await recent_decisions(limit=max(1, min(n, 100)))
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
    return {
        "decisions": [
            {
                "id": r.id,
                "kind": r.kind,
                "inputs": _to_jsonable(r.inputs),
                "outputs": _to_jsonable(r.outputs),
                "reason": r.reason,
                "created_at": r.created_at.isoformat(),
            }
            for r in rows
        ]
    }


async def _git_log(n: int = 10) -> dict[str, Any]:
    n = max(1, min(n, 50))
    cmd = [
        "git",
        "-C",
        str(_repo_root()),
        "log",
        "--pretty=format:%h %ad %s",
        "--date=short",
        f"-n{n}",
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return {"error": "git log timed out"}
    if proc.returncode != 0:
        msg = stderr.decode("utf-8", errors="replace").strip()
        return {"error": f"git unavailable: {msg or 'no .git directory in this deployment'}"}
    text = stdout.decode("utf-8", errors="replace")
    return {"commits": [line for line in text.splitlines() if line]}


# ---------- propose_change ----------


_VALID_KINDS = {"order", "strategy_param", "watchlist_edit"}


async def _current_state_for(kind: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    """Look up the current state that the proposed payload would replace."""
    if kind == "strategy_param":
        sleeve = payload.get("sleeve")
        if not isinstance(sleeve, str):
            return None
        sleeves = await get_all_sleeves()
        for s in sleeves:
            if s.sleeve == sleeve:
                return {
                    "sleeve": s.sleeve,
                    "target_pct": str(s.target_pct),
                    "target_delta_put_risk_on": str(s.target_delta_put_risk_on),
                    "target_delta_put_neutral": str(s.target_delta_put_neutral),
                    "target_delta_call": str(s.target_delta_call),
                    "target_dte_min": s.target_dte_min,
                    "target_dte_max": s.target_dte_max,
                    "profit_take_pct": str(s.profit_take_pct),
                    "roll_trigger_delta": str(s.roll_trigger_delta),
                    "symbol_whitelist": s.symbol_whitelist,
                    "enabled": s.enabled,
                }
        return None
    if kind == "watchlist_edit":
        sleeve = payload.get("sleeve")
        if not isinstance(sleeve, str):
            return None
        sleeves = await get_all_sleeves()
        for s in sleeves:
            if s.sleeve == sleeve:
                return {"sleeve": s.sleeve, "symbol_whitelist": s.symbol_whitelist}
        return None
    return None


async def _propose_change(
    kind: str,
    payload: dict[str, Any],
    reason: str,
    proposed_by: int,
) -> dict[str, Any]:
    if kind not in _VALID_KINDS:
        return {"error": f"kind must be one of {sorted(_VALID_KINDS)}"}
    if not isinstance(payload, dict):
        return {"error": "payload must be an object"}
    if not isinstance(reason, str) or not reason.strip():
        return {"error": "reason must be a non-empty string"}
    try:
        current_state = await _current_state_for(kind, payload)
        pending_id = await pending_changes_db.propose(
            kind=cast(pending_changes_db.PendingKind, kind),
            payload=payload,
            current_state=current_state,
            reason=reason,
            proposed_by=proposed_by,
        )
        await events_db.enqueue_event(
            "pending_change_created",
            {"pending_id": pending_id},
        )
    except Exception as exc:
        _log.error("chat.propose_change.failed", kind=kind, error=str(exc))
        return {"error": f"{type(exc).__name__}: {exc}"}
    return {
        "pending_id": pending_id,
        "status": "pending",
        "note": (
            "Proposal queued. Shawn must tap Approve in Telegram before "
            "anything is applied."
        ),
    }


# ---------- public surface: tool definitions + dispatch ----------


TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "read_file",
        "description": "Read a file from the kai-trader repository.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Repo-relative path, e.g. 'src/kai_trader/strategy/worker.py'.",
                }
            },
            "required": ["path"],
        },
    },
    {
        "name": "list_dir",
        "description": "List entries in a repository directory.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Repo-relative directory; '.' for the root.",
                }
            },
            "required": ["path"],
        },
    },
    {
        "name": "grep_repo",
        "description": (
            "Search the kai-trader repository for a regex. Restricted to "
            "code, SQL, markdown, TOML, YAML files."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "path_glob": {
                    "type": "string",
                    "description": "Optional repo-relative directory to scope the search.",
                },
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "query_supabase",
        "description": (
            "Run a single SELECT or WITH statement against the read-only "
            "Postgres role. Multi-statement, INSERT, UPDATE, DELETE, and "
            "DDL are rejected. Rows are capped at 200."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sql": {"type": "string"},
                "max_rows": {
                    "type": "integer",
                    "description": "Optional row cap, max 200.",
                },
            },
            "required": ["sql"],
        },
    },
    {
        "name": "alpaca_read",
        "description": (
            "Read live data from Alpaca: account snapshot, open positions, "
            "latest quote, latest trade, options chain. No write methods."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "endpoint": {
                    "type": "string",
                    "enum": sorted(_ALPACA_ENDPOINTS),
                },
                "params": {"type": "object"},
            },
            "required": ["endpoint"],
        },
    },
    {
        "name": "recent_decisions",
        "description": (
            "Read the most recent rows from decision_log. Each row records "
            "an approved-and-applied change with the inputs that drove it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "n": {"type": "integer", "minimum": 1, "maximum": 100},
            },
        },
    },
    {
        "name": "git_log",
        "description": "Recent commits in the repository, oneline format.",
        "input_schema": {
            "type": "object",
            "properties": {
                "n": {"type": "integer", "minimum": 1, "maximum": 50},
            },
        },
    },
    {
        "name": "propose_change",
        "description": (
            "Propose a change to trades, strategy params, or the watchlist. "
            "Writes a row to pending_changes with status='pending' and "
            "fires an event so Shawn sees an Approve / Reject / Modify "
            "card in Telegram. Nothing is applied until Shawn approves."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": ["order", "strategy_param", "watchlist_edit"],
                },
                "payload": {"type": "object"},
                "reason": {
                    "type": "string",
                    "description": "Why this change makes sense right now.",
                },
            },
            "required": ["kind", "payload", "reason"],
        },
    },
]


async def dispatch(
    name: str,
    tool_input: dict[str, Any],
    *,
    proposed_by: int,
) -> str:
    """Route a single tool call. Returns a JSON string for ``tool_result``.

    ``proposed_by`` is the Telegram user id that initiated the chat turn,
    threaded through so ``propose_change`` can record who asked.
    """
    try:
        if name == "read_file":
            result = await _read_file(str(tool_input.get("path", "")))
        elif name == "list_dir":
            result = await _list_dir(str(tool_input.get("path", "")))
        elif name == "grep_repo":
            result = await _grep_repo(
                str(tool_input.get("pattern", "")),
                tool_input.get("path_glob"),
            )
        elif name == "query_supabase":
            result = await _query_supabase(
                str(tool_input.get("sql", "")),
                int(tool_input.get("max_rows", 200) or 200),
            )
        elif name == "alpaca_read":
            result = await _alpaca_read(
                str(tool_input.get("endpoint", "")),
                tool_input.get("params") or {},
            )
        elif name == "recent_decisions":
            result = await _recent_decisions(int(tool_input.get("n", 20) or 20))
        elif name == "git_log":
            result = await _git_log(int(tool_input.get("n", 10) or 10))
        elif name == "propose_change":
            result = await _propose_change(
                str(tool_input.get("kind", "")),
                tool_input.get("payload") or {},
                str(tool_input.get("reason", "")),
                proposed_by=proposed_by,
            )
        else:
            result = {"error": f"unknown tool: {name}"}
    except Exception as exc:
        _log.error("chat.tool.unhandled", tool=name, error=str(exc))
        result = {"error": f"{type(exc).__name__}: {exc}"}
    return json.dumps(_to_jsonable(result), default=str)


def list_tool_names() -> list[str]:
    """Return tool names in declaration order. Used by tests."""
    return [str(t["name"]) for t in TOOL_DEFINITIONS]
