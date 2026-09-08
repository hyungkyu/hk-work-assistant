FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
# The migrations travel with the image. Without them a container asked to
# migrate finds an empty directory, and before apply_migrations learned to
# refuse that, it reported the database fully migrated.
COPY sql ./sql

RUN pip install --no-cache-dir .

USER 65532:65532

EXPOSE 8080

CMD ["uvicorn", "rlwrld_worklog.web:app", "--host", "0.0.0.0", "--port", "8080"]

