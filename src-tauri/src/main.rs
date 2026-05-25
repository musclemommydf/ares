// SPDX-License-Identifier: MIT OR Apache-2.0
// Copyright (c) 2026 Ares

//! Ares desktop shell (Tauri) — Track D / D3.
//!
//! Replaces the Electron wrapper (`../electron/main.js`). The Ares backend already
//! serves the UI + API + WebSockets **same-origin**, so this shell takes the
//! "Option A" path: spawn (or reuse) the backend, wait for `/api/v1/health`, then
//! point the webview straight at `http://127.0.0.1:<port>`. There is **no static
//! file server, no `/api` proxy, and no WebSocket forwarding** — the hardest part
//! of the Electron main process simply does not exist here.
//!
//! Implemented:
//!  - backend lifecycle: reuse a running backend, else first-run bootstrap (venv,
//!    pip, `npm run build`) streamed to the splash, then spawn + health-wait + the
//!    restart-on-remote-toggle path; the child is killed on app exit.
//!  - Remote Access commands backing `window.aresDesktop.getRemote/setRemote`, plus
//!    an init script that shims `window.aresDesktop` / `window.electronAPI` (so the
//!    frontend needs no changes) and seeds `localStorage['ares.token']` when remote
//!    auth is on so the desktop skips the login screen.
//!  - App menu (File/View/Tools/Help), forced dark theme, single-instance.
//!
//! TODO(D3.5): browser geolocation via GeoClue (webkit permission signal). The
//! robust field path is the in-app GPS sources (gpsd / serial NMEA / SDR GPSDO),
//! so this is a nice-to-have, not a blocker.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::io::{BufRead, BufReader};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use serde::{Deserialize, Serialize};
use tauri::menu::{MenuBuilder, MenuItemBuilder, PredefinedMenuItem, SubmenuBuilder};
use tauri::{Emitter, Manager, RunEvent, Theme, WebviewUrl, WebviewWindowBuilder, WindowEvent};

const DEFAULT_BACKEND_PORT: u16 = 8000;

/// Backend TCP port. Override with `ARES_DESKTOP_PORT` to run a second instance
/// (e.g. a separate "Cyber" build) alongside the default on 8000 without clashing.
fn backend_port() -> u16 {
    std::env::var("ARES_DESKTOP_PORT")
        .ok()
        .and_then(|s| s.trim().parse().ok())
        .unwrap_or(DEFAULT_BACKEND_PORT)
}
const ABOUT_URL: &str = "https://github.com/musclemommydf/ares";

/// The backend child, kept process-global so an OS-signal handler (SIGTERM /
/// SIGINT / SIGHUP) can reach it — not just the window-close / RunEvent paths.
static BACKEND: Mutex<Option<Child>> = Mutex::new(None);

// ── config ────────────────────────────────────────────────────────────────────
#[derive(Clone, Default, Serialize, Deserialize)]
struct RemoteCfg {
    enabled: bool,
    #[serde(default)]
    password: String,
}

#[derive(Serialize)]
struct RemoteStatus {
    enabled: bool,
    has_password: bool,
    port: u16,
    urls: Vec<String>,
}

struct AppState {
    remote: Mutex<RemoteCfg>,
    config_dir: PathBuf,
}

impl AppState {
    fn remote_cfg_path(&self) -> PathBuf {
        self.config_dir.join("remote.json")
    }
    fn load_remote(&self) -> RemoteCfg {
        std::fs::read_to_string(self.remote_cfg_path())
            .ok()
            .and_then(|s| serde_json::from_str(&s).ok())
            .unwrap_or_default()
    }
    fn save_remote(&self, cfg: &RemoteCfg) {
        let _ = std::fs::create_dir_all(&self.config_dir);
        if let Ok(s) = serde_json::to_string(cfg) {
            let _ = std::fs::write(self.remote_cfg_path(), s);
        }
    }
}

