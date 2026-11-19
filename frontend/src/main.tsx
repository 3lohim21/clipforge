import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode, lazy, Suspense } from "react";
import { createRoot } from "react-dom/client";
import { createBrowserRouter, RouterProvider } from "react-router";
import { ApiError } from "./api/client";
import { RequireAuth } from "./components/AppShell";
import { Spinner, ToastProvider } from "./components/ui";
import { LoginPage } from "./pages/Login";
import { ProjectsPage } from "./pages/Projects";
import "./styles.css";

// The editor is the heaviest screen; load it on demand so the dashboard paints fast.
const ProjectPage = lazy(() => import("./pages/Project"));
const WorkspacePage = lazy(() => import("./workspace/Workspace"));
const TeamPage = lazy(() => import("./pages/Team"));

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 10_000,
      refetchOnWindowFocus: false,
      retry: (count, err) => !(err instanceof ApiError && err.status >= 400 && err.status < 500) && count < 2,
    },
  },
});

const fallback = (
  <div className="auth">
    <Spinner />
  </div>
);

const router = createBrowserRouter([
  { path: "/login", element: <LoginPage /> },
  {
    element: <RequireAuth />,
    children: [
      { path: "/", element: <ProjectsPage /> },
      { path: "/team", element: <Suspense fallback={fallback}><TeamPage /></Suspense> },
      { path: "/projects/:projectId", element: <Suspense fallback={fallback}><ProjectPage /></Suspense> },
      { path: "/videos/:videoId", element: <Suspense fallback={fallback}><WorkspacePage /></Suspense> },
    ],
  },
]);

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <ToastProvider>
        <RouterProvider router={router} />
      </ToastProvider>
    </QueryClientProvider>
  </StrictMode>,
);
