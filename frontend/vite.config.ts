import { defineConfig } from 'vite';
import { cpSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
const root = fileURLToPath(new URL('.', import.meta.url));
export default defineConfig({
  root, publicDir: false,
  server: { host: '127.0.0.1', port: 5173, proxy: { '/api': 'http://127.0.0.1:8000' } },
  preview: { port: 4174 }, build: { outDir: 'dist', emptyOutDir: true },
  plugins: [{ name: 'lingxi-local-assets', closeBundle() { cpSync(root + '/assets', root + '/dist/assets', { recursive: true }); } }]
});
