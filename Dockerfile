FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    tor curl gcc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# FIX #2: Copy torrc so Tor uses proper config
COPY main.py .
COPY torrc .

EXPOSE 7860

# FIX #3: Run as root so Tor can bootstrap properly in container
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "7860", "--workers", "1"]
