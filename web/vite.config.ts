import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  build: {
    outDir: '../src/pyaerial/static',
    emptyOutDir: true,
  },
  server: {
    proxy: {
      '/api': 'http://127.0.0.1:10090',
      '/ws': {
        target: 'ws://127.0.0.1:10090',
        ws: true,
      },
    },
  },
});
