FROM python:3.11-slim

WORKDIR /app

# System deps for psycopg2-binary
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev gcc && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Flask CLI needs this to discover the app factory for migration commands
# (e.g., docker compose run --rm app flask db migrate -m "add column")
ENV FLASK_APP=wsgi

EXPOSE 5000

# startup.sh: auto-runs pending migrations, then starts the app
RUN chmod +x startup.sh
CMD ["bash", "startup.sh"]
