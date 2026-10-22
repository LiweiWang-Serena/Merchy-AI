import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The Flask server: it serves the API, the original pages and store.css.
const flask = "http://127.0.0.1:5050";

export default defineConfig({
  plugins: [react()],
  // Built files are served by Flask at /react/, next to the original shop at /.
  base: "/react/",
  server: {
    port: 5173,
    // During `npm run dev` this page runs on port 5173; these paths are
    // passed through to Flask so the API, product pages and cart still work.
    proxy: {
      "/api": flask,
      "/static": flask,
      "/product": flask,
      "/cart": flask,
      "/checkout": flask,
      "^/$": flask,
    },
    // src/main.tsx imports ../frontend/store.css, one folder up.
    fs: { allow: [".."] },
  },
});
