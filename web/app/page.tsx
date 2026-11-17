"use client";

import { useCallback, useEffect, useState, type ReactNode } from "react";
import { logout } from "./login/actions";

type Any = Record<string, any>;
const API = "/api/backend";

async function get(path: string) {
  const r = await fetch(`${API}${path}`, { cache: "no-store" });
  if (!r.ok) throw new Error(`${path}: HTTP ${r.status}`);
  return r.json();
}

async function send(method: "POST" | "PATCH", path: string, body?: unknown) {
  const r = await fetch(`${API}${path}`, {
    method,
    headers: { "content-type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!r.ok) {
    const d = await r.json().catch(() => ({}));
    throw new Error(d.detail ?? `HTTP ${r.status}`);
  }
  return r.json();
}

const TABS = ["Live", "Overview", "Pipeline", "Clips", "Calendar", "Submissions", "Campaigns", "Accounts", "Analytics",
  "Bandit", "Prompts", "Decisions"] as const;
type Tab = (typeof TABS)[number];

function Pill({ s }: { s: string | null | undefined }) {
  return <span className={`pill ${s ?? ""}`}>{s ?? "-"}</span>;
}

function useData<T = any>(path: string, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(() => {
    get(path).then((d) => { setData(d); setError(null); }).catch((e) => setError(String(e.message ?? e)));
  }, [path]);
  useEffect(() => { load(); const t = setInterval(load, 30000); return () => clearInterval(t); }, [load, ...deps]);
  return { data, error, reload: load };
}

function Section({ title, error, children, actions }: { title: string; error?: string | null; children: ReactNode; actions?: ReactNode }) {
  return (
    <section>
      <div className="row" style={{ justifyContent: "space-between" }}>
        <h2>{title}</h2>
        {actions}
      </div>
      {error && <p className="warn">{error}</p>}
      {children}
    </section>
  );
}

function fmt(t?: string | null) {
  return t ? new Date(t).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "-";
}

// ---------------------------------------------------------------- views
function Overview() {
  const { data: o, error, reload } = useData<Any>("/api/overview");
  const { data: money } = useData<Any[]>("/api/money?days=30");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const toggle = async () => {
    if (!o) return;
    if (!o.kill_switch && !confirm("Stop all publishing now?")) return;
    setBusy(true);
    try { await send("POST", "/api/kill-switch", { on: !o.kill_switch }); reload(); setMsg(null); }
    catch (e: any) { setMsg(e.message); }
    setBusy(false);
  };
  const max = Math.max(1, ...(money ?? []).map((d) => Math.max(d.earnings, d.spend)));
  return (
    <Section title="Overview" error={error ?? msg} actions={
      o && <button className={o.kill_switch ? "primary" : "danger"} disabled={busy} onClick={toggle}>
        {o.kill_switch ? "Resume publishing" : "Kill switch: stop publishing"}
      </button>}>
      {o && (
        <>
          {o.kill_switch && <p className="warn">Kill switch is ON: nothing will be published.</p>}
          {!o.youtube_audit_passed && <p className="muted">YouTube posts are held until the API audit passes; Instagram posts normally.</p>}
          <div className="cards">
            {[["Videos in progress", o.videos_in_progress], ["Clips (24h)", o.clips_24h], ["Posted (24h)", o.posted_24h],
              ["Scheduled", o.scheduled], ["Held", o.held], ["Submissions to do", o.pending_submissions],
              ["Failures (24h)", o.failures_24h], ["Est. earnings", `$${o.estimated_earnings.toFixed(2)}`],
              ["Spend (24h)", `$${o.spend_24h.toFixed(2)}`], ["Render threshold", o.render_threshold]].map(([l, n]) => (
              <div className="card" key={String(l)}><div className="n">{String(n)}</div><div className="l">{l}</div></div>
            ))}
          </div>
        </>
      )}
      <h3>Earnings by day (last 30 days)</h3>
      <div className="bars" title="Estimated earnings from approved views you enter">
        {(money ?? []).map((d) => <div key={d.date} title={`${d.date}: $${d.earnings} earned, $${d.spend} spent`} style={{ height: `${(d.earnings / max) * 100}%` }} />)}
      </div>
      <p className="muted">Spend stays $0 on the free stack. Earnings appear once you enter approved views on the Analytics tab.</p>
    </Section>
  );
}

function Pipeline() {
  const { data, error } = useData<Any[]>("/api/videos");
  return (
    <Section title="Pipeline" error={error}>
      <table>
        <thead><tr><th>Video</th><th>Stage</th><th>Highlights</th><th>Rank</th><th>Latest step</th><th>Rights basis</th></tr></thead>
        <tbody>{(data ?? []).map((v) => (
          <tr key={v.id}><td>{v.title}<div className="muted">{v.channel} · {v.platform} · {v.minutes} min</div></td>
            <td><Pill s={v.status} /></td><td className="num">{v.highlights}</td><td className="num">{v.rank ?? "-"}</td>
            <td className="muted">{v.last}</td><td className="muted">{v.rights_basis}</td></tr>))}
        </tbody>
      </table>
    </Section>
  );
}

function Clips() {
  const { data, error } = useData<Any[]>("/api/clips");
  return (
    <Section title="Scheduled clips" error={error}>
      {data && data.length === 0 && <p className="muted">Nothing scheduled right now.</p>}
      <div className="gallery">{(data ?? []).map((c) => (
        <div className="card" key={c.clip_id}>
          {c.preview ? <video src={`${API}${c.preview}`} controls preload="metadata" /> : <p className="muted">Not rendered</p>}
          <b>{c.title}</b> <Pill s={c.status} />
          {(c.posts ?? []).map((p: Any) => (
            <div key={`${p.platform}${p.scheduled_at}`}>
              <b>{p.platform}</b> {p.account} · {p.scheduled_at
                ? new Date(p.scheduled_at.endsWith("Z") || p.scheduled_at.includes("+") ? p.scheduled_at : `${p.scheduled_at}Z`)
                    .toLocaleString("en-IN", { timeZone: "Asia/Kolkata", weekday: "short", day: "numeric", month: "short",
                                               hour: "2-digit", minute: "2-digit" }) + " IST"
                : "time not set"}
            </div>))}
          <div className="muted">{c.hook}</div>
          <div>Score <b>{c.score}</b>{c.scores && <span className="muted"> · hook {c.scores.hook_strength} · payoff {c.scores.payoff} · clarity {c.scores.standalone_clarity} · signal z {c.scores.signal_z}</span>}</div>
          <div className="muted">{c.reason}</div>
          {c.gate_reason && <div className="muted">Gate: {c.gate_reason}</div>}
          <div className="muted">{c.channel} · {c.start_s.toFixed(0)}-{c.end_s.toFixed(0)}s · {c.style_preset ?? ""} {c.layout ?? ""}</div>
        </div>))}
      </div>
    </Section>
  );
}

function Calendar() {
  const { data, error, reload } = useData<Any[]>("/api/posts?days=7");
  const [msg, setMsg] = useState<string | null>(null);
  const days: string[] = [];
  for (let i = -1; i < 6; i++) { const d = new Date(); d.setDate(d.getDate() + i); days.push(d.toDateString()); }
  const approve = async (id: number) => {
    try { await send("POST", `/api/posts/${id}/approve`); reload(); } catch (e: any) { setMsg(e.message); }
  };
  return (
    <Section title="Post calendar" error={error ?? msg}>
      <div className="cal">{days.map((d) => (
        <div className="day" key={d}><b>{d}</b>
          {(data ?? []).filter((p) => new Date(p.scheduled_at).toDateString() === d).map((p) => (
            <div className="item" key={p.id}>
              {new Date(p.scheduled_at).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })} {p.platform} <Pill s={p.status} />
              <div className="muted">{p.title}</div>
              {p.url && <a href={p.url} target="_blank" rel="noreferrer">open</a>}
              {p.status === "held" && <><div className="muted">{p.reason}</div><button onClick={() => approve(p.id)}>Approve</button></>}
            </div>))}
        </div>))}
      </div>
    </Section>
  );
}

