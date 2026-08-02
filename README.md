# Walter's Hub

A personal static site of small apps, served by GitHub Pages from the `main` branch (root folder). Live at `https://<user>.github.io/fantasy-hub/`.

## Layout

- `index.html` — landing page with an app grid and a data pipeline status panel.
- `draft/` — VBD Draft Board app.
- `data/players.json` — player projections used by the apps.
- `data/status.json` — last refresh result, shown on the landing page.
- `data/state/` — per-app synced state files (created by device sync).
- `lib/sync.js` — device sync module shared by apps.
- `scripts/refresh.py` — data refresh script.

## Data refresh

A GitHub Actions workflow ("Refresh data") runs daily at 10:00 UTC. It pulls 2026 season projections for QB/RB/WR/TE from the Sleeper API, rewrites `data/players.json`, records the result in `data/status.json`, and commits only if something changed. If the fetch fails, `players.json` is left untouched and `status.json` records the error.

To run it manually: open the repo's Actions tab, select "Refresh data", and click "Run workflow".

## Device sync

Apps can save their state (draft picks, settings) back to this repo so it follows you across devices. It works through the GitHub Contents API and needs a token:

1. Create a fine-grained personal access token at github.com > Settings > Developer settings > Fine-grained tokens. Scope it to this repo only, with Contents permission set to read and write.
2. Open an app's sync setup (the sync badge) and paste the owner, repo name, and token.
3. The token is stored only in that device's browser localStorage. It is never committed or sent anywhere except api.github.com.

Synced state is committed to `data/state/<app>.json` on `main`.

## Revoking or rotating the token

- Revoke: github.com > Settings > Developer settings > Fine-grained tokens > delete the token. Sync stops working on every device immediately.
- Rotate: create a new token, then re-run the sync setup on each device and paste the new one. Clearing a browser's site data also removes the stored token from that device.
