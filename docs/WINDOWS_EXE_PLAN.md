# Plan: standalone Windows release (issue #3)

Goal: a user downloads one file from the GitHub release, runs it, and the browser page opens. No Python,
no `.bat`, no first-run environment setup. It must quit cleanly and put the PC's Wi-Fi back.

## 1. What the investigation found

Facts checked in the repository and in a throwaway build (not guesses):

| Finding | Evidence | Consequence |
|---|---|---|
| PyInstaller 6.22.3 builds the app on Python 3.14 in about 54 s | spike build of the current tree, `--onedir` | Tooling works; no source port needed |
| Page, `monitor.js`, `/api/status`, `/api/cameras` and a BLE scan work from the built exe | spike run on port 8099 | `client/server.py` finds `web/` through `__file__`; bleak and WinRT load |
| `--collect-all winrt` plus `--collect-submodules bleak` is enough | spike | Hidden imports solved; the spec must keep them and a smoke test must guard them |
| Bundle is 104 MB (onedir) | `du` on `dist/osmo-client` | Trim with excludes; never use UPX (raises antivirus flags) |
| `bleak.backends.corebluetooth` warns during analysis | build log | Harmless on Windows; exclude it explicitly to keep logs clean |
| A windowed build sets `sys.stdout` and `sys.stderr` to `None` | PyInstaller behaviour, same as `pythonw` | `log()` (`camlink/util.py`), `sys.stdout.reconfigure(...)` (`osmo_client.py` `main`) and `client/report.py` (wraps `sys.stdout`) would crash. Needs a stream guard |
| The only way to stop the app today is Ctrl+C or SIGTERM; `run()`'s `finally` restores Wi-Fi | `osmo_client.py` | A windowed exe has no console, so it needs its own quit path that cancels the same task |
| Closing a console window gives the process about 5 s, then kills it | Windows `CTRL_CLOSE_EVENT` | Wi-Fi restore must run from a console-control handler, or users close the window and stay stranded on the camera's network |
| The web server has no `Host` or `Origin` check on its POST routes | `client/server.py` | Any web page open in the user's browser can POST to `127.0.0.1:8091` (CSRF, DNS rebinding). Adding a quit route makes this worse. Fix first |
| `ps1` comment: WinRT DLLs fail to load from long paths | `osmo-client.ps1` | Keep the install path short; add a deep-path test |
| User data lives in `%LOCALAPPDATA%\osmo-pc` already | `camlink/util.py` | Paired cameras and gallery carry over between the exe and the Python route. Uninstall must keep it by default |
| No automated tests, no CI, no version resource, no icon | `git ls-files` | Add a smoke test and a workflow |
| License is MIT; PyAV wheels bundle FFmpeg | `LICENSE`, `av.libs` | Releases must ship third-party licenses and the FFmpeg notice. Add to `NOTICE.md` |

## 2. Decisions

1. **Onedir, not onefile.** Onefile unpacks about 100 MB to `%TEMP%` on every start (slow, and the
   most common antivirus false-positive pattern). Onedir starts fast and leaves the DLLs on disk where
   Defender can scan them once.
2. **Two shipped forms from one build:**
   - `OsmoClient-<version>-setup.exe`: Inno Setup, per-user, no admin rights, Start menu shortcut,
     proper uninstaller, install path `%LOCALAPPDATA%\Programs\Osmo Client`. This is the primary download.
   - `OsmoClient-<version>-windows-x64.zip`: portable, unzip anywhere short.
3. **Two executables sharing one `_internal` folder:**
   - `OsmoClient.exe`: windowed (no console), tray icon, the normal entry point.
   - `OsmoClient-console.exe`: same code with a console, for `--report`, `--help` and support.
     The camera report prints instructions and needs a visible log.
4. **Tray icon with Open / Open downloads folder / Quit**, written with `ctypes` (`Shell_NotifyIcon`) in a
   small module to avoid adding a dependency. The code base already uses `ctypes` for Wi-Fi. Fallback if
   that proves fiddly in testing: `pystray` pinned with hashes.
