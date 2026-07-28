import react from '@vitejs/plugin-react';
import { defineConfig } from 'vitest/config';

export default defineConfig({
  root: 'frontend',
  plugins: [react()],
  test: {
    environment: 'jsdom',
    setupFiles: './test/setup.ts',
    include: ['src/**/*.test.{ts,tsx}'],
    restoreMocks: true,
  },
});
