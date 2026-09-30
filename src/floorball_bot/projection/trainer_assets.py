from __future__ import annotations

import hashlib
import re

from floorball_bot.attachments import public_media_path, reviewed_media, select_media_links
from floorball_bot.errors import ValidationBlocked


async def apply_trainer_assets(
    connection, *, fields, city_id, city_slug, session_id, draft_id, actor_id, author_id, language
) -> None:
    media = fields.get("media") or {}
    rights = media.get("permission") == "да" and media.get("minors_permission") in {
        "да",
        "нет несовершеннолетних",
    }
    if media.get("hero"):
        images = await reviewed_media(
            connection,
            session_id=session_id,
            field_path="media.hero",
            value=media["hero"],
            granted=rights,
            actor_id=actor_id,
            author_id=author_id,
            draft_id=draft_id,
            guardian_confirmed=media.get("minors_permission") == "да",
        )
        await select_media_links(
            connection, entity_type="city", entity_id=city_id, purpose="hero", images=images
        )
        await connection.execute(
            "UPDATE cities SET hero_url=$2 WHERE id=$1",
            city_id,
            public_media_path(images[0]) if images else "/assets/heroes/clubs.png",
        )
        await connection.execute(
            "UPDATE city_content SET hero_media_id=$2 WHERE city_id=$1",
            city_id,
            images[0]["id"] if images else None,
        )
    if "gallery" in media:
        images = await reviewed_media(
            connection,
            session_id=session_id,
            field_path="media.gallery",
            value=media.get("gallery") or "",
            granted=rights,
            actor_id=actor_id,
            author_id=author_id,
            draft_id=draft_id,
            guardian_confirmed=media.get("minors_permission") == "да",
        )
        await select_media_links(
            connection, entity_type="city", entity_id=city_id, purpose="gallery", images=images
        )
        await connection.execute(
            "UPDATE city_gallery_items SET deleted_at=now() WHERE city_id=$1 "
            "AND source_key LIKE 'dialogue:gallery:%'",
            city_id,
        )
        for index, image in enumerate(images):
            caption = str(media.get("caption") or "")
            await connection.execute(
                """INSERT INTO city_gallery_items(city_id,source_key,public_id,src,thumbnail,
                   width,height,caption_ru,caption_kz,alt_ru,alt_kz,sort_order)
                   VALUES ($1,$2,$3,$4,$4,$5,$6,$7,$8,$9,$10,$11)
                   ON CONFLICT (city_id,source_key) DO UPDATE SET src=$4,thumbnail=$4,
                   caption_ru=CASE WHEN $12='ru' THEN $7 ELSE city_gallery_items.caption_ru END,
                   caption_kz=CASE WHEN $12='kz' THEN $8 ELSE city_gallery_items.caption_kz END,
                   alt_ru=CASE WHEN $12='ru' THEN $9 ELSE city_gallery_items.alt_ru END,
                   alt_kz=CASE WHEN $12='kz' THEN $10 ELSE city_gallery_items.alt_kz END,
                   sort_order=$11,deleted_at=NULL,updated_at=now()""",
                city_id,
                f"dialogue:gallery:{image['sha256']}",
                str(image["id"]),
                public_media_path(image),
                image["width"],
                image["height"],
                caption if language == "ru" else "",
                caption if language == "kz" else "",
                caption[:300] if language == "ru" else "",
                caption[:300] if language == "kz" else "",
                index,
                language,
            )
    keys = []
    for index, profile in enumerate(fields.get("players") or []):
        # v1 drafts remain applicable; new v2 asks for an explicit stable key so
        # spelling corrections, translated names and city changes preserve identity.
        key = profile.get("profile_key") or (
            f"legacy-{city_slug[:24]}-"
            + hashlib.sha256(str(profile.get("name") or "").casefold().encode()).hexdigest()[:24]
        )
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,79}", key) or key in keys:
            raise ValidationBlocked("player keys must be unique permanent lowercase codes")
        keys.append(key)
        name = str(profile.get("name") or "")
        bio = str(profile.get("bio") or "")
        minor = profile.get("minor", True)
        guardian = (profile.get("guardian_permission") == "да"
                    if "guardian_permission" in profile else media.get("minors_permission") == "да")
        granted = profile.get("publish_permission") == "да" and (not minor or guardian)
        if granted and (not name or not bio):
            raise ValidationBlocked("a public player requires name and biography")
        source_key = f"dialogue:player:{key}"
        player_id = await connection.fetchval(
            """INSERT INTO players(source_key,name_ru,name_kz,bio_ru,bio_kz,position_ru,
               position_kz,selected_for_publication,approved_for_publication,created_by,updated_by)
               VALUES ($1,COALESCE($2,''),COALESCE($3,''),COALESCE($4,''),COALESCE($5,''),
               COALESCE($6,''),COALESCE($7,''),$8,$8,$9,$9)
               ON CONFLICT (source_key) WHERE source_key IS NOT NULL DO UPDATE SET
               name_ru=COALESCE($2,players.name_ru),name_kz=COALESCE($3,players.name_kz),
               bio_ru=COALESCE($4,players.bio_ru),bio_kz=COALESCE($5,players.bio_kz),
               position_ru=COALESCE($6,players.position_ru),
               position_kz=COALESCE($7,players.position_kz),selected_for_publication=$8,
               approved_for_publication=$8,status='active',deleted_at=NULL,
               revision=players.revision+1,updated_by=$9,updated_at=now() RETURNING id""",
            source_key,
            name if language == "ru" else None,
            name if language == "kz" else None,
            bio if language == "ru" else None,
            bio if language == "kz" else None,
            str(profile.get("position") or "") if language == "ru" else None,
            str(profile.get("position") or "") if language == "kz" else None,
            granted,
            actor_id,
        )
        await connection.execute(
            """UPDATE player_city_memberships SET valid_to=GREATEST(CURRENT_DATE-1,valid_from)
               WHERE player_id=$1 AND city_id<>$2 AND valid_to IS NULL
                 AND source_key LIKE 'dialogue:player:%'""",
            player_id,
            city_id,
        )
        await connection.execute(
            """INSERT INTO player_city_memberships(player_id,city_id,valid_from,source_key)
               VALUES ($1,$2,CURRENT_DATE,$3) ON CONFLICT (source_key)
               WHERE source_key IS NOT NULL DO UPDATE SET city_id=$2,valid_to=NULL""",
            player_id,
            city_id,
            f"{source_key}:city:{city_slug}",
        )
        await connection.execute(
            """INSERT INTO consents(subject_type,subject_id,scope,status,evidence_private,
               legal_text_version,granted_by,reviewed_by,minor,guardian_confirmed)
               VALUES ('player',$1,'name_bio',$2,$3,'bot-profile-rights-v1',$4,$5,$6,$7)""",
            player_id,
            "granted" if granted else "withdrawn",
            f"approved draft {draft_id}; player {key}",
            author_id,
            actor_id,
            minor,
            guardian,
        )
        if profile.get("photo"):
            photos = await reviewed_media(
                connection,
                session_id=session_id,
                field_path=f"players.{index}.photo",
                value=profile["photo"],
                granted=granted and rights,
                actor_id=actor_id,
                author_id=author_id,
                draft_id=draft_id,
                guardian_confirmed=guardian or media.get("minors_permission") == "да",
            )
            await select_media_links(
                connection,
                entity_type="player",
                entity_id=player_id,
                purpose="portrait",
                images=photos,
            )
            await connection.execute(
                "UPDATE players SET photo_url=$2 WHERE id=$1",
                player_id,
                public_media_path(photos[0]) if photos else "",
            )
            await connection.execute(
                """INSERT INTO consents(subject_type,subject_id,scope,status,evidence_private,
                   legal_text_version,granted_by,reviewed_by) VALUES
                   ('player',$1,'portrait',$2,$3,'bot-profile-rights-v1',$4,$5)""",
                player_id,
                "granted" if photos else "withdrawn",
                f"approved draft {draft_id}; player {key}",
                author_id,
                actor_id,
            )
