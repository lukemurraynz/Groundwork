# Groundwork control plane.
#
# The only service that may call a model. Holds read-only access to customer tenants and never
# writes to one — that authority belongs to the orchestrator alone.
#
# Multi-stage so the runtime image carries no build toolchain. Runs as a non-root user with a
# read-only root filesystem enforced by the pod security context; nothing here needs to write to
# its own image.

FROM mcr.microsoft.com/azurelinux/base/python:3.12 AS builder

# Same path as the runtime stage's WORKDIR below, deliberately. `uv sync` bakes an absolute shebang
# (e.g. #!/build/.venv/bin/python3) into every script under .venv/bin. Building at a different path
# than where the venv is later COPYed produces a shebang pointing at a path that doesn't exist in
# the runtime image — the venv is copied correctly, but `exec .venv/bin/uvicorn` fails with
# "no such file or directory" because the *interpreter* it points to is missing, not the script
# itself. Caught only via a live pod CrashLoopBackOff; nothing offline exercises the built image.
WORKDIR /app

# uv is the project's declared package manager, pinned. Installed from PyPI rather than copied
# from a container image: the ghcr.io tag I first used did not exist, and a version on PyPI is
# verifiable. Never a curl-to-bash installer.
RUN pip install --no-cache-dir uv==0.11.14

# Dependency layer first. Source changes far more often than the lockfile, so this ordering keeps
# the expensive layer cached across ordinary edits.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --extra controlplane --extra telemetry

COPY src/ ./src/
RUN uv sync --frozen --extra controlplane --extra telemetry

FROM mcr.microsoft.com/azurelinux/base/python:3.12 AS runtime

# Non-root. The pod security context enforces this too, but an image that only works as root
# would make that enforcement a deployment-time surprise rather than a build-time fact.
# The Azure Linux minimal image has no shadow-utils, so groupadd/useradd do not exist. Writing
# the passwd and group entries directly avoids installing a package into the runtime image purely
# to create one account — less to patch, smaller attack surface.
#
# Kubernetes enforces runAsUser numerically and would work without any entry at all, but tooling
# that looks up the uid (and error messages) read better when the name resolves.
RUN printf 'groundwork:x:10001:10001::/nonexistent:/usr/sbin/nologin\n' >> /etc/passwd \
 && printf 'groundwork:x:10001:\n' >> /etc/group

WORKDIR /app

COPY --from=builder --chown=groundwork:groundwork /app/.venv /app/.venv
COPY --from=builder --chown=groundwork:groundwork /app/src /app/src
# The control plane is what actually loads the blueprint catalogue at startup
# (api/main.py's lifespan) — a container without it fails fast with BlueprintLoadError, which is
# how this was found. GROUNDWORK_BLUEPRINTS_PATH backs the Settings.blueprints_path field the
# lifespan reads; without it, the fallback ("infra/blueprints", relative to a repo checkout) does
# not exist inside this image.
COPY --chown=groundwork:groundwork infra/blueprints/ /app/blueprints/

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH="/app/src" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    GROUNDWORK_BLUEPRINTS_PATH="/app/blueprints"

USER groundwork

EXPOSE 8080

# No HEALTHCHECK directive: Kubernetes owns liveness and readiness, and FR-042 requires readiness to
# reflect real dependency health. A Docker-level healthcheck would be a second, weaker opinion.

CMD ["uvicorn", "groundwork_controlplane.api.main:app", "--host", "0.0.0.0", "--port", "8080"]
