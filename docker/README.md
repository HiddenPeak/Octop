# inhere-box Docker Deployment

---

This directory contains the Docker build and deployment assets for inhere-box.

### Files

| File | Description |
|------|-------------|
| `Dockerfile` | Multi-stage image definition (build context is the repo root) |
| `../.dockerignore` | Build context ignore rules (applies to both Podman and BuildKit) |
| `docker_build.sh` | Build image from source (BuildKit cache enabled by default) |
| `docker-compose.yml` | One-command local / self-hosted deployment |
| `docker-compose.postgres.yml` | PostgreSQL (+ pgvector) for dual-backend dev/tests |
| `postgres/init-vector.sql` | Instance-level `CREATE EXTENSION vector` (initdb.d; **not** inhere-box migrations) |
| `docker-entrypoint.sh` | Container entrypoint: first-run init + start server |

### Quick start

**Option 1: Compose (recommended)**

From the repository root:

```bash
docker compose -f docker/docker-compose.yml up -d --build
```

Open `http://localhost:8088`. Set `INHERE_BOX_DEFAULT_PASSWORD` to create the initial administrator (at least 8 characters with letters and digits). When it is unset or rejected by the password policy, open the setup wizard instead; its one-time password is saved to `/data/.inhere-box/inhere-box-login.txt`. Initial credentials or setup instructions are written to `/data/.inhere-box/credential.txt`.

Compose sets `nofile` to 65535 for multiple agents, streams and connector subprocesses. When using `docker run`, add `--ulimit nofile=65535:65535`.

**Option 2: Build script**

```bash
bash docker/docker_build.sh
docker run -d \
  --name inhere-box \
  -p 8088:8088 \
  -v inhere-box-data:/data/.inhere-box \
  -e HOME=/data \
  inhere-box:latest
```

### Faster downloads (China mirrors)

Pass mirror env vars when building:

```bash
PIP_INDEX_URL=https://mirrors.cloud.tencent.com/pypi/simple \
PIP_TRUSTED_HOST=mirrors.cloud.tencent.com \
NPM_REGISTRY=https://mirrors.cloud.tencent.com/npm/ \
APT_MIRROR=mirrors.cloud.tencent.com \
bash docker/docker_build.sh
```

### Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `HOME` | `/data` | Must be `/data` so `~/.inhere-box` maps to the data volume |
| `INHERE_BOX_PORT` | `8088` | HTTP listen port |
| `INHERE_BOX_DEFAULT_PASSWORD` | _(unset)_ | First-run admin password (≥8 chars, letters + digits). When unset or rejected, the setup wizard is used |
| `INHERE_BOX_ADMIN_USERNAME` | `admin` | Initial admin username |
| `INHERE_BOX_DATABASE_URL` | — | PostgreSQL DSN (or other `INHERE_BOX_DATABASE_*`; see [configuration.md](../docs/configuration.md)) |
| `INHERE_BOX_DATABASE_DRIVER` | — | `sqlite` \| `postgresql` when overriding defaults via env |
| `OPENAI_API_KEY` | — | OpenAI-compatible API key |
| `DASHSCOPE_API_KEY` | — | Alibaba DashScope API key |

For Compose, put these in `docker/.env`. Values only reach the container if listed under `environment:` in `docker-compose.yml` (Compose interpolates `.env`; it does not auto-export every key). Alternatively write the same keys into the mounted data dir as `~/.inhere-box/env`.

### Connect box models

When inhere-box and the inference services run on the same box, add a **Box** provider in model settings without an API key. The main model defaults to `http://127.0.0.1:58000/v1` with model `RM-01 VLM`. Configure box speech recognition (58100, `qwen3-asr`) and synthesis (58101, `qwen3-tts`) separately in voice settings. Synthesis uses VoiceDesign with a voice description.

inhere-box accesses these URLs from its server. Inside a container, `127.0.0.1` refers to the inhere-box container itself. For another container, use its service name on a shared network; for a service on the host, use a host address reachable from the container. All three URLs are configurable, and box requests omit authentication headers.

Remote providers can coexist with their own keys. Choose the box or a remote model for each turn in **Projects and tasks**, and select speech recognition and synthesis sources independently. Configuring the box does not automatically replace the current remote model or voice selection.

### Data persistence

- Compose mounts host `~/.inhere-box` → container `/data/.inhere-box`
- `docker run` example uses named volume `inhere-box-data`
- First boot creates an administrator only when `INHERE_BOX_DEFAULT_PASSWORD` meets the password policy. Otherwise it starts the setup wizard. Credentials or setup instructions are written to `/data/.inhere-box/credential.txt`.

Stop the old container before upgrading an existing installation. To migrate sibling `.octop` and `.inhere-box` roots inside the container, mount their host parent directory onto `/data`; the entrypoint migrates the old root before initialization or startup. An old root mounted directly at `/data/.octop` is a mount point and cannot be renamed in the container. Rebind its parent, or migrate offline on the host and bind the resulting root to `/data/.inhere-box`. Existing source and destination roots cause a clear error instead of an overwrite. Explicit `INHERE_BOX_HOME=.../.octop` (including the old `OCTOP_HOME` environment prefix) is converted to its sibling `.inhere-box` during migration; save the new path in the deployment configuration afterward.

### Health check

The image probes `GET /api/health`:

```bash
curl http://localhost:8088/api/health
```

### Operations

```bash
docker logs -f inhere-box
docker exec -it inhere-box inhere-box --version
docker compose -f docker/docker-compose.yml down
docker compose -f docker/docker-compose.yml up -d --build
```
