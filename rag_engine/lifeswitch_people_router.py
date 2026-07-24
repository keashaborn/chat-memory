from __future__ import annotations

import os
import uuid
import json
import decimal
import datetime as _dt
import hashlib
import secrets
import asyncpg

from fastapi import APIRouter, HTTPException, Query, Body, Request
from rag_engine.lifeswitch_auth import require_actor_matches_owner
from fastapi.responses import JSONResponse

router = APIRouter()

DSN = os.getenv("POSTGRES_DSN") or ""
if not DSN:
    raise RuntimeError("POSTGRES_DSN missing")

SCHEMA = os.getenv("LIFESWITCH_PEOPLE_SCHEMA", "lifeswitch_people")


def _json_safe(v):
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, (_dt.datetime, _dt.date)):
        return v.isoformat()
    return v


def _row_to_jsonable(r):
    if r is None:
        return None
    d = dict(r)
    out = {}
    for k, v in d.items():
        v = _json_safe(v)
        if isinstance(v, str) and k in {"metadata"}:
            try:
                v = json.loads(v)
            except Exception:
                pass
        out[k] = v
    return out


def _as_uuid(s: str, name: str) -> str:
    try:
        return str(uuid.UUID(str(s)))
    except Exception:
        raise HTTPException(status_code=400, detail=f"invalid {name}")


def _clean_text(v, max_len: int | None = None) -> str:
    s = str(v or "").strip()
    if max_len is not None and len(s) > max_len:
        s = s[:max_len]
    return s


def _direct_pair(a: str, b: str) -> tuple[str, str]:
    au = uuid.UUID(a)
    bu = uuid.UUID(b)
    low, high = sorted([au, bu])
    return str(low), str(high)


def _new_invite_token() -> str:
    return secrets.token_urlsafe(32)


def _token_hash(token: str) -> str:
    raw = str(token or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="token required")
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _safe_invitation_row(row):
    out = _row_to_jsonable(row)
    if isinstance(out, dict):
        out.pop("token_hash", None)
    return out


async def _db():
    return await asyncpg.connect(DSN)


async def _assert_member(conn, conversation_id: str, owner_user_id: str) -> None:
    row = await conn.fetchrow(
        f"""
        select conversation_member_id
        from {SCHEMA}.conversation_member
        where conversation_id=$1::uuid
          and user_id=$2::uuid
          and is_active=true
        limit 1
        """,
        conversation_id,
        owner_user_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="conversation not found")


async def _grant_default_messaging_permissions(
    conn,
    relationship_id: str,
    user_a: str,
    user_b: str,
) -> None:
    await conn.execute(
        f"""
        insert into {SCHEMA}.relationship_permission as rp (
          relationship_id,
          grantor_user_id,
          grantee_user_id,
          permission_scope,
          permission_level,
          is_enabled,
          notes
        )
        values
          ($1::uuid, $2::uuid, $3::uuid, 'messages:send', 'comment', true,
           'Default messaging permission for accepted relationship.'),
          ($1::uuid, $3::uuid, $2::uuid, 'messages:send', 'comment', true,
           'Default messaging permission for accepted relationship.')
        on conflict (relationship_id, grantor_user_id, grantee_user_id, permission_scope)
        do nothing
        """,
        relationship_id,
        user_a,
        user_b,
    )


async def _reset_reaccepted_relationship_permissions(
    conn,
    relationship_id: str,
    user_a: str,
    user_b: str,
) -> None:
    await conn.execute(
        f"""
        update {SCHEMA}.relationship_permission
           set permission_level='none',
               is_enabled=false,
               updated_at=now()
         where relationship_id=$1::uuid
        """,
        relationship_id,
    )
    await conn.execute(
        f"""
        insert into {SCHEMA}.relationship_permission as rp (
          relationship_id,
          grantor_user_id,
          grantee_user_id,
          permission_scope,
          permission_level,
          is_enabled,
          notes
        )
        values
          ($1::uuid, $2::uuid, $3::uuid, 'messages:send', 'comment', true,
           'Default messaging permission for reaccepted relationship.'),
          ($1::uuid, $3::uuid, $2::uuid, 'messages:send', 'comment', true,
           'Default messaging permission for reaccepted relationship.')
        on conflict (relationship_id, grantor_user_id, grantee_user_id, permission_scope)
        do update set
          permission_level=excluded.permission_level,
          is_enabled=excluded.is_enabled,
          notes=excluded.notes,
          updated_at=now()
        """,
        relationship_id,
        user_a,
        user_b,
    )