function Submissions() {
  const { data, error, reload } = useData<Any[]>("/api/submissions");
  const [msg, setMsg] = useState<string | null>(null);
  const mark = async (id: number, status: string) => {
    try { await send("POST", `/api/submissions/${id}`, { status }); reload(); } catch (e: any) { setMsg(e.message); }
  };
  return (
    <Section title="Campaign submissions" error={error ?? msg}>
      <p className="muted">Each campaign clip must be submitted on the marketplace soon after it posts. Copy the post link, open the campaign, paste, then mark it here.</p>
      <table>
        <thead><tr><th>Campaign</th><th>Post</th><th>Deadline</th><th>Status</th><th></th></tr></thead>
        <tbody>{(data ?? []).map((s) => (
          <tr key={s.id}><td>{s.campaign}</td>
            <td><button onClick={() => navigator.clipboard.writeText(s.post_url)}>Copy link</button> <a href={s.post_url} target="_blank" rel="noreferrer">view</a></td>
            <td>{fmt(s.deadline)}</td><td><Pill s={s.status} /></td>
            <td className="row"><a href={s.submit_url} target="_blank" rel="noreferrer">Open submit page</a>
              {["submitted", "approved", "rejected", "paid"].map((st) => <button key={st} onClick={() => mark(s.id, st)}>{st}</button>)}</td></tr>))}
        </tbody>
      </table>
    </Section>
  );
}