// ── paths / helpers ─────────────────────────────────────────────────────────────
/// `src-tauri/` lives at `<repo>/src-tauri`; the backend is `<repo>/backend`.
fn repo_root() -> PathBuf {
    // ARES_REPO_ROOT lets one binary drive a different checkout (e.g. a separate
    // "Cyber" worktree) without rebuilding — overrides the compiled-in manifest dir.
    if let Ok(p) = std::env::var("ARES_REPO_ROOT") {
        let p = p.trim();
        if !p.is_empty() {
            return PathBuf::from(p);
        }
    }
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .map(Path::to_path_buf)
        .unwrap_or_else(|| PathBuf::from("."))
}

fn venv_python(root: &Path) -> PathBuf {
    if cfg!(windows) {
        root.join("backend/.venv/Scripts/python.exe")
    } else {
        root.join("backend/.venv/bin/python")
    }
}

/// Venv python if present, else system python (used to spawn the backend).
fn backend_python(root: &Path) -> String {
    let venv = venv_python(root);
    if venv.exists() {
        venv.to_string_lossy().into_owned()
    } else if cfg!(windows) {
        "python".into()
    } else {
        "python3".into()
    }
}

/// Best-effort primary LAN IP (connect a UDP socket toward a public address and
/// read the local side — no packets are sent). `None` when offline.
fn primary_lan_ip() -> Option<String> {
    use std::net::UdpSocket;
    let sock = UdpSocket::bind("0.0.0.0:0").ok()?;
    sock.connect("8.8.8.8:80").ok()?;
    sock.local_addr().ok().map(|a| a.ip().to_string())
}

/// Open a path or URL in the OS default handler (folder / browser).
fn open_external(target: &str) {
    #[cfg(target_os = "linux")]
    let _ = Command::new("xdg-open").arg(target).spawn();
    #[cfg(target_os = "macos")]
    let _ = Command::new("open").arg(target).spawn();
    #[cfg(target_os = "windows")]
    let _ = Command::new("cmd").args(["/C", "start", "", target]).spawn();
}

// ── backend lifecycle ───────────────────────────────────────────────────────────
fn health_ok() -> bool {
    let url = format!("http://127.0.0.1:{}/api/v1/health", backend_port());
    ureq::get(&url).timeout(Duration::from_millis(900)).call().is_ok()
}

fn wait_for_health(timeout: Duration) -> bool {
    let deadline = Instant::now() + timeout;
    while Instant::now() < deadline {
        if health_ok() {
            return true;
        }
        std::thread::sleep(Duration::from_millis(500));
    }
    false
}

fn spawn_backend(cfg: &RemoteCfg) {
    let root = repo_root();
    let host = if cfg.enabled { "0.0.0.0" } else { "127.0.0.1" };

    let mut cmd = Command::new(backend_python(&root));
    cmd.current_dir(root.join("backend"))
        .args([
            "-m", "uvicorn", "app.main:app",
            "--host", host,
            "--port", &backend_port().to_string(),
        ])
        .env("PYTHONUNBUFFERED", "1")
        .env("PORT", backend_port().to_string())
        .env("HOST", host);
    if cfg.enabled {
        cmd.env("ARES_AUTH", "true");
        if !cfg.password.is_empty() {
            cmd.env("ARES_ADMIN_PASSWORD", &cfg.password);
        }
    } else {
        cmd.env("ARES_AUTH", "false");
    }

    match cmd.spawn() {
        Ok(child) => *BACKEND.lock().unwrap() = Some(child),
        Err(e) => eprintln!("[ares] backend spawn failed: {e}"),
    }
}

fn stop_backend() {
    if let Some(mut child) = BACKEND.lock().unwrap().take() {
        let _ = child.kill();
        let _ = child.wait();
    }
}

/// Run a command, streaming stdout to the splash log; returns success.
fn run_streaming(app: &tauri::AppHandle, program: &str, args: &[&str], cwd: &Path) -> bool {
    let _ = app.emit("ares://log", format!("\n$ {} {}\n", program, args.join(" ")));
    let child = Command::new(program)
        .args(args)
        .current_dir(cwd)
        .stdout(Stdio::piped())
        .stderr(Stdio::inherit())
        .spawn();
    let mut child = match child {
        Ok(c) => c,
        Err(e) => {
            let _ = app.emit("ares://log", format!("failed to start {program}: {e}\n"));
            return false;
        }
    };
    if let Some(out) = child.stdout.take() {
        for line in BufReader::new(out).lines().map_while(Result::ok) {
            let _ = app.emit("ares://log", format!("{line}\n"));
        }
    }
    matches!(child.wait(), Ok(s) if s.success())
}