async def _assert_message_send_authorized(
    conn,
    sender_user_id: str,
    recipient_user_id: str,
) -> str:
    row = await conn.fetchrow(
        f"""
        select r.relationship_id
        from {SCHEMA}.relationship r
        join {SCHEMA}.relationship_permission rp
          on rp.relationship_id=r.relationship_id
         and rp.grantor_user_id=$2::uuid
         and rp.grantee_user_id=$1::uuid
         and rp.permission_scope='messages:send'
         and rp.permission_level <> 'none'
         and rp.is_enabled=true
        where (
          (r.requester_user_id=$1::uuid and r.addressee_user_id=$2::uuid)
          or
          (r.requester_user_id=$2::uuid and r.addressee_user_id=$1::uuid)
        )
          and r.status='accepted'
        limit 1
        for share of r, rp
        """,
        sender_user_id,
        recipient_user_id,
    )
    if not row:
        raise HTTPException(status_code=403, detail="messaging permission required")
    return str(row["relationship_id"])


async def _assert_conversation_send_authorized(
    conn,
    conversation_id: str,
    sender_user_id: str,
) -> None:
    await _assert_member(conn, conversation_id, sender_user_id)
    recipients = await conn.fetch(
        f"""
        select cm.user_id
        from {SCHEMA}.conversation c
        join {SCHEMA}.conversation_member cm
          on cm.conversation_id=c.conversation_id
         and cm.is_active=true
        where c.conversation_id=$1::uuid
          and c.is_active=true
          and cm.user_id <> $2::uuid
        for share of c, cm
        """,
        conversation_id,
        sender_user_id,
    )
    if not recipients:
        raise HTTPException(status_code=409, detail="conversation has no active recipient")
    for recipient in recipients:
        await _assert_message_send_authorized(
            conn,
            sender_user_id,
            str(recipient["user_id"]),
        )


@router.post("/invitations/create")
async def create_invitation(
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
    relationship_kind: str = Query("friend"),
    label: str = Body(""),
    notes: str = Body(""),
):
    owner = require_actor_matches_owner(req, owner_user_id)

    relationship_kind = _clean_text(relationship_kind, 80) or "friend"
    if relationship_kind not in {"friend", "training_partner", "plan_helper", "coach"}:
        raise HTTPException(status_code=400, detail="invalid relationship_kind")

    token = _new_invite_token()
    thash = _token_hash(token)

    conn = await _db()
    try:
        row = await conn.fetchrow(
            f"""
            insert into {SCHEMA}.invitation (
              token_hash,
              created_by_user_id,
              relationship_kind,
              label,
              notes,
              status
            )
            values ($1, $2::uuid, $3, $4, $5, 'pending')
            returning
              invitation_id,
              token_hash,
              created_by_user_id,
              accepted_by_user_id,
              relationship_kind,
              label,
              notes,
              status,
              expires_at,
              accepted_at,
              revoked_at,
              created_at,
              updated_at
            """,
            thash,
            owner,
            relationship_kind,
            _clean_text(label, 200),
            _clean_text(notes, 2000),
        )

        out = _safe_invitation_row(row)
        out["token"] = token
        return JSONResponse(out)
    finally:
        await conn.close()


@router.get("/invitations")
async def list_invitations(
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
    include_inactive: int = Query(0, ge=0, le=1),
):
    owner = require_actor_matches_owner(req, owner_user_id)
    status_filter = "" if include_inactive else "and i.status='pending'"

    conn = await _db()
    try:
        rows = await conn.fetch(
            f"""
            select
              i.invitation_id,
              i.created_by_user_id,
              coalesce(creator.display_name, i.created_by_user_id::text) as creator_display_name,
              i.accepted_by_user_id,
              coalesce(accepted.display_name, '') as accepted_display_name,
              i.relationship_kind,
              i.label,
              i.notes,
              i.status,
              i.expires_at,
              i.accepted_at,
              i.revoked_at,
              i.created_at,
              i.updated_at
            from {SCHEMA}.invitation i
            left join {SCHEMA}.user_profile creator
              on creator.user_id=i.created_by_user_id
            left join {SCHEMA}.user_profile accepted
              on accepted.user_id=i.accepted_by_user_id
            where i.created_by_user_id=$1::uuid
              {status_filter}
            order by i.created_at desc
            limit 100
            """,
            owner,
        )
        return JSONResponse([_row_to_jsonable(r) for r in rows])
    finally:
        await conn.close()


@router.get("/invitations/preview")
async def preview_invitation(
    token: str = Query(..., min_length=10),
):
    thash = _token_hash(token)

    conn = await _db()
    try:
        row = await conn.fetchrow(
            f"""
            select
              i.invitation_id,
              i.created_by_user_id,
              coalesce(creator.display_name, i.created_by_user_id::text) as creator_display_name,
              i.relationship_kind,
              i.label,
              i.notes,
              i.status,
              i.expires_at,
              i.accepted_at,
              i.revoked_at,
              i.created_at,
              i.updated_at
            from {SCHEMA}.invitation i
            left join {SCHEMA}.user_profile creator
              on creator.user_id=i.created_by_user_id
            where i.token_hash=$1
            limit 1
            """,
            thash,
        )

        if not row:
            raise HTTPException(status_code=404, detail="invitation not found")

        out = _row_to_jsonable(row)
        if out.get("status") == "pending" and str(out.get("expires_at") or ""):
            # The accept route enforces expiration. Preview reports current row.
            pass
        return JSONResponse(out)
    finally:
        await conn.close()


