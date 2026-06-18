# Cubbes — All Student Study Activity Hub

A live, filterable dashboard of student study activity, backed by PostHog (HogQL).
Static frontend (`index.html`, `wrapped.html`) + a small Python proxy API (`serve.py`).

## Architecture

- **Frontend** (`index.html`, `wrapped.html`) — static; hosted on **Vercel**.
- **Backend** (`serve.py`) — a long-lived Python server that proxies PostHog HogQL
  queries (some take many seconds, so it is **not** serverless); hosted on **Render**.
- The frontend calls the backend via `window.API_BASE` (set in each HTML `<head>`).
  CORS is open on the backend, so the split-origin setup works.

## Local development

```bash
POSTHOG_API_KEY=phx_your_key python3 serve.py
# open http://localhost:8765/index.html
```
`window.API_BASE` is `""` (same origin), so local just works.

## Deploy

### 1. Backend on Render (do this first — you need its URL)

1. Push this folder to a GitHub repo (see below).
2. Render → **New → Blueprint** (or **Web Service**) → pick this repo.
   `render.yaml` configures it: `python serve.py`, Python runtime.
3. Set env var **`POSTHOG_API_KEY`** = your PostHog personal API key (with query/read scope).
   `POSTHOG_PROJECT_ID` defaults to `238227`.
4. Deploy → copy the service URL, e.g. `https://cubbes-student-hub.onrender.com`.

> Free tier sleeps when idle, so the first request after a pause is slow (~30–60s).

### 2. Point the frontend at the backend

In **both** `index.html` and `wrapped.html`, set the `<head>` config to the Render URL:

```html
<script>
  window.API_BASE = "https://cubbes-student-hub.onrender.com";
</script>
```
Commit and push.

### 3. Frontend on Vercel

1. Vercel → **New Project** → import the same repo.
2. Framework preset: **Other** (it's static). No build command, output = repo root.
3. Deploy. Your dashboard is at `https://<project>.vercel.app/` (serves `index.html`).

## Security

- **Never commit the PostHog API key.** It is read from `POSTHOG_API_KEY` at runtime only.
- Rotate the key at https://us.posthog.com/settings/user-api-keys if it has ever been shared.

## Notes

- The backend caches responses for 20s and serves requests concurrently (threaded).
- `POSTHOG_PROJECT_ID` and `POSTHOG_HOST` are also overridable via env vars.
