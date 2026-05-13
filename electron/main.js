/**
 * Electron main process — Ares
 * Serves the frontend via a local HTTP server so that fetch('/api/v1/...')
 * resolves correctly (not as file:// which silently fails).
 * Also spawns the Python backend and proxies /api + /ws to port 8000.
 */
const { app, BrowserWindow, Menu, shell, dialog, ipcMain, nativeTheme } = require('electron')
const path = require('path')
const { spawn, execSync } = require('child_process')
const http  = require('http')
const https = require('https')
const fs    = require('fs')
const net   = require('net')

let mainWindow   = null
let backendProcess = null
let frontendServer = null

const ICON_PATH = path.join(__dirname, '..', 'frontend', 'public', 'icon.png')

const BACKEND_PORT  = 8000
const FRONTEND_PORT = 3100          // internal static file server

// ── Utility: find a free port ─────────────────────────────────────────────────
function getFreePort(preferred) {
  return new Promise((resolve) => {
    const srv = net.createServer()
    srv.listen(preferred, () => {
      const { port } = srv.address()
      srv.close(() => resolve(port))
    })
    srv.on('error', () => {
      const srv2 = net.createServer()
      srv2.listen(0, () => {
        const { port } = srv2.address()
        srv2.close(() => resolve(port))
      })
    })
  })
}

// ── First-run setup: backend venv + frontend build, all from the app ──────────
// (so the user never has to touch a terminal — launch the app and it bootstraps
//  the Python environment, installs deps, and builds the UI on first run.)

function venvPython() {
  const d = path.join(__dirname, '..', 'backend', '.venv')
  return process.platform === 'win32' ? path.join(d, 'Scripts', 'python.exe') : path.join(d, 'bin', 'python')
}
function systemPython() {
  for (const c of (process.platform === 'win32' ? ['python', 'py'] : ['python3', 'python'])) {
    try { execSync(`${c} -c "import sys; assert sys.version_info >= (3,10)"`, { timeout: 4000, stdio: 'ignore' }); return c } catch {}
  }
  return null
}
function showSplash(status) {
  const html = `<!DOCTYPE html><html style="background:#0d1117;margin:0;height:100%"><body style="display:flex;align-items:center;justify-content:center;height:100vh;flex-direction:column;font-family:system-ui,sans-serif;color:#8b949e;margin:0">
<div style="font-size:24px;color:#00b4d8;font-weight:700;letter-spacing:1px">Ares</div>
<div id="status" style="margin-top:12px;font-size:13px">${status}</div>
<pre id="log" style="margin-top:14px;max-height:240px;width:78%;overflow:auto;font-size:11px;line-height:1.4;color:#6e7681;white-space:pre-wrap;background:#0b0f14;border:1px solid #21262d;border-radius:6px;padding:8px"></pre>
</body></html>`
  return mainWindow.loadURL('data:text/html;charset=utf-8,' + encodeURIComponent(html))
}
function setSplash(text) { mainWindow?.webContents.executeJavaScript(`(function(){var s=document.getElementById('status');if(s)s.textContent=${JSON.stringify(text)}})()`).catch(() => {}) }
function appendLog(text) { mainWindow?.webContents.executeJavaScript(`(function(){var l=document.getElementById('log');if(l){l.textContent+=${JSON.stringify(text)};l.scrollTop=l.scrollHeight}})()`).catch(() => {}) }
function splashError(msg) { setSplash('⚠ ' + msg); appendLog(`\n\nSetup failed.\nYou can also run install.sh (Linux/macOS) or install.bat (Windows) from a terminal once, then relaunch.\n`) }

