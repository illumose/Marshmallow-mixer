# Marshmallow private mixer

Review-only hardening of the existing URL-based mixer. Nothing in this branch deploys it or changes the existing running service.

`POST /mix` accepts exactly a JSON object with `voice_url` and `music_url`. The result is streamed `audio/mpeg` with `Cache-Control: no-store`. It preserves the original 7-second voice delay, 10% background level, 4-second tail and 3-second fade. Empty/oversized/malformed audio fails closed. A job has a 90-second processing budget, 32 MiB per input/output, a 600-second maximum source duration and two concurrent slots (held through response completion). No automatic retry.

## Required configuration

- `MIXER_AUTH_TOKEN`: a dedicated random URL-safe token of 32–256 characters. Send it in `Authorization: Bearer …` through a dedicated server-side/n8n credential. Never reuse the BFF callback key, storage admin key, or a mobile credential.
- `MIXER_VOICE_ORIGIN`: the exact HTTPS origin of the project's private storage, without a trailing slash.
- `MIXER_MUSIC_HOSTS`: comma-separated exact reviewed catalog hostnames. No wildcard, scheme or port.
- `MIXER_MUSIC_REDIRECT_HOSTS`: optional comma-separated exact reviewed redirect hostnames. Redirects to unreviewed hosts are rejected; voice redirects are always rejected. Inspect the actual licensed catalog's redirect chain before configuring these. An observed hostname is not proof of licensing.

`GET /health` is a liveness check only. Authenticated `GET /ready` checks the configuration/token binding without downloading media or running FFmpeg; verify it through the dedicated credential before any paid generation. It does not certify upstream media access. An unconfigured `/mix` returns fixed 503; bad authentication returns fixed 401 before reading the body. New authentication is intentionally incompatible with an unauthenticated legacy caller. Use a separately reviewed prototype target and update only the isolated native draft's endpoint/credential before activation; do not silently replace the working legacy service. Keep the original workflows and service as rollback/reference.

The mixer verifies public DNS and connects to that pinned IP while checking TLS for the original hostname. Music redirects are revalidated at every hop. Inputs are streamed with byte/deadline limits; FFmpeg/FFprobe accept only local file/pipe protocols and mp3/wav/mov demuxers. All request files are erased after a successful response, send failure or processing failure. Cancellation while a worker is running defers deletion until the worker actually exits, keeping its concurrency slot occupied. Abrupt process/host loss is not an application cleanup guarantee: the target must use ephemeral temporary storage with no volume/backups, and its disk/logging/retention behavior needs review before private content.

## Local checks

Use Python 3.11+ and installed `ffmpeg`/`ffprobe`. Install the hash-locked requirements in an isolated virtual environment, then run `python -m unittest discover -s tests -v`. CI builds the actual Python 3.11 image and runs the suite as its non-root user with networking off, a read-only root and bounded ephemeral `/tmp`. The suite makes no external media/provider calls. It includes a real local synthetic WAV → mixed MP3 round trip, timing inspection, URL/DNS/TLS/redirect limits, authentication, body/byte limits, concurrency and cleanup on success/failure/disconnect/cancellation. Dependency pins do not replace a current dependency advisory review.

## Remaining live gates

Code/CI passing does not authorize deployment. Review the target, dedicated secret/credential binding, actual storage and licensed music redirect behavior, disabled access/body/header logging, host-level temporary storage, five-minute whole-pipeline latency and the installed-device journey. Do not run paid generation or send real group audio until these checks and the user's deployment/activation approval are complete. See `EXECPLAN.md`.
