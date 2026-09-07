import sounddevice as sd
info = sd.query_devices(29)
print(info)
print("max_input_channels:", info["max_input_channels"])
print("default_samplerate:", info["default_samplerate"])
