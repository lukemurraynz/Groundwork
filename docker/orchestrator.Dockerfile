# Groundwork orchestrator (execution worker).
#
# Writes to customer tenants under a per-tenant workload identity. Imports no model library —
# enforced by tests/unit/test_import_boundaries.py and by the ruff banned-api rule, and reflected
# here in the dependency extras: this image installs `orchestrator`, never `controlplane`.
#
# That is not a packaging detail. It means a model client is absent from the runtime image, so
# the deterministic-execution boundary holds even if someone adds an import by mistake.

FROM mcr.microsoft.com/azurelinux/base/python:3.12 AS builder

# Same path as the runtime stage's WORKDIR below, deliberately. `uv sync` bakes an absolute shebang
# (e.g. #!/build/.venv/bin/python3) into every script under .venv/bin. Building at a different path
# than where the venv is later COPYed produces a shebang pointing at a path that doesn't exist in
# the runtime image. Caught only via a live pod CrashLoopBackOff on the controlplane image; nothing
# offline exercises the built image, and this Dockerfile has the identical structure.
WORKDIR /app

RUN pip install --no-cache-dir uv==0.11.14

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --extra orchestrator --extra telemetry

COPY src/ ./src/
RUN uv sync --frozen --extra orchestrator --extra telemetry

FROM mcr.microsoft.com/azurelinux/base/python:3.12 AS runtime

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
COPY --chown=groundwork:groundwork infra/blueprints/ /app/blueprints/

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH="/app/src" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    GROUNDWORK_BLUEPRINTS_PATH="/app/blueprints"

USER groundwork

EXPOSE 8080

CMD ["uvicorn", "groundwork_orchestrator.worker:app", "--host", "0.0.0.0", "--port", "8080"]