5. **Single instance** through a named mutex. A second launch opens the existing page and exits, instead
   of today's port-in-use error. A message box (not a console line) reports real startup failures.
6. **Logging to a file** (`%LOCALAPPDATA%\osmo-pc\logs\osmo-client.log`, rotated, 5 x 1 MB) when no console
   exists. Console builds still print. The page's existing report scrubbing of the home path stays.
7. **Signing is a separate, later phase.** Ship unsigned first with clear README text on SmartScreen,
   then apply to SignPath Foundation (free for OSS; MIT and a public repo qualify). Azure Artifact
   Signing is a paid fallback with regional limits for individuals. Signing does not remove SmartScreen
   warnings at once; reputation builds with downloads.
8. **No auto-update, no telemetry.** Optional "new version available" check is out of scope and would be
   opt-in only.

## 3. Work breakdown

### Phase 0: prerequisites (small, ship even without the exe)
- `client/server.py`: reject any request whose `Host` header is not `127.0.0.1:<port>` or
  `localhost:<port>`, and any POST whose `Origin` is present and not the same. Return 403. This closes
  the CSRF and DNS-rebinding hole before a quit route exists.
- `camlink/util.py`: `setup_streams()`: if `sys.stdout` or `sys.stderr` is `None`, point them at the log
  file; otherwise `reconfigure(encoding="utf-8", errors="replace")`. Call it first in `main()`. Replaces
  the bare `sys.stdout.reconfigure(...)`.
- New `camlink/resources.py` (or in `util.py`): `resource_path(*parts)` returning the frozen
  (`sys._MEIPASS`) or source location. `client/server.py` `PAGE_FILE` uses it, so the frozen layout is
  explicit and not an accident of `__file__`.

### Phase 1: quit path and shutdown (the "nice to have" items in the issue)
- `osmo_client.py`: a module-level `Shutdown` helper holding the main task and loop; `request_quit()`
  calls `loop.call_soon_threadsafe(task.cancel)`. This reuses the existing `finally` that restores Wi-Fi,
  so there is exactly one teardown path.
- Entry points that call it: `POST /api/quit`, tray Quit, `SetConsoleCtrlHandler`
  (`CTRL_C`, `CTRL_BREAK`, `CTRL_CLOSE`, `CTRL_LOGOFF`, `CTRL_SHUTDOWN` events), and a hidden top-level
  window handling `WM_QUERYENDSESSION` / `WM_ENDSESSION` for the windowed exe (it receives no console
  events).
- Console close gives about 5 s: the handler blocks until teardown finishes or 4.5 s pass, then logs.
  Teardown currently allows up to 20 s for multiview tiles; reduce the cap on this path and note it.
- Page: a **Quit** button (confirm dialog, text says Wi-Fi is returned), and after quit the page shows
  "Osmo Client has stopped" instead of a dead connection. Needs one string and one handler in
  `client/web/index.html`.
- Crash safety net: on unhandled exception in the windowed exe, run the Wi-Fi restore, write the
  traceback to the log, show a message box with the log path.

### Phase 2: tray, single instance, startup UX (`client/shell_windows.py`)
- Named mutex `Local\OsmoClient.<port>`; second instance sends `GET /` open to the existing port, then
  exits 0.
- Tray: tooltip "Osmo Client - connected to <camera>" updated from `App.state`; menu Open, Downloads,
  Quit. Runs in its own thread with its own message loop, so it never touches bleak's thread.
- High-DPI manifest, `asInvoker` execution level (never ask for admin), long-path-aware manifest.
- `--no-browser`, `--off`, `--port`, and the rest of the CLI stay unchanged (the console exe and
  shortcuts rely on them).

### Phase 3: packaging (`packaging/`)
- `packaging/osmo-client.spec`: committed spec (not a long CLI line). Both EXEs in one `COLLECT`.
  `datas` for `client/web`; `collect_submodules("bleak")`; `collect_all("winrt")` (the spike needed it);
  `excludes` for `tkinter`, `unittest`, `pydoc`, `bleak.backends.corebluetooth`, `bleak.backends.bluezdbus`;
  `upx=False`; version resource from `packaging/version_info.py` generated from `camlink.__version__`
  so the exe's Properties dialog shows name, version, company, copyright; `assets/osmo-client.ico`.