function run(file, args, cwd, label) {
  return new Promise((resolve, reject) => {
    appendLog(`\n$ ${file} ${args.join(' ')}\n`)
    const p = spawn(file, args, { cwd, env: process.env, shell: process.platform === 'win32' })
    p.stdout.on('data', d => appendLog(d.toString()))
    p.stderr.on('data', d => appendLog(d.toString()))
    p.on('error', e => reject(new Error(`${label}: ${e.message} — is "${file}" installed and on PATH?`)))
    p.on('exit', code => code === 0 ? resolve() : reject(new Error(`${label} failed (exit ${code})`)))
  })
}
function isBackendUp() {
  return new Promise(resolve => {
    const req = http.request({ hostname: '127.0.0.1', port: BACKEND_PORT, path: '/api/v1/health', timeout: 900 },
      res => resolve(res.statusCode === 200))
    req.on('error', () => resolve(false))
    req.on('timeout', () => { req.destroy(); resolve(false) })
    req.end()
  })
}
async function ensureBackendEnv() {
  const backendDir = path.join(__dirname, '..', 'backend')
  const vpy = venvPython()
  if (!fs.existsSync(vpy)) {
    const sys = systemPython()
    if (!sys) throw new Error('Python 3.10+ not found — install it (and the python3-venv package on Debian/Ubuntu)')
    setSplash('First run: creating the Python environment…')
    await run(sys, ['-m', 'venv', '.venv'], backendDir, 'create venv')
  }
  let depsOk = false
  try { execSync(`"${vpy}" -c "import fastapi, numpy, pydantic_settings"`, { stdio: 'ignore', timeout: 6000 }); depsOk = true } catch {}
  if (!depsOk) {
    setSplash('First run: installing backend dependencies (a few minutes the first time)…')
    await run(vpy, ['-m', 'pip', 'install', '--upgrade', 'pip', '-q'], backendDir, 'pip upgrade').catch(() => {})
    await run(vpy, ['-m', 'pip', 'install', '-r', 'requirements.txt'], backendDir, 'pip install -r requirements.txt')
  }
}
async function ensureFrontendBuilt(distDir) {
  if (fs.existsSync(path.join(distDir, 'index.html'))) return
  const feDir = path.join(__dirname, '..', 'frontend')
  setSplash('First run: installing frontend dependencies…')
  await run('npm', ['install', '--no-audit', '--no-fund'], feDir, 'npm install (frontend)')
  setSplash('First run: building the UI (about a minute)…')
  await run('npm', ['run', 'build'], feDir, 'npm run build')
  if (!fs.existsSync(path.join(distDir, 'index.html'))) throw new Error('the frontend build did not produce dist/index.html')
}

// ── Serve frontend + proxy API ─────────────────────────────────────────────────
function startFrontendServer(distDir, port) {
  const MIME = {
    '.html': 'text/html',
    '.js':   'application/javascript',
    '.mjs':  'application/javascript',
    '.css':  'text/css',
    '.svg':  'image/svg+xml',
    '.png':  'image/png',
    '.jpg':  'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.gif':  'image/gif',
    '.webp': 'image/webp',
    '.ico':  'image/x-icon',
    '.json': 'application/json',
    '.geojson': 'application/json',
    '.txt':  'text/plain',
    '.xml':  'application/xml',
    '.wasm': 'application/wasm',          // Cesium decoders (draco/basis)
    '.glb':  'model/gltf-binary',
    '.gltf': 'model/gltf+json',
    '.bin':  'application/octet-stream',
    '.woff': 'font/woff',
    '.woff2':'font/woff2',
    '.ttf':  'font/ttf',
    '.terrain': 'application/octet-stream',
  }

  const server = http.createServer((req, res) => {
    const url = req.url.split('?')[0]

    // Proxy /api/* and /ws/* to the Python backend
    if (url.startsWith('/api/') || url.startsWith('/ws/')) {
      const options = {
        hostname: '127.0.0.1',
        port: BACKEND_PORT,
        path: req.url,
        method: req.method,
        headers: { ...req.headers, host: `127.0.0.1:${BACKEND_PORT}` },
      }
      const proxy = http.request(options, (backRes) => {
        res.writeHead(backRes.statusCode, backRes.headers)
        backRes.pipe(res)
      })
      proxy.on('error', (e) => {
        res.writeHead(502)
        res.end(`Backend unavailable: ${e.message}`)
      })
      req.pipe(proxy)
      return
    }

    // Serve static files from dist/
    let filePath = path.join(distDir, url === '/' ? 'index.html' : url)

    // SPA fallback: unknown paths → index.html
    if (!fs.existsSync(filePath)) {
      filePath = path.join(distDir, 'index.html')
    }

    const ext  = path.extname(filePath)
    const mime = MIME[ext] || 'application/octet-stream'

    fs.readFile(filePath, (err, data) => {
      if (err) {
        res.writeHead(404)
        res.end('Not found')
        return
      }
      res.writeHead(200, { 'Content-Type': mime })
      res.end(data)
    })
  })

  server.listen(port)
  return server
}

