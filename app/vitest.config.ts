import { defineConfig } from 'vitest/config'
import vue from '@vitejs/plugin-vue'
import { resolve } from 'path'

export default defineConfig({
  plugins: [vue()],
  test: {
    environment: 'jsdom',
    include: ['tests/**/*.spec.ts'],
  },
  resolve: {
    // 测试里以 src/renderer/src 为根导入业务模块（与 renderer 构建同源码路径）
    alias: { '@src': resolve(__dirname, 'src/renderer/src') },
  },
})
