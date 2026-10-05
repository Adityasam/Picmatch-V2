# Picmatch

Event photo finder. An organiser uploads event photos and shares a QR code. Attendees take a selfie on their phone and instantly see every photo they appear in.

Face matching runs **in the attendee's browser**. The selfie is never uploaded; only the names of matching photos are sent to the server.

Live at **https://picmatch.yosavi.com**

## How it works

1. **Organiser** (`/admin`) creates an event and uploads photos. Photos are shrunk in the browser before upload (`static/compress.js`, long side ≤ 2560 px).
2. **Background worker** (`app.py` → `encode.js`) detects every face with [face-api](https://github.com/vladmandic/face-api) and TensorFlow. It writes 128-number face descriptors to `data/<slug>/faces.json` and thumbnails to `data/<slug>/thumbs/`. One event is encoded at a time.
3. **Attendee** opens the event link or QR code (`/e/<slug>`) and takes a selfie. The browser compares it with `faces.json` and asks the server for the URLs of the matching photos.
4. Every **scan** (any outcome) and every photo **download** is logged for the scan analytics dashboard.

## Areas

| URL | Who | What |
|---|---|---|
| `/e/<slug>` | Anyone with the link | Attendee page: selfie → matching photos |
| `/login`, `/admin` | Users (event organisers) | Create and edit events, upload photos, start processing, get the QR code |
| `/manage/login`, `/manage` | Admins (Picmatch staff) | Create users, activate/deactivate them, reset passwords, soft-delete |
| `/manage/scans` | Admins | Scan analytics: totals, scans per day, outcomes, per-event stats, recent scans with IP and device, CSV export |

Users and admins are separate accounts with separate logins. Admin accounts are created from the command line only.

## Setup

Requirements: **Python 3.10+** and **Node.js 18–22**. `@tensorflow/tfjs-node` 4.22 doesn't support Node 23+.

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt gunicorn
npm install                      # face-api, tfjs-node, canvas (native build)
venv/bin/flask --app app create-admin <username>
venv/bin/flask --app app run --debug    # http://127.0.0.1:5000
```

The `models/` folder holds the face-api weights. The server's `encode.js` and the attendee's browser load the same files.

### Configuration

Set these in the environment or in a `.env` file next to `app.py`. Real environment variables take precedence over `.env`.

| Variable | Default | Purpose |
|---|---|---|
| `PICMATCH_DATA` | `./data` | Database, event photos, faces, thumbnails, generated secret key |
| `PICMATCH_NODE` | `node` on `PATH` | Full path to the Node binary used to run `encode.js` (services often have no Node on `PATH`) |
| `PICMATCH_SECRET` | generated into `data/secret_key` | Flask session secret. Logins survive restarts as long as it stays the same |

`data/`, `.env`, `venv/` and `node_modules/` are git-ignored. **Back up `data/`**: it is the only copy of the database and the photos.

## Command-line tools

```bash
venv/bin/flask --app app create-admin <username>           # new admin (prompts for password)
venv/bin/flask --app app reset-admin-password <username>   # set a new admin password
```

User (organiser) accounts and their passwords are managed in the browser at `/manage`.

## Scan analytics

Two SQLite tables in `data/picmatch.db` are created automatically on startup:

- **`scans`**: one row per selfie scan, with time (UTC), event slug, **visitor IP**, user agent, outcome and number of photos found. Outcomes are `matched`, `no_match`, `no_face` and `not_ready`. Matched scans are recorded by `POST /api/e/<slug>/paths`; the others are reported by the page through `POST /api/e/<slug>/scan`.
- **`downloads`**: one row per photo downloaded from the results page (`/i/<slug>/<name>?dl=1&s=<scan id>`), linked to the scan. Previews don't count.

Rows are never deleted automatically, and they survive event deletion. IP addresses are personal data, so consider a retention period if your events require one.

The app runs behind nginx, so `ProxyFix` reads the real visitor IP and https scheme from `X-Forwarded-For` / `X-Forwarded-Proto`. Without it, every scan would be logged from `127.0.0.1`.

## Tests

```bash
venv/bin/python test_app.py      # prints "ok"
```

This is a smoke test against a throwaway data folder. It covers accounts and permissions, events, uploads, the photo-paths API, scan and download recording, and the dashboard and CSV export.

## Deployment (picmatch.yosavi.com)

- **systemd service** `picmatch.service`: `gunicorn --workers 3 --bind 127.0.0.1:8013 app:app`, run as `administrator` from `/opt/Webapps/Picmatch`.
- **nginx** terminates TLS (Let's Encrypt) and proxies to `127.0.0.1:8013`.
- **Deploying a change:** `git pull`, then `sudo systemctl restart picmatch`. If processing was cut off by the restart, the event goes back in the queue and resumes where it stopped.

## Branches

- **`main`**: production; this is what runs on the server.
- **`dev-aditya`**: manual development; merged into `main` by pull request.
- **`dev-aditya-auto`**: changes made by the AI assistant; review and merge into `main` by pull request.
