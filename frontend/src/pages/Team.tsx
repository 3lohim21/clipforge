import { useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { Link } from "react-router";
import { del, post } from "../api/client";
import { keys, useMe, useUsers } from "../api/queries";
import { TopBar } from "../components/AppShell";
import { Plus, Trash } from "../components/icons";
import { errorText, Spinner, useToast } from "../components/ui";
import { timeAgo } from "../lib/format";

export default function TeamPage() {
  const me = useMe();
  const users = useUsers();
  const qc = useQueryClient();
  const toast = useToast();
  const [form, setForm] = useState({ email: "", name: "", password: "", role: "editor" });
  const isAdmin = me.data && ["owner", "admin"].includes(me.data.user.role);

  const add = async (e: FormEvent) => {
    e.preventDefault();
    try {
      await post("/api/users", form);
      setForm({ email: "", name: "", password: "", role: "editor" });
      qc.invalidateQueries({ queryKey: keys.users });
      toast("Teammate added — share their password securely");
    } catch (err) {
      toast(errorText(err), true);
    }
  };

  return (
    <div className="shell">
      <TopBar>
        <Link to="/">Clients</Link>
        <span className="sep">/</span>
        <span style={{ color: "var(--text)" }}>Team</span>
      </TopBar>
      <main className="page">
        <div className="container" style={{ maxWidth: 860 }}>
          <div className="page-head">
            <div className="grow">
              <h1>Team</h1>
              <div className="muted">Editors can clip and export; admins can also manage the team.</div>
            </div>
          </div>
          {users.isPending ? (
            <Spinner />
          ) : (
            <div className="card" style={{ marginBottom: 18 }}>
              <table className="table">
                <thead>
                  <tr>
                    <th>Member</th>
                    <th>Role</th>
                    <th>Last sign-in</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {users.data?.map((u) => (
                    <tr key={u.id}>
                      <td>
                        <div style={{ fontWeight: 600 }}>{u.name || u.email}</div>
                        <div className="faint" style={{ fontSize: 12 }}>
                          {u.email}
                        </div>
                      </td>
                      <td>
                        <span className="badge">{u.role}</span>
                      </td>
                      <td className="muted">{u.last_login_at ? timeAgo(u.last_login_at) : "never"}</td>
                      <td>
                        {isAdmin && u.role !== "owner" && u.id !== me.data?.user.id && (
                          <button
                            className="btn sm icon ghost"
                            title="Remove"
                            onClick={async () => {
                              if (!confirm(`Remove ${u.email}?`)) return;
                              await del(`/api/users/${u.id}`);
                              qc.invalidateQueries({ queryKey: keys.users });
                            }}
                          >
                            <Trash />
                          </button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {isAdmin && (
            <form className="card pad stack" onSubmit={add}>
              <div className="section-title">Add a teammate</div>
              <div className="settings-grid">
                <input className="input" type="email" required placeholder="email@agency.com" value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} />
                <input className="input" placeholder="Name" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
                <input className="input" type="password" required minLength={10} placeholder="Temporary password (10+)" value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} />
                <select className="select" value={form.role} onChange={(e) => setForm({ ...form, role: e.target.value })}>
                  <option value="editor">Editor</option>
                  <option value="admin">Admin</option>
                </select>
              </div>
              <div>
                <button className="btn primary">
                  <Plus /> Add teammate
                </button>
              </div>
            </form>
          )}
        </div>
      </main>
    </div>
  );
}
