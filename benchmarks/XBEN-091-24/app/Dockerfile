
FROM python:3.8-slim-buster

WORKDIR /app

# Re-route apt to the Debian archives for Buster and disable expiration checks
RUN echo "deb http://archive.debian.org/debian buster main" > /etc/apt/sources.list && \
    echo "Acquire::Check-Valid-Until \"false\";" > /etc/apt/apt.conf.d/10no-check-valid-until && \
    apt-get update && apt-get install -y curl

COPY requirements.txt requirements.txt
RUN pip install -r requirements.txt

COPY app.py app.py
COPY templates/ templates/

ARG FLAG
ENV FLAG=$FLAG

CMD ["python3", "app.py"]
