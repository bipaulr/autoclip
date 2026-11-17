"use server";

import { cookies } from "next/headers";
import { redirect } from "next/navigation";
import { COOKIE, sessionToken } from "@/lib/auth";

export async function login(form: FormData) {
  const pw = process.env.ADMIN_PASSWORD;
  const given = String(form.get("password") ?? "");
  if (!pw || given.length !== pw.length || !timingSafeEqual(given, pw)) redirect("/login?error=1");
  const store = await cookies();
  store.set(COOKIE, (await sessionToken())!, { httpOnly: true, sameSite: "strict", path: "/", maxAge: 60 * 60 * 24 * 30 });
  redirect("/");
}

export async function logout() {
  (await cookies()).delete(COOKIE);
  redirect("/login");
}

function timingSafeEqual(a: string, b: string): boolean {
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}
