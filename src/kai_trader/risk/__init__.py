"""Deterministic risk layer.

The gate in :mod:`kai_trader.risk.gate` is the single choke point every
new-entry trade proposal must pass before the strategy worker may submit
it. Decision producers (today the CSP screener in
``strategy/candidates.py``, later any quant or AI layer) emit
``TradeIntent`` proposals; only :func:`kai_trader.risk.gate.apply_gate`
turns a proposal into an :class:`kai_trader.risk.gate.ApprovedIntent`,
and only ``ApprovedIntent`` is accepted by the worker's submission path.
"""

from kai_trader.risk.gate import (
    ApprovedIntent as ApprovedIntent,
)
from kai_trader.risk.gate import (
    GateRejection as GateRejection,
)
from kai_trader.risk.gate import (
    GateResult as GateResult,
)
from kai_trader.risk.gate import (
    RiskContext as RiskContext,
)
from kai_trader.risk.gate import (
    apply_gate as apply_gate,
)
