import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  build: {
    target: 'es2020',
    sourcemap: false,
    outDir: '../mclaw/web/static',
    emptyOutDir: true,
    rollupOptions: {
      output: {
        manualChunks: { react: ['react', 'react-dom'], markdown: ['react-markdown', 'remark-gfm'] },
      },
    },
  },
  server: { proxy: { '/api': { target: 'http://127.0.0.1:18789', ws: true } } },
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/test-setup.ts'],
    exclude: ['node_modules/**', 'src/vendor/**'],
  },
});
