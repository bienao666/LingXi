FROM python:3.13.15-slim-bookworm
WORKDIR /app
COPY inference/requirements.lock .
RUN pip install --no-cache-dir --extra-index-url https://download.pytorch.org/whl/cpu -r requirements.lock
COPY backend/lingxi/builtin_models.py .
COPY inference/app.py .
RUN useradd --uid 10001 --create-home inference && mkdir /models && chown inference /models
USER inference
ENV HF_HOME=/models HF_HUB_DISABLE_TELEMETRY=1 HF_HUB_DISABLE_XET=1 TOKENIZERS_PARALLELISM=false PYTHONUNBUFFERED=1
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8090", "--workers", "1", "--no-access-log"]
