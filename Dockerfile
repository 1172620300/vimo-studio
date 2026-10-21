# 国内服务器可在 .env 里覆盖：BASE_IMAGE / APT_MIRROR / PIP_INDEX_URL（见 README「部署到服务器」）
ARG BASE_IMAGE=python:3.11-slim
FROM ${BASE_IMAGE}
ARG APT_MIRROR=
ARG PIP_INDEX_URL=
RUN if [ -n "$APT_MIRROR" ]; then sed -i "s#http://deb.debian.org#$APT_MIRROR#g" /etc/apt/sources.list.d/*.sources /etc/apt/sources.list 2>/dev/null || true; fi \
    && apt-get update && apt-get install -y --no-install-recommends ffmpeg fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir ${PIP_INDEX_URL:+-i $PIP_INDEX_URL} -r requirements.txt
COPY . .
ENV DRAMA_DATA_DIR=/data PYTHONUNBUFFERED=1
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/healthz')" || exit 1
CMD ["python", "-m", "drama_agent", "serve", "--host", "0.0.0.0", "--port", "8000"]
