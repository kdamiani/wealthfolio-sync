FROM python:3.15.0-alpine@sha256:2699b90f4d562a22e2dd076c82f964032f64814204bff36d271a54ba4cc2c7e2

RUN apk add --no-cache tzdata \
 && addgroup -S -g 10001 wfsync \
 && adduser -S -u 10001 -G wfsync -H -s /sbin/nologin wfsync \
 && mkdir /state && chown 10001:10001 /state

WORKDIR /app
COPY LICENSE /app/LICENSE
COPY wfsync /app/wfsync
COPY tests /app/tests
# The image only builds when the tests pass.
RUN python -m unittest discover -s tests -t . && rm -rf /app/tests

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 10001:10001
EXPOSE 8090
HEALTHCHECK --interval=60s --timeout=5s --retries=3 CMD wget -q -O - http://127.0.0.1:8090/healthz || exit 1
CMD ["python", "-m", "wfsync", "serve"]
