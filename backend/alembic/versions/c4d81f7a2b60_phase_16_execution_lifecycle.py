"""phase 16 execution lifecycle: fills and the execution audit trail

Revision ID: c4d81f7a2b60
Revises: d3e8f1a25b74
Create Date: 2026-10-03 12:20:00.000000

Purely additive. No Phase 1-12 table or column is dropped, renamed or retyped, no historical row
is modified, and every table Phase 9 already created keeps its exact Phase 9 shape.

Why a migration is needed at all
--------------------------------
A planned, broker-style execution needs two records that Phase 9 had no reason to keep, because
a Phase 9 order fills once and immediately:

``portfolio_executions``
    One row per **fill**. ``portfolio_trades`` is deliberately one row per order
    (``order_id`` is ``UNIQUE``) because a trade is the completed transaction record of a
    *fully filled* order — which is exactly right for Phase 9 and cannot express an order that
    filled in two parts. A partial fill had nowhere to be written.

``portfolio_execution_events``
    The immutable execution audit trail. No event log exists anywhere in the project, and a
    refused order is otherwise indistinguishable from one that was never attempted.

The six new ``portfolio_orders`` columns
----------------------------------------
``filled_quantity``, ``remaining_quantity``
    What makes a partial fill expressible on the order itself. Phase 9 writes neither, so a
    Phase 9 order reads ``0/0`` and every existing Phase 9 code path is unaffected.

``execution_reference``
    The identifier the execution adapter assigned. For the simulated adapter it is derived
    deterministically from the idempotency key; the column names the *role*, so a future real
    adapter's own order id fills the same column and no portfolio logic changes.

``execution_plan_id``
    Groups the orders produced by one execution plan, so a plan and its orders are readable
    together.

``submitted_at``, ``completed_at``
    The two lifecycle timestamps. ``executed_at`` remains the time of the final fill, which is
    what every Phase 9 reader already expects it to mean.

Default handling
----------------
Every new column is added with a server default and ``NOT NULL`` where the column is a
counter, so MySQL fills existing rows instead of failing on a null. The ``status`` column is
untouched: the Phase 16 lifecycle states are values written into the existing ``String(20)``
column, and ``PARTIALLY_FILLED`` is 16 characters, so no widening is required.

Downgrade
---------
``downgrade()`` drops the two new tables and the six new columns, returning the schema to the
Phase 12 shape. Portfolio state — positions, orders, trades, snapshots, cash ledger — is
untouched either way; orders written by Phase 16 lose only their Phase 16 columns, and the
tables that hold the fills and the audit trail go with the downgrade.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'c4d81f7a2b60'
down_revision: Union[str, None] = 'd3e8f1a25b74'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: Portable primary-key type: BIGINT on MySQL, INTEGER on SQLite.
_PK = sa.BigInteger().with_variant(sa.Integer, 'sqlite')


def upgrade() -> None:
    op.create_table(
        'portfolio_executions',
        sa.Column('id', _PK, autoincrement=True, nullable=False),
        sa.Column('portfolio_id', _PK, nullable=False),
        sa.Column('order_id', _PK, nullable=False),
        sa.Column('stock_id', sa.Integer(), nullable=False),
        sa.Column('trading_mode', sa.String(length=20), server_default='PAPER', nullable=False),
        sa.Column('side', sa.String(length=10), nullable=False),
        sa.Column('quantity', sa.BigInteger(), nullable=False),
        sa.Column('execution_price', sa.Numeric(18, 4), nullable=False),
        sa.Column('gross_value', sa.Numeric(18, 2), nullable=False),
        sa.Column('fees', sa.Numeric(18, 2), server_default='0', nullable=False),
        sa.Column('net_cash_impact', sa.Numeric(18, 2), nullable=False),
        sa.Column('realized_pnl', sa.Numeric(18, 2), server_default='0', nullable=False),
        sa.Column('execution_reference', sa.String(length=64), nullable=False),
        sa.Column('cash_after', sa.Numeric(18, 2), nullable=False),
        sa.Column('quantity_after', sa.BigInteger(), nullable=False),
        sa.Column('executed_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column('execution_plan_id', sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(['portfolio_id'], ['portfolios.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['order_id'], ['portfolio_orders.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['stock_id'], ['stocks.id'], ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_portfolio_executions_portfolio_id', 'portfolio_executions', ['portfolio_id'])
    op.create_index('ix_portfolio_executions_order_id', 'portfolio_executions', ['order_id'])
    op.create_index('ix_portfolio_executions_stock_id', 'portfolio_executions', ['stock_id'])
    op.create_index(
        'ix_portfolio_executions_execution_reference',
        'portfolio_executions',
        ['execution_reference'],
    )
    op.create_index(
        'ix_portfolio_executions_execution_plan_id', 'portfolio_executions', ['execution_plan_id']
    )
    op.create_index('ix_portfolio_executions_executed_at', 'portfolio_executions', ['executed_at'])
    # The persistence-level guarantee that one adapter fill can never be booked twice.
    op.create_index(
        'uq_execution_portfolio_reference',
        'portfolio_executions',
        ['portfolio_id', 'execution_reference'],
        unique=True,
    )
    op.create_index(
        'ix_execution_history', 'portfolio_executions', ['portfolio_id', 'executed_at', 'id']
    )
    op.create_index(
        'ix_execution_order_history', 'portfolio_executions', ['order_id', 'executed_at', 'id']
    )

    op.create_table(
        'portfolio_execution_events',
        sa.Column('id', _PK, autoincrement=True, nullable=False),
        sa.Column('portfolio_id', _PK, nullable=False),
        sa.Column('event_type', sa.String(length=40), nullable=False),
        sa.Column('entity_type', sa.String(length=20), nullable=False),
        sa.Column('entity_id', sa.String(length=64), nullable=False),
        sa.Column('correlation_id', sa.String(length=64), nullable=True),
        sa.Column('execution_plan_id', sa.String(length=64), nullable=True),
        sa.Column('details_json', sa.Text(), nullable=True),
        sa.Column('timestamp', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(['portfolio_id'], ['portfolios.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_portfolio_execution_events_portfolio_id', 'portfolio_execution_events', ['portfolio_id']
    )
    op.create_index(
        'ix_portfolio_execution_events_event_type', 'portfolio_execution_events', ['event_type']
    )
    op.create_index(
        'ix_portfolio_execution_events_entity_id', 'portfolio_execution_events', ['entity_id']
    )
    op.create_index(
        'ix_portfolio_execution_events_correlation_id',
        'portfolio_execution_events',
        ['correlation_id'],
    )
    op.create_index(
        'ix_portfolio_execution_events_execution_plan_id',
        'portfolio_execution_events',
        ['execution_plan_id'],
    )
    op.create_index(
        'ix_portfolio_execution_events_timestamp', 'portfolio_execution_events', ['timestamp']
    )
    op.create_index(
        'ix_execution_event_history',
        'portfolio_execution_events',
        ['portfolio_id', 'timestamp', 'id'],
    )
    op.create_index(
        'ix_execution_event_entity',
        'portfolio_execution_events',
        ['portfolio_id', 'entity_type', 'entity_id'],
    )

    op.add_column(
        'portfolio_orders',
        sa.Column('filled_quantity', sa.BigInteger(), server_default='0', nullable=False),
    )
    op.add_column(
        'portfolio_orders',
        sa.Column('remaining_quantity', sa.BigInteger(), server_default='0', nullable=False),
    )
    op.add_column(
        'portfolio_orders', sa.Column('execution_reference', sa.String(length=64), nullable=True)
    )
    op.add_column(
        'portfolio_orders', sa.Column('execution_plan_id', sa.String(length=64), nullable=True)
    )
    op.add_column('portfolio_orders', sa.Column('submitted_at', sa.DateTime(), nullable=True))
    op.add_column('portfolio_orders', sa.Column('completed_at', sa.DateTime(), nullable=True))
    op.create_index(
        'ix_portfolio_orders_execution_plan_id', 'portfolio_orders', ['execution_plan_id']
    )


def downgrade() -> None:
    op.drop_index('ix_portfolio_orders_execution_plan_id', table_name='portfolio_orders')
    op.drop_column('portfolio_orders', 'completed_at')
    op.drop_column('portfolio_orders', 'submitted_at')
    op.drop_column('portfolio_orders', 'execution_plan_id')
    op.drop_column('portfolio_orders', 'execution_reference')
    op.drop_column('portfolio_orders', 'remaining_quantity')
    op.drop_column('portfolio_orders', 'filled_quantity')

    op.drop_index('ix_execution_event_entity', table_name='portfolio_execution_events')
    op.drop_index('ix_execution_event_history', table_name='portfolio_execution_events')
    op.drop_index('ix_portfolio_execution_events_timestamp', table_name='portfolio_execution_events')
    op.drop_index(
        'ix_portfolio_execution_events_execution_plan_id',
        table_name='portfolio_execution_events',
    )
    op.drop_index(
        'ix_portfolio_execution_events_correlation_id', table_name='portfolio_execution_events'
    )
    op.drop_index('ix_portfolio_execution_events_entity_id', table_name='portfolio_execution_events')
    op.drop_index('ix_portfolio_execution_events_event_type', table_name='portfolio_execution_events')
    op.drop_index(
        'ix_portfolio_execution_events_portfolio_id', table_name='portfolio_execution_events'
    )
    op.drop_table('portfolio_execution_events')

    op.drop_index('ix_execution_order_history', table_name='portfolio_executions')
    op.drop_index('ix_execution_history', table_name='portfolio_executions')
    op.drop_index('uq_execution_portfolio_reference', table_name='portfolio_executions')
    op.drop_index('ix_portfolio_executions_executed_at', table_name='portfolio_executions')
    op.drop_index('ix_portfolio_executions_execution_plan_id', table_name='portfolio_executions')
    op.drop_index(
        'ix_portfolio_executions_execution_reference', table_name='portfolio_executions'
    )
    op.drop_index('ix_portfolio_executions_stock_id', table_name='portfolio_executions')
    op.drop_index('ix_portfolio_executions_order_id', table_name='portfolio_executions')
    op.drop_index('ix_portfolio_executions_portfolio_id', table_name='portfolio_executions')
    op.drop_table('portfolio_executions')