FROM ubuntu:24.04

# Install system dependencies
RUN DEBIAN_FRONTEND=noninteractive apt-get update && DEBIAN_FRONTEND=noninteractive apt install -y git build-essential fakeroot sudo debhelper librtlsdr-dev pkg-config libncurses5-dev gnupg librtlsdr-dev libusb-dev python3 python3-dev python3-pip rtl-sdr

# Create workdir
COPY ./* /opt/PyAerial

# Prepare
RUN python3 -m pip install -r requirements.txt
RUN git clone https://github.com/flightaware/dump1090.git

# Install requirements
RUN cd dump1090 && make RTLSDR=yes
RUN cd ..

# Run the program
CMD /dump1090/dump1090 --net --raw ; python3 pyaerial.py