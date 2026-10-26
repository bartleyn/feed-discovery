FROM python:3.12-slim

# Unprivileged runtime user. The API is reachable from the public internet
# through the tunnel; a bug in it should not hand out root in the container.
RUN groupadd --system app && useradd --system --gid app --home-dir /app --shell /usr/sbin/nologin app

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# .dockerignore keeps .env, .git, data dumps and caches out of this layer.
COPY --chown=app:app . .

USER app

EXPOSE 8391
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8391"]
