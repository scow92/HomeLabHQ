FROM python:3.13-slim

WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
        openssh-client \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 homelabhq \
    && useradd --uid 10001 --gid homelabhq --create-home --shell /usr/sbin/nologin homelabhq \
    && mkdir /data \
    && chown homelabhq:homelabhq /data

COPY requirements.txt .
COPY constraints.txt .
RUN pip install --no-cache-dir -r requirements.txt -c constraints.txt

# Preserve a usable source tree even when the host checkout has restrictive
# permissions (for example an agent-owned 0660 worktree).
COPY --chown=homelabhq:homelabhq backend/ ./backend/
COPY --chown=homelabhq:homelabhq web/ ./web/

ENV HLHQ_DATA_DIR=/data \
    HLHQ_WEB_DIR=/app/web \
    HLHQ_PORT=8770 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
EXPOSE 8770 8771
VOLUME ["/data"]

USER homelabhq
CMD ["python3", "backend/app.py"]
