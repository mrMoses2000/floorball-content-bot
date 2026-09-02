from __future__ import annotations

from datetime import datetime
from typing import Literal
from urllib.parse import urlparse
from uuid import UUID

import asyncpg
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from floorball_bot.auth import require_roles
from floorball_bot.dialogue.evaluator import evaluate_gaps
from floorball_bot.dialogue.repository import DialogueSpecRepository
from floorball_bot.domain import Actor, Role
from floorball_bot.workflow import canonical_hash


class NewsProjectionRejected(ValueError):
    pass


class NewsBlockInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["paragraph", "heading", "quote"]
    text: str = Field(min_length=1, max_length=2400)


class NewsSourceInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    label: str = Field(min_length=1, max_length=200)
    url: str = Field(max_length=1000)

    @field_validator("url")
    @classmethod
    def https_only(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("source URL must use HTTPS")
        return value


class NewsMediaInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    image_url: str = Field(max_length=1000)
    alt_ru: str = Field(min_length=1, max_length=300)
    alt_kz: str = Field(min_length=1, max_length=300)
    alt_en: str = Field(default="", max_length=300)
    rights_confirmed: Literal[True]

    @field_validator("image_url")
    @classmethod
    def safe_image_url(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("news image URL must use HTTPS")
        return value


class NewsDraftInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    scope: Literal["national", "city"]
    city_slug: str = Field(default="", max_length=80, pattern=r"^$|^[a-z0-9-]+$")
    slug: str = Field(max_length=120, pattern=r"^[a-z0-9-]+$")
    published_at: datetime
    title_ru: str = Field(min_length=1, max_length=240)
    title_kz: str = Field(min_length=1, max_length=240)
    title_en: str = Field(default="", max_length=240)
    excerpt_ru: str = Field(min_length=1, max_length=600)
    excerpt_kz: str = Field(min_length=1, max_length=600)
    excerpt_en: str = Field(default="", max_length=600)
    body_ru: list[NewsBlockInput] = Field(min_length=1, max_length=30)
    body_kz: list[NewsBlockInput] = Field(min_length=1, max_length=30)
    body_en: list[NewsBlockInput] = Field(default_factory=list, max_length=30)
    sources: list[NewsSourceInput] = Field(default_factory=list, max_length=10)
    media: NewsMediaInput | None = None
    video_url: str = Field(default="", max_length=1000)
    publication_permission: Literal[True]

    @field_validator("video_url")
    @classmethod
    def safe_video_url(cls, value: str) -> str:
        if value:
            parsed = urlparse(value)
            if parsed.scheme != "https" or not parsed.netloc:
                raise ValueError("news video URL must use HTTPS")
        return value

    @model_validator(mode="after")
    def validate_scope(self) -> NewsDraftInput:
        if (self.scope == "city") != bool(self.city_slug):
            raise ValueError("city scope requires city_slug; national scope forbids it")
        return self


class NewsGalleryManifestItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    mediaId: UUID
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    caption: str = Field(default="", max_length=600)


class NewsApplicationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    application_id: UUID
    draft_id: UUID
    revision: int
    news_id: UUID
    slug: str
    applied: bool


async def apply_approved_news_draft(
    pool: asyncpg.Pool, *, draft_id: UUID, actor: Actor
) -> NewsApplicationResult:
    require_roles(actor, Role.REVIEWER, Role.SUPERADMIN)
    loaded = DialogueSpecRepository().load("news")
    async with pool.acquire() as connection, connection.transaction():
        await connection.execute(
            "SELECT pg_advisory_xact_lock(hashtext($1))", f"news-draft:{draft_id}"
        )
        row = await connection.fetchrow(
            """
            SELECT d.status, d.current_revision, d.approved_revision, d.created_by,
                   r.content, r.content_hash, s.workflow, s.definition_version,
                   s.definition_hash, s.context_hash, s.id AS session_id
            FROM drafts d
            JOIN draft_revisions r
              ON r.draft_id=d.id AND r.revision=d.approved_revision
            JOIN conversation_sessions s ON s.id=d.session_id
            WHERE d.id=$1 FOR UPDATE OF d
            """,
            draft_id,
        )
        if not row or row["status"] != "approved":
            raise NewsProjectionRejected("news draft must be approved")
        revision = row["approved_revision"]
        if revision != row["current_revision"] or row["workflow"] != "news":
            raise NewsProjectionRejected("news approval is stale or belongs to another workflow")
        if (
            row["definition_version"] != loaded.spec.version
            or row["definition_hash"] != loaded.sha256
        ):
            raise NewsProjectionRejected("news dialogue definition changed")
        content = row["content"]
        if not isinstance(content, dict) or canonical_hash(content) != row["content_hash"]:
            raise NewsProjectionRejected("news revision hash is invalid")
        if (
            content.get("dialogue_mode") != "news"
            or content.get("definition_hash") != loaded.sha256
            or content.get("definition_version") != loaded.spec.version
            or content.get("context_hash") != row["context_hash"]
        ):
            raise NewsProjectionRejected("news revision pins changed")
        fields = content.get("fields")
        if not isinstance(fields, dict):
            raise NewsProjectionRejected("news fields must be an object")
        if not evaluate_gaps(loaded.spec, fields).can_publish:
            raise NewsProjectionRejected("news draft is incomplete for publication")
        draft = NewsDraftInput.model_validate(fields)
        gallery_manifest = [
            NewsGalleryManifestItem.model_validate(item)
            for item in content.get("media_manifest", [])
        ]
        if len(gallery_manifest) > 10:
            raise NewsProjectionRejected("news gallery exceeds ten photos")

        existing_application = await connection.fetchrow(
            """
            SELECT id, news_id FROM news_projection_applications
            WHERE draft_id=$1 AND revision=$2
            """,
            draft_id,
            revision,
        )
        if existing_application:
            return NewsApplicationResult(
                application_id=existing_application["id"],
                draft_id=draft_id,
                revision=revision,
                news_id=existing_application["news_id"],
                slug=draft.slug,
                applied=False,
            )

        creator_roles = {
            Role(item["role_name"])
            for item in await connection.fetch(
                "SELECT role_name FROM user_roles WHERE user_id=$1 AND revoked_at IS NULL",
                row["created_by"],
            )
        }
        city_id = None
        if draft.scope == "national":
            if not creator_roles.intersection({Role.FEDERATION_EDITOR, Role.SUPERADMIN}):
                raise NewsProjectionRejected("national news requires federation editor authority")
        else:
            city_id = await connection.fetchval(
                "SELECT id FROM cities WHERE slug=$1 AND active=TRUE AND deleted_at IS NULL",
                draft.city_slug,
            )
            if city_id is None:
                raise NewsProjectionRejected("news city is unavailable")
            scoped = await connection.fetchval(
                """
                SELECT EXISTS(
                    SELECT 1 FROM user_city_scopes
                    WHERE user_id=$1 AND city_id=$2 AND revoked_at IS NULL
                )
                """,
                row["created_by"],
                city_id,
            )
            if Role.SUPERADMIN not in creator_roles and not (
                Role.CITY_COACH in creator_roles and scoped
            ):
                raise NewsProjectionRejected("city news author lacks the selected city scope")

        current = await connection.fetchrow(
            "SELECT id, scope, city_id FROM news_items WHERE slug=$1 FOR UPDATE", draft.slug
        )
        if current and (current["scope"] != draft.scope or current["city_id"] != city_id):
            raise NewsProjectionRejected("news slug is already bound to another scope")
        media = draft.media
        news_id = await connection.fetchval(
            """
            INSERT INTO news_items(
                slug, scope, city_id, title_ru, title_kz, title_en,
                excerpt_ru, excerpt_kz, excerpt_en, body_ru, body_kz, body_en,
                sources, image_url, image_alt_ru, image_alt_kz, image_alt_en,
                media_rights_confirmed, video_url, published_at, created_by, updated_by
            ) VALUES (
                $1,$2,$3,$4,$5,$6,$7,$8,$9,$10::jsonb,$11::jsonb,$12::jsonb,
                $13::jsonb,$14,$15,$16,$17,$18,$19,$20,$21,$21
            )
            ON CONFLICT (slug) DO UPDATE SET
                title_ru=EXCLUDED.title_ru, title_kz=EXCLUDED.title_kz,
                title_en=EXCLUDED.title_en, excerpt_ru=EXCLUDED.excerpt_ru,
                excerpt_kz=EXCLUDED.excerpt_kz, excerpt_en=EXCLUDED.excerpt_en,
                body_ru=EXCLUDED.body_ru, body_kz=EXCLUDED.body_kz,
                body_en=EXCLUDED.body_en, sources=EXCLUDED.sources,
                image_url=EXCLUDED.image_url, image_alt_ru=EXCLUDED.image_alt_ru,
                image_alt_kz=EXCLUDED.image_alt_kz, image_alt_en=EXCLUDED.image_alt_en,
                media_rights_confirmed=EXCLUDED.media_rights_confirmed,
                video_url=EXCLUDED.video_url, published_at=EXCLUDED.published_at,
                status='approved', revision=news_items.revision+1,
                updated_by=EXCLUDED.updated_by, updated_at=now(), deleted_at=NULL
            RETURNING id
            """,
            draft.slug,
            draft.scope,
            city_id,
            draft.title_ru,
            draft.title_kz,
            draft.title_en,
            draft.excerpt_ru,
            draft.excerpt_kz,
            draft.excerpt_en,
            [item.model_dump(mode="json") for item in draft.body_ru],
            [item.model_dump(mode="json") for item in draft.body_kz],
            [item.model_dump(mode="json") for item in draft.body_en],
            [item.model_dump(mode="json") for item in draft.sources],
            media.image_url if media else "",
            media.alt_ru if media else "",
            media.alt_kz if media else "",
            media.alt_en if media else "",
            bool(media and media.rights_confirmed),
            draft.video_url,
            draft.published_at,
            row["created_by"],
        )
        gallery_rows: list[asyncpg.Record] = []
        for item in gallery_manifest:
            media_row = await connection.fetchrow(
                """
                SELECT ma.id, ma.sha256, ma.width, ma.height, ma.derivative_path,
                       ma.moderation_status,
                       (
                           SELECT c.status FROM consents c
                           WHERE c.subject_type='media' AND c.subject_id=ma.id
                             AND c.scope='media_publication'
                           ORDER BY c.updated_at DESC, c.created_at DESC, c.id DESC
                           LIMIT 1
                       ) AS consent_status
                FROM news_session_media nsm
                JOIN media_assets ma ON ma.id=nsm.media_id
                WHERE nsm.session_id=$1 AND nsm.media_id=$2 AND ma.deleted_at IS NULL
                """,
                row["session_id"],
                item.mediaId,
            )
            if (
                not media_row
                or media_row["sha256"] != item.sha256
                or media_row["width"] != item.width
                or media_row["height"] != item.height
                or not media_row["derivative_path"]
                or media_row["moderation_status"] != "approved"
                or media_row["consent_status"] != "granted"
            ):
                raise NewsProjectionRejected("news gallery media changed or lacks permission")
            gallery_rows.append(media_row)
        await connection.execute("DELETE FROM news_media_items WHERE news_id=$1", news_id)
        for sort_order, media_row in enumerate(gallery_rows):
            await connection.execute(
                """
                INSERT INTO news_media_items(
                    news_id, media_id, alt_ru, alt_kz, alt_en, sort_order
                ) VALUES ($1,$2,$3,$4,$5,$6)
                """,
                news_id,
                media_row["id"],
                draft.title_ru,
                draft.title_kz,
                draft.title_en,
                sort_order,
            )
        application_id = await connection.fetchval(
            """
            INSERT INTO news_projection_applications(
                draft_id, revision, news_id, content_hash, applied_by
            ) VALUES ($1,$2,$3,$4,$5) RETURNING id
            """,
            draft_id,
            revision,
            news_id,
            row["content_hash"],
            actor.user_id,
        )
        return NewsApplicationResult(
            application_id=application_id,
            draft_id=draft_id,
            revision=revision,
            news_id=news_id,
            slug=draft.slug,
            applied=True,
        )
