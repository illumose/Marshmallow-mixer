"""Synthetic local render probe; approximate one 0.25-CPU replica, no network."""
import concurrent.futures
import json
import os
import resource
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]


def worker(case):
    sys.path.insert(0, str(ROOT))
    import main as mixer
    os.sched_setaffinity(0, {min(os.sched_getaffinity(0))})
    with tempfile.TemporaryDirectory(prefix='synthetic-resource-') as directory:
        root = Path(directory)
        voice, music = root / 'voice.mp3', root / 'music.mp3'
        music_seconds = 30 if case == 'single' else 16.5
        for path, frequency, seconds in [(voice, 1000, 98.113), (music, 440, music_seconds)]:
            subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i',
                            f'sine=frequency={frequency}:sample_rate=44100:duration={seconds}',
                            '-c:a', 'libmp3lame', '-y', str(path)], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)

        def local_download(value, target, settings, is_voice, deadline):
            shutil.copyfile(voice if is_voice else music, target)

        mixer.download = local_download

        def render(index):
            destination = root / str(index)
            destination.mkdir()
            started = time.monotonic()
            output = mixer.render({'voice_url': 'synthetic', 'music_url': 'synthetic'}, None, destination)
            return {'render_wall_seconds': round(time.monotonic() - started, 3),
                    'duration_seconds': round(mixer.duration(output, time.monotonic() + 10), 3)}

        jobs = 1 if case == 'single' else 2
        with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as executor:
            results = list(executor.map(render, range(jobs)))
        own_peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
        child_peak = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss * 1024
        print(json.dumps({'case': case, 'jobs': jobs, 'music_seconds': music_seconds, 'results': results,
                          'worker_peak_rss_bytes': own_peak,
                          'largest_child_peak_rss_bytes': child_peak,
                          'conservative_memory_upper_bound_bytes': own_peak + jobs * child_peak}))


def rss_tree(pid):
    """Read only this probe's known process tree; no other app processes."""
    total = 0
    try:
        for line in Path(f'/proc/{pid}/status').read_text().splitlines():
            if line.startswith('VmRSS:'):
                total += int(line.split()[1]) * 1024
        children = Path(f'/proc/{pid}/task/{pid}/children').read_text().split()
        total += sum(rss_tree(int(child)) for child in children)
    except (OSError, ValueError):
        pass
    return total


def run(case):
    process = subprocess.Popen([sys.executable, __file__, '--worker', case],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               start_new_session=True, text=True)
    started = time.monotonic()
    peak = 0
    stopped_reason = None
    def send_signal(value):
        try:
            os.killpg(process.pid, value)
        except ProcessLookupError:
            pass
    try:
        while process.poll() is None:
            send_signal(signal.SIGCONT)
            time.sleep(0.025)
            if process.poll() is not None:
                break
            send_signal(signal.SIGSTOP)
            peak = max(peak, rss_tree(process.pid))
            if peak > 450_000_000:
                stopped_reason = 'aggregate_rss_exceeded_450MB'
                break
            if time.monotonic() - started > 90:
                stopped_reason = 'outer_90_second_budget_exceeded'
                break
            time.sleep(0.075)
        if stopped_reason:
            send_signal(signal.SIGKILL)
        else:
            send_signal(signal.SIGCONT)
        stdout, stderr = process.communicate(timeout=5)
    finally:
        if process.poll() is None:
            send_signal(signal.SIGKILL)
            process.wait(timeout=5)
    result = {'case': case, 'exit_code': process.returncode, 'stop_reason': stopped_reason,
              'peak_aggregate_rss_bytes_sampled': peak or None,
              'outer_wall_seconds': round(time.monotonic() - started, 3),
              'method': 'one CPU affinity; whole process group resumed 25ms/stopped 75ms; sampled aggregate RSS'}
    if process.returncode == 0:
        result['measurement'] = json.loads(stdout)
        if result['measurement']['conservative_memory_upper_bound_bytes'] > 450_000_000:
            stopped_reason = result['stop_reason'] = 'conservative_memory_upper_bound_exceeded_450MB'
    else:
        result['synthetic_error'] = stderr[-1800:]
    print(json.dumps(result), flush=True)
    return process.returncode == 0 and stopped_reason is None


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--worker':
        worker(sys.argv[2])
    else:
        if not run('single'):
            sys.exit(1)
        sys.exit(0 if run('two_near_copy_cap') else 1)
