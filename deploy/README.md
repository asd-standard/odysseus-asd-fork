# Docker deployment and data migration

Runbook for moving this checkout (with all users and their data) to another
Linux amd64 host. The image contains code only; `./data` and `.env` are
deployment artifacts and must be migrated separately.

Target layout:

```
/opt/odysseus/
├── .env                 # copied from deploy/.env.production.example (mode 600)
├── docker-compose.yml
├── data/                # imported user data, secrets, uploads, SQLite DBs
├── logs/
└── ...                  # repository checkout
```

## 0. Preconditions on the target

```bash
uname -m                      # expected: x86_64
docker --version              # Docker Engine 24+
docker compose version        # Compose v2
python3 --version             # used by the data migration helper
df -h /opt                    # allow for the image plus ~1 GB of data
tailscale ip -4               # note the Tailscale IPv4 address
id -u && id -g                # use these for PUID / PGID
```

If the host has no Python 3, run the migration helper through a container:

```bash
docker run --rm -v "$PWD":/work -w /work python:3.12-slim \
  python deploy/migrate_data.py import odysseus-data.tar.gz --data-dir ./data
```

If the fork is private, make sure the target has a deploy key or that you can
copy a source archive instead of cloning over HTTPS.

## 1. Get the source

The migration helpers and compose changes must be present on the target. Use
whichever path fits your workflow.

**Option A — push the branch, then clone it.** This is the reproducible choice
once the changes are committed and pushed to the fork:

```bash
sudo mkdir -p /opt/odysseus
sudo chown "$USER":"$USER" /opt/odysseus
git clone git@github.com:asd-standard/odysseus-asd-fork.git /opt/odysseus
cd /opt/odysseus

# Pin the code you are migrating from. Check out the branch/commit that
# includes these deploy helpers (the local branch is `local-odysseus`).
# Do not check out the older app-only commit: it lacks deploy/ and the
# compose build arguments.
git checkout local-odysseus
```

**Option B — copy the current working tree.** Use this while the deploy
helpers are still uncommitted. It never copies `data/`, `.env`, logs, or git
history, so no user data or secrets are sent in this step:

```bash
rsync -a \
  --exclude '/data/' --exclude '/logs/' --exclude '/backups/' --exclude '/.git/' \
  --exclude '/.env' --exclude 'odysseus-data-*.tar.gz*' --exclude '__pycache__/' \
  ./ target:/opt/odysseus/
```

(Adjust `target` to your SSH/Tailscale host alias.) The target directory must
already exist and be writable by your SSH user.

## 2. Configure the deployment

```bash
install -m 600 deploy/.env.production.example .env
$EDITOR .env
```

Set at minimum:

| Variable | Value |
| --- | --- |
| `APP_BIND` | the Tailscale IP from `tailscale ip -4` |
| `ALLOWED_ORIGINS` | `http://<tailscale-ip>:7000,http://<magicdns-name>:7000` |
| `PUID` / `PGID` | target user's ids |
| `INSTALL_OPTIONAL` | `true` for feature parity |
| `SECURE_COOKIES` | `false` for plain HTTP on the tailnet |

The DeepSeek endpoint and API key live encrypted in `data/app.db`; the key is
`data/.app_key`. They are migrated in step 3 and need no `.env` entry.

## 3. Export the data on the old host

A consistent final copy needs the local app stopped. Stop it first if you want
the snapshot to include the last writes:

```bash
# on the old host, in the repository directory
# Ctrl-C the `python app.py` process, or kill its PID
python deploy/migrate_data.py export --out /tmp/odysseus-data.tar.gz
```

The exporter:

- uses SQLite's online backup API for every `*.db` (safe even if the app is
  still running, but a stopped app gives the most consistent final state);
- includes `auth.json`, `.app_key`, settings, preferences, presets, memory,
  uploads, personal documents, skills, deep research, generated images, mail
  attachments, agent workspace, and the FastEmbed model cache;
- excludes disposable caches, logs, `data/local` (architecture-specific), and
  `data/sessions.json` (old login sessions; users simply log in again);
- rewrites `uploads/uploads.json` paths from the old absolute host path to
  `/app/data/uploads/...`, which is what the container sees;
- writes a `.sha256` checksum and a `.manifest.json` with per-user row counts.

Keep the old `data/` directory untouched until the new host is verified.

Copy the bundle and checksum over Tailscale/SSH:

