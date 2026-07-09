FROM ubuntu:24.04

RUN DEBIAN_FRONTEND=noninteractive apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    git build-essential pkg-config librtlsdr-dev libusb-dev libncurses-dev python3 python3-pip python3-venv rtl-sdr && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /opt/PyAerial
COPY . /opt/PyAerial

RUN python3 -m pip install --break-system-packages ".[all]"

# Build dump1090 for the dump1090 receiver
RUN git clone --depth 1 https://github.com/flightaware/dump1090.git /opt/dump1090 && \
    make -C /opt/dump1090 RTLSDR=yes

ENV PYAERIAL_CONFIG=/opt/PyAerial/config.yaml

CMD /opt/dump1090/dump1090 --net --raw --quiet & exec pyaerial run -c "$PYAERIAL_CONFIG"
