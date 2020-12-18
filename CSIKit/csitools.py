import os

import numpy as np

from .matlab import db, dbinv
import json

def get_CSI(trace, metric="amplitude", antenna_stream=None, scaled=True):
    csi_shape = trace[0]["csi"].shape
    # csi_key = "scaled_csi" if (scaled and len(csi_shape) == 3) else "csi"
    csi_key = "scaled_csi"
    # csi_key = "csi"

    no_frames = len(trace)
    no_subcarriers = csi_shape[0]

    if len(csi_shape) == 3:
        if antenna_stream == None:
            antenna_stream = 0

    csi = np.zeros((no_subcarriers, no_frames))

    for x in range(no_frames):
        scaled_entry = trace[x][csi_key]
        for y in range(no_subcarriers):
            if metric == "amplitude":
                if antenna_stream is not None:
                    csi[y][x] = db(abs(scaled_entry[y][antenna_stream][antenna_stream]))
                else:
                    csi[y][x] = db(abs(scaled_entry[y]))
            elif metric == "phasediff":
                if scaled_entry.shape[1] >= 2:
                    #Not 100% sure this generates correct Phase Difference.
                    csi[y][x] = np.angle(scaled_entry[y][1][0])-np.angle(scaled_entry[y][0][0])
                else:
                    #Unable to calculate phase difference on single antenna configurations.
                    return False

    return (csi, no_frames, no_subcarriers)

def get_timestamps(trace, relative=True):
    key = "timestamp" if relative else "timestamp_low"
    return list([x[key] for x in trace])

def get_total_rss(rssi_a, rssi_b, rssi_c, agc):
    """
        Calculates the Received Signal Strength (RSS) in dBm
        Careful here: rssis could be zero
    """
    rssi_mag = 0
    if rssi_a != 0:
        rssi_mag = rssi_mag + dbinv(rssi_a)
    if rssi_b != 0:
        rssi_mag = rssi_mag + dbinv(rssi_b)
    if rssi_c != 0:
        rssi_mag = rssi_mag + dbinv(rssi_c)

    #Interpreting RSS magnitude as power for RSS/dBm conversion.
    #This is consistent with Linux 802.11n CSI Tool's MATLAB implementation.
    #As seen in get_total_rss.m.
    return db(rssi_mag, "pow") - 44 - agc
 
def get_snr(entry):
    """
    calculates the snr of an entry and returns it
    """
    if not ("rssi_a" in entry and "rssi_b" in entry and "rssi_c" in entry and "agc" in entry):
        raise Exception("invalid entry rssi or agc is missing")
    if not ("noise" in entry):
        raise Exception("missing noise at entry")  

    rss_dBm = get_total_rss(entry["rssi_a"],entry["rssi_b"],entry["rssi_c"],entry["agc"])
    noise_dBm = entry["noise"]
    snr_dB = rss_dBm - noise_dBm 
    return snr_dB

