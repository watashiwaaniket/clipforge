"""allow antigravity provider in api_keys

Revision ID: 0002_add_antigravity_provider
Revises: 0001_initial
Create Date: 2026-11-18 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002_add_antigravity_provider"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_api_keys_provider", "api_keys", type_="check")
    op.create_check_constraint(
        "ck_api_keys_provider",
        "api_keys",
        "provider IN ('openai', 'groq', 'together', 'ollama', 'compatible', 'antigravity')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_api_keys_provider", "api_keys", type_="check")
    op.create_check_constraint(
        "ck_api_keys_provider",
        "api_keys",
        "provider IN ('openai', 'groq', 'together', 'ollama', 'compatible')",
    )