```bash
scp /tmp/odysseus-data.tar.gz* target:/opt/odysseus/
```

## 4. Import on the target

```bash
cd /opt/odysseus
python3 deploy/migrate_data.py verify odysseus-data.tar.gz
python3 deploy/migrate_data.py import odysseus-data.tar.gz --data-dir ./data
```

The importer:

- verifies the SHA256 and rejects absolute paths, `..`, hardlinks, and symlinks
  that escape `data/`;
- normalises upload paths to `/app/data/uploads/...` and checks every indexed
  upload actually exists;
- refuses to overwrite a non-empty `./data` unless `--force` is passed; with
  `--force`, the previous tree is stashed as `data.before-import-<timestamp>`;
- sets `data/.app_key`, `auth.json`, and the SQLite databases to mode `600`;
- chowns to `PUID`/`PGID` when run as root (the container entrypoint also
  repairs ownership on first start).

Delete the transferred bundle after a successful import (it contains password
hashes, the encryption key, and personal data).

## 5. Build and start

```bash
cd /opt/odysseus
docker compose build odysseus
docker compose up -d
docker compose logs -f odysseus
```

The entrypoint runs `setup.py`; because `auth.json` already exists it does not
create a new admin, it only repairs directory ownership and starts uvicorn.

### If the optional build fails on Python 3.14

`faster-whisper` (via `ctranslate2`) is the most likely package without a
cp314 wheel; STT/TTS are disabled in the migrated settings. Fallback to the
useful extras only:

```bash
INSTALL_OPTIONAL=false docker compose build odysseus
docker compose up -d
docker compose exec -T odysseus pip install --user \
  PyMuPDF 'markitdown[docx,pptx,xlsx,xls]' ddgs
docker compose restart odysseus
```

`/app/.local` is bind-mounted to `./data/local`, so the install persists across
container restarts. Alternatively pin the selective install in the Dockerfile
for future rebuilds.

## 6. Verify

```bash
curl -fsS "http://<tailscale-ip>:7000/api/health"
```

`/api/ready` runs the database/data-directory integrity checks and returns 503
unless they pass. It requires a logged-in session (401 when anonymous), so open
it from the browser after logging in. It is the quickest way to catch an import
problem.

- Browse to `http://<tailscale-ip>:7000` from a tailnet device.
- Log in as both users (`admin`, `flower-exam`); passwords are unchanged.
- Check session counts/history, documents, notes, calendars, scheduled tasks,
  skills, and memory.
- Download an uploaded PDF from a chat: this proves the `/app/data/uploads`
  path rewrite worked.
- Send a test message to the configured cloud model: this proves `.app_key`
  decrypts the endpoint API key.
- Confirm search works (`docker compose ps` should show searxng healthy) and
  the logs show a ChromaDB connection.

## 7. Operations

`restart: unless-stopped` brings the stack back after a reboot as long as the
Docker daemon itself is enabled:

```bash
sudo systemctl enable --now docker
```

### Updates

```bash
cd /opt/odysseus
git fetch && git checkout <new-fork-commit>
docker compose build odysseus
docker compose up -d
```

`./data` persists across rebuilds.

### Backups

Back up the bind mount and the ChromaDB volume:

```bash
cd /opt/odysseus
mkdir -p backups
tar czf "backups/data-$(date +%F-%H%M).tar.gz" data
docker run --rm \
  -v odysseus_chromadb-data:/chroma \
  -v "$PWD/backups":/backup \
  alpine tar czf "/backup/chromadb-$(date +%F-%H%M).tar.gz" -C /chroma .
```

The volume name is prefixed with the Compose project name (the directory name
by default); confirm with `docker volume ls`.

### Tailscale notes

- The compose port mapping binds only to `APP_BIND`. Changing the Tailscale IP
  requires updating `.env` and `docker compose up -d`.
- Plain HTTP is encrypted by WireGuard on the tailnet. If you later want HTTPS
  with a valid certificate, set `APP_BIND=127.0.0.1`,
  `SECURE_COOKIES=true`, put `tailscale serve` in front, and add the HTTPS
  origin to `ALLOWED_ORIGINS` / `OAUTH_REDIRECT_BASE_URL`.

### Rollback

The old host's `data/` was never modified. If the new deployment is wrong,
start the old app again. On the target, an import with `--force` leaves a
`data.before-import-*` copy that can be restored by renaming it back to
`data/` while the stack is stopped.
