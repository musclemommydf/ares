# Remote control over the network

Ares is a client/server app: a FastAPI backend (the radio brain) and a React
web UI. "Remote control" means **run the backend on the SDR appliance and drive
it from a laptop or phone** over the network. The backend now serves the web UI
itself, so any device with a browser is a full control surface — no per-client
install, no build step on the client.

```
   ┌────────────────────────────┐         LAN / Wi-Fi / USB-NIC / BT-PAN
   │  SDR appliance              │   ┌───────────────────────────────────────┐
   │  Matchstiq X40 / ZC706+     │   │  laptop browser  ·  Android browser/PWA │
   │  FMCOMMS5 / Pluto / USRP    │◀──┤  Electron desktop app                   │
   │  ─ ares backend :8000 ──────┼──▶│  → http://<appliance-ip>:8000           │
   │  ─ serves the web UI + API  │   └───────────────────────────────────────┘
   └────────────────────────────┘
```

## Easiest: the desktop app (no terminal)

In the **Ares desktop app**, open the menu (☰) → **Remote Access…**, set a
password, and hit **Enable remote access**. The app relaunches its bundled backend
bound to the network with auth on, and shows a **QR code + URL** to open on the
phone. Your desktop stays signed in automatically (it talks to the backend over a
loopback proxy that injects an admin token; remote devices authenticate with the
password). Toggle it off to go back to loopback-only. This is the recommended path
when "the appliance" is just your laptop/desktop running the app.

## Headless appliance (server, no GUI)

For a board with no desktop session, run the backend directly:

```bash
cd ares/backend
# build the UI once (or copy a prebuilt dist and point ARES_FRONTEND_DIST at it)
( cd ../frontend && npm ci && npm run build )

# bind to all interfaces, turn on auth, set a known admin password
HOST=0.0.0.0 PORT=8000 ARES_AUTH=true ARES_ADMIN_PASSWORD='choose-a-strong-one' \
  .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Notes
- **Auth is automatic off-loopback.** `ARES_AUTH=auto` (the default) turns auth ON
  whenever `HOST` isn't loopback, so a networked deployment is authenticated out
  of the box. `ARES_AUTH=true` forces it. `ARES_ADMIN_PASSWORD` pins the `admin`
  password every boot so you can log in without scraping the log; without it, a
  random admin password is generated and logged once.
- `ARES_FRONTEND_DIST=/path/to/dist` serves a prebuilt UI from anywhere (handy on
  a headless board where you don't want the Node toolchain).
- Run it under systemd / a process supervisor for an always-on appliance.

## From a laptop or phone (client)

1. Put the appliance and the client on the **same network** (see Transports).
2. Open **`http://<appliance-ip>:8000`** in any browser.
3. You'll get the **Connect** screen: confirm the Host, enter `admin` + your
   password, tap **Connect**. The token is stored on the device; the whole UI
   (map, SDR, DF, audio, waterfall) then drives the remote backend.
4. **Install as an app (optional):** Android Chrome → ⋮ → *Add to Home screen*;
   desktop Chrome/Edge → the install icon in the address bar. It launches
   standalone (PWA manifest included). *Note: full PWA install needs HTTPS or
   localhost; over plain-HTTP LAN it still works as a home-screen shortcut.*

The layout is **responsive**: on phones/tablets the desktop 3-pane grid stacks
into a single scrolling column (header → map → controls → results/DF) with
touch-sized targets; on a laptop you get the full desktop layout.

The **Electron desktop app** can either *be* the server (menu → Remote Access…,
above) or *be a client* pointed at a remote appliance — for the latter, set
`localStorage.setItem('ares.host','http://<appliance-ip>:8000'); location.reload()`
in DevTools, or just open a browser tab at the appliance URL.

## Transports

All of these are just **IP reachability to the appliance** — once the backend is
on `0.0.0.0:8000`, the UI is identical across them:

| Transport      | How                                                                 |
|----------------|---------------------------------------------------------------------|
| **Ethernet**   | Plug in; use the appliance's LAN IP.                                 |
| **Wi-Fi**      | Same SSID, or run the appliance as a Wi-Fi AP/hotspot and join it.   |
| **USB**        | USB-NIC gadget (RNDIS/CDC-ECM) — the board appears as a network iface (this is exactly how the Pluto's `ip:192.168.2.1` link works). |
| **Thunderbolt**| Thunderbolt/USB4 networking presents an IP link — same as Ethernet.  |
| **Bluetooth**  | Pair + bring up **BT-PAN (NAP/PANU)**; that yields an IP link, then connect to the appliance's PAN address. (Plain BLE GATT is not a supported transport.) |

## Security

- Keep `ARES_AUTH=true` on anything reachable beyond localhost; tokens are
  HMAC-signed and expire. A bad/expired token bounces the UI back to the login
  screen automatically.
- For untrusted networks, terminate **TLS** at a reverse proxy (Caddy/nginx) in
  front of `:8000` and browse `https://…`; the UI auto-uses `wss://` for sockets
  when served over HTTPS. CORS is open (`*`) but the serve-the-UI path is
  same-origin, so CORS doesn't enter the picture for the web client.

## SDR hardware on the appliance

Remote control is **independent of which radio** the appliance uses — the backend
talks to a local driver (`backend/app/core/sdr/drivers/`). Current driver IDs:
`matchstiq_x40`, `uhd_usrp`, `plutosdr`, `fmcomms5`, `antsdr_e200`, `heimdall`,
`synthetic`.

- **Matchstiq X40** — `matchstiq_x40` driver present.
- **USRP** — `uhd_usrp` (UHD).
- **ZC706 + FMCOMMS5** — `fmcomms5` driver (`drivers/fmcomms5.py`): in-process
  pyadi-iio `adi.FMComms5`, the two AD9361 chips' LOs tuned together → **4
  phase-coherent RX** for DF (channels 0,1 on chip A · 2,3 on chip B). Default URI
  `ip:192.168.2.1` (override per device). It needs inter-channel phase
  calibration (compass-calibrate vs a known emitter) — a bare board has no
  built-in coherence reference. Falls back to synthetic IQ when no board is
  reachable. Requires `pyadi-iio` on the appliance.
