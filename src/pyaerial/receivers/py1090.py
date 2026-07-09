"""
Pure-Python RTL-SDR receiver (a much less capable stand-in for dump1090).

Decodes Mode S / ADS-B directly from raw IQ samples. Requires ``pyrtlsdr`` and a
working ``librtlsdr`` install. Adapted from "The 1090 Megahertz Riddle".
"""
from __future__ import annotations

import time
import warnings
from typing import Any

import numpy as np
from pyModeS.util import bin2hex, crc, df

from pyaerial.receivers import Receiver, register_receiver

warnings.filterwarnings("ignore", category=DeprecationWarning)
import rtlsdr  # noqa: E402  (import after warning filter, may fail without libusb)

SAMPLING_RATE = 2e6
SAMPLES_PER_MICROSECOND = 2
MODES_FREQUENCY = 1090e6
BUFFER_SIZE = 16384 * 16
READ_SIZE = 1024 * 100

PREAMBLE_BITS = 8
FRAME_BITS = 112
PREAMBLE = [1, 0, 1, 0, 0, 0, 0, 1, 0, 1, 0, 0, 0, 0, 0, 0]
TH_AMP_DIFF = 0.8  # amplitude threshold difference between a 0 and 1 bit


@register_receiver("py1090")
class Py1090Receiver(Receiver):
    def configure(self, arguments: dict) -> None:
        self.rtl_index = str(arguments.get("rtl_index", "0"))
        self.signal_buffer: list[float] = []
        self.noise_floor = 1e6

    def _initialize_sdr(self):
        address: Any = self.rtl_index
        serials = rtlsdr.RtlSdr.get_device_serial_addresses()
        if address in serials:
            address = rtlsdr.RtlSdr.get_default_input_device(address)
        try:
            sdr = rtlsdr.RtlSdr(address)
        except rtlsdr.rtlsdr.LibUSBError:
            return None
        sdr.sample_rate = SAMPLING_RATE
        sdr.center_freq = MODES_FREQUENCY
        sdr.gain = 496
        return sdr

    def _calc_noise(self) -> float:
        window = SAMPLES_PER_MICROSECOND * 100
        total_len = len(self.signal_buffer)
        means = (
            np.array(self.signal_buffer[: total_len // window * window])
            .reshape(-1, window)
            .mean(axis=1)
        )
        return float(min(means))

    def _process_buffer(self) -> list[list[Any]]:
        self.noise_floor = min(self._calc_noise(), self.noise_floor)
        min_sig_amp = 3.162 * self.noise_floor  # 10 dB SNR
        messages: list[list[Any]] = []
        buffer = self.signal_buffer
        buffer_length = len(buffer)

        i = 0
        while i < buffer_length:
            if buffer[i] < min_sig_amp:
                i += 1
                continue

            frame_start = i + PREAMBLE_BITS * 2
            if not _check_preamble(buffer[i:frame_start]):
                i += 1
                continue

            frame_length = (FRAME_BITS + 1) * 2
            frame_pulses = buffer[frame_start:frame_start + frame_length]
            if not frame_pulses:
                break
            threshold = max(frame_pulses) * 0.2

            bits: list[int] = []
            frame_index = 0
            for frame_index in range(0, frame_length, 2):
                pair = frame_pulses[frame_index:frame_index + 2]
                if len(pair) < 2:
                    break
                if pair[0] < threshold and pair[1] < threshold:
                    break
                bits.append(1 if pair[0] >= pair[1] else 0)

            i = frame_start + frame_index
            if bits:
                msg_hex = bin2hex("".join(str(b) for b in bits))
                if _check_msg(msg_hex):
                    messages.append([msg_hex, time.time()])

        self.signal_buffer = buffer[i:]
        return messages

    def run(self) -> str | None:
        sdr = self._initialize_sdr()
        if sdr is None:
            return "could not initialize SDR (is it connected?)"

        self.log.info("SDR initialized (index %s)", self.rtl_index)
        try:
            while not self.should_stop():
                try:
                    data = sdr.read_samples(READ_SIZE)
                except rtlsdr.rtlsdr.LibUSBError:
                    return "lost connection to SDR (was it disconnected?)"
                self.signal_buffer.extend(np.absolute(data).tolist())
                if len(self.signal_buffer) >= BUFFER_SIZE:
                    for msg, timestamp in self._process_buffer():
                        if df(msg) in (17, 18):  # true ADS-B
                            self.emit(msg, timestamp)
        finally:
            try:
                sdr.close()
            except Exception:  # pragma: no cover - best-effort cleanup
                pass
        return None


def _check_preamble(pulses) -> bool:
    if len(pulses) != 16:
        return False
    return all(abs(pulses[i] - PREAMBLE[i]) <= TH_AMP_DIFF for i in range(16))


def _check_msg(msg: str) -> bool:
    msg_df = df(msg)
    length = len(msg)
    if msg_df == 17 and length == 28:
        return crc(msg) == 0
    if msg_df in (20, 21) and length == 28:
        return True
    if msg_df in (4, 5, 11) and length == 14:
        return True
    return False
