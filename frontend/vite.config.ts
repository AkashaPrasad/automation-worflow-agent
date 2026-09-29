import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { fileURLToPath } from "node:url";

// Static SPA build (frontend/dist) for Cloudflare Pages.
// The in-browser mock backend is only bundled when VITE_MOCK=1; otherwise `@mock` resolves to a stub.
export default defineConfig(({ mode }) => {
  const env = { ...loadEnv(mode, process.cwd(), "VITE_"), ...process.env };
  const mock = env.VITE_MOCK === "1";
  return {
    plugins: [react(), tailwindcss()],
    resolve: {
      alias: {
        "@mock": fileURLToPath(new URL(mock ? "./src/mock/engine.ts" : "./src/mock/stub.ts", import.meta.url)),
      },
    },
    server: { port: 5173, strictPort: false },
    preview: { port: 4173 },
    build: {
      target: "es2022",
      sourcemap: false,
      chunkSizeWarningLimit: 900,
    },
  };
});
