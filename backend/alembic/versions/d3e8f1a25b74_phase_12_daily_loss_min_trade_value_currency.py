"""phase 12 portfolio currency, daily-loss limit and minimum trade value

Revision ID: d3e8f1a25b74
Revises: f5a1c7d94b20
Create Date: 2026-10-03 10:05:00.000000

Purely additive. No Phase 1-11 table or column is dropped, renamed or retyped, and no
historical row is modified. There is no new table: everything Phase 12 adds is either a new
column or a new key inside an existing JSON configuration column, both of which the engine
already knows how to read.

What it adds
------------
``portfolios.currency``
    The ISO-4217 code every monetary figure of a portfolio is denominated in. The engine
    performs no currency conversion, so this states a unit rather than enabling conversion —
    without it a portfolio's cash, equity and P&L are bare numbers whose scale is a guess.

    Added ``NOT NULL`` with a server default, which is what keeps an existing portfolio valid
    through the migration: MySQL fills every existing row with the default rather than failing
    on a null, and the ORM declares the same default so a row created outside the service still
    gets one. Every historical figure of an existing portfolio is by definition in the default
    currency, so the backfill relabels nothing and changes no number.

``risk_config_json -> max_daily_loss_pct``
    The daily-loss limit. Stored inside the existing risk configuration JSON rather than in a
    column because that is already where every per-portfolio limit lives, and because the two
    new Phase 12 rules are *configuration*, not state: there is nothing to aggregate over them.

``risk_config_json -> min_trade_value``
    The smallest order value the engine will execute. Same storage decision, same reasoning.

    Both keys are written by ``_limits_to_dict`` for every portfolio the service touches, and
    both are read defensively (``getattr``/``.get``-style fallbacks), so a portfolio written
    before this revision keeps working with both rules disabled. A stored config that predates
    these keys therefore behaves exactly as it did before, which is the whole point of leaving
    them absent rather than writing explicit nulls over an existing blob.

Downgrade
---------
``downgrade()`` drops the ``currency`` column and rewrites ``risk_config_json`` without the two
new keys, so a downgrade returns both the schema and every stored configuration to the Phase 11
shape. Portfolio state — positions, orders, trades, snapshots, ledger — is untouched either way.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'd3e8f1a25b74'
down_revision: Union[str, None] = 'f5a1c7d94b20'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: Currency an existing portfolio is backfilled with. Every figure such a portfolio has already
#: recorded is in this currency, so the value is a description of stored numbers, not a relabel.
_BACKFILL_CURRENCY = 'INR'

#: Keys introduced in Phase 12, stripped again on downgrade.
_PHASE_12_RISK_KEYS = ('max_daily_loss_pct', 'min_trade_value')


def upgrade() -> None:
    op.add_column(
        'portfolios',
        sa.Column(
            'currency',
            sa.String(length=3),
            nullable=False,
            server_default=_BACKFILL_CURRENCY,
        ),
    )


def downgrade() -> None:
    op.drop_column('portfolios', 'currency')
    _strip_phase_12_risk_keys()


def _strip_phase_12_risk_keys() -> None:
    """Rewrite every stored risk config without the Phase 12 keys.

    Read-then-write per row rather than a single SQL expression, because the keys live in a JSON
    document whose formatting is this project's own (sorted keys, decimals as strings) and
    rewriting it in the database would reformat every stored limit. A row whose config cannot be
    parsed is left untouched: it was already unreadable by the engine, and overwriting it here
    would destroy the only evidence of what is stored.
    """
    import json

    connection = op.get_bind()
    rows = connection.execute(
        sa.text("SELECT id, risk_config_json FROM portfolios WHERE risk_config_json IS NOT NULL")
    ).fetchall()
    for portfolio_id, raw in rows:
        try:
            config = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if not isinstance(config, dict):
            continue
        if not any(key in config for key in _PHASE_12_RISK_KEYS):
            continue
        for key in _PHASE_12_RISK_KEYS:
            config.pop(key, None)
        connection.execute(
            sa.text("UPDATE portfolios SET risk_config_json = :config WHERE id = :id"),
            {'config': json.dumps(config, sort_keys=True), 'id': portfolio_id},
        )