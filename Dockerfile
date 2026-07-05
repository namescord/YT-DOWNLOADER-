# Optional alternative to the native Python runtime.
# Use this if you want a real system ffmpeg instead of the pip-bundled one.
# On Render: create the service as "Docker" instead of "Python".
FROM python:3.11-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# Render injects $PORT
CMD gunicorn app:app --workers 1 --threads 4 --worker-class gthread --timeout 300 --bind 0.0.0.0:$PORT