function Campaigns() {
  const { data, error, reload } = useData<Any[]>("/api/campaigns");
  const [msg, setMsg] = useState<string | null>(null);
  const patch = async (id: number, body: Any) => {
    try { await send("PATCH", `/api/campaigns/${id}`, body); reload(); } catch (e: any) { setMsg(e.message); }
  };
  return (
    <Section title="Campaigns (ranked by expected value)" error={error ?? msg}>
      <p className="muted">Only mark a campaign joined after you have joined it on the marketplace and accepted its terms.</p>
      <table>
        <thead><tr><th>Campaign</th><th>Status</th><th>EV / clip</th><th>Why</th><th>Rate</th><th>Budget</th><th></th></tr></thead>
        <tbody>{(data ?? []).map((c) => (
          <tr key={c.id}><td><a href={c.url} target="_blank" rel="noreferrer">{c.name}</a><div className="muted">{c.marketplace} · {(c.allowed_platforms ?? []).join(", ")}</div></td>
            <td><Pill s={c.status} /></td><td className="num">{c.ev_score ?? "-"}</td><td className="muted">{c.ev_reason}</td>
            <td className="num">{c.rate_per_1k ? `$${c.rate_per_1k}/1K` : "pool"}</td>
            <td className="num">{c.budget_remaining ?? "?"} / {c.budget_total ?? "?"}</td>
            <td className="row">{c.status === "open" && <button onClick={() => confirm(`Have you joined "${c.name}" on the marketplace?`) && patch(c.id, { status: "joined" })}>I joined it</button>}
              {c.status === "joined" && <button onClick={() => patch(c.id, { status: "paused" })}>Pause</button>}
              {c.status === "paused" && <button onClick={() => patch(c.id, { status: "joined" })}>Resume</button>}</td></tr>))}
        </tbody>
      </table>
    </Section>
  );
}