/// First-run: ensure the backend venv exists and its core deps are importable.
fn ensure_backend_env(app: &tauri::AppHandle, root: &Path) -> bool {
    let backend = root.join("backend");
    let vpy = venv_python(root);
    if !vpy.exists() {
        let _ = app.emit("ares://status", "First run: creating the Python environment…");
        let sys = if cfg!(windows) { "python" } else { "python3" };
        if !run_streaming(app, sys, &["-m", "venv", ".venv"], &backend) {
            return false;
        }
    }
    let deps_ok = Command::new(&vpy)
        .args(["-c", "import fastapi, numpy, pydantic_settings"])
        .current_dir(&backend)
        .status()
        .map(|s| s.success())
        .unwrap_or(false);
    if !deps_ok {
        let _ = app.emit("ares://status", "First run: installing backend dependencies (a few minutes the first time)…");
        let vpy_s = vpy.to_string_lossy().into_owned();
        let _ = run_streaming(app, &vpy_s, &["-m", "pip", "install", "--upgrade", "pip", "-q"], &backend);
        if !run_streaming(app, &vpy_s, &["-m", "pip", "install", "-r", "requirements.txt"], &backend) {
            return false;
        }
    }
    true
}

/// First-run: ensure the frontend bundle exists (the backend serves it).
fn ensure_frontend_built(app: &tauri::AppHandle, root: &Path) -> bool {
    if root.join("frontend/dist/index.html").exists() {
        return true;
    }
    let fe = root.join("frontend");
    let npm = if cfg!(windows) { "npm.cmd" } else { "npm" };
    let _ = app.emit("ares://status", "First run: installing frontend dependencies…");
    if !run_streaming(app, npm, &["install", "--no-audit", "--no-fund"], &fe) {
        return false;
    }
    let _ = app.emit("ares://status", "First run: building the UI (about a minute)…");
    if !run_streaming(app, npm, &["run", "build"], &fe) {
        return false;
    }
    root.join("frontend/dist/index.html").exists()
}

/// Log in as `admin` and return a bearer token so the loopback desktop window can
/// skip the login screen when remote auth is on. `None` when auth is off.
fn mint_admin_token(cfg: &RemoteCfg) -> Option<String> {
    if !cfg.enabled || cfg.password.is_empty() {
        return None;
    }
    let url = format!("http://127.0.0.1:{}/api/v1/auth/login", backend_port());
    let body = serde_json::json!({ "username": "admin", "password": cfg.password }).to_string();
    let resp = ureq::post(&url)
        .set("Content-Type", "application/json")
        .timeout(Duration::from_secs(4))
        .send_string(&body)
        .ok()?;
    let v: serde_json::Value = serde_json::from_str(&resp.into_string().ok()?).ok()?;
    v.get("token").and_then(|t| t.as_str()).map(str::to_string)
}

// ── Remote Access commands (called by the in-app panel) ───────────────────────────
#[tauri::command]
fn remote_get(state: tauri::State<AppState>) -> RemoteStatus {
    let cfg = state.remote.lock().unwrap().clone();
    let urls = primary_lan_ip()
        .map(|ip| vec![format!("http://{}:{}", ip, backend_port())])
        .unwrap_or_default();
    RemoteStatus {
        enabled: cfg.enabled,
        has_password: !cfg.password.is_empty(),
        port: backend_port(),
        urls,
    }
}

#[tauri::command]
fn remote_set(state: tauri::State<AppState>, cfg: RemoteCfg) -> Result<RemoteStatus, String> {
    let mut new_cfg = cfg;
    if new_cfg.password.is_empty() {
        new_cfg.password = state.remote.lock().unwrap().password.clone();
    }
    if new_cfg.enabled && new_cfg.password.is_empty() {
        return Err("Set a password before enabling remote access.".into());
    }
    *state.remote.lock().unwrap() = new_cfg.clone();
    state.save_remote(&new_cfg);

    stop_backend();
    std::thread::sleep(Duration::from_millis(400)); // let the port free
    spawn_backend(&new_cfg);
    wait_for_health(Duration::from_secs(30));
    Ok(remote_get(state))
}

