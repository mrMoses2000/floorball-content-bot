from __future__ import annotations

import copy
import re
from uuid import UUID

from floorball_bot.dialogue.evaluator import evaluate_gaps
from floorball_bot.dialogue.models import FieldType
from floorball_bot.dialogue.patches import validate_field_value
from floorball_bot.dialogue.repository import DialogueSpecRepository
from floorball_bot.errors import ValidationBlocked


def attachment_field(spec, fields: dict, path: str):
    parts = path.split(".")
    parent = next((f for f in spec.fields if f.id == parts[0]), None)
    if parent is None:
        raise ValidationBlocked("unknown attachment field")
    if len(parts) == 1 and parent.type == FieldType.FILE:
        return parent, fields, parent.id
    if parent.type == FieldType.RECORD and len(parts) == 2:
        record = fields.setdefault(parent.id, {})
        child_id = parts[1]
    elif parent.type == FieldType.RECORD_LIST and len(parts) == 3:
        if not re.fullmatch(r"0|[1-9][0-9]?", parts[1]):
            raise ValidationBlocked("invalid attachment item index")
        records = fields.get(parent.id) or []
        index = int(parts[1])
        if index >= len(records):
            raise ValidationBlocked("add the profile before attaching its portrait")
        record = records[index]
        child_id = parts[2]
    else:
        raise ValidationBlocked("field is not an attachment")
    child = next((f for f in parent.children if f.id == child_id), None)
    if child is None or child.type != FieldType.FILE or not isinstance(record, dict):
        raise ValidationBlocked("field is not an attachment")
    return parent, record, child.id


async def attach_session_media(
    connection,
    *,
    session_id: UUID,
    user_id: UUID,
    media_id: UUID,
    field_path: str,
    expected_record: dict | None = None,
) -> None:
    session = await connection.fetchrow(
        """SELECT s.workflow,s.status,s.definition_hash,m.structured_memory
           FROM conversation_sessions s JOIN conversation_memory m ON m.session_id=s.id
           WHERE s.id=$1 AND s.user_id=$2 FOR UPDATE OF s,m""",
        session_id,
        user_id,
    )
    if not session or session["status"] != "active":
        raise ValidationBlocked("attachment session is not active")
    loaded = DialogueSpecRepository().load(session["workflow"], sha256=session["definition_hash"])
    if loaded.sha256 != session["definition_hash"]:
        raise ValidationBlocked("attachment session definition changed")
    permitted = await connection.fetchval(
        """SELECT EXISTS(SELECT 1 FROM users u JOIN user_roles r ON r.user_id=u.id
           WHERE u.id=$1 AND u.active AND u.deleted_at IS NULL AND r.revoked_at IS NULL
           AND r.role_name=ANY($2::text[]))""",
        user_id,
        list(loaded.spec.allowed_roles),
    )
    if not permitted:
        raise ValidationBlocked("attachment actor no longer has access")
    media = await connection.fetchrow(
        "SELECT uploader_id,deleted_at,derivative_path FROM media_assets WHERE id=$1",
        media_id,
    )
    # A globally deduplicated image may have a different original uploader. Its
    # bytes still need evidence that this actor uploaded them into this session.
    if not media or media["deleted_at"] or not media["derivative_path"]:
        raise ValidationBlocked("attachment image is unavailable")
    memory = copy.deepcopy(session["structured_memory"] or {})
    fields = memory.setdefault("fields", {})
    parent, record, child_id = attachment_field(loaded.spec, fields, field_path)
    if expected_record is not None and record_identity(record) != expected_record:
        raise ValidationBlocked("profile changed while its image was processing; upload again")
    if field_path == "media.gallery":
        ids = [item for item in str(record.get(child_id) or "").split(",") if item]
        if str(media_id) not in ids:
            if len(ids) >= 15:
                raise ValidationBlocked("city gallery is limited to fifteen images")
            ids.append(str(media_id))
        record[child_id] = ",".join(ids)
    else:
        record[child_id] = str(media_id)
    validate_field_value(parent, fields[parent.id])
    await connection.execute(
        """INSERT INTO session_media_attachments(session_id,field_path,media_id)
           VALUES ($1,$2,$3) ON CONFLICT DO NOTHING""",
        session_id,
        field_path,
        media_id,
    )
    gaps = evaluate_gaps(loaded.spec, fields)
    memory["critical_missing"] = [
        g.field_id for g in (*gaps.required_to_start, *gaps.required_for_submit)
    ]
    await connection.execute(
        """UPDATE conversation_memory SET structured_memory=$2,revision=revision+1,
           updated_at=now() WHERE session_id=$1""",
        session_id,
        memory,
    )
    await connection.execute(
        """UPDATE conversation_sessions SET current_step=$2,revision=revision+1,
           last_activity_at=now(),updated_at=now() WHERE id=$1""",
        session_id,
        gaps.next_field_id or "ready_to_submit",
    )


