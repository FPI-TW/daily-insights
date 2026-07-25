"""Standardize locale codes across persisted application data.

Revision ID: 20260725_0005
Revises: 20260724_0004
Create Date: 2026-07-25
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260725_0005"
down_revision: str | None = "20260724_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _drop_locale_constraints() -> None:
    op.drop_constraint(
        op.f("ck_report_publications_presentations_have_supported_locales"),
        "report_publications",
        type_="check",
    )
    for table in (
        "asset_migration_entries",
        "podcast_episode_translations",
        "podcast_episode_audio_variants",
    ):
        op.drop_constraint(op.f(f"ck_{table}_locale_supported"), table, type_="check")


def _create_locale_constraints(*, traditional: str, simplified: str) -> None:
    op.create_check_constraint(
        op.f("ck_report_publications_presentations_have_supported_locales"),
        "report_publications",
        "jsonb_typeof(presentations) = 'object' "
        f"AND presentations ?& ARRAY['{traditional}', '{simplified}', 'en'] "
        f"AND presentations - ARRAY['{traditional}', '{simplified}', 'en'] = '{{}}'::jsonb",
    )
    for table in (
        "asset_migration_entries",
        "podcast_episode_translations",
        "podcast_episode_audio_variants",
    ):
        op.create_check_constraint(
            op.f(f"ck_{table}_locale_supported"),
            table,
            f"locale IN ('{traditional}', '{simplified}', 'en')",
        )


def _rename_locale_values(*, traditional_from: str, simplified_from: str) -> None:
    traditional_to = "zh-hant" if traditional_from == "zh-TW" else "zh-TW"
    simplified_to = "zh-hans" if simplified_from == "zh-CN" else "zh-CN"

    op.execute(
        f"""
        UPDATE assets
        SET
            locale = CASE locale
                WHEN '{traditional_from}' THEN '{traditional_to}'
                WHEN '{simplified_from}' THEN '{simplified_to}'
                ELSE locale
            END,
            localized_titles =
                localized_titles - '{traditional_from}' - '{simplified_from}'
                || CASE
                    WHEN localized_titles ? '{traditional_from}'
                    THEN jsonb_build_object(
                        '{traditional_to}',
                        localized_titles -> '{traditional_from}'
                    )
                    ELSE '{{}}'::jsonb
                END
                || CASE
                    WHEN localized_titles ? '{simplified_from}'
                    THEN jsonb_build_object(
                        '{simplified_to}',
                        localized_titles -> '{simplified_from}'
                    )
                    ELSE '{{}}'::jsonb
                END
        WHERE
            locale IN ('{traditional_from}', '{simplified_from}')
            OR localized_titles ?| ARRAY['{traditional_from}', '{simplified_from}']
        """
    )
    for table in (
        "asset_migration_entries",
        "podcast_episode_translations",
        "podcast_episode_audio_variants",
    ):
        op.execute(
            f"""
            UPDATE {table}
            SET locale = CASE locale
                WHEN '{traditional_from}' THEN '{traditional_to}'
                WHEN '{simplified_from}' THEN '{simplified_to}'
                ELSE locale
            END
            WHERE locale IN ('{traditional_from}', '{simplified_from}')
            """
        )

    op.execute(
        f"""
        UPDATE report_publications
        SET presentations = jsonb_build_object(
            '{traditional_to}',
            (presentations -> '{traditional_from}')
                || jsonb_build_object('locale', '{traditional_to}'),
            '{simplified_to}',
            (presentations -> '{simplified_from}')
                || jsonb_build_object('locale', '{simplified_to}'),
            'en',
            presentations -> 'en'
        )
        """
    )


def upgrade() -> None:
    _drop_locale_constraints()
    _rename_locale_values(traditional_from="zh-TW", simplified_from="zh-CN")
    _create_locale_constraints(traditional="zh-hant", simplified="zh-hans")


def downgrade() -> None:
    _drop_locale_constraints()
    _rename_locale_values(traditional_from="zh-hant", simplified_from="zh-hans")
    _create_locale_constraints(traditional="zh-TW", simplified="zh-CN")
