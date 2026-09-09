import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()], build: { sourcemap: false },
  server: { port: 5173, strictPort: true, proxy: { '/api': {
    target: 'http://127.0.0.1:4319', changeOrigin: true,
    configure(proxy) { proxy.on('proxyReq', (proxyReq, req) => {
      if (req.headers.origin === 'http://127.0.0.1:5173') proxyReq.setHeader('Origin', 'http://127.0.0.1:4319');
    }); },
  } } },
});