// ── Find Python ───────────────────────────────────────────────────────────────
function findPython() {
  const vpy = venvPython()           // OS-aware venv path
  if (fs.existsSync(vpy)) return vpy
  return systemPython()
}

// ── Start Python backend ──────────────────────────────────────────────────────
function startBackend() {
  const python = findPython()
  if (!python) {
    dialog.showErrorBox('Python 3 not found',
      'Install Python 3.10+ and run install.sh first.')
    app.quit()
    return
  }

  const backendDir = path.join(__dirname, '..', 'backend')
  if (!fs.existsSync(path.join(backendDir, 'app', 'main.py'))) {
    dialog.showErrorBox('Backend missing', `Cannot find backend at: ${backendDir}`)
    app.quit()
    return
  }

  const env = { ...process.env, PYTHONUNBUFFERED: '1', PORT: String(BACKEND_PORT), HOST: '127.0.0.1' }

  backendProcess = spawn(python,
    ['-m', 'uvicorn', 'app.main:app',
     '--host', '127.0.0.1', '--port', String(BACKEND_PORT)],
    { cwd: backendDir, env, stdio: ['ignore', 'pipe', 'pipe'] }
  )

  backendProcess.stdout.on('data', d => process.stdout.write(`[backend] ${d}`))
  backendProcess.stderr.on('data', d => process.stderr.write(`[backend] ${d}`))
  backendProcess.on('exit', (code, signal) => {
    if (code !== 0 && code !== null && mainWindow && !app.isQuitting) {
      console.error(`Backend exited: code=${code} signal=${signal}`)
    }
  })
}

// ── Wait for backend health check ─────────────────────────────────────────────
function waitForBackend(ms = 30000) {
  return new Promise((resolve, reject) => {
    const deadline = Date.now() + ms
    const check = () => {
      const req = http.request(
        { hostname: '127.0.0.1', port: BACKEND_PORT, path: '/api/v1/health', timeout: 1000 },
        res => { if (res.statusCode === 200) resolve(); else retry() }
      )
      req.on('error', retry)
      req.on('timeout', retry)
      req.end()
    }
    const retry = () => {
      if (Date.now() > deadline) return reject(new Error('Backend did not start in time'))
      setTimeout(check, 500)
    }
    check()
  })
}

// ── Create window ─────────────────────────────────────────────────────────────
async function createWindow() {
  const distDir = path.join(__dirname, '..', 'frontend', 'dist')

  mainWindow = new BrowserWindow({
    // Initial size is only used until the first call to maximize() below; we still set a sane
    // pre-maximize size so the "restore" button on the title bar gives a usable window rather
    // than something tiny.
    width: 1440,
    height: 900,
    minWidth: 900,
    minHeight: 600,
    title: 'Ares',
    backgroundColor: '#0d1117',
    icon: fs.existsSync(ICON_PATH) ? ICON_PATH : undefined,
    show: false,
    webPreferences: {
      nodeIntegration: false,
      contextIsolation: true,
      preload: path.join(__dirname, 'preload.js'),
      webSecurity: true,
    },
  })
  // Always open maximized — Ares is information-dense, the map + sidebars + bottom panel only
  // fit comfortably above ~1600 px wide. (Restore is still one click on the title bar.)
  mainWindow.maximize()
  // Hide the native OS menu bar — all actions are in the in-app hamburger menu.
  mainWindow.setMenuBarVisibility(false)
  mainWindow.webContents.on('before-input-event', (event, input) => {
    if (input.key === 'F12') mainWindow?.webContents.toggleDevTools()
    if (input.key === 'F11') mainWindow?.setFullScreen(!mainWindow.isFullScreen())
  })
  mainWindow.on('closed', () => { mainWindow = null })

  await showSplash('Starting Ares…')
  mainWindow.show()

  // ── First-run setup, all from the app (no terminal needed). Idempotent. ──
  let backendUp = false
  try {
    backendUp = await isBackendUp()                 // already running? (e.g. started manually) → just use it
    if (!backendUp) await ensureBackendEnv()        // create venv + pip install if missing
    await ensureFrontendBuilt(distDir)              // npm install + npm run build if dist/ missing
  } catch (err) {
    console.error('Setup error:', err)
    splashError(err.message)
    return                                          // leave the window showing the error + log; user can quit/relaunch
  }

  // local static server (serves dist/, proxies /api + /ws → backend)
  const port = await getFreePort(FRONTEND_PORT)
  frontendServer = startFrontendServer(distDir, port)
  console.log(`Frontend server: http://127.0.0.1:${port}`)

  if (!backendUp) { setSplash('Starting backend…'); startBackend() }
  else setSplash('Connecting to the running backend…')

  try {
    await waitForBackend(60000)                     // generous: a fresh-installed env is slower to import
    console.log('Backend ready — loading frontend')
    setSplash('Loading…')
    await mainWindow.loadURL(`http://127.0.0.1:${port}`)
  } catch (err) {
    console.error('Startup error:', err)
    splashError(err.message + ' — check the log above')
  }
}

