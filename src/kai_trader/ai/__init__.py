"""AI decision layer for new CSP entries (Phase A1).

Sits strictly between the deterministic screener and the deterministic
risk gate:

    screener -> TradeIntent proposals -> AI TAKE/REJECT -> apply_gate
    -> ApprovedIntent -> worker submission -> broker

The package can only shrink and reorder the proposal list the screener
produced. It holds no broker imports, cannot construct
``ApprovedIntent``, and cannot touch system flags or risk limits; a
regression test pins those properties. Any failure (timeout, malformed
response, provider error, missing key) fails CLOSED for new entries:
the affected candidate is rejected, never traded blind. Position
management (rolls, profit-takes, assignments, covered calls,
reconciliation) never routes through this package.
"""

from kai_trader.ai.decision import (
    AIDecisionEngine as AIDecisionEngine,
)
from kai_trader.ai.decision import (
    AIFilterOutcome as AIFilterOutcome,
)
from kai_trader.ai.decision import (
    Evaluation as Evaluation,
)
from kai_trader.ai.models import (
    AIDecision as AIDecision,
)
