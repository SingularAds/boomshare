import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// The dashboard is served by the API itself, from /dashboard, so the built
// asset URLs have to be rooted there. Output lands inside the Python package
// because the Dockerfile ships `app/` and nothing else.
export default defineConfig({
  base: "/dashboard/",
  plugins: [react(), tailwindcss()],
  build: {
    outDir: "../app/api/static/dashboard",
    emptyOutDir: true,
  },
  server: {
    // `npm run dev` talks to a locally running API.
    proxy: { "/admin": "http://localhost:8000" },
  },
});
