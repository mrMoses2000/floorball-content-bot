from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import asyncpg
from pydantic import BaseModel, ConfigDict

from floorball_bot.attachments import public_media_path, reviewed_media, select_media_links
from floorball_bot.auth import require_roles
from floorball_bot.dialogue.evaluator import evaluate_gaps
from floorball_bot.dialogue.patches import validate_field_value
from floorball_bot.dialogue.repository import DialogueSpecRepository
from floorball_bot.domain import Actor, PublicFederation, Role
from floorball_bot.exporters import project_federation_payload
from floorball_bot.projection.apply import ProjectionRejected
from floorball_bot.workflow import canonical_hash


class FederationApplicationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    application_id: UUID
    draft_id: UUID
    revision: int
    applied: bool


def _localized(record: dict, mapping: dict[str, str], suffix: str) -> dict:
    return {
        target + suffix: record[source] for source, target in mapping.items() if source in record
    }


def _merge_items(existing: list[dict], incoming: list[dict], *, identity: str) -> list[dict]:
    identifiers = [item[identity] for item in incoming]
    if len(identifiers) != len(set(identifiers)):
        raise ProjectionRejected("ambiguous duplicate federation item identity")
    result = deepcopy(existing)
    for item in incoming:
        matches = [old for old in result if old.get(identity) == item[identity]]
        if len(matches) > 1:
            raise ProjectionRejected("ambiguous federation item identity")
        if matches:
            matches[0].update(item)
        else:
            result.append(item)
    return result


def plan_federation_projection(
    workflow: str, fields: dict[str, Any], existing: dict[str, Any], *, spec=None
) -> dict[str, Any]:
    """Preserve the other source language and unrelated sections; never translate facts."""
    if workflow not in {"strategy", "history", "leadership"}:
        raise ProjectionRejected("unsupported federation dialogue")
    loaded = DialogueSpecRepository().load(workflow)
    spec = spec or loaded.spec
    definitions = {field.id: field for field in spec.fields}
    if set(fields) - definitions.keys():
        raise ProjectionRejected("unknown federation field")
    fields = {key: validate_field_value(definitions[key], value) for key, value in fields.items()}
    if not evaluate_gaps(spec, fields).can_publish or fields.get("text_consent") is not True:
        raise ProjectionRejected("federation draft is incomplete or lacks text consent")
    suffix = {"ru": "Ru", "kz": "Kz"}.get(fields.get("source_language"))
    if suffix is None:
        raise ProjectionRejected("federation source language must be ru or kz")
    result = deepcopy(existing)
    if workflow == "strategy":
        mission = result.setdefault("mission", {})
        mission.update(_localized(fields, {"mission": "statement", "vision": "vision"}, suffix))
        for key, mapping in {
            "values": {"title": "title", "description": "description"},
            "goals": {
                "title": "title",
                "description": "description",
                "kpi": "kpi",
                "deadline": "deadline",
                "owner": "owner",
            },
        }.items():
            if key not in fields:
                continue
            current = mission.setdefault(key, [])
            for index, item in enumerate(fields[key]):
                # The reviewed list order aligns language variants of the same list.
                if index >= len(current):
                    current.append({"id": f"{key}-{index + 1}"})
                current[index].update(_localized(item, mapping, suffix))
    elif workflow == "history":
        for field, section, mapping in (
            (
                "history_entries",
                "history",
                {
                    "period": "year",
                    "title": "title",
                    "description": "description",
                },
            ),
            (
                "achievements",
                "achievements",
                {
                    "period": "year",
                    "title": "title",
                    "description": "description",
                },
            ),
            ("roadmap", "roadmap", {"phase": "phase", "label": "label", "items": "items"}),
        ):
            incoming = []
            for index, item in enumerate(fields.get(field, [])):
                value = _localized(item, mapping, suffix)
                if section == "roadmap":
                    value.update(id=f"roadmap-{index + 1}", done=item["done"] == "да")
                else:
                    value["sourceUrl"] = item["source_url"]
                    # Source and period identify the same historical fact in either language.
                    value["id"] = canonical_hash(
                        {
                            "source": item["source_url"],
                            "period": item["period"],
                        }
                    )[:32]
                    for old in result.get(section, []):
                        if old.get("sourceUrl") == value["sourceUrl"] and item["period"] in {
                            old.get("yearRu"),
                            old.get("yearKz"),
                        }:
                            value["id"] = old["id"]
                incoming.append(value)
            result[section] = _merge_items(result.get(section, []), incoming, identity="id")
    else:
        incoming = []
        seen = set()
        for profile in fields["profiles"]:
            key = profile["profile_key"]
            if key in seen:
                raise ProjectionRejected("duplicate leadership profile key")
            seen.add(key)
            if profile.get("portrait_url"):
                raise ProjectionRejected("upload leadership portraits through the bot")
            public = profile["public_contacts"] == "да"
            incoming.append(
                {
                    "id": key,
                    **_localized(
                        profile,
                        {
                            "name": "name",
                            "role": "role",
                            "bio": "bio",
                            "focus": "focus",
                        },
                        suffix,
                    ),
                    "email": profile.get("email", "") if public else "",
                    "phone": profile.get("phone", "") if public else "",
                }
            )
        result["leadership"] = _merge_items(result.get("leadership", []), incoming, identity="id")
    return PublicFederation.model_validate(result).model_dump(mode="json")


