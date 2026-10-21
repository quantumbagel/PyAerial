# Base images are pinned by tag and digest; Dependabot (docker ecosystem) bumps both.

# ---- web portal -------------------------------------------------------------
# node:22 currently resolves to 22.23, which throws SyntaxError while loading tsc.
FROM node:22.16-bookworm-slim@sha256:048ed02c5fd52e86fda6fbd2f6a76cf0d4492fd6c6fee9e2c463ed5108da0e34 AS webbuild
ENV NODE_DISABLE_COMPILE_CACHE=1
WORKDIR /opt/PyAerial
COPY web/package.json web/package-lock.json ./web/
RUN cd web && npm ci
COPY web/ ./web/
RUN cd web && npm run build

# ---- dump1090 ---------------------------------------------------------------
FROM ubuntu:noble-20260917@sha256:534baea6a22c03a63003dbc8dbe78fe34bc0d7e595d9a9dc9834884ff530eb55 AS dump1090build
# Bump tag and commit together; the build fails if the tag no longer points at the commit.
ARG DUMP1090_TAG=v11.1
ARG DUMP1090_COMMIT=0339a57b89cd6e61856cbb13ae342c31ae7be5ac
RUN DEBIAN_FRONTEND=noninteractive apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    ca-certificates git build-essential pkg-config librtlsdr-dev libusb-1.0-0-dev libncurses-dev && \
    rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 --branch "${DUMP1090_TAG}" --recurse-submodules \
        https://github.com/flightaware/dump1090.git /src/dump1090 && \
    test "$(git -C /src/dump1090 rev-parse HEAD)" = "${DUMP1090_COMMIT}" && \
    make -C /src/dump1090 RTLSDR=yes BLADERF=no HACKRF=no LIMESDR=no SOAPYSDR=no \
        DUMP1090_VERSION="${DUMP1090_TAG}" dump1090

# ---- python venv ------------------------------------------------------------
FROM ubuntu:noble-20260917@sha256:534baea6a22c03a63003dbc8dbe78fe34bc0d7e595d9a9dc9834884ff530eb55 AS pybuild
RUN DEBIAN_FRONTEND=noninteractive apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends python3 python3-venv && \
    rm -rf /var/lib/apt/lists/*
RUN python3 -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
# Every dependency (and the setuptools build backend) comes from the hashed lockfile.
COPY requirements.lock /tmp/requirements.lock
RUN pip install --require-hashes -r /tmp/requirements.lock
COPY . /src/pyaerial
COPY --from=webbuild /opt/PyAerial/src/pyaerial/static /src/pyaerial/src/pyaerial/static
RUN pip install --no-deps --no-build-isolation /src/pyaerial && pip check

# ---- runtime ----------------------------------------------------------------
FROM ubuntu:noble-20260917@sha256:534baea6a22c03a63003dbc8dbe78fe34bc0d7e595d9a9dc9834884ff530eb55

RUN DEBIAN_FRONTEND=noninteractive apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    python3 rtl-sdr libncurses6 tini ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# Unprivileged runtime user. plugdev (gid 46 on Debian/Ubuntu/Raspberry Pi OS hosts)
# is the usual owner of RTL-SDR device nodes; see docker-compose.yml for other hosts.
RUN groupadd --system --gid 10001 pyaerial && \
    useradd --system --uid 10001 --gid pyaerial --groups plugdev \
        --home-dir /data --no-create-home --shell /usr/sbin/nologin pyaerial && \
    install -d -o pyaerial -g pyaerial -m 0750 /data

COPY --from=pybuild /opt/venv /opt/venv
COPY --from=dump1090build /src/dump1090/dump1090 /opt/dump1090/dump1090
RUN ! ldd /opt/dump1090/dump1090 | grep 'not found'
COPY --chmod=0755 scripts/run-engine.sh /opt/PyAerial/scripts/run-engine.sh
COPY config.yaml /opt/PyAerial/config.yaml

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYAERIAL_CONFIG=/opt/PyAerial/config.yaml \
    PYAERIAL_HISTORY=/data/pyaerial.db \
    PYAERIAL_AIRCRAFT_DB=/data/aircraft.db \
    PYAERIAL_START_DUMP1090=1

WORKDIR /data
USER pyaerial

ENTRYPOINT ["tini", "--"]
CMD ["/opt/PyAerial/scripts/run-engine.sh"]
