import { defineConfig } from "vite";

// Le build va directement dans le package Python : servi par castdeck, 100 % offline.
export default defineConfig({
  base: "./",
  build: {
    outDir: "../castdeck/static",
    emptyOutDir: true,
    target: "es2020",
    chunkSizeWarningLimit: 1500,
  },
  server: {
    // npm run dev  +  castdeck <dossier> (port 8000)
    proxy: { "/api": { target: "http://127.0.0.1:8000", changeOrigin: true } },
  },
  test: { environment: "node" },
});
