"""Seed default Content Studio pages and starter sections.

Revision ID: 0007
Revises: 0006
Create Date: 2026-04-26
"""

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Seed core website pages (idempotent)
    op.execute(
        """
        INSERT INTO website_pages (slug, title, description, status)
        VALUES
            ('home', 'Home', 'Homepage content blocks and hero messaging', 'draft'),
            ('about', 'About', 'About page mission, vision, and company information', 'draft'),
            ('privacy', 'Privacy Policy', 'Privacy policy content', 'draft'),
            ('terms', 'Terms & Conditions', 'Terms and conditions content', 'draft'),
            ('refund', 'Refund Policy', 'Refund and cancellation policy content', 'draft')
        ON CONFLICT (slug) DO NOTHING
        """
    )

    # Home starter sections
    op.execute(
        """
        INSERT INTO website_page_sections (page_id, section_key, section_type, position, is_visible, payload)
        SELECT wp.id, 'hero_main', 'hero', 0, true,
               '{"headline":"Master Cyber Security Through Real Labs","subheadline":"Hands-on labs, guided learning, and production-like environments.","ctaText":"Explore Labs","ctaLink":"/labs"}'::jsonb
        FROM website_pages wp
        WHERE wp.slug = 'home'
          AND NOT EXISTS (
            SELECT 1 FROM website_page_sections s
            WHERE s.page_id = wp.id AND s.section_key = 'hero_main'
          )
        """
    )
    op.execute(
        """
        INSERT INTO website_page_sections (page_id, section_key, section_type, position, is_visible, payload)
        SELECT wp.id, 'features_overview', 'rich_text', 1, true,
               '{"title":"Why Learn With RangeOps","body":"Build skills using practical scenarios, not only theory."}'::jsonb
        FROM website_pages wp
        WHERE wp.slug = 'home'
          AND NOT EXISTS (
            SELECT 1 FROM website_page_sections s
            WHERE s.page_id = wp.id AND s.section_key = 'features_overview'
          )
        """
    )

    # About starter section
    op.execute(
        """
        INSERT INTO website_page_sections (page_id, section_key, section_type, position, is_visible, payload)
        SELECT wp.id, 'about_intro', 'rich_text', 0, true,
               '{"title":"Who We Are","body":"We build practical cyber security learning experiences for real-world readiness."}'::jsonb
        FROM website_pages wp
        WHERE wp.slug = 'about'
          AND NOT EXISTS (
            SELECT 1 FROM website_page_sections s
            WHERE s.page_id = wp.id AND s.section_key = 'about_intro'
          )
        """
    )

    # Legal pages starter text blocks
    op.execute(
        """
        INSERT INTO website_page_sections (page_id, section_key, section_type, position, is_visible, payload)
        SELECT wp.id, 'privacy_body', 'rich_text', 0, true,
               '{"title":"Privacy Policy","body":"Replace this with your approved legal privacy policy text."}'::jsonb
        FROM website_pages wp
        WHERE wp.slug = 'privacy'
          AND NOT EXISTS (
            SELECT 1 FROM website_page_sections s
            WHERE s.page_id = wp.id AND s.section_key = 'privacy_body'
          )
        """
    )
    op.execute(
        """
        INSERT INTO website_page_sections (page_id, section_key, section_type, position, is_visible, payload)
        SELECT wp.id, 'terms_body', 'rich_text', 0, true,
               '{"title":"Terms & Conditions","body":"Replace this with your approved terms and conditions text."}'::jsonb
        FROM website_pages wp
        WHERE wp.slug = 'terms'
          AND NOT EXISTS (
            SELECT 1 FROM website_page_sections s
            WHERE s.page_id = wp.id AND s.section_key = 'terms_body'
          )
        """
    )
    op.execute(
        """
        INSERT INTO website_page_sections (page_id, section_key, section_type, position, is_visible, payload)
        SELECT wp.id, 'refund_body', 'rich_text', 0, true,
               '{"title":"Refund Policy","body":"Replace this with your approved refund and cancellation policy text."}'::jsonb
        FROM website_pages wp
        WHERE wp.slug = 'refund'
          AND NOT EXISTS (
            SELECT 1 FROM website_page_sections s
            WHERE s.page_id = wp.id AND s.section_key = 'refund_body'
          )
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DELETE FROM website_page_sections
        WHERE section_key IN (
            'hero_main',
            'features_overview',
            'about_intro',
            'privacy_body',
            'terms_body',
            'refund_body'
        )
        """
    )
    op.execute(
        """
        DELETE FROM website_pages
        WHERE slug IN ('home', 'about', 'privacy', 'terms', 'refund')
          AND status = 'draft'
        """
    )

