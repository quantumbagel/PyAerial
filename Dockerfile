FROM node:22-bookworm-slim AS webbuild
WORKDIR /opt/PyAerial
COPY web/package.json web/package-lock.json ./web/
RUN cd web && npm ci
COPY web/ ./web/
RUN cd web && npm run build

FROM ubuntu:24.04

RUN DEBIAN_FRONTEND=noninteractive apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    git build-essential pkg-config librtlsdr-dev libusb-dev libncurses-dev python3 python3-pip python3-venv rtl-sdr tini && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /opt/PyAerial
COPY . /opt/PyAerial
COPY --from=webbuild /opt/PyAerial/src/pyaerial/static /opt/PyAerial/src/pyaerial/static

RUN python3 -m pip install --break-system-packages ".[all]"

# Build dump1090 for the dump1090 receiver
RUN git clone --depth 1 https://github.com/flightaware/dump1090.git /opt/dump1090 && \
    make -C /opt/dump1090 RTLSDR=yes

ENV PYAERIAL_CONFIG=/opt/PyAerial/config.yaml
ENV PYAERIAL_START_DUMP1090=1

RUN chmod +x /opt/PyAerial/scripts/run-engine.sh

ENTRYPOINT ["tini", "--"]
CMD ["/opt/PyAerial/scripts/run-engine.sh"]
