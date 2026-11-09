ARG BASE_IMAGE=python:3.11-slim
FROM ${BASE_IMAGE}

ARG PIP_INDEX_URL=https://pypi.org/simple

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

WORKDIR /app

# python:slim ships pip/certifi; avoid apt-get so the build works behind
# restricted Debian mirrors as well as on Docker Hub.
COPY requirements.txt .
RUN pip install --no-cache-dir --index-url "$PIP_INDEX_URL" -r requirements.txt

COPY src ./src
COPY policies ./policies
COPY prompts ./prompts
COPY scripts ./scripts

RUN useradd --create-home --uid 10001 app \
    && mkdir -p /app/data \
    && chown -R app:app /app
USER app

EXPOSE 8000
CMD ["python", "-m", "codereview.server"]
