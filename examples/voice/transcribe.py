#!/usr/bin/env python3
"""Print the transcript of an audio file with faster-whisper — a ready-made VOICE_CMD.

    VOICE_CMD=python3 /path/to/vps-director/examples/voice/transcribe.py {file}
    VOICE_CMD=python3 /path/to/vps-director/examples/voice/transcribe.py --language ru --model small {file}

Everything runs on this server; no audio leaves it. Needs `pip install faster-whisper` for the
Python that runs this script (a venv is fine: point VOICE_CMD at that venv's python). The model is
downloaded once on first use (small ≈ 500 MB) and cached.

Settings (flags, or environment variables for the bot's service):
    --model     WHISPER_MODEL     tiny / base / small / medium / large-v3   (default small)
    --language  WHISPER_LANGUAGE  "auto" or a code like en, ru               (default auto)
    --device    WHISPER_DEVICE    auto / cpu / cuda                          (default auto)
    --compute   WHISPER_COMPUTE   int8 / int8_float16 / float16 / float32   (default int8)

Two settings matter most for short voice messages: VAD is on (silence is cut, so Whisper does not
"hear" words in it) and the previous segment is not fed back as a prompt (no repeated phrases).
A fixed --language helps a lot with messages of a few words.

Exit status 0 and the text on stdout; otherwise the reason on stderr (the bot then says it could
not transcribe the message and sends nothing to the agent).
"""
import argparse
import os
import sys


def build_parser():
    env = os.environ.get
    parser = argparse.ArgumentParser(description="Transcribe an audio file with faster-whisper.")
    parser.add_argument("file", help="audio file (Telegram voice notes are .oga / Opus)")
    parser.add_argument("--model", default=env("WHISPER_MODEL", "small"))
    parser.add_argument("--language", default=env("WHISPER_LANGUAGE", "auto"))
    parser.add_argument("--device", default=env("WHISPER_DEVICE", "auto"))
    parser.add_argument("--compute", default=env("WHISPER_COMPUTE", "int8"))
    parser.add_argument("--beam", type=int, default=5)
    return parser


def transcribe(path, model_name="small", language="auto", device="auto", compute="int8", beam=5, model_factory=None):
    if model_factory is None:
        from faster_whisper import WhisperModel  # imported here: --help works without it
        model_factory = WhisperModel
    model = model_factory(model_name, device=device, compute_type=compute)
    segments, _info = model.transcribe(
        path,
        language=None if language in ("", "auto") else language,
        vad_filter=True,                   # skip silence instead of inventing words in it
        vad_parameters={"min_silence_duration_ms": 500},
        condition_on_previous_text=False,  # no feedback loop of repeated phrases
        beam_size=beam,
    )
    return " ".join(seg.text.strip() for seg in segments if seg.text.strip()).strip()


def main(argv=None, model_factory=None):
    args = build_parser().parse_args(argv)
    if not os.path.isfile(args.file):
        print("no such file: %s" % args.file, file=sys.stderr)
        return 2
    try:
        text = transcribe(args.file, args.model, args.language, args.device, args.compute, args.beam, model_factory)
    except ImportError:
        print("faster-whisper is not installed for %s: pip install faster-whisper "
              "(or install it in a venv and use that venv's python in VOICE_CMD)" % sys.executable, file=sys.stderr)
        return 3
    except Exception as exc:  # noqa: BLE001  — a bad file, no space for the model, …
        print("transcription failed: %s" % exc, file=sys.stderr)
        return 1
    if not text:
        print("no speech found", file=sys.stderr)
        return 1
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
