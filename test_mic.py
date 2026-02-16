"""
Microphone test script for Anker PowerConf C200.
Tests: device detection, recording, audio quality metrics, playback.
"""

import sys
import time
import numpy as np
import sounddevice as sd
from scipy.io import wavfile

SAMPLE_RATE = 48000  # C200 supports 48kHz
RECORD_SECONDS = 5
OUTPUT_FILE = "test_recording.wav"


def list_devices():
    """List all audio input devices."""
    print("\n=== Available Input Devices ===\n")
    devices = sd.query_devices()
    input_devices = []
    for i, d in enumerate(devices):
        if d["max_input_channels"] > 0:
            input_devices.append(i)
            marker = " <-- default" if i == sd.default.device[0] else ""
            print(
                f"  [{i}] {d['name']}"
                f"  (channels: {d['max_input_channels']}, "
                f"rate: {d['default_samplerate']:.0f} Hz){marker}"
            )
    print()
    return input_devices


def select_device(input_devices):
    """Let user pick the C200 device."""
    choice = input("Enter device number (or press Enter for default): ").strip()
    if choice == "":
        dev = sd.default.device[0]
        print(f"  Using default device [{dev}]: {sd.query_devices(dev)['name']}")
        return dev
    idx = int(choice)
    if idx not in input_devices:
        print(f"  Error: {idx} is not a valid input device.")
        sys.exit(1)
    print(f"  Selected [{idx}]: {sd.query_devices(idx)['name']}")
    return idx


def check_device_caps(device_id):
    """Check supported sample rates for the device."""
    print("\n=== Device Capabilities ===\n")
    info = sd.query_devices(device_id)
    print(f"  Name:           {info['name']}")
    print(f"  Input channels: {info['max_input_channels']}")
    print(f"  Default rate:   {info['default_samplerate']:.0f} Hz")

    test_rates = [16000, 22050, 44100, 48000]
    supported = []
    for rate in test_rates:
        try:
            sd.check_input_settings(device=device_id, samplerate=rate, channels=1)
            supported.append(rate)
        except Exception:
            pass
    print(f"  Supported rates: {', '.join(str(r) for r in supported)} Hz")

    global SAMPLE_RATE
    if SAMPLE_RATE not in supported:
        SAMPLE_RATE = int(info["default_samplerate"])
        print(f"  ** Using fallback rate: {SAMPLE_RATE} Hz")
    else:
        print(f"  ** Using rate: {SAMPLE_RATE} Hz")
    print()


def record_audio(device_id):
    """Record a short clip and return the audio data."""
    print(f"=== Recording {RECORD_SECONDS}s of audio ===\n")
    print("  Speak now! (e.g., say: 'The quick brown fox jumps over the lazy dog')\n")

    audio = sd.rec(
        int(RECORD_SECONDS * SAMPLE_RATE),
        samplerate=SAMPLE_RATE,
        channels=1,
        dtype="float32",
        device=device_id,
    )
    for i in range(RECORD_SECONDS, 0, -1):
        print(f"  Recording... {i}s remaining", end="\r")
        time.sleep(1)
    sd.wait()
    print("  Recording complete!          ")

    # Save to WAV
    audio_int16 = np.int16(audio * 32767)
    wavfile.write(OUTPUT_FILE, SAMPLE_RATE, audio_int16)
    print(f"  Saved to: {OUTPUT_FILE}\n")
    return audio.flatten()


def analyze_audio(audio):
    """Compute audio quality metrics."""
    print("=== Audio Quality Metrics ===\n")

    # Basic stats
    peak = np.max(np.abs(audio))
    rms = np.sqrt(np.mean(audio**2))
    db_peak = 20 * np.log10(peak + 1e-10)
    db_rms = 20 * np.log10(rms + 1e-10)

    print(f"  Peak level:   {peak:.4f} ({db_peak:.1f} dBFS)")
    print(f"  RMS level:    {rms:.4f} ({db_rms:.1f} dBFS)")

    # Clipping check
    clip_count = np.sum(np.abs(audio) > 0.99)
    print(f"  Clipping:     {'YES (' + str(clip_count) + ' samples)' if clip_count else 'None'}")

    # Silence detection (estimate noise floor from quietest 10%)
    frame_size = int(0.02 * SAMPLE_RATE)  # 20ms frames
    n_frames = len(audio) // frame_size
    frame_rms = np.array([
        np.sqrt(np.mean(audio[i * frame_size:(i + 1) * frame_size] ** 2))
        for i in range(n_frames)
    ])
    noise_floor = np.percentile(frame_rms, 10)
    signal_level = np.percentile(frame_rms, 90)
    snr = 20 * np.log10((signal_level + 1e-10) / (noise_floor + 1e-10))

    print(f"  Noise floor:  {20 * np.log10(noise_floor + 1e-10):.1f} dBFS")
    print(f"  Signal level: {20 * np.log10(signal_level + 1e-10):.1f} dBFS")
    print(f"  Est. SNR:     {snr:.1f} dB")

    # Speech detection
    speech_frames = np.sum(frame_rms > noise_floor * 3)
    speech_pct = speech_frames / n_frames * 100
    print(f"  Speech detected: {speech_pct:.0f}% of recording")

    # Verdict
    print("\n=== Verdict ===\n")
    issues = []
    if peak < 0.01:
        issues.append("Very low signal - mic may not be working")
    elif peak < 0.05:
        issues.append("Low signal - try moving closer to mic or increasing input volume")
    if snr < 10:
        issues.append("Low SNR - noisy environment or poor mic quality")
    if clip_count > 100:
        issues.append("Significant clipping - reduce input volume")
    if speech_pct < 10:
        issues.append("Very little speech detected - did you speak during recording?")

    if not issues:
        print("  PASS - Audio looks good for speech recognition!")
        if snr > 20:
            print("  Excellent SNR - should produce very accurate transcriptions.")
        elif snr > 15:
            print("  Good SNR - should work well for speech recognition.")
    else:
        print("  ISSUES DETECTED:")
        for issue in issues:
            print(f"    - {issue}")
    print()


def playback_test():
    """Offer to play back the recording."""
    choice = input("Play back the recording? [y/N]: ").strip().lower()
    if choice == "y":
        print("  Playing...", end="", flush=True)
        rate, data = wavfile.read(OUTPUT_FILE)
        sd.play(data, rate)
        sd.wait()
        print(" done.\n")


def main():
    print("=" * 50)
    print("  Microphone Test - Anker PowerConf C200")
    print("=" * 50)

    input_devices = list_devices()
    if not input_devices:
        print("No input devices found!")
        sys.exit(1)

    # Accept device ID from command line, or prompt interactively
    if len(sys.argv) > 1:
        device_id = int(sys.argv[1])
        if device_id not in input_devices:
            print(f"Error: {device_id} is not a valid input device.")
            sys.exit(1)
        print(f"  Using device [{device_id}]: {sd.query_devices(device_id)['name']}\n")
    else:
        device_id = select_device(input_devices)

    check_device_caps(device_id)
    audio = record_audio(device_id)
    analyze_audio(audio)

    # Skip playback in non-interactive mode (e.g. piped stdin)
    try:
        playback_test()
    except EOFError:
        pass

    print("Done! If the test passed, we're ready to set up RealtimeSTT.")


if __name__ == "__main__":
    main()
