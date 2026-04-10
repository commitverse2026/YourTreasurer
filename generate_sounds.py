import wave, struct, random, math
import os

os.makedirs('Static/audio', exist_ok=True)

def generate_tone(filename, freq=440.0, duration=0.2, noise=False):
    sampleRate = 44100.0
    with wave.open(filename, 'w') as wavef:
        wavef.setnchannels(1)
        wavef.setsampwidth(2)
        wavef.setframerate(sampleRate)
        for i in range(int(duration * sampleRate)):
            if noise:
                value = int(random.uniform(-32767, 32767))
            else:
                value = int(32767.0 * math.cos(freq * math.pi * float(i) / float(sampleRate)) * (1.0 - i/(duration*sampleRate)))
            data = struct.pack('<h', value)
            wavef.writeframesraw(data)

print("Generating WAV files...")
# Clink = high pitch
generate_tone('Static/audio/clink.wav', 1200.0, 0.1)
# Crumple = noise
generate_tone('Static/audio/crumple.wav', noise=True, duration=0.3)
# Whoosh = lower pitch sweep (hard to do simply, let's just do a smooth mid tone)
generate_tone('Static/audio/whoosh.wav', 200.0, 0.4)
# Unlock = pleasant chirp
generate_tone('Static/audio/unlock.wav', 800.0, 0.2)
# Kaching = high pitch medium duration
generate_tone('Static/audio/kaching.wav', 1500.0, 0.3)
print("Done!")
