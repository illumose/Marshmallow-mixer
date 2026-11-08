"""Local FFmpeg regressions; load only stdlib media helpers from main.py."""
import ast
import math
from pathlib import Path
import struct
import subprocess
import tempfile
import time
import unittest
import wave


SOURCE = Path(__file__).resolve().parents[1] / "main.py"
tree = ast.parse(SOURCE.read_text())
import copy


class StripAnnotations(ast.NodeTransformer):
    def visit_FunctionDef(self, node):
        node = self.generic_visit(node)
        node.returns = None
        for arg in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs):
            arg.annotation = None
        return node


wanted = {"MediaFailure", "remaining", "duration", "music_edges", "music_copies", "music_filter", "render"}
nodes = [node for node in tree.body if isinstance(node, (ast.ClassDef, ast.FunctionDef)) and node.name in wanted]
NS = {"Path": Path, "math": math, "re": __import__("re"), "subprocess": subprocess, "time": time}
nodes = StripAnnotations().visit(ast.Module(body=copy.deepcopy(nodes), type_ignores=[])).body
exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SOURCE), "exec"), NS)
MediaFailure = NS["MediaFailure"]
music_edges = NS["music_edges"]
music_copies = NS["music_copies"]
music_filter = NS["music_filter"]


def write_tone(path, *, lead=0.0, tone=0.4, tail=0.0, gap=0.0, frequency=440):
    rate = 44100
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        frames = bytearray()
        frames.extend(b"\0\0" * round(lead * rate))
        if gap:
            for i in range(round(tone * rate)):
                frames.extend(struct.pack("<h", int(12000 * math.sin(2 * math.pi * frequency * i / rate))))
            frames.extend(b"\0\0" * round(gap * rate))
            for i in range(round(tone * rate)):
                frames.extend(struct.pack("<h", int(12000 * math.sin(2 * math.pi * frequency * i / rate))))
        else:
            for i in range(round(tone * rate)):
                frames.extend(struct.pack("<h", int(12000 * math.sin(2 * math.pi * frequency * i / rate))))
        frames.extend(b"\0\0" * round(tail * rate))
        out.writeframes(frames)


