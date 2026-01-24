import { NextRequest } from "next/server";
import { randomUUID } from "crypto";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const BRAINS_URL = (process.env.BRAINS_URL || "http://172.31.32.171:8088").replace(/\/+$/, "");

export async function POST(req: NextRequest, ctx: { params: Promise<{ my_food_id: string }> }) {
  const rid = req.headers.get("x-request-id") || randomUUID();
  const { my_food_id } = await ctx.params;

  const upstream = `${BRAINS_URL}/lifeswitch/nutrition/my_foods/${encodeURIComponent(my_food_id)}/deactivate`;
  const r = await fetch(upstream, { method: "POST", headers: { "x-request-id": rid }, cache: "no-store" });
  const body = await r.text();

  return new Response(body, {
    status: r.status,
    headers: {
      "content-type": r.headers.get("content-type") || "application/json; charset=utf-8",
      "x-request-id": rid,
    },
  });
}
