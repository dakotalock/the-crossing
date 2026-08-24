FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PYTHONDONTWRITEBYTECODE=1

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY alembic.ini /app/alembic.ini
COPY alembic /app/alembic
COPY crossing /app/crossing

EXPOSE 8000
# Graceful shutdown: uvicorn waits for in-flight requests (see docs/deploy.md).
CMD ["uvicorn", "crossing.api:app", "--host", "0.0.0.0", "--port", "8000", "--timeout-graceful-shutdown", "30"]
