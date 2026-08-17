# Playwright's official base image comes with Chromium and every OS-level
# dependency it needs already installed — avoids a long, fragile list of
# apt-get commands to get headless Chrome running in the container.
FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "bot.py"]
