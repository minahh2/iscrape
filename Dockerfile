FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Pre-install Chromium browser binary
RUN playwright install chromium

COPY main.py .

EXPOSE 5009

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "5009"]
