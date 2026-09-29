const { app, BrowserWindow } = require('electron')
const path = require('path')
app.disableHardwareAcceleration()
app.commandLine.appendSwitch('no-sandbox')
app.commandLine.appendSwitch('disable-gpu')
setTimeout(() => { console.error('timeout'); app.exit(1) }, 30000)
app.whenReady().then(async () => {
  const win = new BrowserWindow({ show: false, width: 900, height: 1400, useContentSize: true })
  await win.loadFile(path.join(__dirname, 'variants2.html'))
  await new Promise(r => setTimeout(r, 500))
  const fs = require('fs')
  const H = await win.webContents.executeJavaScript('document.body.scrollHeight')
  const vh = 1400
  for (let i = 0; i * vh < H; i++) {
    await win.webContents.executeJavaScript(`window.scrollTo(0, ${i * vh})`)
    await new Promise(r => setTimeout(r, 120))
    const img = await win.webContents.capturePage()
    fs.writeFileSync(path.join(__dirname, `sheet2_${i}.png`), img.toPNG())
  }
  console.log('saved', Math.ceil(H / vh), 'shots, contentHeight=', H)
  app.exit(0)
})