function Accounts() {
  const { data, error, reload } = useData<Any[]>("/api/accounts");
  const [msg, setMsg] = useState<string | null>(null);
  const patch = async (id: number, body: Any) => {
    try { await send("PATCH", `/api/accounts/${id}`, body); reload(); } catch (e: any) { setMsg(e.message); }
  };
  return (
    <Section title="Accounts" error={error ?? msg}>
      <table>
        <thead><tr><th>Account</th><th>Niche</th><th>Status</th><th>Auto-approve above score</th><th>Daily cap</th><th></th></tr></thead>
        <tbody>{(data ?? []).map((a) => (
          <tr key={a.id}><td>{a.handle}<div className="muted">{a.platform} · style {a.style_preset}</div></td><td>{a.niche}</td>
            <td><Pill s={a.active ? "active" : "paused"} />{a.paused_reason && <div className="muted">{a.paused_reason}</div>}</td>
            <td><input type="number" min={0} max={100} defaultValue={a.auto_approve_score} style={{ width: 70 }}
              onBlur={(e) => patch(a.id, { auto_approve_score: Number(e.target.value) })} /></td>
            <td><input type="number" min={0} max={10} defaultValue={a.daily_cap} style={{ width: 60 }}
              onBlur={(e) => patch(a.id, { daily_cap: Number(e.target.value) })} /></td>
            <td><button onClick={() => patch(a.id, { active: !a.active })}>{a.active ? "Pause" : "Resume"}</button></td></tr>))}
        </tbody>
      </table>
    </Section>
  );
}

function Analytics() {
  const { data, error, reload } = useData<Any>("/api/analytics");
  const [msg, setMsg] = useState<string | null>(null);
  const setApproved = async (id: number, v: string) => {
    if (v === "") return;
    try { await send("POST", `/api/posts/${id}/approved-views`, { approved_views: Number(v) }); reload(); } catch (e: any) { setMsg(e.message); }
  };
  return (
    <Section title="Analytics" error={error ?? msg}>
      <div className="cards">{Object.entries(data?.campaigns ?? {}).map(([name, c]: [string, any]) => (
        <div className="card" key={name}><div className="n">${c.earnings.toFixed(2)}</div><div className="l">{name}: {c.approved_views.toLocaleString()} approved of {c.views.toLocaleString()} views</div></div>))}
        {Object.entries(data?.accounts ?? {}).map(([h, a]: [string, any]) => (
          <div className="card" key={h}><div className="n">{a.views.toLocaleString()}</div><div className="l">{h}: views over {a.posts} posts</div></div>))}
      </div>
      <table>
        <thead><tr><th>Clip</th><th>Account</th><th>Views</th><th>Likes</th><th>Shares</th><th>Avg watched</th><th>Reward</th><th>Approved views (from marketplace)</th><th>Earnings</th></tr></thead>
        <tbody>{(data?.clips ?? []).map((c: Any) => (
          <tr key={c.post_id}><td>{c.title}<div className="muted">{c.bucket}</div></td><td>{c.account}</td><td className="num">{c.views}</td><td className="num">{c.likes}</td>
            <td className="num">{c.shares}</td><td className="num">{c.avg_view_pct ? `${c.avg_view_pct.toFixed(0)}%` : "-"}</td>
            <td className="num">{c.reward ?? "-"}</td>
            <td><input type="number" min={0} defaultValue={c.approved_views ?? ""} style={{ width: 110 }} onBlur={(e) => setApproved(c.post_id, e.target.value)} /></td>
            <td className="num">${c.earnings.toFixed(2)}</td></tr>))}
        </tbody>
      </table>
    </Section>
  );
}

function Bandit() {
  const { data, error } = useData<Any[]>("/api/bandit");
  return (
    <Section title="What's working (bandit arms)" error={error}>
      <p className="muted">Probability each option beats the account's median clip. More posts make these sharper.</p>
      <table>
        <thead><tr><th>Account</th><th>Choice</th><th>Option</th><th>Win chance</th><th>Posts</th></tr></thead>
        <tbody>{(data ?? []).map((b, i) => (
          <tr key={i}><td>{b.account}</td><td>{b.dimension.replace(/_/g, " ")}</td><td>{b.value}</td><td className="num">{(b.mean * 100).toFixed(0)}%</td><td className="num">{b.pulls}</td></tr>))}
        </tbody>
      </table>
    </Section>
  );
}