@router.post("/invitations/accept")
async def accept_invitation(
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
    token: str = Query(..., min_length=10),
):
    accepter = require_actor_matches_owner(req, owner_user_id)
    thash = _token_hash(token)

    conn = await _db()
    invitation_expired = False
    row = None
    updated_inv = None
    try:
        async with conn.transaction():
            inv = await conn.fetchrow(
                f"""
                select
                  invitation_id,
                  created_by_user_id,
                  accepted_by_user_id,
                  relationship_kind,
                  label,
                  notes,
                  status,
                  expires_at,
                  created_at
                from {SCHEMA}.invitation
                where token_hash=$1
                for update
                """,
                thash,
            )

            if not inv:
                raise HTTPException(status_code=404, detail="invitation not found")

            inviter = str(inv["created_by_user_id"])
            if inviter == accepter:
                raise HTTPException(status_code=400, detail="cannot accept your own invitation")

            if str(inv["status"]) != "pending":
                raise HTTPException(status_code=400, detail=f"invitation is {inv['status']}")

            expires_at = inv["expires_at"]
            if expires_at and expires_at < _dt.datetime.now(_dt.timezone.utc):
                await conn.execute(
                    f"""
                    update {SCHEMA}.invitation
                       set status='expired',
                           updated_at=now()
                     where invitation_id=$1::uuid
                    """,
                    str(inv["invitation_id"]),
                )
                invitation_expired = True
            else:
                existing = await conn.fetchrow(
                    f"""
                    select relationship_id, status, updated_at
                    from {SCHEMA}.relationship
                    where (
                      (requester_user_id=$1::uuid and addressee_user_id=$2::uuid)
                      or
                      (requester_user_id=$2::uuid and addressee_user_id=$1::uuid)
                    )
                    limit 1
                    for update
                    """,
                    inviter,
                    accepter,
                )
                if existing and str(existing["status"]) == "blocked":
                    raise HTTPException(status_code=409, detail="relationship is blocked")
                if existing and str(existing["status"]) == "accepted":
                    raise HTTPException(status_code=409, detail="relationship is already accepted")
                if (
                    existing
                    and str(existing["status"]) == "revoked"
                    and inv["created_at"] <= existing["updated_at"]
                ):
                    raise HTTPException(
                        status_code=409,
                        detail="invitation predates the relationship disconnect",
                    )

                row = await conn.fetchrow(
                    f"""
                    insert into {SCHEMA}.relationship as existing_relationship (
                      requester_user_id,
                      addressee_user_id,
                      status,
                      relationship_kind,
                      label,
                      notes
                    )
                    values ($1::uuid, $2::uuid, 'accepted', $3, $4, $5)
                    on conflict (
                      least(requester_user_id, addressee_user_id),
                      greatest(requester_user_id, addressee_user_id)
                    )
                    do update set
                      status='accepted',
                      relationship_kind=excluded.relationship_kind,
                      label=excluded.label,
                      notes=excluded.notes,
                      updated_at=now()
                    where existing_relationship.status in ('pending', 'revoked')
                    returning
                      relationship_id,
                      requester_user_id,
                      addressee_user_id,
                      status,
                      relationship_kind,
                      label,
                      notes,
                      created_at,
                      updated_at
                    """,
                    inviter,
                    accepter,
                    str(inv["relationship_kind"]),
                    _clean_text(inv["label"], 200),
                    _clean_text(inv["notes"], 2000),
                )
                if not row:
                    raise HTTPException(status_code=409, detail="relationship cannot be accepted")

                if existing is not None:
                    await _reset_reaccepted_relationship_permissions(
                        conn,
                        str(row["relationship_id"]),
                        inviter,
                        accepter,
                    )
                else:
                    await _grant_default_messaging_permissions(
                        conn,
                        str(row["relationship_id"]),
                        inviter,
                        accepter,
                    )

                updated_inv = await conn.fetchrow(
                    f"""
                    update {SCHEMA}.invitation
                       set status='accepted',
                           accepted_by_user_id=$2::uuid,
                           accepted_at=now(),
                           updated_at=now()
                     where invitation_id=$1::uuid
                    returning
                      invitation_id,
                      created_by_user_id,
                      accepted_by_user_id,
                      relationship_kind,
                      label,
                      notes,
                      status,
                      expires_at,
                      accepted_at,
                      revoked_at,
                      created_at,
                      updated_at
                    """,
                    str(inv["invitation_id"]),
                    accepter,
                )

        if invitation_expired:
            raise HTTPException(status_code=410, detail="invitation expired")

        return JSONResponse({
            "invitation": _row_to_jsonable(updated_inv),
            "relationship": _row_to_jsonable(row),
        })
    finally:
        await conn.close()


