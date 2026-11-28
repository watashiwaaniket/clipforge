"""Allow Codex and Claude scoring providers."""
from collections.abc import Sequence

from alembic import op

revision: str = "0003_acp_providers"
down_revision: str | None = "0002_add_antigravity_provider"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_api_keys_provider", "api_keys", type_="check")
    op.create_check_constraint(
        "ck_api_keys_provider",
        "api_keys",
        "provider IN ('openai', 'groq', 'together', 'ollama', 'compatible', "
            "'antigravity', 'codex', 'claude')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_api_keys_provider", "api_keys", type_="check")
    op.create_check_constraint(
        "ck_api_keys_provider",
        "api_keys",
        "provider IN ('openai', 'groq', 'together', 'ollama', 'compatible', 'antigravity')",
    )
