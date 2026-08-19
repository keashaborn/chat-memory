from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import uuid
from zoneinfo import ZoneInfo

import asyncpg

from seebx.capabilities.plans.data_plan import create_lifeswitch_data_plan_v1
from seebx.capabilities.plans.domain_context import TrustedLifeSwitchContextRequestV1
from seebx.capabilities.plans.domain_provider import LifeSwitchDomainContextProviderV1
from seebx.adapters.lifeswitch_domain_postgres import (
    PostgresLifeSwitchDomainReaderV1,
)


BASE_QUERIES = (
    "Who won America's Next Top Model in 2015?",
    "How am I doing?",
    "Was I low on protein Monday?",
    "How is my squat progressing?",
)


async def main() -> None:
    dsn = str(os.environ.get("POSTGRES_DSN") or "").strip()
    if not dsn:
        raise RuntimeError("POSTGRES_DSN is required")
    conn = await asyncpg.connect(dsn, command_timeout=30)
    try:
        async with conn.transaction(readonly=True):
            owner = await conn.fetchrow(
                """
                select state.owner_user_id, version.owner_timezone
                from lifeswitch_agentic.plan_owner_state state
                join lifeswitch_agentic.plan_versions version
                  on version.owner_user_id = state.owner_user_id
                 and version.id = state.active_plan_version_id
                where state.active_plan_version_id is not null
                order by version.activated_at desc
                limit 1
                """
            )
            if owner is None:
                print(json.dumps({"status": "skipped", "reason": "no_active_plan_owner"}))
                return
            owner_user_id = owner["owner_user_id"]
            owner_timezone = str(owner["owner_timezone"] or "UTC")
            today = dt.datetime.now(ZoneInfo(owner_timezone)).date()
            recent_activity_day = await conn.fetchval(
                """
                select max(day)
                from (
                    select day
                    from lifeswitch_training.training_session_current_v
                    where owner_user_id = $1 and finished_at is not null
                    union all
                    select day
                    from lifeswitch_training.conditioning_session_current_v
                    where owner_user_id = $1 and is_active = true
                ) activity
                """,
                owner_user_id,
            )
            provider = LifeSwitchDomainContextProviderV1(
                PostgresLifeSwitchDomainReaderV1(conn)
            )
            results = []
            cases = [(query, today) for query in BASE_QUERIES]
            if recent_activity_day is not None:
                cases.append(("Show my workout today", recent_activity_day))
            for query, planning_day in cases:
                data_plan = create_lifeswitch_data_plan_v1(query, today=planning_day)
                request = TrustedLifeSwitchContextRequestV1.create(
                    request_id=uuid.uuid4(),
                    authenticated_actor_user_id=owner_user_id,
                    owner_user_id=owner_user_id,
                    owner_timezone=owner_timezone,
                    query=query,
                    data_plan=data_plan,
                )
                envelope = await provider.select(request)
                results.append(
                    {
                        "intent": data_plan.intent,
                        "status": envelope.status,
                        "plan_source": envelope.plan_source,
                        "section_count": len(envelope.sections),
                        "record_counts": [section.record_count for section in envelope.sections],
                        "estimated_prompt_tokens": envelope.estimated_prompt_tokens,
                        "source_relations": [
                            list(section.source_relations) for section in envelope.sections
                        ],
                    }
                )
            print(
                json.dumps(
                    {
                        "status": "passed",
                        "query_count": len(results),
                        "results": results,
                        "personal_values_emitted": False,
                        "owner_identifier_emitted": False,
                        "writes_performed": False,
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
