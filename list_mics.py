import sounddevice as sd

for i, d in enumerate(sd.query_devices()):
    if d["max_input_channels"] > 0:
        print(i, d["name"], "| channels:", d["max_input_channels"], "| default_samplerate:", d["default_samplerate"], "| hostapi:", d["hostapi"])
