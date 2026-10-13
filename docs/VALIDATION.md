# Validation

Verified locally on 13 October 2026 on Windows, Python 3.14.7, Node.js 24.20.0, React 19.3.0, and Vite 8.3.4.

- **33 backend tests passed.** Source preservation, exact native JSON decimals, locale parsing, ambiguous dates, duplicate keys, schema drift, lossless header mappings, quarantine accounting, preview identity, upload limits, API exports, and CLI file protection.
- **11 browser scenarios passed** using Playwright with Microsoft Edge. No JavaScript page errors were recorded. The machine-readable report is [browser-validation.json](browser-validation.json).
- **Production React build passed.** Runtime assets are local; no external font or runtime CDN is required.
- **Windows launcher tested from a fresh project virtual environment.** Dependency installation, npm clean install, production build, and serving the UI all completed. Browser verification then ran against the project-owned environment.
- **Real CLI order scan passed its expected quality result.** 12 data records, 7 accepted, 5 quarantined, 23 transformations in accepted records, exit code 1.
- **Desktop and 390px mobile screenshots inspected.** Table overflow stays inside its scroll container. [Desktop](workbench.png) and [mobile](mobile.png).

Docker was not executed because the installed Docker Desktop engine was unavailable. Remote checks are tracked separately on the repository [Actions page](https://github.com/vanovri/datarescue/actions). No public app hosting or real-world data accuracy is claimed.

Browser verification exercises examples, schema changes, dirty-preview export blocking, ZIP download, pagination, header mapping, error handling, and desktop/mobile containment. The reproducible script is `scripts/verify_browser.py`.

```bash
pip install -e '.[dev,browser]'
python -m pytest
python scripts/verify_browser.py --channel msedge
```

The browser script expects the app to be running at `http://127.0.0.1:8763`. Use `--channel chromium` after installing Playwright Chromium if Microsoft Edge is unavailable.

All datasets used in checks are synthetic. The browser screenshot illustrates those demo files only. Remote GitHub CI, public hosting, and real-world data accuracy are separate from these checks.
