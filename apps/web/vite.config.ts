import vue from "@vitejs/plugin-vue";
import { defineConfig, type ProxyOptions } from "vite";

const API_ROUTE_PATTERN =
  "^/(health|projects|workspaces|jobs|settings|templates|team|platform)(?:/|:|$)";

function createApiProxy(): Record<string, ProxyOptions> {
  return {
    [API_ROUTE_PATTERN]: {
      target: "http://127.0.0.1:8000",
      changeOrigin: true,
      // /settings and /templates are both browser routes and API roots. A hard navigation must
      // stay in Vite; fetch requests (whose Accept is not text/html) still reach FastAPI.
      bypass(request) {
        return request.headers.accept?.includes("text/html") ? "/index.html" : undefined;
      },
    },
  };
}

export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    strictPort: true,
    // Production is same-origin. Local dev and preview preserve that contract without a hidden
    // environment file, while HTML navigations still remain in the SPA.
    proxy: createApiProxy(),
  },
  preview: {
    port: 4173,
    strictPort: true,
    proxy: createApiProxy(),
  },
});
