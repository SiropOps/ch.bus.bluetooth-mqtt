FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
# Some Raspberry Pi platforms build pycryptodome/dbus-fast from source.
# Python headers are already provided by the official Python image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libc6-dev \
    && pip install --no-cache-dir -r requirements.txt \
    && apt-get purge -y --auto-remove gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/*
COPY bluetooth_gateway ./bluetooth_gateway
STOPSIGNAL SIGTERM
CMD ["python", "-m", "bluetooth_gateway"]
