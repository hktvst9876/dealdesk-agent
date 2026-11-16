FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY dealdesk ./dealdesk
COPY profiles ./profiles
RUN pip install --no-cache-dir .
ENV PROFILE=salt_beach_club \
    DEALDESK_DB=/app/data/dealdesk.db \
    PYTHONUNBUFFERED=1
VOLUME ["/app/data"]
CMD ["sh", "-c", "dealdesk run \"$PROFILE\""]