// ── App menu ──────────────────────────────────────────────────────────────────
function buildMenu() {
  const template = [
    {
      label: 'File',
      submenu: [
        { label: 'Export Coverage (GeoJSON)', click: () => mainWindow?.webContents.send('export-geojson') },
        { type: 'separator' },
        { role: 'quit' },
      ],
    },
    {
      label: 'View',
      submenu: [
        { role: 'reload' },
        { role: 'forceReload' },
        { label: 'Developer Tools', accelerator: 'F12',
          click: () => mainWindow?.webContents.toggleDevTools() },
        { type: 'separator' },
        { role: 'resetZoom' }, { role: 'zoomIn' }, { role: 'zoomOut' },
        { type: 'separator' },
        { role: 'togglefullscreen' },
      ],
    },
    {
      label: 'Tools',
      submenu: [
        {
          label: 'Purge Terrain Cache',
          click: async () => {
            const r = await dialog.showMessageBox(mainWindow, {
              type: 'question', buttons: ['Cancel', 'Purge'],
              message: 'Delete all cached terrain data?',
              detail: 'SRTM tiles will be re-downloaded on next use.',
            })
            if (r.response === 1) mainWindow?.webContents.send('purge-cache')
          },
        },
        { label: 'Open Data Folder', click: () =>
            shell.openPath(path.join(__dirname, '..', 'backend', 'data')) },
        { label: 'API Documentation',
          click: () => shell.openExternal(`http://127.0.0.1:${BACKEND_PORT}/docs`) },
      ],
    },
    {
      label: 'Help',
      submenu: [
        { label: 'About', click: () => dialog.showMessageBox(mainWindow, {
            type: 'info',
            title: 'Ares',
            message: 'Ares v5.2.0 — alpha',
            detail:
              'Terrain-based RF propagation & geolocation platform.\n\n' +
              'Models: ITM/Longley-Rice, Hata, COST-231, Two-Ray,\n' +
              'ITU-R P.452 / P.528 / P.1546\n\n' +
              'Terrain: SRTM auto-download (30m/90m)\n' +
              'Space weather: NOAA SWPC real-time\n' +
              'GPU: CUDA via CuPy (RTX 5070 Ti detected)',
          })
        },
      ],
    },
  ]
  Menu.setApplicationMenu(Menu.buildFromTemplate(template))
}

// ── Lifecycle ─────────────────────────────────────────────────────────────────
app.whenReady().then(() => {
  // Force dark mode for all native chrome — including the OS open-file dialog used
  // when loading a KMZ/KML (on Linux this makes the GTK file picker honour a dark theme).
  try { nativeTheme.themeSource = 'dark' } catch (_) {}

  // Identify the app to the desktop environment so the dock/taskbar
  // shows the correct icon and groups windows properly.
  app.setName('Ares')

  // Windows taskbar grouping
  if (process.platform === 'win32') {
    app.setAppUserModelId('com.ares.app')
  }

  // Set app icon for taskbar / dock (Linux/Windows)
  if (fs.existsSync(ICON_PATH)) {
    try { app.setIcon(ICON_PATH) } catch (_) {}
  }
  createWindow()
})

app.on('window-all-closed', () => {
  cleanup()
  if (process.platform !== 'darwin') app.quit()
})

app.on('activate', () => {
  if (BrowserWindow.getAllWindows().length === 0) createWindow()
})

function cleanup() {
  app.isQuitting = true
  if (frontendServer) { frontendServer.close(); frontendServer = null }
  if (backendProcess) { backendProcess.kill(); backendProcess = null }
}

app.on('before-quit', cleanup)
process.on('exit', cleanup)
