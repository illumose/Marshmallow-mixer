import asyncio
import io
import json
import math
from pathlib import Path
import re
import socket
import ssl
import subprocess
import struct
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
import wave

import main as m

CONFIG = m.Settings("synthetic_only_" + "x" * 32, "https://storage.example.invalid",
                    frozenset({"music.example.invalid"}), frozenset({"cdn.example.invalid"}))
VOICE = CONFIG.voice_origin + "/storage/v1/object/sign/marshmallow-private/11111111-1111-4111-8111-111111111111/22222222-2222-4222-8222-222222222222?token=synthetic.jwt-token"
MUSIC = "https://music.example.invalid/licensed.mp3?dl=1"
PAYLOAD = {"voice_url": VOICE, "music_url": MUSIC}


async def request(app, payload=PAYLOAD, *, token=CONFIG.token, body=None, content_type="application/json", query=b"", send_hook=None, chunks=None, method="POST", path="/mix"):
    raw = json.dumps(payload).encode() if body is None else body
    headers = [(b"content-type", content_type.encode())]
    if token is not None:
        headers.append((b"authorization", ("Bearer " + token).encode()))
    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"}, "http_version": "1.1", "method": method, "scheme": "https", "path": path, "raw_path": path.encode(), "query_string": query, "headers": headers, "server": ("mixer.example.invalid", 443), "client": ("127.0.0.1", 1), "extensions": {"http.response.pathsend": {}}}
    reads = []
    messages = []
    parts = list(chunks) if chunks is not None else [raw]

    async def receive():
        reads.append(True)
        if parts:
            return {"type": "http.request", "body": parts.pop(0), "more_body": bool(parts)}
        return {"type": "http.disconnect"}

    async def send(message):
        if send_hook:
            await send_hook(message)
        messages.append(message)

    await app(scope, receive, send)
    start = next(x for x in messages if x["type"] == "http.response.start")
    data = b"".join(x.get("body", b"") for x in messages)
    return start["status"], dict(start.get("headers", [])), data, reads


class FakeResponse:
    def __init__(self, data=b"synthetic audio", status=200, headers=None):
        self.status = status
        self.headers = {"Content-Type": "audio/mpeg", **(headers or {})}
        self.data = io.BytesIO(data)

    def getheader(self, key, default=None):
        return self.headers.get(key, default)

    def read1(self, size):
        return self.data.read(size)


