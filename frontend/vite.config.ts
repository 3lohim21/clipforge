import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In development the API runs on :8000; proxying keeps everything same-origin so the
// HttpOnly session cookie and <video> range requests work exactly as in production.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": { target: "http://127.0.0.1:8000", changeOrigin: false },
    },
  },
  build: {
    target: "es2022",
    sourcemap: true,
    chunkSizeWarningLimit: 600,
  },
});
