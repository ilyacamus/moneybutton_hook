FROM python:3.13-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY hookbot ./hookbot
COPY telegram_bot ./telegram_bot
RUN pip install --no-cache-dir ".[bot]"

ENV HOOKBOT_DB=/data/hookbot.db
VOLUME /data
CMD ["python", "-m", "telegram_bot"]
