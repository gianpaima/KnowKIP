"""contexto web atribuido: web_document, web_person_mention y web_reference

Capa de contexto (docs/web-context-design.md): páginas de prensa, redes
sociales y otras fuentes admitidas, capturadas con la cadena probatoria de
siempre (publication_item serie WEB → artifact/artifact_version → CAS) y
parseadas a un `web_document` con menciones de persona y referencias a normas.
Separada por construcción del registro funcional: nada de aquí crea ni
modifica eventos, asignaciones ni fechas.

Los enums nuevos (`SourceAuthority.PRESS/SOCIAL_MEDIA/OTHER_WEB`,
`WebDocumentKind`, `WebBodyScope`, `ReviewTaskType.WEB_*`) no requieren DDL:
todos los enums del esquema son VARCHAR sin CHECK (native_enum=False), así que
solo las tablas nuevas necesitan migración. Todo es aditivo.

Revision ID: e3a9c52d71b4
Revises: b6e2a7d94f13
Create Date: 2026-08-18 13:30:00.000000

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e3a9c52d71b4"
down_revision = "b6e2a7d94f13"
branch_labels = None
depends_on = None

_KIND = sa.Enum(
    "NEWS_ARTICLE",
    "SOCIAL_POST",
    "SOCIAL_PROFILE",
    "INSTITUTIONAL_NEWS",
    "OTHER",
    name="web_document_kind",
    native_enum=False,
    length=48,
)
_BODY_SCOPE = sa.Enum(
    "FULL",
    "PARTIAL_PAYWALL",
    "METADATA_ONLY",
    name="web_body_scope",
    native_enum=False,
    length=48,
)
_RESOLUTION_STATUS = sa.Enum(
    "UNRESOLVED",
    "CANDIDATE_MATCH",
    "AUTO_LINKED",
    "IDENTIFIER_LINKED",
    "PRECEDENT_LINKED",
    "OFFICE_CORROBORATED",
    "HUMAN_CONFIRMED",
    "HUMAN_REJECTED",
    "MERGED",
    "SPLIT",
    name="resolution_status",
    native_enum=False,
    length=48,
)
_REFERENCE_TYPE = sa.Enum(
    "INTERNAL_SEEN_DOCUMENT",
    "NORMATIVE_CITATION",
    "PRIOR_APPOINTMENT",
    "MODIFIES",
    "REPEALS",
    "CORRECTS",
    "OTHER",
    name="reference_type",
    native_enum=False,
    length=48,
)


def upgrade() -> None:
    op.create_table(
        "web_document",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("publication_item_id", sa.String(length=32), nullable=False),
        sa.Column("kind", _KIND, nullable=False),
        sa.Column("headline_raw", sa.String(length=1000), nullable=True),
        sa.Column("published_at_raw", sa.String(length=100), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("modified_at_raw", sa.String(length=100), nullable=True),
        sa.Column("author_raw", sa.String(length=400), nullable=True),
        sa.Column("account_raw", sa.String(length=300), nullable=True),
        sa.Column("section_raw", sa.String(length=200), nullable=True),
        sa.Column("language", sa.String(length=10), nullable=True),
        sa.Column("body_scope", _BODY_SCOPE, nullable=False),
        sa.Column("parsed_from_artifact_version_id", sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(["publication_item_id"], ["publication_item.id"]),
        sa.ForeignKeyConstraint(["parsed_from_artifact_version_id"], ["artifact_version.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    # Único además de índice: un publication_item es a lo sumo un web_document,
    # igual que legal_document con el suyo — pero aquí sí con el candado puesto.
    op.create_index(
        "ix_web_document_publication_item_id", "web_document", ["publication_item_id"], unique=True
    )

    op.create_table(
        "web_person_mention",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("web_document_id", sa.String(length=32), nullable=False),
        sa.Column("text_raw", sa.String(length=400), nullable=False),
        sa.Column("text_normalized", sa.String(length=400), nullable=False),
        sa.Column("role_context_raw", sa.String(length=1000), nullable=True),
        sa.Column("role_context_normalized", sa.String(length=1000), nullable=True),
        sa.Column("evidence_span_id", sa.String(length=32), nullable=False),
        sa.Column("canonical_person_id", sa.String(length=32), nullable=True),
        sa.Column("resolution_status", _RESOLUTION_STATUS, nullable=False),
        sa.Column("matched_by", sa.Text(), nullable=True),
        sa.Column("identity_precedent_id", sa.String(length=32), nullable=True),
        sa.ForeignKeyConstraint(["web_document_id"], ["web_document.id"]),
        sa.ForeignKeyConstraint(["evidence_span_id"], ["evidence_span.id"]),
        sa.ForeignKeyConstraint(["canonical_person_id"], ["person.id"]),
        sa.ForeignKeyConstraint(["identity_precedent_id"], ["identity_precedent.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_web_person_mention_web_document_id", "web_person_mention", ["web_document_id"])
    op.create_index("ix_web_person_mention_text_normalized", "web_person_mention", ["text_normalized"])
    op.create_index(
        "ix_web_person_mention_role_context_normalized",
        "web_person_mention",
        ["role_context_normalized"],
    )
    op.create_index(
        "ix_web_person_mention_canonical_person_id", "web_person_mention", ["canonical_person_id"]
    )
    op.create_index(
        "ix_web_person_mention_identity_precedent_id",
        "web_person_mention",
        ["identity_precedent_id"],
    )

    op.create_table(
        "web_reference",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("web_document_id", sa.String(length=32), nullable=False),
        sa.Column("reference_type", _REFERENCE_TYPE, nullable=False),
        sa.Column("target_document_id", sa.String(length=32), nullable=True),
        sa.Column("target_number_raw", sa.String(length=300), nullable=False),
        sa.Column("target_doc_kind_raw", sa.String(length=200), nullable=True),
        sa.Column("evidence_span_id", sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(["web_document_id"], ["web_document.id"]),
        sa.ForeignKeyConstraint(["target_document_id"], ["legal_document.id"]),
        sa.ForeignKeyConstraint(["evidence_span_id"], ["evidence_span.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_web_reference_web_document_id", "web_reference", ["web_document_id"])


def downgrade() -> None:
    op.drop_index("ix_web_reference_web_document_id", table_name="web_reference")
    op.drop_table("web_reference")
    op.drop_index("ix_web_person_mention_identity_precedent_id", table_name="web_person_mention")
    op.drop_index("ix_web_person_mention_canonical_person_id", table_name="web_person_mention")
    op.drop_index("ix_web_person_mention_role_context_normalized", table_name="web_person_mention")
    op.drop_index("ix_web_person_mention_text_normalized", table_name="web_person_mention")
    op.drop_index("ix_web_person_mention_web_document_id", table_name="web_person_mention")
    op.drop_table("web_person_mention")
    op.drop_index("ix_web_document_publication_item_id", table_name="web_document")
    op.drop_table("web_document")