async def apply_approved_federation_draft(
    pool: asyncpg.Pool, *, draft_id: UUID, actor: Actor
) -> FederationApplicationResult:
    require_roles(actor, Role.REVIEWER, Role.SUPERADMIN)
    async with pool.acquire() as connection, connection.transaction():
        # Serialize every federation writer, including different language submissions.
        await connection.execute("SELECT pg_advisory_xact_lock(hashtext('federation-projection'))")
        row = await connection.fetchrow(
            """
            SELECT d.status, d.current_revision, d.approved_revision, r.content, r.content_hash,
                   s.workflow, s.definition_version, s.definition_hash, s.context_hash,
                   s.user_id, s.id AS session_id
            FROM drafts d
            JOIN conversation_sessions s ON s.id=d.session_id
            JOIN draft_revisions r ON r.draft_id=d.id AND r.revision=d.approved_revision
            WHERE d.id=$1 FOR UPDATE OF d
            """,
            draft_id,
        )
        if not row or row["status"] != "approved":
            raise ProjectionRejected("federation draft must be approved")
        revision = row["approved_revision"]
        workflow = row["workflow"]
        if revision != row["current_revision"] or workflow not in {
            "strategy",
            "history",
            "leadership",
        }:
            raise ProjectionRejected("federation approval is stale or has another workflow")
        try:
            loaded = DialogueSpecRepository().load(workflow, sha256=row["definition_hash"])
        except ValueError as exc:
            raise ProjectionRejected("federation revision pins changed") from exc
        content = row["content"]
        if (
            row["definition_hash"] != loaded.sha256
            or row["definition_version"] != loaded.spec.version
            or not isinstance(content, dict)
            or canonical_hash(content) != row["content_hash"]
            or content.get("dialogue_mode") != workflow
            or content.get("definition_hash") != loaded.sha256
            or content.get("definition_version") != loaded.spec.version
            or content.get("context_hash") != row["context_hash"]
        ):
            raise ProjectionRejected("federation revision pins changed")
        previous = await connection.fetchval(
            "SELECT id FROM federation_projection_applications WHERE draft_id=$1 AND revision=$2",
            draft_id,
            revision,
        )
        if previous:
            return FederationApplicationResult(
                application_id=previous,
                draft_id=draft_id,
                revision=revision,
                applied=False,
            )
        now = datetime.now(UTC)
        before = (await project_federation_payload(connection, generated_at=now)).federation
        fields = content.get("fields")
        if not isinstance(fields, dict):
            raise ProjectionRejected("federation fields must be an object")
        projected = plan_federation_projection(
            workflow, fields, before.model_dump(mode="json"), spec=loaded.spec
        )
        if workflow == "leadership":
            for index, profile in enumerate(fields["profiles"]):
                leader = next(
                    item for item in projected["leadership"] if item["id"] == profile["profile_key"]
                )
                leader_id = await _save_leader(
                    connection, leader, profile, actor, row["user_id"], draft_id
                )
                portraits = await reviewed_media(
                    connection,
                    session_id=row["session_id"],
                    field_path=f"profiles.{index}.portrait",
                    value=profile.get("portrait", ""),
                    granted=profile["profile_portrait_permission"] == "да",
                    actor_id=actor.user_id,
                    author_id=row["user_id"],
                    draft_id=draft_id,
                )
                if profile.get("portrait") or profile["profile_portrait_permission"] != "да":
                    await select_media_links(
                        connection,
                        entity_type="leadership",
                        entity_id=leader_id,
                        purpose="portrait",
                        images=portraits,
                    )
                if portraits or profile["profile_portrait_permission"] != "да":
                    await connection.execute(
                        "UPDATE leadership_profiles SET media_id=$2,photo_url=$3 WHERE id=$1",
                        leader_id,
                        portraits[0]["id"] if portraits else None,
                        public_media_path(portraits[0]) if portraits else "",
                    )
                await connection.execute(
                    """INSERT INTO consents(subject_type,subject_id,scope,status,
                       evidence_private,legal_text_version,granted_by,reviewed_by)
                       VALUES ('leadership',$1,'portrait',$2,$3,'bot-profile-rights-v1',$4,$5)""",
                    leader_id,
                    "granted" if profile["profile_portrait_permission"] == "да" else "withdrawn",
                    f"approved draft {draft_id}",
                    row["user_id"],
                    actor.user_id,
                )
        else:
            sections = (
                ("mission",) if workflow == "strategy" else ("history", "achievements", "roadmap")
            )
            for section in sections:
                await connection.execute(
                    """
                    INSERT INTO federation_sections(
                        section_key,item_key,public_content,created_by,updated_by
                    )
                    VALUES ($1,'main',$2::jsonb,$3,$3)
                    ON CONFLICT (section_key,item_key) DO UPDATE
                    SET public_content=EXCLUDED.public_content, deleted_at=NULL,
                        revision=federation_sections.revision+1, updated_by=$3, updated_at=now()
                    """,
                    section,
                    projected[section],
                    actor.user_id,
                )
        after = (await project_federation_payload(connection, generated_at=now)).federation
        application = await connection.fetchval(
            """
            INSERT INTO federation_projection_applications(
                draft_id,revision,workflow,content_hash,before_hash,after_hash,applied_by
            ) VALUES ($1,$2,$3,$4,$5,$6,$7) RETURNING id
            """,
            draft_id,
            revision,
            workflow,
            row["content_hash"],
            canonical_hash(before.model_dump(mode="json")),
            canonical_hash(after.model_dump(mode="json")),
            actor.user_id,
        )
        return FederationApplicationResult(
            application_id=application,
            draft_id=draft_id,
            revision=revision,
            applied=True,
        )


