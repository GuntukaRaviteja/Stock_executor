"""phase 3 market data schema updates

Revision ID: a29e40bea890
Revises: e40ea9e35d92
Create Date: 2026-10-02 14:08:54.050046

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'a29e40bea890'
down_revision: Union[str, None] = 'e40ea9e35d92'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Phase 3: Add market and provider_symbol columns to stocks table
    op.add_column('stocks', sa.Column('market', sa.String(length=50), nullable=True))
    op.add_column('stocks', sa.Column('provider_symbol', sa.String(length=50), nullable=True))
    op.add_column('stocks', sa.Column('updated_at', sa.DateTime(), nullable=True))
    op.create_index(op.f('ix_stocks_provider_symbol'), 'stocks', ['provider_symbol'], unique=False)

    # Phase 3: Add adjusted_close and updated_at columns to stock_prices table
    op.add_column('stock_prices', sa.Column('adjusted_close', sa.Numeric(precision=18, scale=6), nullable=True))
    op.add_column('stock_prices', sa.Column('updated_at', sa.DateTime(), nullable=True))

    # Ensure the unique constraint on (stock_id, price_date) has the correct name
    # First check if the old constraint exists and drop it
    # The existing constraint is named "stock_id" - rename it to uq_stock_price_date
    try:
        op.drop_index('stock_id', table_name='stock_prices')
    except Exception:
        pass
    op.create_unique_constraint('uq_stock_price_date', 'stock_prices', ['stock_id', 'price_date'])

    # Add indexes for better query performance
    op.create_index(op.f('ix_stock_prices_stock_id'), 'stock_prices', ['stock_id'], unique=False)
    op.create_index(op.f('ix_stock_prices_price_date'), 'stock_prices', ['price_date'], unique=False)


def downgrade() -> None:
    # Drop indexes
    op.drop_index(op.f('ix_stock_prices_price_date'), table_name='stock_prices')
    op.drop_index(op.f('ix_stock_prices_stock_id'), table_name='stock_prices')
    op.drop_index(op.f('ix_stocks_provider_symbol'), table_name='stocks')

    # Drop unique constraint
    op.drop_constraint('uq_stock_price_date', 'stock_prices', type_='unique')

    # Recreate the original index name
    op.create_index('stock_id', 'stock_prices', ['stock_id', 'price_date'], unique=True)

    # Drop columns from stock_prices
    op.drop_column('stock_prices', 'updated_at')
    op.drop_column('stock_prices', 'adjusted_close')

    # Drop columns from stocks
    op.drop_column('stocks', 'updated_at')
    op.drop_column('stocks', 'provider_symbol')
    op.drop_column('stocks', 'market')