class URLAndDownloadTests(unittest.TestCase):
    def test_configuration_and_private_capability_scope(self):
        CONFIG.validate()
        self.assertEqual(m.allowed_url(VOICE, CONFIG, True).hostname, "storage.example.invalid")
        for value in [VOICE.replace("storage.example.invalid", "foreign.invalid"), VOICE + "&other=1", VOICE + "&%74oken=second", VOICE.replace("?token=", "?x="), VOICE.split("?")[0], VOICE.replace("marshmallow-private", "public"), VOICE.replace("22222222-2222-4222-8222-222222222222", "not-a-key")]:
            with self.subTest(value=value), self.assertRaises(m.MediaFailure):
                m.allowed_url(value, CONFIG, True)
        for config in [m.Settings("short", CONFIG.voice_origin, CONFIG.music_hosts), m.Settings(CONFIG.token, CONFIG.voice_origin + "/", CONFIG.music_hosts), m.Settings(CONFIG.token, CONFIG.voice_origin, frozenset()), m.Settings(CONFIG.token, CONFIG.voice_origin, frozenset({"*.example.invalid"}))]:
            with self.assertRaises(m.MediaFailure):
                config.validate()

    def test_ambiguous_or_unsafe_urls_rejected(self):
        values = ["http://music.example.invalid/a", "https://user@music.example.invalid/a", "https://music.example.invalid:443/a", "https://music.example.invalid./a", "https://music.example.invalid/a#x", "https://music.example.invalid/../a", "https://music.example.invalid/%2e%2e/a", "https://music.example.invalid/%252e/a", "https://music.example.invalid/%2f/a", "https://music.example.invalid/a?x=%0a", "https://music.example.invalid/a?x=%zz", "https://müsic.example.invalid/a", "https://music.example.invalid\\@evil.invalid/a", "https://music.example.invalid/" + "x" * 8192]
        values += [MUSIC + chr(n) for n in range(33)] + [MUSIC + "\U0001f600", "HTTPS://music.example.invalid/a"]
        for value in values:
            with self.subTest(value=value), self.assertRaises(m.MediaFailure):
                m.https_url(value)
        with self.assertRaises(m.MediaFailure):
            m.allowed_url("https://cdn.example.invalid/a", CONFIG, False)
        m.allowed_url("https://cdn.example.invalid/a", CONFIG, False, redirected=True)

    def test_dns_rejects_private_mixed_multicast_and_tunnel_addresses(self):
        for ips in [["127.0.0.1"], ["169.254.169.254"], ["10.0.0.1"], ["93.184.216.34", "192.168.1.1"], ["::1"], ["224.0.0.1"], ["2002:7f00:1::"], ["::ffff:127.0.0.1"]]:
            rows = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443)) for ip in ips]
            with patch.object(m.socket, "getaddrinfo", return_value=rows), self.assertRaises(m.MediaFailure):
                m.public_ip("music.example.invalid", time.monotonic() + 5)
        with patch.object(m.socket, "getaddrinfo", return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]):
            self.assertEqual(m.public_ip("music.example.invalid", time.monotonic() + 5), "93.184.216.34")

    def test_tls_connects_to_pinned_ip_and_verifies_original_hostname(self):
        sock = Mock()
        context = ssl.create_default_context()
        conn = m.PinnedHTTPS("music.example.invalid", "93.184.216.34", 3)
        self.assertTrue(conn._context.check_hostname)
        self.assertEqual(conn._context.verify_mode, ssl.CERT_REQUIRED)
        conn._context = Mock(wrap_socket=Mock(return_value=sock))
        with patch.object(m.socket, "create_connection", return_value=sock) as connect:
            conn.connect()
        connect.assert_called_once_with(("93.184.216.34", 443), 3)
        conn._context.wrap_socket.assert_called_once_with(sock, server_hostname="music.example.invalid")
        conn.close()

    def test_dns_and_job_deadlines_fail_closed(self):
        future = Mock()
        future.result.side_effect = m.FutureTimeout()
        with patch.object(m.DNS_POOL, "submit", return_value=future), self.assertRaises(m.MediaFailure):
            m.public_ip("music.example.invalid", time.monotonic() + 5)
        future.cancel.assert_called_once()
        with self.assertRaises(m.MediaFailure):
            m.remaining(time.monotonic() - 1)

    def test_subprocess_has_finite_timeout_and_local_only_protocols(self):
        with patch.object(m.subprocess, "run", return_value=Mock(stdout=b"1.25")) as run:
            self.assertEqual(m.duration(Path("synthetic.wav"), time.monotonic() + 5), 1.25)
            args = run.call_args.args[0]
            self.assertEqual(args[args.index("-protocol_whitelist") + 1], "file,pipe")
            self.assertEqual(args[args.index("-format_whitelist") + 1], "mp3,wav,mov")
            self.assertLessEqual(run.call_args.kwargs["timeout"], 5)
        for value in [b"nan", b"inf", b"0", b"601"]:
            with patch.object(m.subprocess, "run", return_value=Mock(stdout=value)), self.assertRaises(m.MediaFailure):
                m.duration(Path("synthetic.wav"), time.monotonic() + 5)

    def download(self, responses, *, voice=False, value=MUSIC, limit=32):
        made = []
        queue = list(responses)

        def connection(host, ip, timeout):
            c = Mock(transport_socket=Mock())
            c.getresponse.return_value = queue.pop(0)
            made.append((host, ip, timeout, c))
            return c

        with tempfile.TemporaryDirectory() as tmp, patch.object(m, "MAX_BYTES", limit), patch.object(m, "public_ip", return_value="93.184.216.34"), patch.object(m, "PinnedHTTPS", side_effect=connection):
            path = Path(tmp) / "audio"
            try:
                m.download(value, path, CONFIG, voice, time.monotonic() + 5)
                return path.read_bytes(), made
            finally:
                for *_, conn in made:
                    self.assertEqual(conn.close.call_count, 1)

    def test_download_is_bounded_and_uses_pinned_tls_with_no_ambient_proxy(self):
        body, made = self.download([FakeResponse(headers={"Content-Length": "15"})])
        self.assertEqual(body, b"synthetic audio")
        self.assertEqual(made[0][1], "93.184.216.34")
        self.assertGreater(made[0][2], 0)
        self.assertEqual(made[0][3].request.call_args.kwargs["headers"]["Accept-Encoding"], "identity")
        self.assertNotIn("Authorization", made[0][3].request.call_args.kwargs["headers"])

    def test_invalid_status_encoding_type_size_empty_and_length_mismatch(self):
        responses = [FakeResponse(status=404), FakeResponse(headers={"Content-Encoding": "gzip"}), FakeResponse(headers={"Content-Type": "text/html"}), FakeResponse(headers={"Content-Length": "33"}), FakeResponse(headers={"Content-Length": "-1"}), FakeResponse(headers={"Content-Length": "1"}), FakeResponse(data=b""), FakeResponse(data=b"x" * 33)]
        for response in responses:
            with self.subTest(response=response), self.assertRaises(m.MediaFailure):
                self.download([response])

    def test_voice_redirects_denied_music_only_explicit_hosts_and_bounded_hops(self):
        with self.assertRaises(m.MediaFailure):
            self.download([FakeResponse(status=302, headers={"Location": MUSIC})], voice=True, value=VOICE)
        with self.assertRaises(m.MediaFailure):
            self.download([FakeResponse(status=302, headers={"Location": "https://foreign.invalid/audio"})])
        data, made = self.download([FakeResponse(status=302, headers={"Location": "https://cdn.example.invalid/audio"}), FakeResponse()])
        self.assertEqual(data, b"synthetic audio")
        self.assertEqual([x[0] for x in made], ["music.example.invalid", "cdn.example.invalid"])
        with self.assertRaises(m.MediaFailure):
            self.download([FakeResponse(status=302, headers={"Location": MUSIC}) for _ in range(4)])


