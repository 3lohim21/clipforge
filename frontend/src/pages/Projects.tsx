import { useMemo, useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router";
import { useCreateProject, useProjects } from "../api/queries";
import { TopBar } from "../components/AppShell";
import { Film, Plus } from "../components/icons";
import { Empty, errorText, Modal, Spinner } from "../components/ui";
import { fmtCompact, fmtMoney, timeAgo } from "../lib/format";

export function ProjectsPage() {
  const projects = useProjects();
  const [creating, setCreating] = useState(false);
  const totals = useMemo(() => {
    const list = projects.data ?? [];
    const revenue = list.reduce((s, p) => s + (p.stats.est_revenue_usd ?? 0), 0);
    return {
      videos: list.reduce((s, p) => s + p.stats.videos, 0),
      clips: list.reduce((s, p) => s + p.stats.clips_saved, 0),
      renders: list.reduce((s, p) => s + p.stats.renders, 0),
      views: list.reduce((s, p) => s + p.stats.views, 0),
      revenue: list.some((p) => p.stats.est_revenue_usd != null) ? revenue : null,
    };
  }, [projects.data]);

  return (
    <div className="shell">
      <TopBar>
        <span>Clients</span>
        <span className="spacer" />
        <Link to="/team" className="btn sm ghost">
          Team
        </Link>
      </TopBar>
      <main className="page">
        <div className="container">
          <div className="page-head">
            <div className="grow">
              <h1>Clients &amp; campaigns</h1>
              <div className="muted">Each project holds one creator's videos, brand kit and clip rules.</div>
            </div>
            <button className="btn primary" onClick={() => setCreating(true)}>
              <Plus /> New project
            </button>
          </div>

          <div className="kpis">
            <Kpi label="Source videos" value={fmtCompact(totals.videos)} />
            <Kpi label="Clips saved" value={fmtCompact(totals.clips)} />
            <Kpi label="Exports" value={fmtCompact(totals.renders)} />
            <Kpi label="Tracked views" value={fmtCompact(totals.views)} />
            <Kpi label="Est. revenue" value={fmtMoney(totals.revenue)} />
          </div>

          {projects.isPending ? (
            <Spinner />
          ) : !projects.data?.length ? (
            <div className="card">
              <Empty icon={<Film width={28} height={28} />} title="No projects yet">
                Create one per client or campaign, then paste video links to start clipping.
                <div style={{ marginTop: 14 }}>
                  <button className="btn primary" onClick={() => setCreating(true)}>
                    <Plus /> New project
                  </button>
                </div>
              </Empty>
            </div>
          ) : (
            <div className="grid">
              {projects.data.map((p) => (
                <Link key={p.id} to={`/projects/${p.id}`} className="card project-card">
                  <div>
                    <h3 className="ellipsis">{p.name}</h3>
                    <div className="faint" style={{ fontSize: 12 }}>
                      Updated {timeAgo(p.updated_at)}
                    </div>
                  </div>
                  <div className="stats">
                    <Stat k="Videos" v={p.stats.videos} />
                    <Stat k="Clips" v={p.stats.clips_saved} />
                    <Stat k="Views" v={p.stats.views} />
                  </div>
                  {p.stats.clips_suggested > 0 && (
                    <span className="badge violet" style={{ alignSelf: "flex-start" }}>
                      {p.stats.clips_suggested} moments to review
                    </span>
                  )}
                </Link>
              ))}
            </div>
          )}
        </div>
      </main>
      {creating && <NewProjectModal onClose={() => setCreating(false)} />}
    </div>
  );
}

function Kpi({ label, value }: { label: string; value: string }) {
  return (
    <div className="card kpi">
      <div className="v">{value}</div>
      <div className="k">{label}</div>
    </div>
  );
}

function Stat({ k, v }: { k: string; v: number }) {
  return (
    <div className="stat">
      <div className="v">{fmtCompact(v)}</div>
      <div className="k">{k}</div>
    </div>
  );
}

function NewProjectModal({ onClose }: { onClose: () => void }) {
  const [name, setName] = useState("");
  const create = useCreateProject();
  const navigate = useNavigate();
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const p = await create.mutateAsync(name.trim());
    navigate(`/projects/${p.id}`);
  };
  return (
    <Modal title="New project" onClose={onClose}>
      <form className="stack" onSubmit={submit}>
        <label className="field">
          <span className="label">Client or campaign name</span>
          <input className="input" autoFocus required maxLength={120} value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. Diary of a CEO clips" />
        </label>
        {create.error && <div className="error-text">{errorText(create.error)}</div>}
        <div className="row">
          <span className="spacer" />
          <button type="button" className="btn ghost" onClick={onClose}>
            Cancel
          </button>
          <button className="btn primary" disabled={!name.trim() || create.isPending}>
            Create
          </button>
        </div>
      </form>
    </Modal>
  );
}
