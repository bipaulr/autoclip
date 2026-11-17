// Proxies the browser to the FastAPI backend. Only reachable with a valid session (see middleware),
// and adds the admin password server-side, so it never reaches the browser.
import type { NextRequest } from "next/server";

const API = process.env.API_URL ?? "http://127.0.0.1:8010";

async function forward(req: NextRequest, ctx: { params: Promise<{ path: string[] }> }) {
  const { path } = await ctx.params;
  const target = `${API}/${path.map(encodeURIComponent).join("/")}${req.nextUrl.search}`;
  const headers: Record<string, string> = { "X-Admin-Password": process.env.ADMIN_PASSWORD ?? "" };
  const type = req.headers.get("content-type");
  if (type) headers["content-type"] = type;
  const range = req.headers.get("range");
  if (range) headers["range"] = range;
  const body = ["GET", "HEAD"].includes(req.method) ? undefined : await req.text();
  // The API restarts on every deploy; wait for it briefly instead of showing a broken dashboard.
  let res!: Response;
  for (let attempt = 0; ; attempt++) {
    try {
      res = await fetch(target, { method: req.method, headers, body, cache: "no-store" });
      break;
    } catch (e) {
      if (attempt >= 5) {
        return Response.json({ detail: `API unreachable: ${(e as Error).message}` }, { status: 503 });
      }
      await new Promise((r) => setTimeout(r, 1000 * (attempt + 1)));
    }
  }
  const out = new Headers();
  for (const h of ["content-type", "content-length", "content-range", "accept-ranges"]) {
    const v = res.headers.get(h);
    if (v) out.set(h, v);
  }
  return new Response(res.body, { status: res.status, headers: out });
}

export { forward as GET, forward as POST, forward as PATCH };