def rms_windows(path, window=0.1):
    import contextlib
    with contextlib.closing(wave.open(str(path), "rb")) as audio:
        rate = audio.getframerate()
        frames = audio.readframes(audio.getnframes())
    samples = struct.unpack("<" + "h" * (len(frames) // 2), frames)
    stride = round(rate * window)
    return [math.sqrt(sum(x * x for x in samples[i:i + stride]) / max(1, len(samples[i:i + stride]))) / 32768
            for i in range(0, len(samples) - stride + 1, stride)]


class MusicContinuityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_padded_source_exposes_old_loop_silence_and_candidate_is_continuous(self):
        source = self.root / "padded.wav"
        old_result = self.root / "old.wav"
        candidate = self.root / "candidate.wav"
        write_tone(source, lead=5.0, tone=2.0, tail=0.3)
        total = 8.0

        # Reproduce the former hard loop. The source's leading silence is repeated too.
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-stream_loop", "-1", "-i", str(source),
                        "-t", str(total), "-c:a", "pcm_s16le", "-y", str(old_result)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=20)
        old_rms = rms_windows(old_result)
        self.assertLess(max(old_rms[:5]), 0.005, "baseline must reproduce the leading silence regression")

        start, end, _ = music_edges(source, time.monotonic() + 15)
        graph = music_filter(start, end, total)
        inputs = ["-i", str(source)] * (music_copies(start, end, total) + 1)
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", *inputs, "-filter_complex", graph,
                        "-map", "[music]", "-c:a", "pcm_s16le", "-y", str(candidate)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=20)
        candidate_rms = rms_windows(candidate)
        self.assertAlmostEqual(len(candidate_rms) * 0.1, total, delta=0.1)
        self.assertGreater(candidate_rms[0], 0.08, "trimmed candidate should begin on music")
        self.assertGreater(min(candidate_rms[:-30]), 0.08, "no 100 ms gaps through repeated bed before final fade")
        self.assertIn("acrossfade", graph)
        self.assertNotIn("stream_loop", graph)

    def test_all_silent_and_internal_silence_fail_closed(self):
        silent = self.root / "silent.wav"
        gap = self.root / "gap.wav"
        write_tone(silent, lead=1.0, tone=0.0)
        write_tone(gap, tone=0.4, gap=0.25)
        for path in (silent, gap):
            with self.subTest(path=path.name):
                with self.assertRaises(MediaFailure) as failure:
                    music_edges(path, time.monotonic() + 15)
                expected_stage = "music_interior_silence" if path == gap else None
                self.assertEqual(failure.exception.stage, expected_stage)

    def test_repeat_count_is_bounded_and_single_long_clip_needs_no_crossfade(self):
        self.assertIn("acrossfade", music_filter(0.0, 2.0, 5.0))
        self.assertEqual(music_copies(0.0, 3.0, 13.5), 8)
        with self.assertRaises(MediaFailure):
            music_filter(0.0, 3.0, 13.6)
        long_graph = music_filter(0.0, 20.0, 5.0)
        self.assertNotIn("acrossfade", long_graph)
        with self.assertRaises(MediaFailure):
            music_filter(0.0, 0.01, 100.0)

    def test_single_long_clip_and_voice_delay_are_preserved(self):
        voice = self.root / "voice.wav"
        music = self.root / "long_music.wav"
        result = self.root / "mixed.wav"
        write_tone(voice, tone=2.0, frequency=1000)
        write_tone(music, tone=12.0)
        total = 13.0
        start, end, _ = music_edges(music, time.monotonic() + 15)
        bed = music_filter(start, end, total)
        filters = ("[0:a]aresample=44100,adelay=7000|7000,apad=pad_dur=4[voice];" +
                   bed + ";[music]volume=0.10,afade=t=out:st=10:d=3[bg];" +
                   "[voice][bg]amix=inputs=2:duration=first:dropout_transition=0,aresample=44100[aout]")
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(voice), *(["-i", str(music)] * music_copies(start, end, total)),
                        "-filter_complex", filters, "-map", "[aout]", "-c:a", "pcm_s16le", "-y", str(result)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=20)
        self.assertAlmostEqual(len(rms_windows(result)) * 0.1, total, delta=0.1)
        # The independent 1 kHz voice tone is absent before the original 7 second delay.
        filtered = self.root / "voice_band.wav"
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(result), "-af",
                        "bandpass=f=1000:width_type=h:w=100", "-c:a", "pcm_s16le", "-y", str(filtered)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=20)
        band = rms_windows(filtered)
        self.assertLess(max(band[:68]), 0.002)
        self.assertGreater(min(band[71:85]), 0.05)

    def test_production_render_helper_keeps_bed_and_voice_timing(self):
        voice = self.root / "source_voice.wav"
        music = self.root / "source_music.wav"
        write_tone(voice, tone=2.0, frequency=1000)
        write_tone(music, tone=3.0)

        def local_download(value, path, settings, is_voice, deadline):
            import shutil
            shutil.copyfile(voice if is_voice else music, path)

        namespace = dict(NS)
        namespace.update(download=local_download, MAX_BYTES=32 * 1024 * 1024, JOB_SECONDS=90)
        namespace["render"].__globals__.update(namespace)
        output = namespace["render"]({"voice_url": "local", "music_url": "local"}, None, self.root)
        self.assertAlmostEqual(NS["duration"](output, time.monotonic() + 5), 13.0, delta=0.5)
        decoded = self.root / "rendered.wav"
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(output), "-c:a", "pcm_s16le", "-y", str(decoded)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=20)
        tone_band = self.root / "rendered_voice_band.wav"
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(decoded), "-af",
                        "bandpass=f=1000:width_type=h:w=100", "-c:a", "pcm_s16le", "-y", str(tone_band)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=20)
        band = rms_windows(tone_band)
        self.assertLess(max(band[:68]), 0.003)
        self.assertGreater(min(band[71:85]), 0.03)
        music_band = self.root / "rendered_music_band.wav"
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(decoded), "-af",
                        "bandpass=f=440:width_type=h:w=80", "-c:a", "pcm_s16le", "-y", str(music_band)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=20)
        music_rms = rms_windows(music_band)
        self.assertGreater(music_rms[0], 0.005)
        self.assertGreater(min(music_rms[:-30]), 0.005, "production render should retain music without 100 ms gaps")


if __name__ == "__main__":
    unittest.main()
