import react from '@vitejs/plugin-react';
// From vitest/config, not vite: only Vitest's defineConfig accepts the `test` block.
import { defineConfig } from 'vitest/config';

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // Fail rather than silently moving to another port: a moved port falls outside the
    // API's CORS allow-list and presents as an unexplained network error.
    strictPort: true,
  },
  build: {
    outDir: 'dist',
    sourcemap: true,
    rollupOptions: {
      output: {
        // Split the heavy, rarely-changing dependencies out of the app bundle. This
        // does not reduce total bytes, it fixes cache behaviour: without it, editing
        // one component invalidates the ~400 kB charting library for every user on
        // every deploy.
        // recharts is deliberately absent: it is lazy-imported by the one view that
        // needs it, so naming it here would pull it back into the eager graph.
        manualChunks: {
          react: ['react', 'react-dom'],
          query: ['@tanstack/react-query'],
        },
      },
    },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test-setup.ts'],
  },
});
