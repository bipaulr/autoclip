// Single admin password from env. The session cookie is an HMAC of a fixed label with the password,
// so it can be checked without a session store and stops working if the password changes.
export const COOKIE = "autoclip_session";

async function hmac(key: string, msg: string): Promise<string> {
  const enc = new TextEncoder();
  const k = await crypto.subtle.importKey("raw", enc.encode(key), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const sig = await crypto.subtle.sign("HMAC", k, enc.encode(msg));
  return Array.from(new Uint8Array(sig)).map((b) => b.toString(16).padStart(2, "0")).join("");
}

export async function sessionToken(): Promise<string | null> {
  const pw = process.env.ADMIN_PASSWORD;
  return pw ? hmac(pw, "autoclip-dashboard-session-v1") : null;
}

export async function isValidSession(value: string | undefined): Promise<boolean> {
  const expected = await sessionToken();
  if (!expected || !value || value.length !== expected.length) return false;
  let diff = 0;
  for (let i = 0; i < value.length; i++) diff |= value.charCodeAt(i) ^ expected.charCodeAt(i);
  return diff === 0;
}
