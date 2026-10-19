"""Speech-to-text for dictated bills.

The browser records WAV via the streamlit-mic-recorder component; recognition uses
Google's free Web Speech endpoint through the ``speech_recognition`` package
(no API key, needs internet). Claude then turns the transcript into a bill.
"""
from __future__ import annotations

import io
from typing import Optional


class SpeechError(RuntimeError):
    pass


def transcribe(audio_bytes: bytes, sample_rate: int, sample_width: int, language: str = "en-IN") -> str:
    """Transcribe raw PCM WAV bytes captured by the mic component."""
    if not audio_bytes:
        raise SpeechError("No audio was recorded.")
    try:
        import speech_recognition as sr
    except ImportError as e:  # pragma: no cover
        raise SpeechError("Install 'SpeechRecognition' (pip install -r requirements.txt).") from e
    r = sr.Recognizer()
    audio = None
    if audio_bytes[:4] == b"RIFF":
        # streamlit-mic-recorder (format="wav") delivers a complete WAV file, whose channel count
        # follows the browser (often stereo). Treating it as raw mono PCM plays it at half speed
        # and recognition fails – so parse the header and let the library down-mix to mono.
        try:
            with sr.AudioFile(io.BytesIO(audio_bytes)) as src:
                audio = r.record(src)
        except Exception:  # noqa: BLE001 - fall back to the raw interpretation below
            audio = None
    if audio is None:
        audio = sr.AudioData(audio_bytes, int(sample_rate or 16000), int(sample_width or 2))
    try:
        text = r.recognize_google(audio, language=language)
    except sr.UnknownValueError as e:
        raise SpeechError("Could not understand the audio – speak closer to the mic and a little slower.") from e
    except sr.RequestError as e:
        raise SpeechError(f"Speech service unreachable (internet?): {e}") from e
    except Exception as e:  # pragma: no cover - defensive
        raise SpeechError(f"Speech recognition failed: {e}") from e
    text = (text or "").strip()
    if not text:
        raise SpeechError("Empty transcript.")
    return text


def transcribe_file(data: bytes, language: str = "en-IN") -> str:
    """Transcribe an uploaded WAV/AIFF/FLAC file."""
    if not data:
        raise SpeechError("Empty audio file.")
    try:
        import speech_recognition as sr
    except ImportError as e:  # pragma: no cover
        raise SpeechError("Install 'SpeechRecognition' (pip install -r requirements.txt).") from e
    r = sr.Recognizer()
    try:
        with sr.AudioFile(io.BytesIO(data)) as src:
            audio = r.record(src)
    except Exception as e:
        raise SpeechError("Only WAV / AIFF / FLAC audio files are supported for upload.") from e
    try:
        text = r.recognize_google(audio, language=language)
    except sr.UnknownValueError as e:
        raise SpeechError("Could not understand the audio.") from e
    except sr.RequestError as e:
        raise SpeechError(f"Speech service unreachable (internet?): {e}") from e
    except Exception as e:  # pragma: no cover - defensive (e.g. socket timeout not wrapped by the lib)
        raise SpeechError(f"Speech recognition failed: {e}") from e
    text = (text or "").strip()
    if not text:
        raise SpeechError("Empty transcript.")
    return text
