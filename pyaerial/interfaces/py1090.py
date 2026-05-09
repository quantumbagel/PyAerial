"""
Pure-python ADS-B receiver (RTL-SDR). Lower quality than dump1090.
"""
import time
import warnings

import numpy as np
import pyModeS as pms
import rtlsdr

from pyaerial.constants import STORE_PIPELINE_LAST_RETURN, STORE_PIPELINE_MESSAGES

warnings.filterwarnings("ignore", category=DeprecationWarning)

sampling_rate = 2e6
samples_per_microsecond = 2
modes_frequency = 1090e6
buffer_size = 16384 * 16
read_size = 1024 * 100
pbits = 8
fbits = 112
preamble = [1, 0, 1, 0, 0, 0, 0, 1, 0, 1, 0, 0, 0, 0, 0, 0]
th_amp_diff = 0.8
signal_buffer = []
noise_floor = 1e6


def initialize_sdr(address):
    serials = rtlsdr.RtlSdr.get_device_serial_addresses()
    if address in serials:
        address = rtlsdr.RtlSdr.get_default_input_device(address)
    try:
        sdr = rtlsdr.RtlSdr(address)
    except rtlsdr.rtlsdr.LibUSBError:
        return None
    sdr.sample_rate = sampling_rate
    sdr.center_freq = modes_frequency
    sdr.gain = 496
    return sdr


def calc_noise() -> float:
    window = samples_per_microsecond * 100
    total_len = len(signal_buffer)
    means = (
        np.array(signal_buffer[: total_len // window * window])
        .reshape(-1, window)
        .mean(axis=1)
    )
    return min(means)


def process_buffer():
    global noise_floor
    global signal_buffer

    noise_floor = min(calc_noise(), noise_floor)
    min_sig_amp = 3.162 * noise_floor
    messages = []
    buffer_length = len(signal_buffer)
    i = 0
    while i < buffer_length:
        if signal_buffer[i] < min_sig_amp:
            i += 1
            continue

        frame_start = i + pbits * 2
        if check_preamble(signal_buffer[i:frame_start]):
            frame_length = (fbits + 1) * 2
            frame_end = frame_start + frame_length
            frame_pulses = signal_buffer[frame_start:frame_end]
            if not len(frame_pulses):
                break
            threshold = max(frame_pulses) * 0.2
            binary_messages = []
            frame_index = 0
            for frame_index in range(0, frame_length, 2):
                frame_slice = frame_pulses[frame_index : frame_index + 2]
                if len(frame_slice) < 2:
                    break
                if frame_slice[0] < threshold and frame_slice[1] < threshold:
                    break
                if frame_slice[0] >= frame_slice[1]:
                    c = 1
                elif frame_slice[0] < frame_slice[1]:
                    c = 0
                else:
                    binary_messages = []
                    break
                binary_messages.append(c)

            i = frame_start + frame_index

            if len(binary_messages) > 0:
                msg_hex = pms.bin2hex("".join([str(b) for b in binary_messages]))
                if check_msg(msg_hex):
                    messages.append([msg_hex, time.time()])
        else:
            i += 1

    signal_buffer = signal_buffer[i:]
    return messages


def check_preamble(pulses) -> bool:
    if len(pulses) != 16:
        return False
    for j in range(16):
        if abs(pulses[j] - preamble[j]) > th_amp_diff:
            return False
    return True


def check_msg(msg) -> bool:
    df = pms.df(msg)
    message_length = len(msg)
    if df == 17 and message_length == 28:
        if pms.crc(msg) == 0:
            return True
    elif df in [20, 21] and message_length == 28:
        return True
    elif df in [4, 5, 11] and message_length == 14:
        return True
    return False


def read_callback(data, pipeline) -> None:
    amp = np.absolute(data)
    signal_buffer.extend(amp.tolist())
    if len(signal_buffer) >= buffer_size:
        messages = process_buffer()
        handle_messages(messages, pipeline)


def handle_messages(messages, pipeline) -> None:
    for msg, t in messages:
        iden = pms.df(msg)
        if iden in [17, 18]:
            pipeline[STORE_PIPELINE_MESSAGES].append([msg, t])


def run(pipeline, rtl_index="0"):
    sdr = initialize_sdr(rtl_index)
    if sdr is None:
        pipeline[STORE_PIPELINE_LAST_RETURN] = "Couldn't initialize SDR. Is it connected?"
        return

    while True:
        try:
            data = sdr.read_samples(read_size)
        except rtlsdr.rtlsdr.LibUSBError:
            pipeline[STORE_PIPELINE_LAST_RETURN] = "Lost connection to SDR. Was it disconnected?"
            return
        read_callback(data, pipeline)
