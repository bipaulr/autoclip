import { login } from "./actions";

export default async function LoginPage({ searchParams }: { searchParams: Promise<{ error?: string }> }) {
  const { error } = await searchParams;
  const configured = Boolean(process.env.ADMIN_PASSWORD);
  return (
    <main className="login">
      <form action={login} className="card">
        <h1>autoclip</h1>
        {!configured && <p className="warn">Set ADMIN_PASSWORD in .env, then restart the dashboard.</p>}
        <label htmlFor="pw">Admin password</label>
        <input id="pw" name="password" type="password" autoFocus required />
        {error && <p className="warn">Wrong password.</p>}
        <button type="submit" disabled={!configured}>Log in</button>
      </form>
    </main>
  );
}