@router.post("/invitations/{invitation_id}/revoke")
async def revoke_invitation(
    invitation_id: str,
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
):
    iid = _as_uuid(invitation_id, "invitation_id")
    owner = require_actor_matches_owner(req, owner_user_id)

    conn = await _db()
    try:
        row = await conn.fetchrow(
            f"""
            update {SCHEMA}.invitation
               set status='revoked',
                   revoked_at=now(),
                   updated_at=now()
             where invitation_id=$1::uuid
               and created_by_user_id=$2::uuid
               and status='pending'
            returning
              invitation_id,
              created_by_user_id,
              accepted_by_user_id,
              relationship_kind,
              label,
              notes,
              status,
              expires_at,
              accepted_at,
              revoked_at,
              created_at,
              updated_at
            """,
            iid,
            owner,
        )

        if not row:
            raise HTTPException(status_code=404, detail="pending invitation not found")
        return JSONResponse(_row_to_jsonable(row))
    finally:
        await conn.close()



@router.get("/relationships")
async def list_relationships(
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
    include_inactive: int = Query(0, ge=0, le=1),
):
    owner = require_actor_matches_owner(req, owner_user_id)
    conn = await _db()
    try:
        status_filter = "" if include_inactive else "and r.status in ('pending','accepted')"
        rows = await conn.fetch(
            f"""
            select
              r.relationship_id,
              r.requester_user_id,
              r.addressee_user_id,
              case
                when r.requester_user_id=$1::uuid then r.addressee_user_id
                else r.requester_user_id
              end as other_user_id,
              r.status,
              r.relationship_kind,
              r.label,
              r.notes,
              r.created_at,
              r.updated_at
            from {SCHEMA}.relationship r
            where (r.requester_user_id=$1::uuid or r.addressee_user_id=$1::uuid)
              {status_filter}
            order by r.updated_at desc
            """,
            owner,
        )
        return JSONResponse([_row_to_jsonable(r) for r in rows])
    finally:
        await conn.close()


@router.post("/relationships/{relationship_id}/revoke")
async def revoke_relationship(
    relationship_id: str,
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
):
    rid = _as_uuid(relationship_id, "relationship_id")
    owner = require_actor_matches_owner(req, owner_user_id)

    conn = await _db()
    try:
        async with conn.transaction():
            row = await conn.fetchrow(
                f"""
                update {SCHEMA}.relationship
                   set status='revoked',
                       updated_at=now()
                 where relationship_id=$1::uuid
                   and (requester_user_id=$2::uuid or addressee_user_id=$2::uuid)
                   and status in ('pending', 'accepted')
                returning
                  relationship_id,
                  requester_user_id,
                  addressee_user_id,
                  case
                    when requester_user_id=$2::uuid then addressee_user_id
                    else requester_user_id
                  end as other_user_id,
                  status,
                  relationship_kind,
                  label,
                  notes,
                  created_at,
                  updated_at
                """,
                rid,
                owner,
            )

            if not row:
                raise HTTPException(status_code=404, detail="active relationship not found")

            await conn.execute(
                f"""
                update {SCHEMA}.relationship_permission
                   set is_enabled=false,
                       permission_level='none',
                       updated_at=now()
                 where relationship_id=$1::uuid
                """,
                rid,
            )

        return JSONResponse(_row_to_jsonable(row))
    finally:
        await conn.close()




@router.get("/permissions/granted-to-me")
async def list_permissions_granted_to_me(
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
):
    owner = require_actor_matches_owner(req, owner_user_id)

    conn = await _db()
    try:
        rows = await conn.fetch(
            f"""
            select
              rp.relationship_permission_id,
              rp.relationship_id,
              rp.grantor_user_id,
              coalesce(grantor_profile.display_name, '') as grantor_display_name,
              rp.grantee_user_id,
              coalesce(grantee_profile.display_name, '') as grantee_display_name,
              rp.permission_scope,
              rp.permission_level,
              rp.is_enabled,
              rp.notes,
              r.relationship_kind,
              r.status as relationship_status,
              rp.created_at,
              rp.updated_at
            from {SCHEMA}.relationship_permission rp
            join {SCHEMA}.relationship r
              on r.relationship_id=rp.relationship_id
            left join {SCHEMA}.user_profile grantor_profile
              on grantor_profile.user_id=rp.grantor_user_id
             and grantor_profile.is_active=true
            left join {SCHEMA}.user_profile grantee_profile
              on grantee_profile.user_id=rp.grantee_user_id
             and grantee_profile.is_active=true
            where rp.grantee_user_id=$1::uuid
              and rp.is_enabled=true
              and r.status='accepted'
            order by lower(coalesce(grantor_profile.display_name, '')) asc,
                     rp.permission_scope asc
            """,
            owner,
        )
        return JSONResponse([_row_to_jsonable(r) for r in rows])
    finally:
        await conn.close()


