# Speech-aware music mastering (local review)

## Target and goal

Work from the GitHub snapshot at commit `9510e1cb511280b1e98151485c07df0947fd0478` in this isolated directory. Add speech-driven music ducking, measured loudness/true-peak control, smooth bounded loops and explicit MP3 encoding while preserving the private `/mix` contract and downloader boundaries.

## Constraints

- Keep `POST /mix` as exactly `{ "voice_url": ..., "music_url": ... }` and keep its streamed `audio/mpeg` response.
- Preserve dedicated bearer auth, exact signed-voice URL scope, reviewed music/redirect hosts, public DNS pinning, TLS hostname verification, streamed size/time limits, local-only FFmpeg demuxing, fixed errors and request-scoped cleanup.
- Preserve the 7-second intro, 4-second tail and 3-second music fade.
- Use synthetic local audio only. Do not fetch catalog tracks or private exports, call audio providers, edit workflows, write to GitHub or deploy.
- Fit the 90-second job budget and a one-CPU / 500-MB target.

## Plan and status

- [x] Fetch the requested commit's `main.py`, tests, README, Dockerfile, requirements and ExecPlan with the GitHub connector.
- [x] Check for root and test-level `AGENTS.md`; neither file exists at the requested commit.
- [x] Add a bounded music loop and crossfade, speech-keyed compression, two-pass measurement plus one linear normalization pass, true-peak guard and explicit MP3 output.
- [x] Extend synthetic tests for format, loudness, post-encode true peak, ducking/recovery, loop-boundary continuity, loop-buffer cap and a 120-second voice source.
- [x] Run source compilation and the repository's standard suite with hash-verified FastAPI/Starlette dependencies on Python 3.12.
- [ ] Run the Python 3.11 CI image; Docker and that interpreter are unavailable in this executor.
- [ ] Return the patch for parent review. Deployment and provider activation are outside scope.

## Implementation

- Voice and music levels enter the mix unchanged except for the existing 10% music gain. `sidechaincompress` uses the delayed voice as its key, with a -24 dBFS threshold, 2:1 ratio, 20 ms attack and 500 ms release. `amix` normalization is disabled to avoid gain changes when both streams are active.
- One measurement pass obtains integrated loudness and true peak. A final linear EBU R128 pass targets -16 LUFS / -2 dBTP and rejects filter output above -1.5 dBTP. After MP3 encoding, a separate FFmpeg decode/measurement pass reads the MP3 input true peak and rejects anything above -1 dBTP. The MP3 response is explicit 192 kb/s, stereo, 44.1 kHz.
- If music ends before the mix, the mixer crossfades the first loop prefix's tail into its head, then loops that segment. The buffer is capped at 120 seconds (about 40 MiB of stereo float audio); tracks under one second fail closed. Music that already covers the mix is trimmed without a loop buffer.
- One mix slot and one FFmpeg filter thread are used for the single-CPU target. URL parsing, auth, private download, response shape, cleanup and the 7/4/3-second timing remain intact.

## Verification

- `python -m py_compile main.py tests/test_mixer.py`: passed.
- 23 standard synthetic tests: passed in 17.347 seconds with hash-verified FastAPI/Starlette dependencies. The suite included the existing authorization/URL/DNS/download/cleanup coverage, real FFmpeg speech/music mixing, encoded loudness/true-peak measurements, a near-full-scale MP3 rejected by the post-encode guard, duck/recovery and loop-boundary continuity assertions, and a 120-second synthetic voice render within `JOB_SECONDS`.
- Local runtime: Python 3.12.14 and FFmpeg 6.1.1; Docker/Python 3.11 CI was unavailable here.
- `PYTHONPATH=/tmp/marshmallow-mixer-deps python3 -m unittest discover -s tests -v` passed with the hash-verified dependency path. The container-level Python 3.11 CI image remains unrun.
- The maximum configured loop buffer is analytically bounded at 41,983,200 bytes for stereo float samples. The executor did not provide a 500-MB cgroup in which to measure peak RSS.
- No real audio, storage URL, provider or catalog was accessed. No remote write, workflow edit or deployment occurred.
