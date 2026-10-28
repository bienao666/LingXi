FROM python:3.13.15-slim-bookworm
WORKDIR /app
COPY backend/requirements.lock /app/requirements.lock
RUN pip install --no-cache-dir -r /app/requirements.lock
COPY backend /app/backend
ENV PYTHONPATH=/app/backend PYTHONUNBUFFERED=1
RUN useradd --uid 10001 --create-home lingxi && mkdir /app/runtime && chown lingxi /app/runtime
USER lingxi
CMD ["uvicorn", "lingxi.api:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
