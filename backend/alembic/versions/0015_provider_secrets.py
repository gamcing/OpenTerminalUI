"""Encrypted provider settings for coordinated database storage."""
from alembic import op
import sqlalchemy as sa

revision = "0015_provider_secrets"
down_revision = "0014_research_review"
branch_labels = None
depends_on = None


def upgrade():
    metadata = sa.MetaData()
    sa.Table(
        "provider_secrets", metadata,
        sa.Column("name", sa.String(128), primary_key=True),
        sa.Column("ciphertext", sa.Text(), nullable=False),
    )
    metadata.create_all(op.get_bind(), checkfirst=True)


def downgrade():
    if sa.inspect(op.get_bind()).has_table("provider_secrets"):
        op.drop_table("provider_secrets")