@router.get("/profiles")
async def list_profiles(
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
    user_ids: str = Query("", max_length=4000),
):
    # owner_user_id is required for auth-proxy symmetry.
    owner_user_id = require_actor_matches_owner(req, owner_user_id)

    ids: list[str] = []
    for part in str(user_ids or "").split(","):
        part = part.strip()
        if not part:
            continue
        ids.append(_as_uuid(part, "user_id"))

    conn = await _db()
    try:
        rows = await conn.fetch(
            f"""
            select
              p.user_id,
              p.display_name,
              case
                when p.user_id=$1::uuid or exists (
                  select 1
                  from {SCHEMA}.relationship accepted_relationship
                  where accepted_relationship.status='accepted'
                    and (
                      (accepted_relationship.requester_user_id=$1::uuid
                       and accepted_relationship.addressee_user_id=p.user_id)
                      or
                      (accepted_relationship.addressee_user_id=$1::uuid
                       and accepted_relationship.requester_user_id=p.user_id)
                    )
                ) then p.email
                else null
              end as email,
              p.source,
              p.is_active,
              p.created_at,
              p.updated_at
            from {SCHEMA}.user_profile p
            where p.is_active=true
              and ($2::uuid[] is null or p.user_id=any($2::uuid[]))
              and (
                p.user_id=$1::uuid
                or exists (
                  select 1
                  from {SCHEMA}.relationship accepted_relationship
                  where accepted_relationship.status='accepted'
                    and (
                      (accepted_relationship.requester_user_id=$1::uuid
                       and accepted_relationship.addressee_user_id=p.user_id)
                      or
                      (accepted_relationship.addressee_user_id=$1::uuid
                       and accepted_relationship.requester_user_id=p.user_id)
                    )
                )
                or exists (
                  select 1
                  from {SCHEMA}.conversation_member mine
                  join {SCHEMA}.conversation_member theirs
                    on theirs.conversation_id=mine.conversation_id
                   and theirs.user_id=p.user_id
                  where mine.user_id=$1::uuid
                )
              )
            order by lower(p.display_name) asc
            """,
            owner_user_id,
            ids or None,
        )

        return JSONResponse([_row_to_jsonable(r) for r in rows])
    finally:
        await conn.close()



async def _assert_relationship_participant(
    conn,
    relationship_id: str,
    owner_user_id: str,
    *,
    require_accepted: bool = False,
    lock: bool = False,
):
    lock_clause = "for share" if lock else ""
    row = await conn.fetchrow(
        f"""
        select
          relationship_id, requester_user_id, addressee_user_id,
          case
            when requester_user_id=$2::uuid then addressee_user_id
            else requester_user_id
          end as other_user_id,
          status, relationship_kind, label, notes, created_at, updated_at
        from {SCHEMA}.relationship
        where relationship_id=$1::uuid
          and (requester_user_id=$2::uuid or addressee_user_id=$2::uuid)
        limit 1
        {lock_clause}
        """,
        relationship_id,
        owner_user_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="relationship not found")
    if require_accepted and str(row["status"]) != "accepted":
        raise HTTPException(status_code=409, detail="accepted relationship required")
    return row


@router.get("/relationships/{relationship_id}/permissions")
async def list_relationship_permissions(
    relationship_id: str,
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
):
    rid = _as_uuid(relationship_id, "relationship_id")
    owner = require_actor_matches_owner(req, owner_user_id)

    conn = await _db()
    try:
        await _assert_relationship_participant(conn, rid, owner)
        rows = await conn.fetch(
            f"""
            select
              relationship_permission_id,
              relationship_id,
              grantor_user_id,
              grantee_user_id,
              permission_scope,
              permission_level,
              is_enabled,
              notes,
              created_at,
              updated_at
            from {SCHEMA}.relationship_permission
            where relationship_id=$1::uuid
            order by permission_scope asc
            """,
            rid,
        )
        return JSONResponse([_row_to_jsonable(r) for r in rows])
    finally:
        await conn.close()


