import time
import numpy as np
import pyModeS as pms
from typing import Any
import warnings

warnings.filterwarnings("ignore", category=DeprecationWarning)  # IDK why they used pkg resources, this supresses
import rtlsdr

# typecodes:
# https://mode-s.org/decode/content/ads-b/1-basics.html.

# These ~25 lines of code took me FOREVER to find (thx "The 1090 Megahertz Riddle")


sampling_rate = 2e6
samples_per_microsec = 2

modes_frequency = 1090e6
buffer_size = 1024 * 200
read_size = 1024 * 100

pbits = 8
fbits = 112
preamble = [1, 0, 1, 0, 0, 0, 0, 1, 0, 1, 0, 0, 0, 0, 0, 0]  # Required beginning of message
th_amp_diff = 0.8  # signal amplitude threshold difference between 0 and 1 bit
signal_buffer = []
noise_floor = 1e6
message_queue = []
exception_queue = None
last_return = ""


def initalize_sdr():
    """
    Initialize the SDR
    :return: the SDR or None if failed
    """
    try:
        sdr = rtlsdr.RtlSdr()
    except rtlsdr.rtlsdr.LibUSBError:
        return None
    sdr.sample_rate = sampling_rate
    sdr.center_freq = modes_frequency
    sdr.gain = "auto"
    return sdr


def calc_noise() -> float:
    """
    Calculate the noise floor of the SDR
    :return: The noise floor
    """
    window = samples_per_microsec * 100  # Take 100 samples
    total_len = len(signal_buffer)  # Our available samples
    means = (
        np.array(signal_buffer[: total_len // window * window])  # Thanks stack overflow
        .reshape(-1, window)
        .mean(axis=1)
    )
    return min(means)


def process_buffer() -> list[list[Any]]:
    """
    Process the raw IQ data into the buffer
    :return: the messages processed
    """

    global noise_floor
    global signal_buffer

    # Calculate the noise floor
    noise_floor = min(calc_noise(), noise_floor)

    # Constant: minimum signal amplitude (10 dB SNR)
    min_sig_amp = 3.162 * noise_floor

    # Mode S messages
    messages = []

    buffer_length = len(signal_buffer)  # Length of the buffer

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

            threshold = max(frame_pulses) * 0.2

            binary_messages = []
            for frame_index in range(0, frame_length, 2):
                frame_slice = frame_pulses[frame_index:frame_index + 2]

                if len(frame_slice) < 2:  # For some reason this caused a crash EVEN THOUGH IT SHOULD ALWAYS BE 2
                    break

                if frame_slice[0] < threshold and frame_slice[1] < threshold:
                    break
                elif frame_slice[0] >= frame_slice[1]:
                    c = 1
                elif frame_slice[0] < frame_slice[1]:
                    c = 0
                else:
                    binary_messages = []
                    break

                binary_messages.append(c)

            i = frame_start + frame_index  # The frame index is how much we had to read to get a single message before
            # the loop broke - kinda janky, but it does work :)

            if len(binary_messages) > 0:  # If we got any messages:
                msghex = pms.bin2hex("".join([str(i) for i in binary_messages]))  # Turn them into normal hexadecimal
                if check_msg(msghex):  # Verify integrity
                    messages.append([msghex, time.time()])  # Add messages w/ time of receival
        else:
            i += 1

    # reset the buffer
    signal_buffer = signal_buffer[i:]

    return messages


def check_preamble(pulses) -> bool:
    """
    Ensure the ADS-B preamble is functional
    :param pulses:
    :return: bool
    """
    if len(pulses) != 16:  # There MUST be 16 pulses, save a bit of CPU
        return False

    for i in range(16):
        if abs(pulses[i] - preamble[i]) > th_amp_diff:  # Check that the amplitude is "close enough"
            return False

    return True


def check_msg(msg) -> bool:
    """
    Check message integrity (if it's an ADS-B message)
    :param msg: The message
    :return: bool
    """
    df = pms.df(msg)
    msglen = len(msg)
    if df == 17 and msglen == 28:  # Identification packet
        if pms.crc(msg) == 0:  # Make sure bits are valid
            return True
    elif df in [20, 21] and msglen == 28:  # Common mode-s message
        return True
    elif df in [4, 5, 11] and msglen == 14:  # Also common mode-s message EXCEPT for 11,
        # which is "all-call" interrogation reply
        return True
    return False


def read_callback(data) -> None:
    """
    Read data, update the buffer, and process messages
    :param data: The new data
    :return: None
    """
    amp = np.absolute(data)  # Positivize
    signal_buffer.extend(amp.tolist())  # Add it to the list

    if len(signal_buffer) >= buffer_size:  # If we have enough to overflow normal buffer size, process data
        messages = process_buffer()
        handle_messages(messages)  # Make sure to process the messages!


def handle_messages(messages) -> None:
    """
    A dummy message handler.
    :param messages: The messages to process
    :return: None
    """
    for msg, t in messages:
        iden = pms.df(msg)
        if iden in [17, 18]:  # true ADS-B message
            message_queue.append([msg, t])


def run() -> str:
    """
    Run the message scanner!
    :return: None
    """
    global last_return

    sdr = initalize_sdr()
    if sdr is None:
        last_return = "Couldn't initalize SDR. Is it connected?"
        return last_return
    while True:
        try:
            data = sdr.read_samples(read_size)
        except rtlsdr.rtlsdr.LibUSBError:
            last_return = "Lost connection to SDR. Was it disconnected?"
            return last_return
        read_callback(data)
