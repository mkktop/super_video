---
name: gbk-settings-unicode-engine-fail
description: 1060 实机「引擎加载失败 UnicodeDecodeError」根因=settings.json 被外部工具存成
  GBK，worker 裸读引爆；已修复并随 v0.4.4 发布
metadata:
  node_type: memory
  type: project
  originSessionId: sess_283f3b77-57bb-42d0-aedf-2ef009b8c4ab
---

2026-09-03 用户另一台 GTX 1060 机器报「引擎加载失败 UnicodeDecodeError: 'utf-8' codec can't decode byte 0xc9 in position 240」（MangaJaNai x2 图片任务）。根因链：`data/settings.json` 被外部工具按 ANSI/GBK 重存（应用自身一直 UTF-8 写，记事本手改是主嫌疑）→ worker `_load_onnx_engine` 内三处**无保护**的 `settings.load()`（`read_text(encoding="utf-8")` 只捕 JSONDecodeError）→ 异常带「引擎加载失败」前缀硬失败。权重文件本身无辜（sha256+原子落盘）。

**Why:** 诊断要点：错误前缀反推唯一裸奔点（图片任务 `_run_image_job` 的引擎加载 try 块；视频任务同路径是裸调、报的是 worker 异常退出）；本机（5080 TRT/dev DML）均无法复现 = 状态型故障，须构造 GBK settings 复现。字节值/位置（0xc9/240）是 GBK 首字节落在文件第 240 字节处，值取决于那台机器的具体内容。

**How to apply:** 修复=sv/server/settings.py `load()` 读 bytes 后 utf-8-sig→GBK 兜底→都失败回默认；save() 恒 UTF-8 写 → GBK 文件在下次保存设置时自然愈合。同类隐患一并盖掉：user_presets.load、registry.load_registry 的 except 改含 ValueError（兼容盖 UnicodeDecodeError）。回归测试 tests/test_settings_encoding.py（GBK 值保留/BOM/垃圾回默认/save 愈合）。1060 机器的即时解法（修复发版前）：删除或记事本另存为 UTF-8 那台的 data/settings.json。另注： MangaJaNai 2048p 的 u8 包装 A/B 在 DML maxdiff=13 超容差(2)→按设计优雅回退标准路径（引擎打印 [engine] 行，非故障）。

**⚠ 同款报错第二病因（2026-09-03 晚，勿按本条套）**：同日装版再报一字不差的「引擎加载失败 UnicodeDecodeError」，但 settings.json 完好（UTF-8）——真实根因是 **DML 显存不足被掩盖**（ORT 错误消息 bytes 内嵌中文 Windows GBK 系统描述，绑定层 utf-8 硬解崩）。诊断分叉：先扫 DATA_ROOT 全 .json 编码排除本条病因，再查 `e.args` 原始 bytes（含 8007000E 即 OOM）。详见 [[dml-unicode-masked-oom]]。
