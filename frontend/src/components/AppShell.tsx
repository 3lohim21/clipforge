import { useQueryClient } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { Link, Navigate, Outlet, useLocation, useNavigate } from "react-router";
import { ApiError, post } from "../api/client";
import { useMe } from "../api/queries";
import { Logo } from "./icons";
import { Spinner } from "./ui";

export function RequireAuth() {
  const me = useMe();
  const location = useLocation();
  if (me.isPending) {
    return (
      <div className="auth">
        <Spinner />
      </div>
    );
  }
  if (me.error instanceof ApiError && me.error.status === 401) {
    return <Navigate to="/login" replace state={{ from: location.pathname }} />;
  }
  return <Outlet />;
}

export function TopBar({ children }: { children?: ReactNode }) {
  const me = useMe();
  const qc = useQueryClient();
  const navigate = useNavigate();
  const logout = async () => {
    await post("/api/auth/logout").catch(() => undefined);
    qc.clear();
    navigate("/login");
  };
  return (
    <header className="topbar">
      <Link to="/" className="brand">
        <Logo />
        ClipForge
      </Link>
      <div className="crumbs grow">{children}</div>
      {me.data && (
        <div className="row">
          <span className="muted" style={{ fontSize: 13 }}>
            {me.data.user.email}
          </span>
          <button className="btn sm ghost" onClick={logout}>
            Sign out
          </button>
        </div>
      )}
    </header>
  );
}
