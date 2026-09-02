"""Add GPU and VRAM worker capacity accounting."""

import sqlalchemy as sa
from alembic import op

revision = "0002_gpu_capacity"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "workers", sa.Column("gpu_count", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column(
        "workers", sa.Column("vram_mb", sa.BigInteger(), nullable=False, server_default="0")
    )
    op.add_column(
        "workers", sa.Column("reserved_gpu_count", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column(
        "workers",
        sa.Column("reserved_vram_mb", sa.BigInteger(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("workers", "reserved_vram_mb")
    op.drop_column("workers", "reserved_gpu_count")
    op.drop_column("workers", "vram_mb")
    op.drop_column("workers", "gpu_count")