async def reviewed_media(
    connection,
    *,
    session_id: UUID,
    field_path: str,
    value: str,
    granted: bool,
    actor_id: UUID,
    author_id: UUID,
    draft_id: UUID,
    guardian_confirmed: bool = False,
) -> list[dict]:
    if not value:
        return []
    try:
        ids = tuple(dict.fromkeys(UUID(item) for item in value.split(",")))
    except ValueError as exc:
        raise ValidationBlocked(
            "upload images through the bot; remote image URLs are not accepted"
        ) from exc
    if len(ids) > (15 if field_path == "media.gallery" else 1):
        raise ValidationBlocked("too many attachment images")
    rows = []
    for media_id in ids:
        row = await connection.fetchrow(
            """SELECT ma.* FROM media_assets ma JOIN session_media_attachments a
               ON a.media_id=ma.id WHERE a.session_id=$1 AND a.field_path=$2 AND ma.id=$3
               AND ma.deleted_at IS NULL AND ma.derivative_path IS NOT NULL
               AND ma.moderation_status IN ('pending','approved') FOR UPDATE OF ma""",
            session_id,
            field_path,
            media_id,
        )
        if row is None:
            raise ValidationBlocked("image is not attached to this reviewed session field")
        await connection.execute(
            """INSERT INTO consents(subject_type,subject_id,scope,status,evidence_private,
               legal_text_version,granted_by,reviewed_by,guardian_confirmed)
               VALUES ('media',$1,'media_publication',$2,$3,'bot-media-rights-v1',$4,$5,$6)""",
            media_id,
            "granted" if granted else "withdrawn",
            f"approved draft {draft_id}; field {field_path}",
            author_id,
            actor_id,
            guardian_confirmed,
        )
        if granted:
            await connection.execute(
                "UPDATE media_assets SET moderation_status='approved',updated_at=now() WHERE id=$1",
                media_id,
            )
            rows.append(dict(row))
    return rows


def public_media_path(row: dict) -> str:
    return f"/assets/content/{row['sha256']}.webp"


def record_identity(record: dict) -> dict:
    return {key: record.get(key) for key in ("profile_key", "name", "full_name")}


def upload_targets(spec, fields: dict, language: str) -> list[dict]:
    targets = []
    for parent in spec.fields:
        records = fields.get(parent.id) or [] if parent.type == FieldType.RECORD_LIST else [None]
        children = (
            parent.children
            if parent.type in {FieldType.RECORD, FieldType.RECORD_LIST}
            else [parent]
        )
        for index, record in enumerate(records):
            for child in children:
                if child.type != FieldType.FILE:
                    continue
                label = child.question.kz if language == "kz" else child.question.ru
                path = parent.id
                if parent.type == FieldType.RECORD_LIST:
                    path += f".{index}"
                    name = (record or {}).get("name") or (record or {}).get("full_name") or ""
                    label = f"{index + 1}. {name}: {label}"
                if child is not parent:
                    path += f".{child.id}"
                targets.append({"path": path, "label": label})
    return targets


async def select_media_links(
    connection, *, entity_type: str, entity_id: UUID, purpose: str, images: list[dict]
) -> None:
    await connection.execute(
        """UPDATE media_links SET selected_for_publication=FALSE
           WHERE entity_type=$1 AND entity_id=$2 AND purpose=$3""",
        entity_type,
        entity_id,
        purpose,
    )
    for index, image in enumerate(images):
        await connection.execute(
            """INSERT INTO media_links(media_id,entity_type,entity_id,purpose,
                   selected_for_publication,sort_order) VALUES ($1,$2,$3,$4,TRUE,$5)
               ON CONFLICT(media_id,entity_type,entity_id,purpose) DO UPDATE SET
                   selected_for_publication=TRUE,sort_order=$5""",
            image["id"],
            entity_type,
            entity_id,
            purpose,
            index,
        )
