# Container Deployment Guidelines

## Scenario: Standalone Docker Compose Service

### 1. Scope / Trigger

Use this contract when adding a standalone third-party service under `deploy/`.
It prevents secret leakage, data-directory collisions, invalid Compose files,
and unverified substitution when Docker Hub requires a transport mirror.

### 2. Signatures

Run commands from the service deployment directory:

```powershell
docker compose config --quiet
docker compose pull
docker compose up -d
docker compose ps
```

When an already-verified local image must be used without another network pull:

```powershell
docker compose up -d --pull never
```

### 3. Contracts

- Location: `deploy/<service>/compose.yaml`.
- Runtime secrets: `deploy/<service>/.env`; `.env` must be ignored by Git.
- Shareable defaults: `deploy/<service>/.env.example`; it must contain no real
  credentials.
- Persistent host data: `deploy/<service>/data/`, mounted to the service's data
  path and ignored by Git.
- Required runtime checks: a Compose health check and an explicit restart
  policy.
- If a mirror transports a Docker Hub image, obtain the source
  `Docker-Content-Digest`, verify the mirrored image has the same digest, and
  only then tag it with the source image name.

### 4. Validation & Error Matrix

| Condition | Required response |
|---|---|
| `docker compose config --quiet` fails | Do not create the container; fix interpolation or YAML first. |
| Required secret is absent | Fail Compose interpolation with `${KEY:?set in .env}`. |
| Host port is already listening | Stop and report the owning process; do not replace it silently. |
| Image pull fails through a proxy | Diagnose proxy/direct paths before using a mirror. |
| Mirror digest differs from Docker Hub | Reject the mirrored image and do not tag or run it. |
| Health becomes `unhealthy` | Inspect health output and container logs; do not report success. |

### 5. Good / Base / Bad Cases

- Good: Compose validates, the source or digest-verified image is present, the
  container is healthy, its HTTP endpoint responds, and data is on the dedicated
  bind mount.
- Base: The container is still in its declared startup grace period; continue
  polling without declaring failure.
- Bad: A real password appears in a tracked file, a generic repository `data/`
  directory is reused, or an unverified mirror image is launched.

### 6. Tests Required

- Run `docker compose config --quiet` and assert exit code 0.
- Run `git check-ignore` for `.env` and a representative persistent data file.
- Inspect the running container and assert `running`, the expected restart
  policy, mount source/destination, published port, and `healthy` status.
- Request the local service endpoint and assert the expected HTTP status.
- For mirror fallback, assert the complete source and mirror digests are equal
  before `docker tag`.

### 7. Wrong vs Correct

Wrong:

```yaml
environment:
  ADMIN_PASSWORD: change-me
volumes:
  - ./data:/data
```

Correct:

```yaml
env_file:
  - .env
environment:
  ADMIN_PASSWORD: "${ADMIN_PASSWORD:?set in .env}"
volumes:
  - type: bind
    source: ${SERVICE_DATA_DIR:-./data}
    target: /data
```
