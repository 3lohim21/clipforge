import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { STATUS_LABEL, scoreTone } from "../lib/format";

export function Spinner() {
  return <span className="spinner" aria-label="Loading" />;
}

export function ProgressBar({ value }: { value: number }) {
  return (
    <div className="progress" role="progressbar" aria-valuenow={Math.round(value * 100)} aria-valuemin={0} aria-valuemax={100}>
      <div style={{ width: `${Math.max(2, Math.min(100, value * 100))}%` }} />
    </div>
  );
}

const STATUS_TONE: Record<string, string> = {
  ready: "green",
  done: "green",
  failed: "red",
  queued: "",
  saved: "green",
  suggested: "violet",
  discarded: "",
};

export function StatusBadge({ status }: { status: string }) {
  const tone = STATUS_TONE[status] ?? "amber";
  const working = !["ready", "done", "failed", "saved", "suggested", "discarded", "queued"].includes(status);
  return (
    <span className={`badge ${tone}`}>
      {working && <span className="spinner" style={{ width: 10, height: 10, borderWidth: 1.5 }} />}
      {STATUS_LABEL[status] ?? status}
    </span>
  );
}

export function Score({ value }: { value: number | null | undefined }) {
  return (
    <span className={`score ${scoreTone(value)}`} title="Virality score (0-100)">
      {value ?? "–"}
    </span>
  );
}

export function Empty({ icon, title, children }: { icon?: ReactNode; title: string; children?: ReactNode }) {
  return (
    <div className="empty">
      {icon}
      <h3>{title}</h3>
      {children && <div>{children}</div>}
    </div>
  );
}

export function Modal({ title, onClose, children }: { title: string; onClose: () => void; children: ReactNode }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div className="modal-backdrop" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal" role="dialog" aria-modal="true" aria-label={title}>
        <h2>{title}</h2>
        {children}
      </div>
    </div>
  );
}

// --- toasts ----------------------------------------------------------------------------------
type Toast = { id: number; text: string; error?: boolean };
const ToastContext = createContext<(text: string, error?: boolean) => void>(() => undefined);

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toast, setToast] = useState<Toast | null>(null);
  const timer = useRef<number | undefined>(undefined);
  const show = useCallback((text: string, error = false) => {
    window.clearTimeout(timer.current);
    setToast({ id: Date.now(), text, error });
    timer.current = window.setTimeout(() => setToast(null), error ? 6000 : 2600);
  }, []);
  return (
    <ToastContext.Provider value={show}>
      {children}
      {toast && (
        <div className={`toast ${toast.error ? "error" : ""}`} role="status" key={toast.id}>
          {toast.text}
        </div>
      )}
    </ToastContext.Provider>
  );
}

export const useToast = () => useContext(ToastContext);

export function errorText(err: unknown): string {
  return err instanceof Error ? err.message : "Something went wrong";
}
