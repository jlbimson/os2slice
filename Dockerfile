# os2slice for Docker Compose (see docs/DOCKER.md). The Home Assistant add-on builds
# from addon/os2slice/ instead.
FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHON_KEYRING_BACKEND=keyring.backends.null.Keyring \
    OS2SLICE_ADDON=1 \
    OS2SLICE_RUNTIME=docker \
    XDG_CONFIG_HOME=/data \
    XDG_STATE_HOME=/data/state
COPY pyproject.toml README.md /src/
COPY src /src/src
RUN pip install --no-cache-dir /src && rm -rf /src \
 && useradd --uid 1000 --user-group --no-create-home os2slice \
 && mkdir -p /data/os2slice /data/state /share/os2slice/inbox \
 && chown -R os2slice:os2slice /data /share/os2slice
USER os2slice
EXPOSE 8443
# The pass/fail table first (it goes to `docker compose logs`), then the server.
CMD ["sh", "-c", "os2slice doctor; exec os2slice serve"]
