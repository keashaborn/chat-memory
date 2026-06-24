from __future__ import annotations

import os
import uuid
import json
import decimal
import datetime as _dt
import asyncpg

from fastapi import APIRouter, HTTPException, Query, Body
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


@router.get("/relationships")
async def list_relationships(
    owner_user_id: str = Query(..., min_length=1),
    include_inactive: int = Query(0, ge=0, le=1),
):
    owner = _as_uuid(owner_user_id, "owner_user_id")
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



@router.get("/profiles")
async def list_profiles(
    owner_user_id: str = Query(..., min_length=1),
    user_ids: str = Query("", max_length=4000),
):
    # owner_user_id is required for auth-proxy symmetry. It is not used for filtering yet.
    _as_uuid(owner_user_id, "owner_user_id")

    ids: list[str] = []
    for part in str(user_ids or "").split(","):
        part = part.strip()
        if not part:
            continue
        ids.append(_as_uuid(part, "user_id"))

    conn = await _db()
    try:
        if ids:
            rows = await conn.fetch(
                f"""
                select user_id, display_name, email, source, is_active, created_at, updated_at
                from {SCHEMA}.user_profile
                where user_id = any($1::uuid[])
                  and is_active=true
                order by lower(display_name) asc
                """,
                ids,
            )
        else:
            rows = await conn.fetch(
                f"""
                select user_id, display_name, email, source, is_active, created_at, updated_at
                from {SCHEMA}.user_profile
                where is_active=true
                order by lower(display_name) asc
                """
            )

        return JSONResponse([_row_to_jsonable(r) for r in rows])
    finally:
        await conn.close()


@router.post("/relationships/upsert")
async def upsert_relationship(
    owner_user_id: str = Query(..., min_length=1),
    other_user_id: str = Query(..., min_length=1),
    status: str = Query("accepted"),
    relationship_kind: str = Query("friend"),
    label: str = Body(""),
    notes: str = Body(""),
):
    owner = _as_uuid(owner_user_id, "owner_user_id")
    other = _as_uuid(other_user_id, "other_user_id")
    if owner == other:
        raise HTTPException(status_code=400, detail="cannot relate user to self")

    status = _clean_text(status, 40) or "accepted"
    if status not in {"pending", "accepted", "blocked", "revoked"}:
        raise HTTPException(status_code=400, detail="invalid status")

    relationship_kind = _clean_text(relationship_kind, 80) or "friend"
    if relationship_kind not in {"friend", "training_partner", "plan_helper", "coach"}:
        raise HTTPException(status_code=400, detail="invalid relationship_kind")

    conn = await _db()
    try:
        row = await conn.fetchrow(
            f"""
            insert into {SCHEMA}.relationship (
              requester_user_id, addressee_user_id,
              status, relationship_kind, label, notes
            )
            values ($1::uuid, $2::uuid, $3, $4, $5, $6)
            on conflict (
              least(requester_user_id, addressee_user_id),
              greatest(requester_user_id, addressee_user_id)
            )
            do update set
              status=excluded.status,
              relationship_kind=excluded.relationship_kind,
              label=excluded.label,
              notes=excluded.notes,
              updated_at=now()
            returning
              relationship_id, requester_user_id, addressee_user_id,
              status, relationship_kind, label, notes, created_at, updated_at
            """,
            owner,
            other,
            status,
            relationship_kind,
            _clean_text(label, 200),
            _clean_text(notes, 2000),
        )
        return JSONResponse(_row_to_jsonable(row))
    finally:
        await conn.close()


@router.get("/conversations")
async def list_conversations(
    owner_user_id: str = Query(..., min_length=1),
    limit: int = Query(50, ge=1, le=200),
):
    owner = _as_uuid(owner_user_id, "owner_user_id")
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
              lm.created_at as last_message_created_at
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


@router.post("/conversations/direct")
async def get_or_create_direct_conversation(
    owner_user_id: str = Query(..., min_length=1),
    other_user_id: str = Query(..., min_length=1),
):
    owner = _as_uuid(owner_user_id, "owner_user_id")
    other = _as_uuid(other_user_id, "other_user_id")
    if owner == other:
        raise HTTPException(status_code=400, detail="cannot create direct conversation with self")

    low, high = _direct_pair(owner, other)
    conn = await _db()
    try:
        async with conn.transaction():
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

        return JSONResponse(_row_to_jsonable(row))
    finally:
        await conn.close()


@router.get("/conversations/{conversation_id}/messages")
async def list_messages(
    conversation_id: str,
    owner_user_id: str = Query(..., min_length=1),
    limit: int = Query(100, ge=1, le=500),
):
    cid = _as_uuid(conversation_id, "conversation_id")
    owner = _as_uuid(owner_user_id, "owner_user_id")
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
    owner_user_id: str = Query(..., min_length=1),
    body: str = Body(...),
    body_format: str = Body("plain"),
    metadata: dict = Body(default_factory=dict),
):
    cid = _as_uuid(conversation_id, "conversation_id")
    owner = _as_uuid(owner_user_id, "owner_user_id")

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
            await _assert_member(conn, cid, owner)
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
