import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  root: 'frontend',
  plugins: [react()],
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    // React Admin and MUI form one shared runtime chunk. Feature screens remain
    // lazy-loaded; 650 kB is the reviewed ceiling for this initial shell.
    chunkSizeWarningLimit: 650,
  },
  server: {
    port: 5173,
    proxy: {
      '/api': 'http://localhost:3000',
    },
  },
});