@router.post("/relationships/{relationship_id}/permissions/upsert")
async def upsert_relationship_permission(
    relationship_id: str,
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
    permission_scope: str = Query(..., min_length=1),
    permission_level: str = Query("none"),
    is_enabled: int = Query(0, ge=0, le=1),
    payload: dict = Body(default_factory=dict),
):
    rid = _as_uuid(relationship_id, "relationship_id")
    owner = require_actor_matches_owner(req, owner_user_id)

    permission_scope = _clean_text(permission_scope, 80)
    allowed_scopes = {
        "messages:send",
        "training:view",
        "nutrition:view",
        "measurements:view",
        "measurements:enter",
        "measurements:edit_recent",
        "plan:view",
        "plan:comment",
        "plan:edit",
        "workout_template:share",
        "workout_template:copy",
    }
    if permission_scope not in allowed_scopes:
        raise HTTPException(status_code=400, detail="invalid permission_scope")

    permission_level = _clean_text(permission_level, 40) or "none"
    if permission_level not in {"none", "view", "comment", "edit", "admin"}:
        raise HTTPException(status_code=400, detail="invalid permission_level")

    conn = await _db()
    try:
        async with conn.transaction():
            rel = await _assert_relationship_participant(
                conn,
                rid,
                owner,
                require_accepted=True,
                lock=True,
            )
            grantee = str(rel["other_user_id"])

            if bool(is_enabled) and permission_scope in {"plan:comment", "plan:edit"}:
                await conn.execute(
                    f"""
                    insert into {SCHEMA}.relationship_permission (
                      relationship_id,
                      grantor_user_id,
                      grantee_user_id,
                      permission_scope,
                      permission_level,
                      is_enabled,
                      notes
                    )
                    values (
                      $1::uuid, $2::uuid, $3::uuid,
                      'plan:view', 'view', true,
                      'Automatically required by Plan collaboration access.'
                    )
                    on conflict (
                      relationship_id,
                      grantor_user_id,
                      grantee_user_id,
                      permission_scope
                    )
                    do update set
                      permission_level='view',
                      is_enabled=true,
                      updated_at=now()
                    """,
                    rid,
                    owner,
                    grantee,
                )

            if not bool(is_enabled) and permission_scope == "plan:view":
                await conn.execute(
                    f"""
                    update {SCHEMA}.relationship_permission
                       set permission_level='none',
                           is_enabled=false,
                           updated_at=now()
                     where relationship_id=$1::uuid
                       and grantor_user_id=$2::uuid
                       and grantee_user_id=$3::uuid
                       and permission_scope in ('plan:comment', 'plan:edit')
                    """,
                    rid,
                    owner,
                    grantee,
                )

            row = await conn.fetchrow(
                f"""
                insert into {SCHEMA}.relationship_permission (
                  relationship_id,
                  grantor_user_id,
                  grantee_user_id,
                  permission_scope,
                  permission_level,
                  is_enabled,
                  notes
                )
                values ($1::uuid, $2::uuid, $3::uuid, $4, $5, $6::boolean, $7)
                on conflict (relationship_id, grantor_user_id, grantee_user_id, permission_scope)
                do update set
                  permission_level=excluded.permission_level,
                  is_enabled=excluded.is_enabled,
                  notes=excluded.notes,
                  updated_at=now()
                returning
                  relationship_permission_id,
                  relationship_id,
                  grantor_user_id,
                  grantee_user_id,
                  permission_scope,
                  permission_level,
                  is_enabled,
                  notes,
                  created_at,
                  updated_at
                """,
                rid,
                owner,
                grantee,
                permission_scope,
                permission_level,
                bool(is_enabled),
                _clean_text(payload.get("notes", "") if isinstance(payload, dict) else "", 2000),
            )
        return JSONResponse(_row_to_jsonable(row))
    finally:
        await conn.close()


@router.post("/relationships/upsert")
async def upsert_relationship(
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
    other_user_id: str = Query(..., min_length=1),
    status: str = Query("accepted"),
    relationship_kind: str = Query("friend"),
    label: str = Body(""),
    notes: str = Body(""),
):
    owner = require_actor_matches_owner(req, owner_user_id)
    other = _as_uuid(other_user_id, "other_user_id")
    if owner == other:
        raise HTTPException(status_code=400, detail="cannot relate user to self")

    status = _clean_text(status, 40) or "accepted"
    if status != "accepted":
        raise HTTPException(
            status_code=400,
            detail="status is managed by invitation and disconnect workflows",
        )

    relationship_kind = _clean_text(relationship_kind, 80) or "friend"
    if relationship_kind not in {"friend", "training_partner", "plan_helper", "coach"}:
        raise HTTPException(status_code=400, detail="invalid relationship_kind")

    conn = await _db()
    try:
        row = await conn.fetchrow(
            f"""
            update {SCHEMA}.relationship
            set
              relationship_kind=$3,
              label=$4,
              notes=$5,
              updated_at=now()
            where status='accepted'
              and (
                (requester_user_id=$1::uuid and addressee_user_id=$2::uuid)
                or
                (requester_user_id=$2::uuid and addressee_user_id=$1::uuid)
              )
            returning
              relationship_id, requester_user_id, addressee_user_id,
              status, relationship_kind, label, notes, created_at, updated_at
            """,
            owner,
            other,
            relationship_kind,
            _clean_text(label, 200),
            _clean_text(notes, 2000),
        )
        if not row:
            raise HTTPException(
                status_code=409,
                detail="accepted relationship required; use the invitation workflow",
            )
        return JSONResponse(_row_to_jsonable(row))
    finally:
        await conn.close()


