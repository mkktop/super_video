// 无头 Electron：把目录下所有 SVG 用 canvas 渲染成 PNG（256/64/32）
// 用法: 在 app/ 目录执行  node_modules/.bin/electron ../design/icons/render.js
const { app, BrowserWindow } = require('electron')
const fs = require('fs')
const path = require('path')

app.disableHardwareAcceleration()
app.commandLine.appendSwitch('no-sandbox')
app.commandLine.appendSwitch('disable-gpu')
setTimeout(() => { console.error('timeout'); app.exit(1) }, 30000)

const dir = __dirname
const outDir = path.join(dir, 'png')
fs.mkdirSync(outDir, { recursive: true })
const files = fs.readdirSync(dir).filter(f => f.endsWith('.svg')).sort()

const parts = files.map(f => {
  const b64 = fs.readFileSync(path.join(dir, f)).toString('base64')
  return `<img id="${f}" src="data:image/svg+xml;base64,${b64}">`
}).join('')

const html = `<!doctype html><body>${parts}<script>
window.render = async () => {
  const out = []
  for (const img of document.images) {
    await img.decode()
    for (const s of [256, 64, 32]) {
      const c = document.createElement('canvas')
      c.width = c.height = s
      c.getContext('2d').drawImage(img, 0, 0, s, s)
      out.push({ name: img.id.replace('.svg', '') + '@' + s + '.png', data: c.toDataURL('image/png') })
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
