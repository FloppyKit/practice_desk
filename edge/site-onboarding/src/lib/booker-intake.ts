/** Server-only: tell the booker an intake form / card landed (completion flags, Chart/Consents PDF, hold). */
const BOOKER = (process.env.BOOKER_INTERNAL_URL?.trim() || "http://127.0.0.1:8090").replace(/\/$/, "");

export async function postToBooker(
  path: string,
  body: unknown,
): Promise<{ ok: boolean; detail?: string; data?: unknown }> {
  const secret = process.env.BOOKER_STAFF_SECRET?.trim() || "";
  if (!secret) return { ok: false, detail: "no booker secret" };
  try {
    const res = await fetch(`${BOOKER}${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Staff-Secret": secret },
      body: JSON.stringify(body),
      cache: "no-store",
      signal: AbortSignal.timeout(15_000),
    });
    const text = await res.text();
    if (!res.ok) return { ok: false, detail: `booker ${res.status}` };
    let data: unknown = {};
    try {
      data = text ? JSON.parse(text) : {};
    } catch {
      /* keep */
    }
    return { ok: true, data };
  } catch (e) {
    return { ok: false, detail: String(e).slice(0, 120) };
  }
}
