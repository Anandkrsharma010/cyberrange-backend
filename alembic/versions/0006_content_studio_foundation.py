"""Content studio foundation: website pages, page sections, course resources.

Revision ID: 0006
Revises: 0005
Create Date: 2026-04-26
"""

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS website_pages (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            slug            TEXT NOT NULL UNIQUE,
            title           TEXT NOT NULL,
            description     TEXT,
            status          TEXT NOT NULL DEFAULT 'draft',
            seo_title       TEXT,
            seo_description TEXT,
            created_by      UUID REFERENCES users(id),
            updated_by      UUID REFERENCES users(id),
            published_at    TIMESTAMPTZ,
            archived_at     TIMESTAMPTZ,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT website_pages_status_check
                CHECK (status IN ('draft', 'published', 'archived'))
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_website_pages_status_updated
            ON website_pages (status, updated_at DESC)
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS website_page_sections (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            page_id         UUID NOT NULL REFERENCES website_pages(id) ON DELETE CASCADE,
            section_key     TEXT NOT NULL,
            section_type    TEXT NOT NULL,
            position        INTEGER NOT NULL DEFAULT 0,
            is_visible      BOOLEAN NOT NULL DEFAULT true,
            payload         JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_by      UUID REFERENCES users(id),
            updated_by      UUID REFERENCES users(id),
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT website_page_sections_unique_position
                UNIQUE (page_id, position)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_website_page_sections_page_position
            ON website_page_sections (page_id, position)
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS course_resources (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            content_id      UUID NOT NULL REFERENCES content_items(id) ON DELETE CASCADE,
            title           TEXT NOT NULL,
            description     TEXT,
            resource_type   TEXT NOT NULL,
            url             TEXT,
            file_key        TEXT,
            mime_type       TEXT,
            position        INTEGER NOT NULL DEFAULT 0,
            is_visible      BOOLEAN NOT NULL DEFAULT true,
            metadata        JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_by      UUID REFERENCES users(id),
            updated_by      UUID REFERENCES users(id),
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT course_resources_type_check
                CHECK (resource_type IN ('text', 'link', 'pdf', 'file', 'manual')),
            CONSTRAINT course_resources_unique_position
                UNIQUE (content_id, position)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_course_resources_content_position
            ON course_resources (content_id, position)
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS course_resources")
    op.execute("DROP TABLE IF EXISTS website_page_sections")
    op.execute("DROP TABLE IF EXISTS website_pages")

