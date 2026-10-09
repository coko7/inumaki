"""Continuous voice control: mic -> faster-whisper -> actions.

Audio is captured with `parec` (PulseAudio/PipeWire) so any source can be
targeted by name. Speech is segmented with a simple energy gate, transcribed
by faster-whisper, then matched against commands in config.toml.
"""
