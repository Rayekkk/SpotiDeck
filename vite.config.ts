import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';
import { fileURLToPath, URL } from 'node:url';
export default defineConfig({
  root: 'preview',
  plugins: [react()],
  resolve: { alias: [{ find: /^\.\/platform$/, replacement: fileURLToPath(new URL('./preview/platform.tsx', import.meta.url)) }] },
  build: { outDir: '../build/preview', emptyOutDir: true },
  server: { port: 5173, strictPort: true },
  test: { root: '.', environment: 'jsdom', include: ['tests/**/*.test.ts', 'tests/**/*.test.tsx'] }
});
