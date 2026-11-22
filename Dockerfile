FROM python:3.15.2-alpine@sha256:0f9552bb28711333da16915ec146f5e6bb44a7fdac9ced15dc17c2e0206346dd

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
