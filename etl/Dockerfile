FROM python:3.11-slim

ENV TZ=Asia/Makassar PYTHONUNBUFFERED=1
RUN apt-get update \
 && apt-get install -y --no-install-recommends cron tzdata \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY src/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ /app/src/
COPY entrypoint.sh /entrypoint.sh
# strip Windows CRLF line endings, otherwise the script fails with "not found"
RUN sed -i 's/\r$//' /entrypoint.sh && chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]