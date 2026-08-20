/// <reference types="vitest/config" />
import { fileURLToPath, URL } from 'node:url';

import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

/**
 * The dev server proxies `/api/*` to the gateway so the browser sees a
 * same-origin API in development. That keeps the frontend's fetch code
 * identical in dev and in production (where Vercel rewrites `/api/*` to the
 * Render service), and it means CORS is a deployment concern rather than
 * something the app has to know about.
 */
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  build: {
    rollupOptions: {
      output: {
        // Recharts and Framer Motion together are most of the bundle, and
        // neither changes between deploys. Splitting them out means a code
        // change ships a ~90kB chunk instead of invalidating 850kB.
        manualChunks: {
          charts: ['recharts'],
          motion: ['framer-motion'],
          vendor: ['react', 'react-dom', 'react-router-dom', '@tanstack/react-query'],
        },
      },
    },
  },
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: process.env.VITE_GATEWAY_URL ?? 'http://localhost:8000',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ''),
      },
    },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    // Playwright owns e2e/. Without this, vitest tries to collect the
    // spec and fails on Playwright's own test() implementation.
    exclude: ['e2e/**', 'node_modules/**', 'dist/**'],
    setupFiles: ['./vitest.setup.ts'],
    css: false,
    // Generous per-test timeout: the first test in a cold worker pays for
    // module transform and jsdom setup, which on a slow machine dwarfs the
    // assertion itself.
    testTimeout: 20_000,
    hookTimeout: 20_000,
  },
});
