// 无头 Electron：把 B-evolution.svg 渲染成全套尺寸 PNG
// 用法: 在 app/ 目录执行  node_modules/.bin/electron ../design/icons/make_icons.js
const { app, BrowserWindow } = require('electron')
const fs = require('fs')
const path = require('path')

app.disableHardwareAcceleration()
app.commandLine.appendSwitch('no-sandbox')
app.commandLine.appendSwitch('disable-gpu')
setTimeout(() => { console.error('timeout'); app.exit(1) }, 30000)

const dir = __dirname
const outDir = path.join(dir, 'final')
fs.mkdirSync(outDir, { recursive: true })
const svg = fs.readFileSync(path.join(dir, 'B-evolution.svg')).toString('base64')
const SIZES = [512, 256, 64, 48, 32, 24, 16]

const html = `<!doctype html><body>
<img src="data:image/svg+xml;base64,${svg}">
<script>
window.render = async () => {
  const out = []
  for (const img of document.images) {
    await img.decode()
    for (const s of ${JSON.stringify(SIZES)}) {
      const c = document.createElement('canvas')
      c.width = c.height = s
      c.getContext('2d').drawImage(img, 0, 0, s, s)
      out.push({ name: 'icon@' + s + '.png', data: c.toDataURL('image/png') })
    }
  }
  return out
}
</script></body>`

app.whenReady().then(async () => {
  const win = new BrowserWindow({ show: false, width: 400, height: 400 })
  await win.loadURL('data:text/html;base64,' + Buffer.from(html).toString('base64'))
  const out = await win.webContents.executeJavaScript('window.render()')
  for (const o of out) {
    fs.writeFileSync(path.join(outDir, o.name), Buffer.from(o.data.split(',')[1], 'base64'))
  }
  console.log('rendered', out.length, 'files ->', outDir)
  app.exit(0)
})
