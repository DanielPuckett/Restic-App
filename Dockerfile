FROM python:3.12-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends restic sshfs && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY restic_wrapper.py app.py ./

ENTRYPOINT ["python", "/app/app.py"]
