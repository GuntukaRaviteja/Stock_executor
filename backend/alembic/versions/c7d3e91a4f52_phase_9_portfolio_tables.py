"""phase 9 portfolio and paper trading tables

Revision ID: c7d3e91a4f52
Revises: b512c4aa5add
Create Date: 2026-10-02 23:55:00.000000

Simulated (paper) portfolios only. No table in this revision connects to a brokerage,
stores credentials, or represents a real financial transaction. Every monetary column is
DECIMAL — floating point is never used for cash, prices, fees or P&L.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'c7d3e91a4f52'
down_revision: Union[str, None] = 'b512c4aa5add'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ### Phase 9: Portfolio & paper trading tables ###
    op.create_table('portfolios',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('description', sa.String(length=255), nullable=True),
    sa.Column('trading_mode', sa.String(length=20), server_default='PAPER', nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('initial_capital', sa.Numeric(precision=18, scale=2), nullable=False),
    sa.Column('current_cash', sa.Numeric(precision=18, scale=2), nullable=False),
    sa.Column('realized_pnl', sa.Numeric(precision=18, scale=2), nullable=False),
    sa.Column('unrealized_pnl', sa.Numeric(precision=18, scale=2), nullable=False),
    sa.Column('total_pnl', sa.Numeric(precision=18, scale=2), nullable=False),
    sa.Column('equity', sa.Numeric(precision=18, scale=2), nullable=False),
    sa.Column('cost_config_json', sa.Text(), nullable=True),
    sa.Column('risk_config_json', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=True),
    sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_portfolios_status'), 'portfolios', ['status'], unique=False)
    op.create_index('ix_portfolio_status_created', 'portfolios', ['status', 'created_at'], unique=False)

    op.create_table('portfolio_positions',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('portfolio_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('stock_id', sa.Integer(), nullable=False),
    sa.Column('quantity', sa.BigInteger(), nullable=False),
    sa.Column('average_entry_price', sa.Numeric(precision=18, scale=4), nullable=False),
    sa.Column('cost_basis', sa.Numeric(precision=18, scale=2), nullable=False),
    sa.Column('entry_fees_total', sa.Numeric(precision=18, scale=2), nullable=False),
    sa.Column('realized_pnl', sa.Numeric(precision=18, scale=2), nullable=False),
    sa.Column('current_price', sa.Numeric(precision=18, scale=4), nullable=True),
    sa.Column('market_value', sa.Numeric(precision=18, scale=2), nullable=True),
    sa.Column('unrealized_pnl', sa.Numeric(precision=18, scale=2), nullable=True),
    sa.Column('opened_at', sa.DateTime(), server_default=sa.text('now()'), nullable=True),
    sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=True),
    sa.ForeignKeyConstraint(['portfolio_id'], ['portfolios.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['stock_id'], ['stocks.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('portfolio_id', 'stock_id', name='uq_position_portfolio_stock')
    )
    op.create_index(op.f('ix_portfolio_positions_portfolio_id'), 'portfolio_positions', ['portfolio_id'], unique=False)
    op.create_index(op.f('ix_portfolio_positions_stock_id'), 'portfolio_positions', ['stock_id'], unique=False)

    op.create_table('portfolio_orders',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('portfolio_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('stock_id', sa.Integer(), nullable=False),
    sa.Column('trading_mode', sa.String(length=20), server_default='PAPER', nullable=False),
    sa.Column('side', sa.String(length=10), nullable=False),
    sa.Column('order_type', sa.String(length=20), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('source', sa.String(length=20), nullable=False),
    sa.Column('quantity', sa.BigInteger(), nullable=False),
    sa.Column('requested_price', sa.Numeric(precision=18, scale=4), nullable=False),
    sa.Column('execution_price', sa.Numeric(precision=18, scale=4), nullable=True),
    sa.Column('gross_value', sa.Numeric(precision=18, scale=2), nullable=True),
    sa.Column('fees', sa.Numeric(precision=18, scale=2), nullable=True),
    sa.Column('net_cash_impact', sa.Numeric(precision=18, scale=2), nullable=True),
    sa.Column('realized_pnl', sa.Numeric(precision=18, scale=2), nullable=True),
    sa.Column('rejection_code', sa.String(length=64), nullable=True),
    sa.Column('rejection_reason', sa.String(length=255), nullable=True),
    sa.Column('strategy_event_id', sa.String(length=128), nullable=True),
    sa.Column('idempotency_key', sa.String(length=128), nullable=True),
    sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=True),
    sa.Column('executed_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['portfolio_id'], ['portfolios.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['stock_id'], ['stocks.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('portfolio_id', 'idempotency_key', name='uq_order_portfolio_idempotency')
    )
    op.create_index(op.f('ix_portfolio_orders_portfolio_id'), 'portfolio_orders', ['portfolio_id'], unique=False)
    op.create_index(op.f('ix_portfolio_orders_stock_id'), 'portfolio_orders', ['stock_id'], unique=False)
    op.create_index(op.f('ix_portfolio_orders_status'), 'portfolio_orders', ['status'], unique=False)
    op.create_index(op.f('ix_portfolio_orders_executed_at'), 'portfolio_orders', ['executed_at'], unique=False)
    op.create_index(op.f('ix_portfolio_orders_strategy_event_id'), 'portfolio_orders', ['strategy_event_id'], unique=False)
    op.create_index(op.f('ix_portfolio_orders_idempotency_key'), 'portfolio_orders', ['idempotency_key'], unique=False)
    op.create_index('ix_portfolio_order_history', 'portfolio_orders', ['portfolio_id', 'created_at', 'id'], unique=False)

    op.create_table('portfolio_trades',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('portfolio_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('order_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('stock_id', sa.Integer(), nullable=False),
    sa.Column('side', sa.String(length=10), nullable=False),
    sa.Column('quantity', sa.BigInteger(), nullable=False),
    sa.Column('execution_price', sa.Numeric(precision=18, scale=4), nullable=False),
    sa.Column('gross_value', sa.Numeric(precision=18, scale=2), nullable=False),
    sa.Column('fees', sa.Numeric(precision=18, scale=2), nullable=False),
    sa.Column('net_cash_impact', sa.Numeric(precision=18, scale=2), nullable=False),
    sa.Column('realized_pnl', sa.Numeric(precision=18, scale=2), nullable=False),
    sa.Column('executed_at', sa.DateTime(), server_default=sa.text('now()'), nullable=True),
    sa.ForeignKeyConstraint(['portfolio_id'], ['portfolios.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['order_id'], ['portfolio_orders.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['stock_id'], ['stocks.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('order_id')
    )
    op.create_index(op.f('ix_portfolio_trades_portfolio_id'), 'portfolio_trades', ['portfolio_id'], unique=False)
    op.create_index(op.f('ix_portfolio_trades_stock_id'), 'portfolio_trades', ['stock_id'], unique=False)
    op.create_index(op.f('ix_portfolio_trades_executed_at'), 'portfolio_trades', ['executed_at'], unique=False)
    op.create_index('ix_portfolio_trade_history', 'portfolio_trades', ['portfolio_id', 'executed_at', 'id'], unique=False)

    op.create_table('portfolio_snapshots',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('portfolio_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('timestamp', sa.DateTime(), nullable=False),
    sa.Column('cash', sa.Numeric(precision=18, scale=2), nullable=False),
    sa.Column('market_value', sa.Numeric(precision=18, scale=2), nullable=False),
    sa.Column('equity', sa.Numeric(precision=18, scale=2), nullable=False),
    sa.Column('realized_pnl', sa.Numeric(precision=18, scale=2), nullable=False),
    sa.Column('unrealized_pnl', sa.Numeric(precision=18, scale=2), nullable=False),
    sa.Column('total_pnl', sa.Numeric(precision=18, scale=2), nullable=False),
    sa.Column('total_fees', sa.Numeric(precision=18, scale=2), nullable=False),
    sa.Column('return_pct', sa.Numeric(precision=10, scale=4), nullable=False),
    sa.ForeignKeyConstraint(['portfolio_id'], ['portfolios.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_portfolio_snapshots_portfolio_id'), 'portfolio_snapshots', ['portfolio_id'], unique=False)
    op.create_index(op.f('ix_portfolio_snapshots_timestamp'), 'portfolio_snapshots', ['timestamp'], unique=False)
    op.create_index('ix_portfolio_snapshot_curve', 'portfolio_snapshots', ['portfolio_id', 'timestamp', 'id'], unique=False)
    # ### End Phase 9 ###


def downgrade() -> None:
    # ### Phase 9: Portfolio & paper trading tables ###
    # Tables are dropped directly rather than index-by-index: in MySQL the single-column
    # indexes back the foreign keys and cannot be dropped while the constraint still
    # exists. Dropping the table removes its indexes and constraints together.
    op.drop_table('portfolio_snapshots')
    op.drop_table('portfolio_trades')
    op.drop_table('portfolio_orders')
    op.drop_table('portfolio_positions')
    op.drop_table('portfolios')
    # ### End Phase 9 ###