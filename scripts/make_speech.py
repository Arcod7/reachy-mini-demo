#!/usr/bin/env python3
"""Render every line the companion can say to speech/<key>.wav.

The robot has no speech engine, so this runs on the laptop; scripts/install.sh then
copies speech/ to the robot. Re-run it after changing src/companion/lines.py or the voice.

Engines:
  kokoro (default)  Kokoro-82M, natural voice. Needs `pip install kokoro-onnx soundfile`
                    and the model files (--model-dir, default ~/reachy/kokoro):
                    kokoro-v1.0.int8.onnx + voices-v1.0.bin from
                    https://github.com/thewh1teagle/kokoro-onnx/releases (model-files-v1.0)
  espeak            espeak-ng, robotic; no model needed.

  scripts/make_speech.py [--engine kokoro|espeak] [--voice af_heart] [--force]
  scripts/make_speech.py --sample af_heart af_bella bf_emma   # compare voices
"""

import argparse
import json
import pathlib
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from companion import lines  # noqa: E402

SAMPLE_TEXT = "Hi! Great to see you! I'm Reachy, and I'm excited to meet you!"


def kokoro_engine(model_dir: pathlib.Path):
    from kokoro_onnx import Kokoro
    import soundfile as sf

    kokoro = Kokoro(str(model_dir / "kokoro-v1.0.int8.onnx"), str(model_dir / "voices-v1.0.bin"))

    def render(text: str, wav: pathlib.Path, voice: str, rate: int, pitch: int) -> None:
        speed = min(max(rate / lines.BASE_RATE, 0.8), 1.3)  # Kokoro has speed but no pitch
        samples, sr = kokoro.create(text, voice=voice, speed=speed, lang="en-us")
        sf.write(str(wav), samples, sr)

    return render


def espeak_engine():
    if shutil.which("espeak-ng") is None:
        sys.exit("espeak-ng not found (sudo dnf install espeak-ng)")

    def render(text: str, wav: pathlib.Path, voice: str, rate: int, pitch: int) -> None:
        subprocess.run(["espeak-ng", "-v", voice, "-s", str(rate), "-p", str(pitch),
                        "-a", "170", "-w", str(wav), text], check=True)

    return render


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--engine", choices=("kokoro", "espeak"), default="kokoro")
    parser.add_argument("--voice", help="kokoro: af_heart (default), af_bella, bf_emma...; espeak: en-us+f3")
    parser.add_argument("--model-dir", type=pathlib.Path, default=pathlib.Path("~/reachy/kokoro").expanduser())
    parser.add_argument("--force", action="store_true", help="re-render existing files")
    parser.add_argument("--sample", nargs="+", metavar="VOICE",
                        help="render a test sentence per voice into /tmp/voice_<name>.wav and exit")
    args = parser.parse_args()
    voice = args.voice or ("af_heart" if args.engine == "kokoro" else "en-us+f3")
    render = kokoro_engine(args.model_dir) if args.engine == "kokoro" else espeak_engine()

    if args.sample:
        for v in args.sample:
            path = pathlib.Path(f"/tmp/voice_{v}.wav")
            render(SAMPLE_TEXT, path, v, lines.BASE_RATE, 50)
            print(path)
        return

    out = ROOT / "speech"
    out.mkdir(exist_ok=True)
    meta = out / "voice.json"
    current = {"engine": args.engine, "voice": voice}
    if meta.exists() and json.loads(meta.read_text()) != current:
        args.force = True  # voice changed: everything must be re-rendered
    manifest, made = {}, 0
    for text, emotion, enthusiasm in lines.all_utterances():
        key = lines.utterance_key(text, emotion, enthusiasm)
        manifest[key] = text
        wav = out / f"{key}.wav"
        if wav.exists() and not args.force:
            continue
        rate, pitch = lines.voice_params(emotion, enthusiasm)
        render(text, wav, voice, rate, pitch)
        made += 1
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1))
    meta.write_text(json.dumps(current))
    for wav in out.glob("*.wav"):  # drop files no line uses any more
        if wav.stem not in manifest:
            wav.unlink()
    print(f"{len(manifest)} lines ({made} rendered) with {args.engine}/{voice} in {out}")


if __name__ == "__main__":
    main()
