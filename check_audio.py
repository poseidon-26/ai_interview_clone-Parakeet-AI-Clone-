import pyaudio

p = pyaudio.PyAudio()
print(f"{'Index':<6} {'Name':<45} {'Inputs':<8} {'Outputs':<8}")
print("-" * 70)
for i in range(p.get_device_count()):
    info = p.get_device_info_by_index(i)
    print(f"{i:<6} {info['name']:<45} {int(info['maxInputChannels']):<8} {int(info['maxOutputChannels']):<8}")
p.terminate()
