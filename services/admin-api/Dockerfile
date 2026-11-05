FROM python:3.12-slim
WORKDIR /srv
RUN pip install --no-cache-dir fastapi==0.115.6 "uvicorn[standard]==0.32.1" asyncpg==0.30.0 maxminddb==2.6.2
COPY fetch_assets.py .
RUN python fetch_assets.py
COPY app ./app
ENV PYTHONUNBUFFERED=1
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8081"]
