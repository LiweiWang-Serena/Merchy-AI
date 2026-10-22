FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependencies first so this layer caches independently of source changes.
COPY backend/requirements.txt ./backend/
RUN pip install --no-cache-dir -r backend/requirements.txt \
 && pip install --no-cache-dir gunicorn==23.0.0

# Copy backend and frontend together so app.py's relative path
# (parent.parent / "frontend") still resolves inside the container.
COPY backend/ ./backend/
COPY frontend/ ./frontend/

RUN useradd --create-home --shell /bin/bash merchy \
 && chown -R merchy:merchy /app
USER merchy

WORKDIR /app/backend
EXPOSE 5050

CMD ["gunicorn", "--bind", "0.0.0.0:5050", "--workers", "2", "--timeout", "120", "app:app"]
