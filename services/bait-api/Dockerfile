FROM python:3.12-slim
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
ENV PYTHONUNBUFFERED=1
CMD ["sh", "-c", "python -m app.engine.fakedb && uvicorn app.main:app --host 0.0.0.0 --port 8000 --no-server-header --proxy-headers --forwarded-allow-ips='*'"]