- `requirements-build.txt` with exact pins and hashes (`pyinstaller`, `pyinstaller-hooks-contrib`,
  `pip-licenses`); runtime pins for the release build from a `pip-compile --generate-hashes` lock, so a
  release is reproducible and a compromised new upstream version cannot slip in.
- `packaging/build.ps1`: creates a clean venv (not the app's user venv) with Python 3.14, installs the
  lock, builds, generates `THIRD_PARTY_LICENSES.txt`, zips, compiles the installer, writes `SHA256SUMS`.
  Same script for local and CI, so CI is not special.
- `packaging/installer.iss`: Inno Setup; per-user (`PrivilegesRequired=lowest`), `AppId` fixed GUID for
  in-place upgrades, offers a desktop shortcut and "start with Windows" (unchecked by default),
  uninstall leaves `%LOCALAPPDATA%\osmo-pc` unless the user ticks "remove my settings and cameras".
- Icon and tray artwork: need a source file (decision below).

### Phase 4: CI and release (`.github/workflows/release.yml`)
- Trigger: tag `v*` (and `workflow_dispatch` for dry runs that do not publish).
- Runner `windows-latest`; actions pinned by commit SHA; `permissions: contents: read` for the build job,
  `contents: write` and `id-token: write` / `attestations: write` only on the publish job.
- Steps: checkout, `setup-python` 3.14, cache pip, run `build.ps1`, smoke test, upload artifacts,
  `actions/attest-build-provenance` on the zip and installer, create the release with assets and
  `SHA256SUMS`.
- Gate: tag version must equal `camlink.__version__`, else fail.
- Smoke test (`packaging/smoke.ps1`, also run locally): start the exe with `--off --no-browser
  --port 18091`, poll `/api/status` for 200 within 30 s, fetch `/` and `/monitor.js` and check byte
  counts, check `Host` rejection returns 403, `POST /api/quit`, assert exit code 0 within 10 s, assert
  no `Traceback` in the log. Repeat from the unzipped portable build in a path 120+ characters deep to
  cover the WinRT long-path issue. Runs for the installer too (silent install to a temp dir, then
  uninstall and assert the folder is gone while the data folder remains).
- Also add a `python -m compileall` and import check step on every pull request (cheap guard so the
  packaged build does not break from a missing hidden import).

### Phase 5: docs
- README Windows section: download the installer first; Python route moves to "Run from source".
  Add SmartScreen text ("More info, Run anyway", why it appears, how to verify the SHA-256 and the
  attestation with `gh attestation verify`), the Windows Firewall prompt (see risks), Bluetooth/Wi-Fi
  requirements, uninstall steps, where the log is.
- `NOTICE.md`: third-party components and licenses bundled (PyAV and its FFmpeg build, Pillow, bleak,
  winrt, Python), with the FFmpeg license note and where the source is available.
- Update troubleshooting and the `--report` instructions (`OsmoClient-console.exe --report`).
- Add a security note in the README about the local-only server and the new `Host`/`Origin` checks.

## 4. Risks and how each is closed

| Risk | Mitigation / check |
|---|---|
| Hidden import missed, works in spike but breaks later (winrt, bleak) | Pinned lock file; CI smoke test launches the real exe; manual BLE scan on hardware per release |
| Windows Firewall prompt when the camera's UDP video arrives | Per-user installer cannot add a rule. Test on a clean Windows 11 machine; if prompted, document "Allow on Private networks"; the sockets are outbound-initiated, so verify whether any prompt appears before deciding on an optional elevated firewall step |
| SmartScreen and Defender warnings on an unsigned exe | Onedir, no UPX, version resource, SignPath application, submit false-positive reports to Microsoft, publish SHA-256 and attestation |
| Wi-Fi left on the camera's network after an abnormal exit | All exits route into the one `finally`; end-session and console handlers; test: kill via tray, console X, `taskkill` (no `/f`), logoff, and verify the saved home network returns. Hard kill (`/f`) cannot be handled: document and keep the existing "reconnect to your Wi-Fi" note |
| Local web server abused by websites | Phase 0 `Host`/`Origin` checks, tested with `curl -H "Host: evil.example"` and a cross-origin POST |
| Long-path WinRT failure | Short default install path; deep-path smoke test; installer warns when a custom path is over 60 characters |
| Python 3.15 and new wheels (PyAV, winrt) | Build locked to 3.14 with `--only-binary`; Dependabot or manual bump with the smoke test as the gate |
| Bundle size 100 MB | Excludes, no UPX; accept about 70-90 MB zipped. Not a blocker |
| FFmpeg licensing | Ship notices; confirm the PyAV wheel's FFmpeg is LGPL and unmodified before first release |
| The repo layout: issue was filed on the fork (`guneethind/osmo-client`), upstream is `luisreysan/osmo-client` | See decision 2 below |

## 5. Verification matrix before the first release

1. Clean Windows 11 VM with no Python: install, launch, page opens, quit from tray, uninstall.
2. Clean Windows 10 22H2 VM: same (Bluetooth cannot be tested in a VM; hardware pass below).
3. Real hardware pass, Osmo Nano and one other model: pair, connect, live view, record, gallery
   download, disconnect, quit, Wi-Fi restored. Run once as the installer build and once as the portable zip.
4. Upgrade path: install 1.6.5 via `.bat`, link a camera, install the exe, confirm the camera is still linked.
5. Second launch while running: opens the existing page, no second process.
6. Defender real-time scan on the zip and installer; VirusTotal submission; record the results in the release notes.
7. Exit-path checks listed under risks.

## 6. Decisions needed from the maintainer

1. **Which repo publishes releases**: the fork or `luisreysan/osmo-client`. This decides who owns the
   signing application and the workflow secrets.
2. **Tray and installer in v1, or zip-only first**: zip plus the Phase 0-1 quit work is a smaller first
   release; installer and tray are Phase 2-3.
3. **Brand assets**: is there an icon, or should one be drawn (a simple camera glyph is enough).
4. **Signing**: apply to SignPath Foundation before the first release, or ship unsigned and sign later.

## 7. Suggested order and size

| Step | Content | Size |
|---|---|---|
| 1 | Phase 0 (security check, streams, resource helper) | small |
| 2 | Phase 1 (quit path, console handler, page button) | medium |
| 3 | Phase 3 spec, lock file, `build.ps1`, smoke test | medium |
| 4 | Phase 4 workflow, dry run with `workflow_dispatch` | small |
| 5 | Phase 2 tray, single instance, installer | medium |
| 6 | Phase 5 docs, hardware pass, tag a release | small |

Each step is a separate commit/PR and keeps the existing `.bat`, `.ps1` and `.sh` launchers working.

## 8. Review addendum (gaps found after the first draft)

- **FFmpeg is a GPL build.** PyAV's Windows wheel bundles `libx264` and `libx265`. v1 ships it under the GPL
  with a notice and source pointers (`packaging/FFMPEG_NOTICE.txt`, `NOTICE.md`); a decode-only LGPL
  FFmpeg build is a follow-up that would also shrink the download.
- **Smart App Control** (on for the author's machine) blocks unsigned programs without reputation and has no
  per-app override. Signing is needed before wide release; the README explains the workaround. Untested:
  a downloaded (Mark of the Web) copy on a machine with it enforced.
- **`netsh` flashed a console window** from a windowed build: fixed with `CREATE_NO_WINDOW`.
- **Windowed detection** uses the console window (`GetConsoleWindow`), not `sys.stdout is None`: a windowed
  program started from a terminal still gets inherited handles.
- **Installer vs running app:** `AppMutex` and `CloseApplications` are set; the uninstaller asks a running copy
  to quit (`/api/quit`) first.
- **Enterprise channels:** silent switches documented, SBOM and checksums in each release, provenance
  attestation. A winget manifest and MSIX are open items.
- **Portable mode:** `portable.txt` next to `OsmoClient.exe` keeps all data in `data\` beside it.
- x64 only; Windows on ARM untested.
