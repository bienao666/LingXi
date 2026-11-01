import { defineConfig } from 'vite';
import { cpSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
const root = fileURLToPath(new URL('.', import.meta.url));
export default defineConfig({
  root, publicDir: false,
  server: { host: '127.0.0.1', port: 5173, proxy: { '/api': {target:'http://127.0.0.1:8000',ws:true}, '/store/v1': {target:'http://127.0.0.1:8001', configure(proxy){proxy.on('proxyReq',(req,source)=>{const cookie=source.headers.cookie?.split(';').map(v=>v.trim()).find(v=>v.startsWith('lingxi_store_session='));req.removeHeader('cookie');if(cookie)req.setHeader('cookie',cookie);});}} } },
  preview: { port: 4174 }, build: { outDir: 'dist', emptyOutDir: true },
  plugins: [{ name: 'lingxi-local-assets', closeBundle() { cpSync(root + '/assets', root + '/dist/assets', { recursive: true }); } }]
});
