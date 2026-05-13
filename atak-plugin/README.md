# ARES-ATAK — ATAK-CIV plugin (skeleton)

ATAK plugin that turns an [Ares](../README.md) server into the propagation /
geolocation backend for ATAK — the open-source counterpart to CloudRF's
SOOTHSAYER ATAK plugin, plus Ares-exclusive DF/geolocation, MANET, interference
and HF/space-weather tooling. See [`../docs/BUILD_PLAN.md`](../docs/BUILD_PLAN.md)
Workstream C for the full plan; this directory is the **P0 module skeleton**.

> **Status: SDK-blocked.** The Ares server, web/desktop globe, deployment, and
> ops surfaces are feature-complete (see `../docs/BUILD_PLAN.md`). The plugin's
> non-SDK code is also in place: REST + WebSocket client, DTOs, settings store,
> Co-Opt manager (adopt-callsign trigger loop), DF manager (`/geolocate/fix`),
> coverage-overlay renderer, Gradle module against ATAK-CIV SDK 5.x, plugin
> descriptor / lifecycle / map component / toolbar entry / dropdown receiver.
>
> What's still open here **needs the tak.gov SDK + publisher accounts** and
> cannot be finished without them:
>   1. The real `com.atakmap.*` / `transapps.*` glue (`TODO(...)` in the Kotlin):
>      dropdown UI inflation, `MapItem` overlay creation from the coverage
>      renderer, radial-menu items (Edit RF, Add LoB, Adopt callsign), CoT
>      publish/subscribe (GPS feed → Co-Opt, suspected-emitter marker → team),
>      and the KMZ import path.
>   2. `./gradlew assembleCivDebug` actually building — the ATAK API jar (`main.jar`)
>      must be wired in per the SDK's instructions (see below).
>   3. CI matrix per supported ATAK SDK line (5.3 / 5.4 / 5.5) — SOOTHSAYER ships
>      ~5 concurrent APKs; budget the same.
>   4. tak.gov (TAK Product Center) + Google Play signing & publication.

## Prerequisites

1. **ATAK-CIV SDK** — download from <https://tak.gov> (a free account is enough;
   the SDK is no longer on GitHub). You need `main.jar` (the ATAK API) and the
   `atak-gradle-plugin`. Place / configure per the SDK's `README`:
   - put `main.jar` where `app/build.gradle` expects it (see the `TODO` there), and
   - set `takRepoUrl` / `takRepoUser` / `takRepoPassword` (or the local-jar path)
     in your `local.properties` — **never commit credentials**.
2. **Android Studio** + Android SDK (compileSdk per the ATAK SDK you target).
3. A signing keystore. The debug keystore works for sideloading; release builds
   for tak.gov / Google Play must be submitted for signing by the TAK Product
   Center (as SOOTHSAYER's plugin is — TAK PC + Google Play signed).

## Build (once the SDK is in place)

```bash
cd atak-plugin
./gradlew assembleCivDebug          # sideload-able APK
adb install -r app/build/outputs/apk/civ/debug/*.apk
```

Then in ATAK: **⋮ → Plugins** (jigsaw icon) → enable **ARES**. Open the ARES tool
from the toolbar, set the server URL (`http://<ares-box>:8000` or your cloud
instance) + credentials in **Settings**.

## ATAK version coupling

Plugins are tightly bound to the ATAK API version. Target the current SDK
(≈5.5, Jetpack Compose UI available) on `main`; maintain a `legacy-sdk` branch
with XML UI for 5.3/5.4. CI builds one APK per supported line. (SOOTHSAYER ships
~5 concurrent APKs — budget for the same.)

## Layout

```
atak-plugin/
├── settings.gradle / build.gradle / gradle.properties / local.properties.example
└── app/
    ├── build.gradle / proguard-rules.pro
    └── src/main/
        ├── AndroidManifest.xml
        ├── assets/plugin.xml                 ATAK plugin descriptor (Lifecycle + Tool)
        ├── res/values/strings.xml
        ├── res/layout/ares_main.xml          dropdown-pane layout (replaceable by Compose on SDK 5.5+)
        └── java/com/ares/atak/plugin/
            ├── AresPluginLifecycle.kt         transapps Lifecycle entry point
            ├── AresMapComponent.kt            AbstractMapComponent — registers receivers / overlays / radial items
            ├── AresPluginTool.kt              toolbar button → SHOW_ARES intent
            ├── AresDropDownReceiver.kt        the right-side pane controller (connection, templates, coverage, Co-Opt, DF)
            ├── SettingsStore.kt               persisted server URL / token / Co-Opt policy / layer toggles
            ├── CoverageOverlayRenderer.kt     coverage GeoJSON → ATAK overlay (vector) | KMZ import (raster)
            ├── CoOptManager.kt                "Co-Opt": adopt a callsign → re-run coverage on time/distance triggers
            ├── DfManager.kt                   DF / geolocation: collect LoBs → /geolocate/fix → suspected-emitter CoT
            └── net/
                ├── AresApiClient.kt           REST + WS client (token auth, self-signed certs, /ws/simulate progress)
                └── AresModels.kt              request/response DTOs (auth, server, packs, coverage, p2p, manet, geo, templates)
```

### Skeleton status (what's modelled vs. wired)
- **Modelled (Kotlin compiles in principle, logic present):** API client surface, DTOs, settings persistence, Co-Opt trigger loop, DF solve flow, coverage-response summarisation.
- **Stubbed (needs the ATAK-CIV SDK):** everything touching `com.atakmap.android.maps.*` / `com.atakmap.comms.*` / `com.atakmap.android.dropdown.*` / `transapps.*` — dropdown UI inflation, map-overlay `MapItem` creation, radial-menu items, CoT publish/subscribe (the GPS feed for Co-Opt, the suspected-emitter marker for DF), KMZ import. Marked with `TODO(...)`.
- **Build:** still does not build until the tak.gov SDK is configured (see Prerequisites).