// ── webview init script (shims the Electron API) ─────────────────────────────────
fn init_script(token: Option<&str>) -> String {
    let seed = token
        .map(|t| {
            format!(
                "try{{localStorage.setItem('ares.token',{});}}catch(e){{}}",
                serde_json::to_string(t).unwrap_or_else(|_| "\"\"".into())
            )
        })
        .unwrap_or_default();
    format!(
        r#"{seed}
window.aresDesktop = {{
  isDesktop: true,
  getRemote: () => window.__TAURI__.core.invoke('remote_get'),
  setRemote: (cfg) => window.__TAURI__.core.invoke('remote_set', {{ cfg }}),
}};
// Electron-compat shims: the menu items emit these Tauri events; the frontend
// already subscribes via window.electronAPI.onExport*/onPurgeCache.
const _on = (name) => (cb) => {{ try {{ window.__TAURI__.event.listen(name, cb); }} catch (e) {{}} }};
window.electronAPI = window.electronAPI || {{
  onExportGeoJSON: _on('export-geojson'),
  onExportPDF: _on('export-pdf'),
  onPurgeCache: _on('purge-cache'),
}};
"#
    )
}

// ── app menu ──────────────────────────────────────────────────────────────────
fn build_and_set_menu(handle: &tauri::AppHandle) -> tauri::Result<()> {
    let export = MenuItemBuilder::with_id("export_geojson", "Export Coverage (GeoJSON)").build(handle)?;
    let reload = MenuItemBuilder::with_id("reload", "Reload").accelerator("CmdOrCtrl+R").build(handle)?;
    let devtools = MenuItemBuilder::with_id("toggle_devtools", "Toggle DevTools").accelerator("F12").build(handle)?;
    let purge = MenuItemBuilder::with_id("purge_cache", "Purge Terrain Cache").build(handle)?;
    let open_data = MenuItemBuilder::with_id("open_data", "Open Data Folder").build(handle)?;
    let api_docs = MenuItemBuilder::with_id("api_docs", "API Documentation").build(handle)?;
    let about = MenuItemBuilder::with_id("about", "About Ares").build(handle)?;

    let file = SubmenuBuilder::new(handle, "File")
        .item(&export)
        .separator()
        .item(&PredefinedMenuItem::quit(handle, None)?)
        .build()?;
    let view = SubmenuBuilder::new(handle, "View")
        .item(&reload)
        .item(&devtools)
        .separator()
        .item(&PredefinedMenuItem::fullscreen(handle, None)?)
        .build()?;
    let tools = SubmenuBuilder::new(handle, "Tools")
        .item(&purge)
        .item(&open_data)
        .item(&api_docs)
        .build()?;
    let help = SubmenuBuilder::new(handle, "Help").item(&about).build()?;

    let menu = MenuBuilder::new(handle)
        .item(&file)
        .item(&view)
        .item(&tools)
        .item(&help)
        .build()?;
    handle.set_menu(menu)?;
    Ok(())
}

fn handle_menu(app: &tauri::AppHandle, id: &str) {
    match id {
        "export_geojson" => {
            let _ = app.emit("export-geojson", ());
        }
        "purge_cache" => {
            let _ = app.emit("purge-cache", ());
        }
        "reload" => {
            if let Some(w) = app.get_webview_window("main") {
                let _ = w.eval("location.reload()");
            }
        }
        "toggle_devtools" => {
            #[cfg(debug_assertions)]
            if let Some(w) = app.get_webview_window("main") {
                if w.is_devtools_open() {
                    w.close_devtools();
                } else {
                    w.open_devtools();
                }
            }
        }
        "open_data" => open_external(&repo_root().join("backend/data").to_string_lossy()),
        "api_docs" => open_external(&format!("http://127.0.0.1:{}/docs", backend_port())),
        "about" => open_external(ABOUT_URL),
        _ => {}
    }
}