@router.get("/conversations")
async def list_conversations(
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
    limit: int = Query(50, ge=1, le=200),
):
    owner = require_actor_matches_owner(req, owner_user_id)
    conn = await _db()
    try:
        rows = await conn.fetch(
            f"""
            select
              c.conversation_id,
              c.conversation_kind,
              c.created_by_user_id,
              c.title,
              c.is_active,
              c.created_at,
              c.updated_at,
              case
                when c.conversation_kind='direct' and c.direct_user_low_id=$1::uuid then c.direct_user_high_id
                when c.conversation_kind='direct' and c.direct_user_high_id=$1::uuid then c.direct_user_low_id
                else null
              end as other_user_id,
              coalesce(other_profile.display_name, '') as other_display_name,
              lm.message_id as last_message_id,
              lm.author_user_id as last_message_author_user_id,
              lm.body as last_message_body,
              lm.created_at as last_message_created_at,
              case
                when c.conversation_kind='direct'
                 and (c.direct_user_low_id=$1::uuid or c.direct_user_high_id=$1::uuid)
                 and exists (
                   select 1
                   from {SCHEMA}.conversation_member send_recipient
                   where send_recipient.conversation_id=c.conversation_id
                     and send_recipient.user_id=case
                       when c.direct_user_low_id=$1::uuid then c.direct_user_high_id
                       else c.direct_user_low_id
                     end
                     and send_recipient.is_active=true
                 )
                then exists (
                  select 1
                  from {SCHEMA}.relationship send_relationship
                  join {SCHEMA}.relationship_permission send_permission
                    on send_permission.relationship_id=send_relationship.relationship_id
                   and send_permission.grantor_user_id=case
                     when c.direct_user_low_id=$1::uuid then c.direct_user_high_id
                     else c.direct_user_low_id
                   end
                   and send_permission.grantee_user_id=$1::uuid
                   and send_permission.permission_scope='messages:send'
                   and send_permission.permission_level <> 'none'
                   and send_permission.is_enabled=true
                  where send_relationship.status='accepted'
                    and least(
                      send_relationship.requester_user_id,
                      send_relationship.addressee_user_id
                    )=c.direct_user_low_id
                    and greatest(
                      send_relationship.requester_user_id,
                      send_relationship.addressee_user_id
                    )=c.direct_user_high_id
                )
                else false
              end as can_send
            from {SCHEMA}.conversation c
            join {SCHEMA}.conversation_member cm
              on cm.conversation_id=c.conversation_id
             and cm.user_id=$1::uuid
             and cm.is_active=true
            left join lateral (
              select message_id, author_user_id, body, created_at
              from {SCHEMA}.message m
              where m.conversation_id=c.conversation_id
                and m.is_deleted=false
              order by m.created_at desc
              limit 1
            ) lm on true
            left join {SCHEMA}.user_profile other_profile
              on other_profile.user_id = case
                when c.conversation_kind='direct' and c.direct_user_low_id=$1::uuid then c.direct_user_high_id
                when c.conversation_kind='direct' and c.direct_user_high_id=$1::uuid then c.direct_user_low_id
                else null
              end
             and other_profile.is_active=true
            where c.is_active=true
            order by coalesce(lm.created_at, c.updated_at) desc
            limit $2
            """,
            owner,
            limit,
        )
        return JSONResponse([_row_to_jsonable(r) for r in rows])
    finally:
        await conn.close()


@router.delete("/conversations/{conversation_id}")
async def remove_conversation_from_list(
    conversation_id: str,
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
):
    cid = _as_uuid(conversation_id, "conversation_id")
    owner = require_actor_matches_owner(req, owner_user_id)
    conn = await _db()
    try:
        row = await conn.fetchrow(
            f"""
            update {SCHEMA}.conversation_member
               set is_active=false,
                   updated_at=now()
             where conversation_id=$1::uuid
               and user_id=$2::uuid
               and is_active=true
            returning conversation_member_id
            """,
            cid,
            owner,
        )
        if not row:
            raise HTTPException(status_code=404, detail="conversation not found")
        return JSONResponse({"removed": cid})
    finally:
        await conn.close()


