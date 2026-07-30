import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Built assets are served by chickenskratch-ui (FastAPI/Starlette) from web/dist;
// the dev server proxies /api to it so `npm run dev` works against a live
// engine.
export default defineConfig({
  plugins: [react()],
  build: { outDir: "dist" },
  server: {
    proxy: {
      "/api": { target: "http://127.0.0.1:8399", changeOrigin: false },
    },
  },
});
