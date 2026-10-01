"""phase 2 schema

Revision ID: e40ea9e35d92
Revises: 
Create Date: 2026-10-01 22:46:53.574582

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'e40ea9e35d92'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Create companies table for Phase 2 company metadata
    op.create_table(
        'companies',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('stock_id', sa.Integer(), nullable=False),
        sa.Column('sector', sa.String(length=100), nullable=True),
        sa.Column('industry', sa.String(length=100), nullable=True),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('ceo', sa.String(length=100), nullable=True),
        sa.Column('website', sa.String(length=255), nullable=True),
        sa.Column('headquarters', sa.String(length=255), nullable=True),
        sa.Column('employees', sa.Integer(), nullable=True),
        sa.Column('market_cap', sa.Numeric(precision=20, scale=2), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['stock_id'], ['stocks.id'], name='fk_companies_stock_id', ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('stock_id', name='uq_companies_stock_id'),
    )

    # 2. Add composite index on stock_events to guarantee event identity uniqueness
    try:
        op.create_index(
            'uq_stock_event_identity',
            'stock_events',
            ['stock_id', 'event_date', 'event_type', 'source_raw_id'],
            unique=True,
        )
    except Exception:
        pass


def downgrade() -> None:
    try:
        op.drop_index('uq_stock_event_identity', table_name='stock_events')
    except Exception:
        pass

    op.drop_table('companies')
