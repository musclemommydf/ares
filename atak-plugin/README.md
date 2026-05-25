# ARES-ATAK — ATAK-CIV plugin

ATAK plugin that turns an [Ares](../README.md) server into the propagation /
geolocation backend for ATAK — the open-source counterpart to CloudRF's
SOOTHSAYER plugin, plus Ares-exclusive DF/geolocation, MANET, interference
and HF/space-weather tooling. See [`../docs/BUILD_PLAN.md`](../docs/BUILD_PLAN.md)
Workstream C for the full feature plan.

## Status

Real SDK wiring is in place: lifecycle (`AresPlugin : AbstractPlugin`),
toolbar entry (`AresPluginTool : AbstractPluginTool`), map component
(`AresMapComponent : DropDownMapComponent`), dropdown receiver
(`AresDropDownReceiver : DropDownReceiver`), coverage overlay renderer
(emits real `Marker` / `MapGroup` items), Co-Opt manager (live coverage
driven by ATAK's `MapView` positions), DF manager (publishes
suspected-emitter CoT to the team via `CommsMapComponent`), `AresApiClient`
(every route the plugin uses already exists in the backend). The skeleton's
`TODO(...)` markers are gone.

Status of the Track D / D1 items:
- **Build verification (D1.1)** — *still pending*: not compiled in this dev
  environment (no JDK / Android SDK / tak.gov SDK). Open on a machine with Android
  Studio Dolphin+ and `./gradlew assembleCivDebug`.