// ── boot: bootstrap (first run), spawn/reuse backend, open the real window ─────────
fn boot(handle: tauri::AppHandle) {
    let root = repo_root();
    if !health_ok() {
        let _ = handle.emit("ares://status", "Starting Ares…");
        if !ensure_backend_env(&handle, &root) {
            let _ = handle.emit("ares://status", "⚠ Backend setup failed — see the log above.");
            return;
        }
        if !ensure_frontend_built(&handle, &root) {
            let _ = handle.emit("ares://status", "⚠ Frontend build failed — see the log above.");
            return;
        }
        let _ = handle.emit("ares://status", "Starting backend…");
        let cfg = handle.state::<AppState>().remote.lock().unwrap().clone();
        spawn_backend(&cfg);
    } else {
        let _ = handle.emit("ares://status", "Connecting to the running backend…");
    }

    if !wait_for_health(Duration::from_secs(90)) {
        let _ = handle.emit("ares://status", "⚠ Backend did not start in time — see the log.");
        return;
    }

    let token = mint_admin_token(&handle.state::<AppState>().remote.lock().unwrap().clone());
    let url = format!("http://127.0.0.1:{}", backend_port());
    let built = WebviewWindowBuilder::new(&handle, "main", WebviewUrl::External(url.parse().unwrap()))
        .title("Ares")
        .inner_size(1440.0, 900.0)
        .min_inner_size(900.0, 600.0)
        .maximized(true)
        .theme(Some(Theme::Dark))
        .initialization_script(init_script(token.as_deref()))
        .build();

    match built {
        Ok(_) => {
            let _ = build_and_set_menu(&handle);
            if let Some(splash) = handle.get_webview_window("splash") {
                let _ = splash.close();
            }
        }
        Err(e) => {
            eprintln!("[ares] main window failed: {e}");
            let _ = handle.emit("ares://status", "⚠ Failed to open the main window — see the log.");
        }
    }
}

fn main() {
    let mut builder = tauri::Builder::default();
    // Single-instance applies only to the default instance. A port-overridden build
    // (ARES_DESKTOP_PORT, e.g. the separate "Cyber" app) skips it so it can run
    // alongside the default rather than just focusing its window.
    if std::env::var("ARES_DESKTOP_PORT").is_err() {
        // single-instance must be the first plugin registered.
        builder = builder.plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            if let Some(w) = app.get_webview_window("main") {
                let _ = w.set_focus();
            }
        }));
    }
    builder
        .setup(|app| {
            let config_dir = app.path().app_config_dir().unwrap_or_else(|_| PathBuf::from("."));
            let state = AppState {
                remote: Mutex::new(RemoteCfg::default()),
                config_dir,
            };
            *state.remote.lock().unwrap() = state.load_remote(); // restore saved choice
            app.manage(state);

            // Hardened shutdown: a termination signal (SIGTERM / SIGINT / SIGHUP)
            // or Ctrl-C must kill the backend child too — not just a window close.
            // We kill it directly (in case the event loop is wedged) and then ask
            // Tauri to exit cleanly (which also runs RunEvent::Exit, idempotent).
            let sig_handle = app.handle().clone();
            if let Err(e) = ctrlc::set_handler(move || {
                stop_backend();
                sig_handle.exit(0);
            }) {
                eprintln!("[ares] could not install signal handler: {e}");
            }

            let handle = app.handle().clone();
            std::thread::spawn(move || boot(handle));
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![remote_get, remote_set])
        .on_menu_event(|app, event| handle_menu(app, event.id().as_ref()))
        .on_window_event(|window, event| {
            if matches!(event, WindowEvent::Destroyed) && window.label() == "main" {
                stop_backend();
            }
        })
        .build(tauri::generate_context!())
        .expect("error while building the Ares desktop shell")
        .run(|_handle, event| {
            // Kill the backend child when the app exits (window close, app.exit, …).
            if matches!(event, RunEvent::ExitRequested { .. } | RunEvent::Exit) {
                stop_backend();
            }
        });
}
