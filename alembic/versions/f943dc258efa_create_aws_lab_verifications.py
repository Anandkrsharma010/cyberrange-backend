"""create_aws_lab_verifications

Revision ID: f943dc258efa
Revises: 0016
Create Date: 2026-06-02 11:45:50.039250

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f943dc258efa'
down_revision: Union[str, Sequence[str], None] = '0016'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'aws_lab_verifications',
        sa.Column('user_id', sa.UUID(), nullable=False),
        sa.Column('verification_code', sa.String(), nullable=True),
        sa.Column('code_sent_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('code_entered', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column('code_entered_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('access_granted', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('user_id')
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('aws_lab_verifications')