- **CI matrix (D1.2)** — *added*: `.github/workflows/atak-plugin.yml` builds
  `assembleCivDebug` across ATAK 5.3 / 5.4 / 5.5, gated on an `ATAK_SDK_URL` repo
  secret (the SDK isn't redistributable) or a self-hosted runner.
- **Radial-menu items (D1.3)** — *implemented, pending build*: `AresMenuReceiver`
  + `assets/menus/menu_ares_point.xml` define "Edit RF" / "Add LoB from here" and
  route the tapped point into the pane (`runCoverageRaw` / `DfManager.addLoB`).
  The one remaining SDK-line-specific call is registering the menu asset on a
  point's radial via `MenuMapAdapter` / `MapMenuReceiver` (TODO in `onCreate`).
- **CoT receive (D1.4a)** — *implemented, pending build*: a
  `CotServiceRemote.CotEventListener` in `AresMapComponent.onCreate` forwards
  foreign emitter CoT into the pane (the substantive parse/fuse also runs
  server-side — backend D1.4b, `cot._parse_cot_track`, which is unit-tested).

## Prerequisites

1. **JDK 11 (Adoptium)** — `apt install temurin-11-jdk` or via SDKMAN. *Do not
   use Oracle JDK* (the SDK README is explicit about this).
2. **Android Studio Dolphin or later** — provides the Android SDK + emulator.
   Set `sdk.dir` in `local.properties` (Android Studio fills this in on
   project import).
3. **ATAK-CIV SDK** — download `atak-mil-mastersdk.zip` (or `atak-civ-sdk.zip`)
   from <https://tak.gov> (free account). Unzip it somewhere stable; you'll
   point `local.properties` at the unzipped directory below.
4. **Signing keystore** — sideload uses the Android debug keystore by default.
   Release builds for tak.gov / Google Play must be submitted unsigned to the
   TAK Product Center for signing (SOOTHSAYER follows this path).

## First-time setup

```bash
cd atak-plugin
cp local.properties.example local.properties
$EDITOR local.properties     # set sdk.dir and takdev.plugin
```

`takdev.plugin` should be an absolute path to the unzipped SDK directory
(`/home/you/atak-sdk/atak-mil-master`), the one containing `main.jar` and
`atak-gradle-takdev.jar`. The takdev gradle plugin auto-discovers them.

## Build & install

```bash
./gradlew assembleCivDebug
adb install -r app/build/outputs/apk/civ/debug/app-civ-debug.apk
```

Then in ATAK on the device/emulator: **⋮ → Plugins** (jigsaw icon) → enable
**ARES**, then tap the ARES tool in the toolbar. Set your Ares server URL +
credentials in the pane that opens (Settings section at the top).

For iterative development from Android Studio: select the **civDebug** build
variant (Build → Select Build Variant), then the `Run` button compiles and
installs the APK while ATAK is running and prompts you to reload the plugin.

## Layout

```
atak-plugin/
├── settings.gradle / build.gradle / gradle.properties / local.properties.example
└── app/
    ├── build.gradle / proguard-rules.pro
    └── src/main/
        ├── AndroidManifest.xml
        ├── assets/plugin.xml                 — IPlugin extension declaration
        ├── res/values/strings.xml
        ├── res/layout/ares_main.xml          — dropdown pane layout
        ├── res/drawable/ic_ares.xml          — toolbar icon (vector)
        └── java/com/ares/atak/plugin/
            ├── AresPlugin.kt                  — IPlugin entry, wires tool + component
            ├── AresPluginTool.kt              — toolbar item → SHOW_ARES intent
            ├── AresMapComponent.kt            — DropDownMapComponent, registers receiver / overlay group
            ├── AresDropDownReceiver.kt        — right-pane controller (login + run coverage + tabs)
            ├── SettingsStore.kt               — SharedPreferences-backed config
            ├── CoverageOverlayRenderer.kt     — coverage GeoJSON → ATAK Markers in a MapGroup
            ├── CoOptManager.kt                — adopt callsign → re-run coverage on time/distance triggers
            ├── DfManager.kt                   — LoBs → /geolocate/fix → suspected-emitter Marker + CoT publish
            └── net/
                ├── AresApiClient.kt           — REST + WS client (token auth, self-signed certs, /ws/simulate)
                └── AresModels.kt              — kotlinx-serialization DTOs (auth, packs, coverage, p2p, manet, geo, templates)
```

## ATAK API version coupling

ATAK plugins are tightly bound to the ATAK API version. `takdev` extracts the
matching `plugin-api` string from your `main.jar` and injects it into the
manifest at build time, so you don't manage it manually — just keep
`takdev.plugin` pointing at the SDK for the ATAK version you're targeting.

Different SDK lines → different `civ-X.Y` build flavors / output paths. The
default flavor here is `civ` (sideloadable). For mil/release builds, submit
the unsigned `civRelease` APK to the TAK Product Center.

## Backend contract (Ares server)

The plugin talks to the Ares server over REST + a single WebSocket. Every
endpoint is already implemented in `backend/app/api/`:

| Plugin call                              | Backend route                                              |
|------------------------------------------|------------------------------------------------------------|
| `login(user, pass)`                      | `POST /api/v1/auth/login`                                  |
| `serverInfo()`                           | `GET  /api/v1/server/info`                                 |
| `listPacks()` / `downloadPack` / `packJob` | `/api/v1/packs[/download][/jobs/{id}]`                   |
| `listTemplates()` / template CRUD        | `/api/v1/atak/templates[/{id}]`                            |
| `templateCoverageRequest(id, lat, lon)`  | `POST /api/v1/atak/templates/{id}/coverage_request?lat=&lon=` |
| `coverage(req)` / `p2p` / `manet`        | `POST /api/v1/simulate/{coverage,p2p,manet}`               |
| `geolocateFix(req)`                      | `POST /api/v1/geolocate/fix`                               |
| `lobRangeEstimate(lob)`                  | `POST /api/v1/lob/range_estimate`                          |
| `exportKmz(geojson, name, minDbm)`       | `POST /api/v1/atak/export/kmz` → KMZ bytes                 |
| `openSimulateProgress(onEvent)`          | `WS   /api/v1/ws/simulate`                                 |

When the server has `ARES_AUTH=false` (the default for dev), `/auth/login`
returns a long-lived synthetic token so the plugin's always-log-in flow keeps
working without configuring users.

## Troubleshooting

- **`Could not resolve :atak-gradle-takdev:`** — check that `local.properties`
  has `takdev.plugin=<absolute path>` and that the path contains
  `atak-gradle-takdev.jar`. The path is read from `local.properties` at
  configure time; `./gradlew --refresh-dependencies` after edits.
- **`PluginContextProvider unavailable`** — the host ATAK is too old (need 5.x).
  Update ATAK on the device.
- **Plugin loads but the toolbar icon is missing** — verify `ic_ares.xml` built;
  release builds with proguard may strip it if you've customised the proguard
  rules — keep `-keep class com.ares.atak.plugin.**` if you do.
- **`No suitable certificate` against an Ares dev server** — tick
  "Allow self-signed cert" in the pane's Settings section, or front the server
  with Caddy / nginx + a real Let's Encrypt cert.