function Prompts() {
  const { data, error, reload } = useData<Any[]>("/api/prompts");
  const [msg, setMsg] = useState<string | null>(null);
  const roll = async (name: string, version: number) => {
    if (!confirm(`Make ${name} v${version} the active prompt?`)) return;
    try { await send("POST", "/api/prompts/rollback", { name, version }); reload(); } catch (e: any) { setMsg(e.message); }
  };
  return (
    <Section title="Highlight prompt versions" error={error ?? msg}>
      {(data ?? []).length === 0 && <p className="muted">The tuner creates the first new version once 16 posted clips have rewards.</p>}
      <table>
        <thead><tr><th>Prompt</th><th>Version</th><th>Status</th><th>Share</th><th>Examples</th><th>Notes</th><th></th></tr></thead>
        <tbody>{(data ?? []).map((p) => (
          <tr key={p.id}><td>{p.name}</td><td className="num">v{p.version}</td><td><Pill s={p.status} /></td><td className="num">{Math.round(p.ab_share * 100)}%</td>
            <td className="num">{p.examples}</td><td className="muted">{p.notes}</td>
            <td>{p.status !== "active" && <button onClick={() => roll(p.name, p.version)}>Roll back to this</button>}</td></tr>))}
        </tbody>
      </table>
    </Section>
  );
}

function Decisions() {
  const { data, error } = useData<Any[]>("/api/decisions?limit=200");
  return (
    <Section title="Decision log" error={error}>
      <table>
        <thead><tr><th>When</th><th>Module</th><th>Action</th><th>Subject</th><th>Reason</th></tr></thead>
        <tbody>{(data ?? []).map((d) => (
          <tr key={d.id}><td>{fmt(d.at)}</td><td>{d.module}</td><td><Pill s={d.action} /></td><td className="muted">{d.subject}</td><td>{d.reason}</td></tr>))}
        </tbody>
      </table>
    </Section>
  );
}


// ---------------------------------------------------------------- live status
const STAGES = ["downloading", "transcribing", "finding signals", "picking highlights", "safety gate", "rendering"];