@router.post("/conversations/direct")
async def get_or_create_direct_conversation(
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
    other_user_id: str = Query(..., min_length=1),
):
    owner = require_actor_matches_owner(req, owner_user_id)
    other = _as_uuid(other_user_id, "other_user_id")
    if owner == other:
        raise HTTPException(status_code=400, detail="cannot create direct conversation with self")

    low, high = _direct_pair(owner, other)
    conn = await _db()
    try:
        async with conn.transaction():
            await _assert_message_send_authorized(conn, owner, other)
            row = await conn.fetchrow(
                f"""
                insert into {SCHEMA}.conversation (
                  conversation_kind, created_by_user_id,
                  direct_user_low_id, direct_user_high_id,
                  title, is_active
                )
                values ('direct', $1::uuid, $2::uuid, $3::uuid, '', true)
                on conflict (direct_user_low_id, direct_user_high_id)
                  where conversation_kind='direct'
                    and direct_user_low_id is not null
                    and direct_user_high_id is not null
                    and is_active=true
                do update set updated_at=now()
                returning
                  conversation_id, conversation_kind, created_by_user_id,
                  direct_user_low_id, direct_user_high_id,
                  title, is_active, created_at, updated_at
                """,
                owner,
                low,
                high,
            )

            cid = str(row["conversation_id"])
            await conn.execute(
                f"""
                insert into {SCHEMA}.conversation_member (conversation_id, user_id, member_role, is_active)
                values ($1::uuid, $2::uuid, 'owner', true)
                on conflict (conversation_id, user_id)
                do update set is_active=true, updated_at=now()
                """,
                cid,
                owner,
            )
            await conn.execute(
                f"""
                insert into {SCHEMA}.conversation_member (conversation_id, user_id, member_role, is_active)
                values ($1::uuid, $2::uuid, 'member', true)
                on conflict (conversation_id, user_id)
                do update set is_active=true, updated_at=now()
                """,
                cid,
                other,
            )

        out = _row_to_jsonable(row)
        out["can_send"] = True
        return JSONResponse(out)
    finally:
        await conn.close()


@router.get("/conversations/{conversation_id}/messages")
async def list_messages(
    conversation_id: str,
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
    limit: int = Query(100, ge=1, le=500),
):
    cid = _as_uuid(conversation_id, "conversation_id")
    owner = require_actor_matches_owner(req, owner_user_id)
    conn = await _db()
    try:
        await _assert_member(conn, cid, owner)
        rows = await conn.fetch(
            f"""
            select
              m.message_id, m.conversation_id, m.author_user_id,
              coalesce(up.display_name, '') as author_display_name,
              m.body, m.body_format, m.metadata,
              m.is_deleted, m.created_at, m.updated_at
            from {SCHEMA}.message m
            left join {SCHEMA}.user_profile up
              on up.user_id=m.author_user_id
             and up.is_active=true
            where m.conversation_id=$1::uuid
              and m.is_deleted=false
            order by m.created_at asc
            limit $2
            """,
            cid,
            limit,
        )
        await conn.execute(
            f"""
            update {SCHEMA}.conversation_member
               set last_read_at=now(), updated_at=now()
             where conversation_id=$1::uuid
               and user_id=$2::uuid
            """,
            cid,
            owner,
        )
        return JSONResponse([_row_to_jsonable(r) for r in rows])
    finally:
        await conn.close()


@router.post("/conversations/{conversation_id}/messages")
async def create_message(
    conversation_id: str,
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
    body: str = Body(...),
    body_format: str = Body("plain"),
    metadata: dict = Body(default_factory=dict),
):
    cid = _as_uuid(conversation_id, "conversation_id")
    owner = require_actor_matches_owner(req, owner_user_id)

    body = _clean_text(body, 8000)
    if not body:
        raise HTTPException(status_code=400, detail="message body required")

    body_format = _clean_text(body_format, 40) or "plain"
    if body_format not in {"plain", "markdown"}:
        raise HTTPException(status_code=400, detail="invalid body_format")

    if not isinstance(metadata, dict):
        raise HTTPException(status_code=400, detail="metadata must be an object")

    conn = await _db()
    try:
        async with conn.transaction():
            await _assert_conversation_send_authorized(conn, cid, owner)
            row = await conn.fetchrow(
                f"""
                insert into {SCHEMA}.message (
                  conversation_id, author_user_id,
                  body, body_format, metadata
                )
                values ($1::uuid, $2::uuid, $3, $4, $5::jsonb)
                returning
                  message_id, conversation_id, author_user_id,
                  body, body_format, metadata,
                  is_deleted, created_at, updated_at
                """,
                cid,
                owner,
                body,
                body_format,
                json.dumps(metadata),
            )
            await conn.execute(
                f"""
                update {SCHEMA}.conversation
                   set updated_at=now()
                 where conversation_id=$1::uuid
                """,
                cid,
            )
        return JSONResponse(_row_to_jsonable(row))
    finally:
        await conn.close()
