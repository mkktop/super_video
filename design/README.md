# 设计资产（design/）

## 目录索引

```
design/
├─ icons/
│  ├─ rainframe/        现役图标定稿源（详见下）
│  │  ├─ rainframe.svg    「极光取景框」定稿源（渐变底 + 白色取景框键，2026-09-25 定稿）
│  │  ├─ make_icons.js    SVG → 全套尺寸 PNG（无头 Electron）
│  │  └─ build_assets.py  PNG → app/build 成品（Pillow）
│  ├─ final/            全套尺寸 PNG（icon@16 … icon@512，由脚本生成）
│  ├─ B-evolution.svg   上一代图标源（已被雨帧方案取代，留档）
│  └─ make_icons.js     上一代的渲染脚本（读 B-evolution.svg）
├─ installer/
│  ├─ make_assets.py    NSIS 安装器素材 + 中文许可协议生成
│  └─ preview/          安装器各页预览 PNG（人工核对用，不进安装包）
├─ UI_OPTIMIZATION_PROMPT.md    UI 优化需求书（结构化大需求，一次全量交付）
└─ settings-redesign-prompt.md  设置页重构需求书
```

`rainframe/` 里的 `variants*.html` + `shot*.js` + `sheet*.png` 是定稿前的候选
变体页与截图拼图（过程件，供回顾，勿再修改）。

## 图标渲染管线（三条命令出全套）

无 ImageMagick，用无头 Electron 渲 SVG、Pillow 组装成品：

```bash
# 1) SVG → 全套尺寸 PNG（在 app/ 目录执行；产物写 design/icons/final/）
cd app && node_modules/.bin/electron ../design/icons/rainframe/make_icons.js

# 2) icon@512 → app/build/icon.ico + icon.png、标题栏 logo、安装器位图
#    （仓库根目录，用 .venv 的 Pillow）
.venv/Scripts/python.exe design/icons/rainframe/build_assets.py

# 3) 安装器专用素材 + 中文许可协议（installerHeader/Sidebar.bmp、license_zh.rtf）
python design/installer/make_assets.py
```

改图标只改 `rainframe.svg`，然后按序跑三条命令；`final/` 与 `app/build/`
下的产物全部由脚本再生，勿手改。
