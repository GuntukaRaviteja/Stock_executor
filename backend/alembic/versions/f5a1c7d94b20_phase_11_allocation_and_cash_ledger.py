"""phase 11 portfolio construction configuration and cash ledger

Revision ID: f5a1c7d94b20
Revises: e4f8b2c7a913
Create Date: 2026-10-03 02:20:00.000000

Purely additive. No Phase 1-10 table or column is dropped, renamed or retyped, and no
historical row is modified: ``allocation_config_json`` is nullable, so an existing portfolio
reads back with "use the application defaults" rather than with an invented configuration.

What it adds
------------
``portfolios.allocation_config_json``
    The Phase 11 construction policy — sizing method, single-position cap, sector cap,
    position-count and exposure caps, cash reserve, per-trade risk budget, stop-loss,
    take-profit, minimum position value and rebalancing threshold.

    Deliberately a *new* column rather than extra keys inside ``risk_config_json``: the two
    answer different questions. ``risk_config_json`` says what a single order may never breach
    (a hard pre-trade gate), ``allocation_config_json`` says how the portfolio is built (a
    policy). Merging them would make "the limits" and "the strategy" one blob, and a
    replacement write to one would silently reset the other.

``portfolio_transactions``
    An append-only cash ledger. One row per movement of ``current_cash``:

    * ``BUY`` / ``SELL`` — one per filled simulated order, linked to its order and trade;
    * ``DEPOSIT`` / ``WITHDRAWAL`` — capital movements, which have no order and no trade.

    ``portfolio_trades`` stays exactly what it was: the record of an *execution*. The ledger
    answers a different question — "how did the cash balance get to where it is" — and the two
    must agree, which is why every fill writes both in the same transaction.

    ``stock_id`` is nullable (a cash movement has no stock) while the portfolio foreign key is
    not, so referential integrity on the owning portfolio is preserved either way. The
    ``portfolio_id + stock_id + timestamp`` and ``portfolio_id + timestamp + id`` indexes
    serve the two questions the ledger is asked: per-stock history and per-period history.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'f5a1c7d94b20'
down_revision: Union[str, None] = 'e4f8b2c7a913'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Reused so the SQLite-backed test database and MySQL get the same key type, matching the
# ``_pk()`` helper the ORM models use.
_PK = sa.BigInteger().with_variant(sa.Integer(), 'sqlite')


def upgrade() -> None:
    # ### Phase 11: portfolio construction policy ###
    # Nullable: a Phase 9/10 portfolio keeps the application defaults until it is updated.
    op.add_column(
        'portfolios',
        sa.Column('allocation_config_json', sa.Text(), nullable=True),
    )

    # ### Phase 11: append-only cash ledger ###
    op.create_table(
        'portfolio_transactions',
        sa.Column('id', _PK, autoincrement=True, nullable=False),
        sa.Column('portfolio_id', _PK, nullable=False),
        sa.Column('transaction_type', sa.String(length=20), nullable=False),
        # Nullable on purpose: a DEPOSIT or WITHDRAWAL has no stock.
        sa.Column('stock_id', sa.Integer(), nullable=True),
        sa.Column('quantity', sa.BigInteger(), nullable=True),
        sa.Column('price', sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column('fees', sa.Numeric(precision=18, scale=2), nullable=False, server_default='0'),
        sa.Column('gross_value', sa.Numeric(precision=18, scale=2), nullable=False, server_default='0'),
        sa.Column('net_cash_impact', sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column('realized_pnl', sa.Numeric(precision=18, scale=2), nullable=False, server_default='0'),
        sa.Column('cash_after', sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column('order_id', _PK, nullable=True),
        sa.Column('trade_id', _PK, nullable=True),
        sa.Column('timestamp', sa.DateTime(), nullable=False),
        sa.Column('reason', sa.String(length=255), nullable=True),
        sa.Column('metadata_json', sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(['portfolio_id'], ['portfolios.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['stock_id'], ['stocks.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['order_id'], ['portfolio_orders.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['trade_id'], ['portfolio_trades.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    # Single-column indexes: portfolio (ownership), transaction_type (type filter),
    # stock (per-stock history) and timestamp (per-period history).
    op.create_index(
        op.f('ix_portfolio_transactions_portfolio_id'),
        'portfolio_transactions',
        ['portfolio_id'],
        unique=False,
    )
    op.create_index(
        op.f('ix_portfolio_transactions_transaction_type'),
        'portfolio_transactions',
        ['transaction_type'],
        unique=False,
    )
    op.create_index(
        op.f('ix_portfolio_transactions_stock_id'),
        'portfolio_transactions',
        ['stock_id'],
        unique=False,
    )
    op.create_index(
        op.f('ix_portfolio_transactions_timestamp'),
        'portfolio_transactions',
        ['timestamp'],
        unique=False,
    )
    # Composite indexes for the two questions the ledger is actually asked.
    op.create_index(
        'ix_portfolio_transaction_stock',
        'portfolio_transactions',
        ['portfolio_id', 'stock_id', 'timestamp'],
        unique=False,
    )
    op.create_index(
        'ix_portfolio_transaction_history',
        'portfolio_transactions',
        ['portfolio_id', 'timestamp', 'id'],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index('ix_portfolio_transaction_history', table_name='portfolio_transactions')
    op.drop_index('ix_portfolio_transaction_stock', table_name='portfolio_transactions')
    op.drop_index(op.f('ix_portfolio_transactions_timestamp'), table_name='portfolio_transactions')
    op.drop_index(op.f('ix_portfolio_transactions_stock_id'), table_name='portfolio_transactions')
    op.drop_index(
        op.f('ix_portfolio_transactions_transaction_type'), table_name='portfolio_transactions'
    )
    op.drop_index(op.f('ix_portfolio_transactions_portfolio_id'), table_name='portfolio_transactions')
    op.drop_table('portfolio_transactions')
    op.drop_column('portfolios', 'allocation_config_json')