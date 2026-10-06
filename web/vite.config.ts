import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// Built straight into the Python app's static folder so the dashboard can serve it at /graph/
// without Node on the machine that runs it. `npm run dev` proxies the API to the running dashboard.
export default defineConfig({
  base: '/graph/',
  plugins: [react()],
  build: { outDir: '../src/static/graph', emptyOutDir: true, chunkSizeWarningLimit: 1500 },
  server: { proxy: { '/api': 'http://127.0.0.1:8765' } },
});
