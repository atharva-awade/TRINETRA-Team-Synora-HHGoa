# VERIFIED - face -> post -> chain
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 HOST=0.0.0.0 PORT=8000
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 ca-certificates && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

# bake the face models into the image so the first run is instant
COPY verified/__init__.py verified/__init__.py
COPY verified/face/__init__.py verified/face/__init__.py
COPY verified/face/models.py verified/face/models.py
RUN python -c "from verified.face.models import ensure_models; ensure_models(print)"

COPY . .
EXPOSE 8000
CMD ["python", "-m", "verified.cli", "serve"]