async def _save_leader(connection, leader, profile, actor, author_id, draft_id) -> UUID:
    key = profile["profile_key"]
    source_key = (
        await connection.fetchval(
            "SELECT source_key FROM leadership_profiles WHERE source_key=ANY($1::text[])",
            [f"bundle:leader:{key}", f"dialogue:leader:{key}"],
        )
        or f"dialogue:leader:{key}"
    )
    leader_id = await connection.fetchval(
        """
        INSERT INTO leadership_profiles(
            source_key,name_ru,name_kz,role_ru,role_kz,bio_ru,bio_kz,focus_ru,focus_kz,
            email_private,phone_private,contacts_are_public,created_by,updated_by
        ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$13)
        ON CONFLICT (source_key) WHERE source_key IS NOT NULL DO UPDATE
        SET name_ru=$2,name_kz=$3,role_ru=$4,role_kz=$5,bio_ru=$6,bio_kz=$7,focus_ru=$8,focus_kz=$9,
            email_private=$10,phone_private=$11,contacts_are_public=$12,
            active=TRUE,deleted_at=NULL,revision=leadership_profiles.revision+1,
            updated_by=$13,updated_at=now()
        RETURNING id
        """,
        source_key,
        leader["nameRu"],
        leader["nameKz"],
        leader["roleRu"],
        leader["roleKz"],
        leader["bioRu"],
        leader["bioKz"],
        leader["focusRu"],
        leader["focusKz"],
        profile.get("email", ""),
        profile.get("phone", ""),
        profile["public_contacts"] == "да",
        actor.user_id,
    )
    await connection.execute(
        """
        INSERT INTO consents(
            subject_type,subject_id,scope,status,evidence_private,granted_by,reviewed_by
        )
        VALUES ('leadership',$1,'contact',$2,$3,$4,$5)
        """,
        leader_id,
        "granted" if profile["public_contacts"] == "да" else "withdrawn",
        f"approved draft {draft_id}; profile {key}",
        author_id,
        actor.user_id,
    )
    return leader_id
