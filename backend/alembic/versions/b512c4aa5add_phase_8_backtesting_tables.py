"""phase 8 backtesting tables

Revision ID: b512c4aa5add
Revises: a29e40bea890
Create Date: 2026-10-02 19:01:40.689926

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'b512c4aa5add'
down_revision: Union[str, None] = 'a29e40bea890'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ### Phase 8: Backtesting tables ###
    op.create_table('backtests',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('stock_id', sa.Integer(), nullable=False),
    sa.Column('strategy_name', sa.String(length=100), nullable=False),
    sa.Column('strategy_version', sa.String(length=50), nullable=False),
    sa.Column('start_date', sa.Date(), nullable=False),
    sa.Column('end_date', sa.Date(), nullable=False),
    sa.Column('initial_capital', sa.Numeric(precision=18, scale=2), nullable=False),
    sa.Column('final_equity', sa.Numeric(precision=18, scale=2), nullable=False),
    sa.Column('total_return_pct', sa.Numeric(precision=10, scale=4), nullable=False),
    sa.Column('cagr_pct', sa.Numeric(precision=10, scale=4), nullable=True),
    sa.Column('peak_equity', sa.Numeric(precision=18, scale=2), nullable=True),
    sa.Column('trough_equity', sa.Numeric(precision=18, scale=2), nullable=True),
    sa.Column('max_drawdown', sa.Numeric(precision=18, scale=2), nullable=True),
    sa.Column('max_drawdown_pct', sa.Numeric(precision=10, scale=4), nullable=False),
    sa.Column('annualized_volatility_pct', sa.Numeric(precision=10, scale=4), nullable=True),
    sa.Column('sharpe_ratio', sa.Numeric(precision=10, scale=4), nullable=True),
    sa.Column('sortino_ratio', sa.Numeric(precision=10, scale=4), nullable=True),
    sa.Column('win_rate_pct', sa.Numeric(precision=10, scale=4), nullable=True),
    sa.Column('profit_factor', sa.Numeric(precision=10, scale=4), nullable=True),
    sa.Column('number_of_trades', sa.Integer(), nullable=False),
    sa.Column('winning_trades', sa.Integer(), nullable=True),
    sa.Column('losing_trades', sa.Integer(), nullable=True),
    sa.Column('average_trade_return_pct', sa.Numeric(precision=10, scale=4), nullable=True),
    sa.Column('average_winning_trade_pct', sa.Numeric(precision=10, scale=4), nullable=True),
    sa.Column('average_losing_trade_pct', sa.Numeric(precision=10, scale=4), nullable=True),
    sa.Column('average_holding_period', sa.Numeric(precision=10, scale=2), nullable=True),
    sa.Column('best_trade_pct', sa.Numeric(precision=10, scale=4), nullable=True),
    sa.Column('worst_trade_pct', sa.Numeric(precision=10, scale=4), nullable=True),
    sa.Column('benchmark_return_pct', sa.Numeric(precision=10, scale=4), nullable=True),
    sa.Column('benchmark_final_equity', sa.Numeric(precision=18, scale=2), nullable=True),
    sa.Column('strategy_vs_benchmark_pct', sa.Numeric(precision=10, scale=4), nullable=True),
    sa.Column('parameters_json', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=True),
    sa.ForeignKeyConstraint(['stock_id'], ['stocks.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_backtest_stock_date', 'backtests', ['stock_id', 'start_date', 'end_date'], unique=False)
    op.create_index(op.f('ix_backtests_stock_id'), 'backtests', ['stock_id'], unique=False)
    op.create_table('backtest_equity',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('backtest_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('date', sa.Date(), nullable=False),
    sa.Column('cash', sa.Numeric(precision=18, scale=2), nullable=False),
    sa.Column('position_value', sa.Numeric(precision=18, scale=2), nullable=False),
    sa.Column('equity', sa.Numeric(precision=18, scale=2), nullable=False),
    sa.ForeignKeyConstraint(['backtest_id'], ['backtests.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_backtest_equity_backtest_date', 'backtest_equity', ['backtest_id', 'date'], unique=True)
    op.create_index(op.f('ix_backtest_equity_backtest_id'), 'backtest_equity', ['backtest_id'], unique=False)
    op.create_table('backtest_trades',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('backtest_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('stock_id', sa.Integer(), nullable=False),
    sa.Column('entry_date', sa.Date(), nullable=False),
    sa.Column('entry_price', sa.Numeric(precision=15, scale=4), nullable=False),
    sa.Column('exit_date', sa.Date(), nullable=False),
    sa.Column('exit_price', sa.Numeric(precision=15, scale=4), nullable=False),
    sa.Column('quantity', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('gross_pnl', sa.Numeric(precision=18, scale=2), nullable=False),
    sa.Column('commission', sa.Numeric(precision=18, scale=2), nullable=False),
    sa.Column('slippage', sa.Numeric(precision=18, scale=2), nullable=False),
    sa.Column('net_pnl', sa.Numeric(precision=18, scale=2), nullable=False),
    sa.Column('return_pct', sa.Numeric(precision=10, scale=4), nullable=False),
    sa.Column('holding_period', sa.Integer(), nullable=False),
    sa.Column('exit_reason', sa.String(length=50), nullable=False),
    sa.ForeignKeyConstraint(['backtest_id'], ['backtests.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['stock_id'], ['stocks.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_backtest_trades_backtest_id'), 'backtest_trades', ['backtest_id'], unique=False)
    op.create_index(op.f('ix_backtest_trades_stock_id'), 'backtest_trades', ['stock_id'], unique=False)
    # ### End Phase 8 ###


def downgrade() -> None:
    # ### Phase 8: Backtesting tables ###
    # Tables are dropped directly rather than index-by-index: in MySQL the
    # `ix_backtest*_stock_id` / `ix_backtest*_backtest_id` indexes back the foreign keys
    # and cannot be dropped while the constraint still exists. Dropping the table removes
    # its indexes and constraints together.
    op.drop_table('backtest_trades')
    op.drop_table('backtest_equity')
    op.drop_table('backtests')
    # ### End Phase 8 ###