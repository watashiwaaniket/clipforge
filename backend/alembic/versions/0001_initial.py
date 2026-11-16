"""initial schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-11-16 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("email", sa.Text(), nullable=False, unique=True),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    op.create_table(
        "api_keys",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "user_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("encrypted_key", sa.LargeBinary(), nullable=False),
        sa.Column("base_url", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "provider IN ('openai', 'groq', 'together', 'ollama', 'compatible')",
            name="ck_api_keys_provider",
        ),
    )

    op.create_table(
        "jobs",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "user_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'queued'")),
        sa.Column("progress_message", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("duration_s", sa.Double(), nullable=True),
        sa.Column("thumbnail_url", sa.Text(), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("enqueued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("platform IN ('twitch', 'youtube')", name="ck_jobs_platform"),
        sa.CheckConstraint(
            "status IN ('queued', 'downloading', 'transcribing', 'scoring', 'clipping', "
            "'completed', 'failed')",
            name="ck_jobs_status",
        ),
    )
    op.create_index("ix_jobs_user_created", "jobs", ["user_id", sa.text("created_at DESC")])
    op.create_index(
        "ix_jobs_active_started_at",
        "jobs",
        ["started_at"],
        postgresql_where=sa.text("status NOT IN ('completed', 'failed')"),
    )

    op.create_table(
        "clips",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "job_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("start_s", sa.Double(), nullable=False),
        sa.Column("end_s", sa.Double(), nullable=False),
        sa.Column("score", sa.Integer(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("hook", sa.Text(), nullable=False),
        sa.Column("score_source", sa.Text(), nullable=False, server_default=sa.text("'llm'")),
        sa.Column("file_path", sa.Text(), nullable=True),
        sa.Column(
            "caption_status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'ready'")),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("score BETWEEN 0 AND 100", name="ck_clips_score"),
        sa.CheckConstraint("end_s > start_s", name="ck_clips_time_order"),
        sa.CheckConstraint("score_source IN ('llm', 'heuristic')", name="ck_clips_score_source"),
        sa.CheckConstraint(
            "caption_status IN ('pending', 'burned', 'failed')",
            name="ck_clips_caption_status",
        ),
        sa.CheckConstraint("status IN ('ready', 'failed')", name="ck_clips_status"),
    )
    op.create_index("ix_clips_job_id", "clips", ["job_id"])


def downgrade() -> None:
    op.drop_index("ix_clips_job_id", table_name="clips")
    op.drop_table("clips")
    op.drop_index("ix_jobs_active_started_at", table_name="jobs")
    op.drop_index("ix_jobs_user_created", table_name="jobs")
    op.drop_table("jobs")
    op.drop_table("api_keys")
    op.drop_table("users")
