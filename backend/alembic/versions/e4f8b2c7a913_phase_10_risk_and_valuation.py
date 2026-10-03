"""phase 10 risk management, execution audit and equity-curve columns

Revision ID: e4f8b2c7a913
Revises: c7d3e91a4f52
Create Date: 2026-10-03 01:05:00.000000

Purely additive. No table or column from Phases 1-9 is dropped, renamed or retyped, and no
historical Phase 1-9 row is modified beyond backfilling the three new snapshot columns with
their only correct value for an existing row.

What it adds
------------
``portfolio_orders``
    ``signal_as_of``               when the decision behind the order was taken, so a
                                   historical fill can be audited (the signal timestamp must
                                   never post-date the execution timestamp);
    ``execution_price_policy``      which price of which bar the fill reference came from;
    ``metadata_json``               free-form audit metadata (strategy, score, indicators).

``portfolio_snapshots``
    ``drawdown_pct``               signed drawdown from the running peak (0 at a peak,
                                   negative below it);
    ``exposure_pct``               market value as a percentage of equity;
    ``peak_equity``                the running peak those two are measured against.

``portfolio_risk_configs`` is deliberately **not** created: risk limits already live in the
``portfolios.risk_config_json`` column written by Phase 9, and a second copy would be a
second source of truth that could disagree with the first. The Phase 10 risk rules use that
existing column unchanged.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'e4f8b2c7a913'
down_revision: Union[str, None] = 'c7d3e91a4f52'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ### Phase 10: execution audit trail on orders ###
    op.add_column(
        'portfolio_orders',
        sa.Column('signal_as_of', sa.DateTime(), nullable=True),
    )
    op.create_index(op.f('ix_portfolio_orders_signal_as_of'), 'portfolio_orders', ['signal_as_of'], unique=False)
    op.add_column(
        'portfolio_orders',
        sa.Column('execution_price_policy', sa.String(length=20), nullable=True),
    )
    op.add_column(
        'portfolio_orders',
        sa.Column('metadata_json', sa.Text(), nullable=True),
    )

    # ### Phase 10: drawdown and exposure on the equity curve ###
    # server_default keeps the ALTER valid against tables that already hold Phase 9 rows.
    op.add_column(
        'portfolio_snapshots',
        sa.Column('drawdown_pct', sa.Numeric(precision=10, scale=4), nullable=False, server_default='0'),
    )
    op.add_column(
        'portfolio_snapshots',
        sa.Column('exposure_pct', sa.Numeric(precision=10, scale=4), nullable=False, server_default='0'),
    )
    op.add_column(
        'portfolio_snapshots',
        sa.Column('peak_equity', sa.Numeric(precision=18, scale=2), nullable=False, server_default='0'),
    )
    # Backfill the one value that is correct for every pre-existing snapshot: before Phase 10
    # no drawdown, exposure or peak was recorded, so 0 is the only figure that does not invent
    # history. New snapshots are written with the real figures by the service.
    op.execute("UPDATE portfolio_snapshots SET peak_equity = equity WHERE peak_equity = 0")


def downgrade() -> None:
    op.drop_column('portfolio_snapshots', 'peak_equity')
    op.drop_column('portfolio_snapshots', 'exposure_pct')
    op.drop_column('portfolio_snapshots', 'drawdown_pct')
    op.drop_column('portfolio_orders', 'metadata_json')
    op.drop_column('portfolio_orders', 'execution_price_policy')
    op.drop_index(op.f('ix_portfolio_orders_signal_as_of'), table_name='portfolio_orders')
    op.drop_column('portfolio_orders', 'signal_as_of')