class AudioPlanTests(unittest.TestCase):
    def test_loop_buffer_is_bounded_for_long_catalog_tracks(self):
        plan = m.loop_parameters(600.0, 611.0)
        self.assertEqual(plan, (120.0, 1.0, 5_247_900))
        self.assertLess(plan[2] * 2 * 4, 43_000_000)  # stereo float samples, bytes
        self.assertIsNone(m.loop_parameters(120.0, 100.0))

    def test_music_must_be_long_enough_to_make_a_smooth_loop(self):
        for value in [0.0, 0.25, 0.999, float("nan"), float("inf"), 601.0]:
            with self.subTest(value=value), self.assertRaises(m.MediaFailure):
                m.loop_parameters(value, 20.0)

    def test_filter_true_peak_is_parsed_and_guarded_before_encoding(self):
        valid = b'{"input_i":"-18.0","input_tp":"-3.0","input_lra":"2.0","input_thresh":"-28.0","target_offset":"0.0","output_tp":"-2.0"}'
        self.assertEqual(m.measured_filter_true_peak(valid), -2.0)
        hot = valid.replace(b'"output_tp":"-2.0"', b'"output_tp":"-1.0"')
        missing = valid.replace(b'"input_tp":"-3.0",', b'')
        malformed = valid.replace(b'"output_tp":"-2.0"', b'"output_tp":"nan"')
        for log in [b"no metrics", missing, hot, malformed]:
            with self.subTest(log=log), self.assertRaises(m.MediaFailure):
                m.measured_filter_true_peak(log)

    def test_encoded_mp3_true_peak_guard_rejects_hot_audio(self):
        rate = 44100
        frequency = 997.0
        samples = [round(32767 * math.sin(2 * math.pi * frequency * i / rate)) for i in range(rate * 2)]
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "hot.wav"
            encoded = Path(tmp) / "hot.mp3"
            with wave.open(str(source), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(rate)
                wav.writeframes(struct.pack("<" + "h" * len(samples), *samples))
            subprocess.run(
                ["ffmpeg", "-nostdin", "-hide_banner", "-v", "error", "-i", str(source), "-af", "volume=3dB",
                 "-c:a", "libmp3lame", "-b:a", "192k", "-ar", str(rate), "-ac", "2", "-y", str(encoded)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=10,
            )
            with self.assertRaises(m.MediaFailure):
                m.measured_encoded_true_peak(encoded, time.monotonic() + 15)

    def test_120_second_synthetic_voice_mix_stays_inside_job_budget(self):
        def local_download(value, path, settings, voice, deadline):
            rate = 16000
            if voice:
                samples = [round(5000 * math.sin(2 * math.pi * 880.0 * i / rate)) for i in range(rate)]
                frames = struct.pack("<" + "h" * len(samples), *samples) * 120
            else:
                seconds = 2.37
                samples = [round(12000 * math.sin(2 * math.pi * 440.0 * i / rate)) for i in range(round(seconds * rate))]
                frames = struct.pack("<" + "h" * len(samples), *samples)
            with wave.open(str(path), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(rate)
                wav.writeframes(frames)

        with tempfile.TemporaryDirectory() as tmp, patch.object(m, "download", side_effect=local_download):
            started = time.monotonic()
            output = m.render(PAYLOAD, CONFIG, Path(tmp))
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, m.JOB_SECONDS)
            self.assertLess(output.stat().st_size, m.MAX_BYTES)
            self.assertAlmostEqual(m.duration(output, time.monotonic() + 5), 131.0, delta=0.5)


class APITests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.app = m.create_app(CONFIG)
        self.directories = []

    def render(self, payload, settings, directory):
        self.directories.append(directory)
        for name in ["voice.audio", "music.audio", "mixed.mp3"]:
            (directory / name).write_bytes(b"synthetic audio")
        return directory / "mixed.mp3"

    async def test_authenticated_readiness_checks_configuration_without_media_calls_or_body_reads(self):
        with patch.object(m, "render") as render, patch.object(m, "download") as download:
            status, _, body, reads = await request(self.app, method="GET", path="/ready")
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body), {"ready": True})
            self.assertFalse(reads)
            self.assertEqual((await request(self.app, method="GET", path="/ready", token=None))[0], 401)
            with patch.dict(m.os.environ, {}, clear=True):
                self.assertEqual((await request(m.create_app(), method="GET", path="/ready"))[0], 503)
            render.assert_not_called()
            download.assert_not_called()

    async def test_auth_fails_before_body_or_render_and_missing_configuration_fails_closed(self):
        with patch.object(m, "render", side_effect=self.render) as render:
            for token in [None, "wrong"]:
                status, _, body, reads = await request(self.app, token=token)
                self.assertEqual(status, 401)
                self.assertFalse(reads)
                self.assertNotIn(b"synthetic.jwt", body)
            with patch.dict(m.os.environ, {}, clear=True):
                self.assertEqual((await request(m.create_app()))[0], 503)
            render.assert_not_called()

    async def test_exact_json_body_bounds_and_scope_enforced_before_render(self):
        with patch.object(m, "render", side_effect=self.render) as render:
            for payload in [{}, {**PAYLOAD, "extra": "x"}, {**PAYLOAD, "voice_url": MUSIC}, {**PAYLOAD, "music_url": "https://127.0.0.1/a"}]:
                self.assertEqual((await request(self.app, payload))[0], 400)
            self.assertEqual((await request(self.app, body=b'{"voice_url":"x","voice_url":"y","music_url":"z"}'))[0], 400)
            self.assertEqual((await request(self.app, query=b"secret=x"))[0], 400)
            self.assertEqual((await request(self.app, content_type="application/x-www-form-urlencoded"))[0], 400)
            self.assertEqual((await request(self.app, chunks=[b"x" * 15_000, b"x" * 15_000]))[0], 413)
            render.assert_not_called()

    async def test_success_streams_private_audio_and_removes_all_files(self):
        with patch.object(m, "render", side_effect=self.render):
            status, headers, body, _ = await request(self.app)
        self.assertEqual(status, 200)
        self.assertEqual(headers[b"cache-control"], b"no-store")
        self.assertEqual(headers[b"content-type"], b"audio/mpeg")
        self.assertEqual(body, b"synthetic audio")
        self.assertTrue(self.directories)
        self.assertTrue(all(not p.exists() for p in self.directories))

    async def test_render_failure_is_fixed_and_cleanup_releases_capacity(self):
        def fail(payload, settings, directory):
            self.render(payload, settings, directory)
            raise RuntimeError(VOICE + " SYNTHETIC_PRIVATE_CONTENT")
        with patch.object(m, "render", side_effect=fail):
            for _ in range(3):
                status, _, body, _ = await request(self.app)
                self.assertEqual(status, 502)
                self.assertEqual(json.loads(body), {"detail": "MIXER_MEDIA_UNAVAILABLE"})
        self.assertTrue(all(not p.exists() for p in self.directories))
        with patch.object(m, "render", side_effect=self.render):
            self.assertEqual((await request(self.app))[0], 200)

    async def test_response_disconnect_cleans_files_and_releases_capacity(self):
        async def fail_send(message):
            if message["type"] == "http.response.body":
                raise RuntimeError("SYNTHETIC_DISCONNECT")
        with patch.object(m, "render", side_effect=self.render):
            with self.assertRaisesRegex(RuntimeError, "SYNTHETIC_DISCONNECT"):
                await request(self.app, send_hook=fail_send)
            self.assertTrue(all(not p.exists() for p in self.directories))
            self.assertEqual((await request(self.app))[0], 200)

    async def test_concurrency_slots_held_until_response_finishes(self):
        gate = asyncio.Event()
        started = asyncio.Event()

        async def block_send(message):
            if message["type"] == "http.response.start":
                started.set()
                await gate.wait()

        with patch.object(m, "render", side_effect=self.render):
            task = asyncio.create_task(request(self.app, send_hook=block_send))
            try:
                await asyncio.wait_for(started.wait(), 3)
                self.assertEqual((await request(self.app))[0], 429)
            finally:
                gate.set()
                await task
            self.assertEqual((await request(self.app))[0], 200)
        self.assertTrue(all(not p.exists() for p in self.directories))

    async def test_cancelled_request_keeps_worker_files_until_thread_exits_then_cleans(self):
        loop = asyncio.get_running_loop()
        started = asyncio.Event()
        release = threading.Event()

        def blocked_render(payload, settings, directory):
            path = self.render(payload, settings, directory)
            loop.call_soon_threadsafe(started.set)
            if not release.wait(3):
                raise RuntimeError("SYNTHETIC_TIMEOUT")
            path.write_bytes(b"finished")
            return path

        with patch.object(m, "render", side_effect=blocked_render):
            task = asyncio.create_task(request(self.app))
            await asyncio.wait_for(started.wait(), 3)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(self.directories[0].exists())
            release.set()
            deadline = time.monotonic() + 3
            while self.directories[0].exists() and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            self.assertFalse(self.directories[0].exists())

    async def test_real_ffmpeg_synthetic_mix_ducks_speech_normalizes_and_measures_peak(self):
        def local_download(value, path, settings, voice, deadline):
            self.directories.append(path.parent)
            source_duration = 4.0 if voice else 2.37
            rate = 16000
            samples = []
            for index in range(round(source_duration * rate)):
                t = index / rate
                if voice:
                    amplitude = 0.60 if 0.4 <= t < 2.4 else 0.0
                    frequency = 880.0
                else:
                    amplitude = 0.60
                    frequency = 440.0
                samples.append(round(32767 * amplitude * math.sin(2 * math.pi * frequency * t)))
            with wave.open(str(path), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(rate)
                wav.writeframes(struct.pack("<" + "h" * len(samples), *samples))

        with patch.object(m, "download", side_effect=local_download):
            status, headers, body, _ = await request(self.app)
        self.assertEqual(status, 200)
        self.assertEqual(headers[b"content-type"], b"audio/mpeg")
        self.assertGreater(len(body), 1000)
        self.assertTrue(all(not p.exists() for p in self.directories))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "result.mp3"
            path.write_bytes(body)
            self.assertAlmostEqual(m.duration(path, time.monotonic() + 5), 15.0, delta=0.5)
            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-protocol_whitelist", "file,pipe", "-format_whitelist", "mp3",
                 "-select_streams", "a:0", "-show_entries", "stream=codec_name,sample_rate,channels,bit_rate",
                 "-of", "json", str(path)], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True, timeout=5,
            )
            stream = json.loads(probe.stdout)["streams"][0]
            self.assertEqual(stream["codec_name"], "mp3")
            self.assertEqual(stream["sample_rate"], "44100")
            self.assertEqual(stream["channels"], 2)
            self.assertEqual(stream["bit_rate"], "192000")

            measured = subprocess.run(
                ["ffmpeg", "-nostdin", "-hide_banner", "-v", "info", "-protocol_whitelist", "file,pipe",
                 "-format_whitelist", "mp3", "-i", str(path), "-filter_complex", "ebur128=peak=true",
                 "-f", "null", "-"], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=True, timeout=10,
            )
            true_peaks = re.findall(rb"True peak:\s*\n\s*Peak:\s*([-+0-9.]+)\s*dBFS", measured.stderr)
            self.assertTrue(true_peaks, measured.stderr.decode("utf-8", errors="replace"))
            self.assertLessEqual(float(true_peaks[-1]), -1.0)
            integrated = re.findall(rb"(?m)^\s*I:\s*([-+0-9.]+)\s*LUFS$", measured.stderr)
            self.assertTrue(integrated, measured.stderr.decode("utf-8", errors="replace"))
            self.assertLessEqual(abs(float(integrated[-1]) - (-16.0)), 1.5)

            decoded = subprocess.run(
                ["ffmpeg", "-nostdin", "-v", "error", "-protocol_whitelist", "file,pipe", "-format_whitelist", "mp3",
                 "-i", str(path), "-map", "0:a:0", "-ac", "1", "-ar", "8000", "-f", "f32le", "pipe:1"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True, timeout=10,
            )
            samples = struct.unpack("<" + "f" * (len(decoded.stdout) // 4), decoded.stdout)

            loop_samples = m.loop_parameters(2.37, 15.0)[2]
            loop_period = loop_samples / m.SAMPLE_RATE

            def seam_jump_ratio(boundary_seconds):
                center = round(boundary_seconds * 8000)
                local = [abs(samples[index] - samples[index - 1])
                         for index in range(center - 120, center + 121)
                         if abs(index - center) > 10]
                seam = [abs(samples[index] - samples[index - 1])
                        for index in range(center - 2, center + 3)]
                return max(seam), max(local)

            for boundary in [2.37, *(loop_period * repeat for repeat in range(1, 5))]:
                with self.subTest(loop_boundary=boundary):
                    seam, local_peak = seam_jump_ratio(boundary)
                    self.assertLessEqual(seam, local_peak * 2.0 + 0.025)

            def music_tone_amplitude(start_seconds):
                sample_rate = 8000
                first = round(start_seconds * sample_rate)
                count = round(0.5 * sample_rate)
                sine = cosine = 0.0
                for offset, sample in enumerate(samples[first:first + count]):
                    phase = 2 * math.pi * 440.0 * offset / sample_rate
                    sine += sample * math.sin(phase)
                    cosine += sample * math.cos(phase)
                return 2 * math.hypot(sine, cosine) / count

            intro_level = music_tone_amplitude(6.0)
            speech_level = music_tone_amplitude(7.8)
            recovery_level = music_tone_amplitude(11.3)
            self.assertGreater(intro_level, 0.001)
            duck_db = 20 * math.log10(speech_level / intro_level)
            self.assertGreaterEqual(duck_db, -14.0)
            self.assertLessEqual(duck_db, -4.0)
            self.assertGreaterEqual(20 * math.log10(recovery_level / speech_level), 3.0)


if __name__ == "__main__":
    unittest.main()