function ago(sec: number) {
  if (sec < 60) return `${Math.round(sec)}s`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m ${Math.round(sec % 60)}s`;
  return `${Math.floor(sec / 3600)}h ${Math.floor((sec % 3600) / 60)}m`;
}

function Live() {
  const [d, setD] = useState<Any | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [now, setNow] = useState(Date.now() / 1000);
  useEffect(() => {
    const load = () => get("/api/live").then((x) => { setD(x); setError(null); }).catch((e) => setError(String(e.message ?? e)));
    load();
    const a = setInterval(load, 5000), b = setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => { clearInterval(a); clearInterval(b); };
  }, []);
  const c = d?.current;
  const running = d?.processing && c?.running;
  const done: string[] = (c?.stages ?? []).filter((s: Any) => s.ended).map((s: Any) => s.stage);
  return (
    <Section title="Live status" error={error}>
      <div className="card" style={{ marginBottom: 14 }}>
        <div className="row" style={{ justifyContent: "space-between" }}>
          <b>{running ? "Working on" : c ? "Last run" : "Idle"}</b>
          <span className={`pill ${running ? "active" : "held"}`}>{running ? "processing" : "waiting for the next run (every 10 min)"}</span>
        </div>
        {c ? (
          <>
            <div style={{ fontSize: 16, margin: "6px 0" }}>{c.video?.title}</div>
            <div className="muted">{c.video?.channel} · {c.video?.platform} · {c.video?.minutes} min · started {ago(now - c.started)} ago</div>
            <div className="row" style={{ margin: "12px 0", gap: 6 }}>
              {STAGES.map((st) => (
                <span key={st} className={`pill ${c.stage === st && running ? "scheduled" : done.includes(st) ? "posted" : ""}`}>
                  {done.includes(st) ? "✓ " : c.stage === st && running ? "▶ " : ""}{st}
                </span>
              ))}
            </div>
            <div><b>{c.stage}</b> <span className="muted">{c.detail}</span></div>
            {c.pct != null && running && (
              <div style={{ background: "var(--line)", borderRadius: 6, height: 10, marginTop: 8 }}>
                <div style={{ width: `${c.pct}%`, background: "var(--accent)", height: 10, borderRadius: 6, transition: "width .5s" }} />
              </div>
            )}
            {c.pct != null && running && <div className="muted">{c.pct}% · stage running {ago(now - (c.stages?.at(-1)?.started ?? now))}</div>}
            {!running && <div className="muted">Finished {ago(now - c.updated)} ago</div>}
          </>
        ) : <p className="muted">Nothing has been processed since the last restart.</p>}
      </div>

      <div className="cards">
        <div className="card"><div className="n">{d?.queue_total ?? "-"}</div><div className="l">videos queued</div></div>
        <div className="card"><div className="n">{d?.recent_clips?.length ?? 0}</div><div className="l">latest clips shown below</div></div>
        <div className="card"><div className="n">{d?.upcoming_posts?.length ?? 0}</div><div className="l">posts scheduled next</div></div>
        <div className="card"><div className="n">{d?.errors?.length ?? 0}</div><div className="l">problems in the last 24h</div></div>
      </div>

      <h3>Next in the queue</h3>
      <table>
        <thead><tr><th>Video</th><th>Length</th><th>Stage</th><th>Rank</th></tr></thead>
        <tbody>{(d?.queue ?? []).map((v: Any) => (
          <tr key={v.platform + v.video_id}><td>{v.title}<div className="muted">{v.channel} · {v.platform}</div></td>
            <td className="num">{v.minutes} min</td><td><Pill s={v.status} /></td><td className="num">{v.rank?.toFixed?.(2) ?? "-"}</td></tr>))}
        </tbody>
      </table>

      <h3>Upcoming posts</h3>
      <table>
        <thead><tr><th>When</th><th>Where</th><th>Clip</th></tr></thead>
        <tbody>{(d?.upcoming_posts ?? []).length === 0 && <tr><td colSpan={3} className="muted">None scheduled yet: they appear once a video finishes rendering.</td></tr>}
          {(d?.upcoming_posts ?? []).map((p: Any) => (
            <tr key={p.id}><td>{fmt(p.scheduled_at)}</td><td>{p.platform} · {p.account}</td><td>{p.title}</td></tr>))}
        </tbody>
      </table>

      <h3>Latest clips</h3>
      <table>
        <thead><tr><th>Clip</th><th>Status</th><th>Made</th></tr></thead>
        <tbody>{(d?.recent_clips ?? []).map((c: Any) => (
          <tr key={c.id}><td>{c.preview ? <a href={`${API}${c.preview}`} target="_blank" rel="noreferrer">{c.title}</a> : c.title}</td>
            <td><Pill s={c.status} /></td><td>{fmt(c.created_at)}</td></tr>))}
        </tbody>
      </table>

      <h3>Problems (last 24h)</h3>
      <table>
        <thead><tr><th>When</th><th>Where</th><th>What</th></tr></thead>
        <tbody>{(d?.errors ?? []).length === 0 && <tr><td colSpan={3} className="muted">None.</td></tr>}
          {(d?.errors ?? []).map((e: Any, i: number) => (
            <tr key={i}><td>{fmt(e.at)}</td><td>{e.module} <Pill s={e.action} /></td><td>{e.reason}</td></tr>))}
        </tbody>
      </table>
    </Section>
  );
}

const VIEWS: Record<Tab, () => ReactNode> = { Live, Overview, Pipeline, Clips, Calendar, Submissions, Campaigns, Accounts,
  Analytics, Bandit, Prompts, Decisions };

export default function Dashboard() {
  const [tab, setTab] = useState<Tab>("Live");
  const View = VIEWS[tab];
  return (
    <div className="shell">
      <nav>
        <h1>autoclip</h1>
        {TABS.map((t) => <button key={t} className={t === tab ? "on" : ""} onClick={() => setTab(t)}>{t}</button>)}
        <form action={logout}><button type="submit" className="muted">Log out</button></form>
      </nav>
      <main className="content"><View /></main>
    </div>
  );
}