def scale_csi_entry(frame):
    """
        This function performs scaling on the retrieved CSI data to account for automatic gain control and other factors.
        Code within this section is largely based on the Linux 802.11n CSI Tool's MATLAB implementation (get_scaled_csi.m).

        Parameters:
            frame {dict} -- CSI frame object for which CSI is to be scaled.
    """

    csi = frame["csi"]

    n_rx = frame["n_rx"]
    n_tx = frame["n_tx"]

    rssi_a = frame["rssi_a"]
    rssi_b = frame["rssi_b"]
    rssi_c = frame["rssi_c"]

    agc = frame["agc"]
    noise = frame["noise"]

    #Calculate the scale factor between normalized CSI and RSSI (mW).
    csi_sq = np.multiply(csi, np.conj(csi))
    csi_pwr = np.sum(csi_sq)
    csi_pwr = np.real(csi_pwr)

    rssi_pwr_db = get_total_rss(rssi_a, rssi_b, rssi_c, agc)
    rssi_pwr = dbinv(rssi_pwr_db)
    #Scale CSI -> Signal power : rssi_pwr / (mean of csi_pwr)
    scale = rssi_pwr / (csi_pwr / 30)

    #Thermal noise may be undefined if the trace was captured in monitor mode.
    #If so, set it to 92.
    noise_db = noise
    if (noise == -127):
        noise_db = -92

    noise_db = np.float(noise_db)
    thermal_noise_pwr = dbinv(noise_db)

    #Quantization error: the coefficients in the matrices are 8-bit signed numbers,
    #max 127/-128 to min 0/1. Given that Intel only uses a 6-bit ADC, I expect every
    #entry to be off by about +/- 1 (total across real and complex parts) per entry.

    #The total power is then 1^2 = 1 per entry, and there are Nrx*Ntx entries per
    #carrier. We only want one carrier's worth of error, since we only computed one
    #carrier's worth of signal above.
    quant_error_pwr = scale * (n_rx * n_tx)

    #Noise and error power.
    total_noise_pwr = thermal_noise_pwr + quant_error_pwr

    # ret now has units of sqrt(SNR) just like H in textbooks.
    ret = csi * np.sqrt(scale / total_noise_pwr)
    if n_tx == 2:
        ret = ret * np.sqrt(2)
    elif n_tx == 3:
        #Note: this should be sqrt(3)~ 4.77dB. But 4.5dB is how
        #Intel and other makers approximate a factor of 3.
        #You may need to change this if your card does the right thing.
        ret = ret * np.sqrt(dbinv(4.5))

    return ret

def scale_csi_entry_pi(csi, header):
    #This is not a true SNR ratio as is the case for the Intel scaling.
    #We do not have agc or noise values so it's just about establishing a scale against RSSI.

    rssi = np.abs(header["rssi"])

    #Calculate the scale factor between normalized CSI and RSSI (mW).
    csi_sq = np.multiply(csi, np.conj(csi))
    csi_pwr = np.sum(csi_sq)
    csi_pwr = np.real(csi_pwr)
    
    rssi_pwr = dbinv(rssi)
    
    #Scale CSI -> Signal power : rssi_pwr / (mean of csi_pwr)
    scale = rssi_pwr / (csi_pwr / 256)

    return csi * np.sqrt(scale)

def scale_timestamps(csi_trace):
    """
        This function adds an additional "timestamp" to each trace by assigning differential timestamps based on "timestamp_low".
        timestamp_low represents the current state of the low 32 bits of the IWL5300's clock.
        Since it wraps around every 72 minutes, the absolute timestamp values are rarely useful without context.
        Relative timestamps are typically easier to graph and represent most use cases more effectively.

        Parameters:
            csi_trace List[dict] -- A list of CSI frame structs as generated by read_bf_file.
    """

    time = [x["timestamp_low"] for x in csi_trace]

    timediff = (np.diff(time))*10e-7
    time_stamp = np.cumsum(timediff)

    csi_trace[0]["timestamp"] = 0
    for x in csi_trace[1:]:
        x["timestamp"] = time_stamp[csi_trace.index(x)-1]

    return csi_trace




def to_json(csi_trace):
    """
        This function converts a csi_trace into the json format. It works for single entry or the hole trace.

        Parameters:
            csi_trace List[dict] -- A list of CSI frame structs as generated by read_bf_file. Works with single csi Entry
    """
    def default(prop):
        if "complex" in str(type(prop)):
            return str(prop)
        if "numpy" in  str(type(prop)):
            print(f"prop is numpy :tpye {type(prop)} prop ->{prop}")
            return prop.tolist()
        if "__dict__" in dir(prop):
            print("has prop")
            return prop.__dict__
        else:
            print(f"Prop has no __dict__ {type(prop)}: \n {prop}")
            #return prop

    entry_clone = csi_trace.copy()
    json_str = json.dumps(entry_clone,default=default, indent=True)
    return json_str