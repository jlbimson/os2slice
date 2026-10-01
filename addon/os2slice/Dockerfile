# Built by the Supervisor on homeassistant from /local_apps/os2slice.
FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHON_KEYRING_BACKEND=keyring.backends.null.Keyring
COPY app/ /src/
RUN pip install --no-cache-dir /src && rm -rf /src
CMD ["os2slice-addon"]
