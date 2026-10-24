"""Private, bounded URL-only mixer. Never log capabilities or provider content."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass
import http.client
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import secrets
import socket
import ssl
import subprocess
import tempfile
import threading
import time
from urllib.parse import parse_qsl, urljoin, urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool

MAX_BYTES = 32 * 1024 * 1024
MAX_BODY = 20_000
JOB_SECONDS = 90
DNS_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="mixer-dns")
HOST = re.compile(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\Z")
UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
VOICE_PATH = re.compile(r"/storage/v1/object/sign/marshmallow-private/" + UUID + "/" + UUID + r"\Z", re.I)


class MediaFailure(Exception):
    """Fixed, content-free failure; underlying exceptions are never returned."""


def host_ok(host: str) -> bool:
    return bool(HOST.fullmatch(host)) and len(host) <= 253 and all(
        0 < len(label) <= 63 and not label.startswith("-") and not label.endswith("-")
        for label in host.split(".")
    )


def https_url(value: str):
    if not isinstance(value, str) or not value.isascii() or not value.startswith("https://") or len(value) > 8192 or re.search(r"[\x00-\x20\x7f\\]", value):
        raise MediaFailure()
    try:
        u = urlsplit(value)
    except ValueError:
        raise MediaFailure() from None
    if u.scheme != "https" or not u.hostname or not host_ok(u.hostname) or u.netloc != u.hostname or "#" in value:
        raise MediaFailure()
    path = u.path or "/"
    if re.search(r"%(?![0-9a-f]{2})|%(?:0[0-9a-f]|1[0-9a-f]|7f|25|2f|5c)", path + u.query, re.I):
        raise MediaFailure()
    if any(re.sub("%2e", ".", part, flags=re.I) in (".", "..") for part in path.split("/")):
        raise MediaFailure()
    return u


@dataclass(frozen=True)
class Settings:
    token: str
    voice_origin: str
    music_hosts: frozenset[str]
    redirect_hosts: frozenset[str] = frozenset()

    def validate(self):
        if not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", self.token):
            raise MediaFailure()
        u = https_url(self.voice_origin)
        if self.voice_origin != "https://" + u.netloc or not self.music_hosts or any(
            not host_ok(h) for h in self.music_hosts | self.redirect_hosts
        ):
            raise MediaFailure()
        return self

    @classmethod
    def from_env(cls):
        split = lambda key: frozenset(x.strip() for x in os.environ.get(key, "").split(",") if x.strip())
        return cls(os.environ.get("MIXER_AUTH_TOKEN", ""), os.environ.get("MIXER_VOICE_ORIGIN", ""),
                   split("MIXER_MUSIC_HOSTS"), split("MIXER_MUSIC_REDIRECT_HOSTS")).validate()


def allowed_url(value: str, settings: Settings, voice: bool, redirected: bool = False):
    u = https_url(value)
    if voice:
        try:
            pairs = parse_qsl(u.query, keep_blank_values=True, strict_parsing=True, errors="strict")
        except (ValueError, UnicodeError):
            raise MediaFailure() from None
        if "https://" + u.netloc != settings.voice_origin or not VOICE_PATH.fullmatch(u.path) or len(pairs) != 1 or pairs[0][0] != "token" or not re.fullmatch(r"[A-Za-z0-9_.-]{1,6000}", pairs[0][1]):
            raise MediaFailure()
    elif u.hostname not in (settings.music_hosts | settings.redirect_hosts if redirected else settings.music_hosts):
        raise MediaFailure()
    return u


def remaining(deadline: float) -> float:
    seconds = deadline - time.monotonic()
    if seconds <= 0:
        raise MediaFailure()
    return seconds


def public_ip(host: str, deadline: float) -> str:
    future = DNS_POOL.submit(socket.getaddrinfo, host, 443, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
    try:
        records = future.result(timeout=min(5, remaining(deadline)))
    except (FutureTimeout, OSError):
        future.cancel()
        raise MediaFailure() from None
    addresses = {ipaddress.ip_address(row[4][0]) for row in records}
    if not addresses or len(addresses) > 16 or any(
        not ip.is_global or ip.is_multicast or ip.is_reserved or
        (ip.version == 6 and (ip.ipv4_mapped or ip.sixtofour or ip.teredo))
        for ip in addresses
    ):
        raise MediaFailure()
    return str(sorted(addresses, key=lambda ip: (ip.version, str(ip)))[0])


class PinnedHTTPS(http.client.HTTPSConnection):
    """Connect to the reviewed public IP while verifying TLS against its hostname."""
    def __init__(self, host: str, ip: str, timeout: float):
        super().__init__(host, timeout=timeout, context=ssl.create_default_context())
        self.public_address = ip
        self.transport_socket = None

    def connect(self):
        sock = socket.create_connection((self.public_address, 443), self.timeout)
        try:
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
            self.transport_socket = self.sock
        except BaseException:
            sock.close()
            raise


def download(value: str, path: Path, settings: Settings, voice: bool, deadline: float):
    for hop in range(4):
        u = allowed_url(value, settings, voice, redirected=hop > 0)
        conn = PinnedHTTPS(u.hostname, public_ip(u.hostname, deadline), min(10, remaining(deadline)))
        try:
            conn.request("GET", (u.path or "/") + ("?" + u.query if u.query else ""), headers={"Accept": "audio/*,application/octet-stream", "Accept-Encoding": "identity"})
            response = conn.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                if voice or hop == 3 or not response.getheader("Location"):
                    raise MediaFailure()
                value = urljoin(value, response.getheader("Location"))
                allowed_url(value, settings, False, redirected=True)
                continue
            if response.status != 200 or response.getheader("Content-Encoding", "identity").lower() != "identity":
                raise MediaFailure()
            mime = response.getheader("Content-Type", "").split(";")[0].lower()
            if mime not in ("audio/mpeg", "audio/mp3", "audio/wav", "audio/x-wav", "audio/mp4", "application/octet-stream"):
                raise MediaFailure()
            length = response.getheader("Content-Length")
            if length is not None and (len(length) > 12 or not length.isascii() or not length.isdecimal() or not 0 < int(length) <= MAX_BYTES):
                raise MediaFailure()
            count = 0
            with path.open("xb") as out:
                while True:
                    conn.transport_socket.settimeout(min(10, remaining(deadline)))
                    chunk = response.read1(min(65536, MAX_BYTES - count + 1))
                    if not chunk:
                        break
                    count += len(chunk)
                    if count > MAX_BYTES:
                        raise MediaFailure()
                    out.write(chunk)
            if count == 0 or (length is not None and count != int(length)):
                raise MediaFailure()
            return
        finally:
            conn.close()
    raise MediaFailure()


def duration(path: Path, deadline: float) -> float:
    args = ["ffprobe", "-v", "error", "-protocol_whitelist", "file,pipe", "-format_whitelist", "mp3,wav,mov", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(path)]
    result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True, timeout=min(10, remaining(deadline)))
    value = float(result.stdout.strip())
    if not math.isfinite(value) or not 0 < value <= 600:
        raise MediaFailure()
    return value


def render(payload: dict, settings: Settings, directory: Path) -> Path:
    deadline = time.monotonic() + JOB_SECONDS
    voice, music, output = (directory / name for name in ("voice.audio", "music.audio", "mixed.mp3"))
    download(payload["voice_url"], voice, settings, True, deadline)
    download(payload["music_url"], music, settings, False, deadline)
    voice_duration = duration(voice, deadline)
    duration(music, deadline)
    total = 7.0 + voice_duration + 4.0
    filters = ("[0:a]aresample=44100,adelay=7000|7000,apad=pad_dur=4.0[voice];"
               f"[1:a]aresample=44100,volume=0.10,afade=t=out:st={total - 3.0}:d=3.0[bg];"
               "[voice][bg]amix=inputs=2:duration=first:dropout_transition=0,aresample=44100[aout]")
    args = ["ffmpeg", "-nostdin", "-v", "error", "-protocol_whitelist", "file,pipe", "-format_whitelist", "mp3,wav,mov", "-i", str(voice),
            "-stream_loop", "-1", "-protocol_whitelist", "file,pipe", "-format_whitelist", "mp3,wav,mov", "-i", str(music),
            "-filter_complex", filters, "-map", "[aout]", "-map_metadata", "-1", "-fs", str(MAX_BYTES), "-y", str(output)]
    subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=min(60, remaining(deadline)))
    if not output.is_file() or not 0 < output.stat().st_size < MAX_BYTES or abs(duration(output, deadline) - total) > 0.5:
        raise MediaFailure()
    return output


class PrivateAudioResponse(FileResponse):
    def __init__(self, path: Path, cleanup):
        super().__init__(path, media_type="audio/mpeg", headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
        self.cleanup = cleanup

    async def __call__(self, scope, receive, send):
        # Keep ownership of file I/O until it is closed, rather than delegating
        # a pathname whose consumption might outlive this response.
        scope = {**scope, "extensions": {k: v for k, v in scope.get("extensions", {}).items()
                                          if k != "http.response.pathsend"}}
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.cleanup()


def create_app(settings: Settings | None = None):
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    slots = threading.BoundedSemaphore(2)
    active_work = set()

    def exact_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise MediaFailure()
            result[key] = value
        return result

    @app.get("/health")
    def health():
        return {"status": "ok"}

    def authorized_settings(request: Request):
        try:
            config = (settings or Settings.from_env()).validate()
        except Exception:
            raise HTTPException(503, "MIXER_NOT_CONFIGURED") from None
        auth = request.headers.get("authorization", "")
        if len(auth) > 512 or not auth.isascii() or not secrets.compare_digest(auth, "Bearer " + config.token):
            raise HTTPException(401, "MIXER_UNAUTHORIZED")
        return config

    @app.get("/ready")
    def ready(request: Request):
        authorized_settings(request)
        return {"ready": True}

    @app.post("/mix")
    async def mix_audio(request: Request):
        config = authorized_settings(request)
        if request.url.query or request.headers.get("content-type", "").split(";")[0].lower() != "application/json":
            raise HTTPException(400, "MIXER_INVALID_REQUEST")
        body = bytearray()
        stream = request.stream().__aiter__()
        body_deadline = time.monotonic() + 10
        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(anext(stream), timeout=min(5, remaining(body_deadline)))
                except StopAsyncIteration:
                    break
                if len(body) + len(chunk) > MAX_BODY:
                    raise HTTPException(413, "MIXER_REQUEST_TOO_LARGE")
                body.extend(chunk)
            payload = json.loads(body.decode("utf8"), object_pairs_hook=exact_object)
            if not isinstance(payload, dict) or set(payload) != {"voice_url", "music_url"}:
                raise MediaFailure()
            allowed_url(payload["voice_url"], config, True)
            allowed_url(payload["music_url"], config, False)
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(400, "MIXER_INVALID_REQUEST") from None
        if not slots.acquire(blocking=False):
            raise HTTPException(429, "MIXER_BUSY")
        directory = None
        cleaned = False
        cleanup_lock = threading.Lock()

        def cleanup():
            nonlocal cleaned
            with cleanup_lock:
                if cleaned:
                    return
                cleaned = True
            try:
                if directory:
                    directory.cleanup()
            finally:
                slots.release()

        work = None
        try:
            directory = tempfile.TemporaryDirectory(prefix="marshmallow-mix-")
            work = asyncio.create_task(run_in_threadpool(render, payload, config, Path(directory.name)))
            active_work.add(work)
            work.add_done_callback(active_work.discard)
            path = await asyncio.shield(work)
        except BaseException as error:
            # A disconnected request must not delete files under a still-running
            # thread. Hold the slot until that thread exits, then erase its files.
            if work is not None and not work.done():
                def finished(task):
                    try:
                        if not task.cancelled():
                            task.exception()
                    finally:
                        cleanup()
                work.add_done_callback(finished)
            else:
                cleanup()
            if not isinstance(error, Exception):
                raise
            raise HTTPException(502, "MIXER_MEDIA_UNAVAILABLE") from None
        try:
            return PrivateAudioResponse(path, cleanup)
        except Exception:
            cleanup()
            raise HTTPException(502, "MIXER_MEDIA_UNAVAILABLE") from None

    return app


app = create_app()
