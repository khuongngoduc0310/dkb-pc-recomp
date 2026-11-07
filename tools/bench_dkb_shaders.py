"""Repeat the DKB cold-cache shader comparison on one saved route.

Each run gets a fresh shader-cache directory and a fresh pipeline-cache file. The prepared case copies only the packed
cache, so every repeat starts from the prepared entries; the baseline case starts empty and translates on demand. The
profile is copied from --profile-source for every run, and TOTK_LIB_PREBUILD=0 keeps boot-time pipeline work out of the
shader comparison. The driver's own shader cache is outside the process and cannot be reset here; the translation
counters are the clean metric, frame times are indicative.

    .venv\\Scripts\\python tools\\bench_dkb_shaders.py --prepared build\\dkb-prep\\strict5 --repeat 3 --seconds 20

Game-derived logs and caches stay under build/ (ignored). Do not attach them to reports.
"""
import argparse
import importlib.util
import json
import os
import shutil
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_tool():
    spec = importlib.util.spec_from_file_location('prepare_dkb_shaders', ROOT / 'tools' / 'prepare_dkb_shaders.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--build', type=Path, default=ROOT / 'build' / 'dkb100')
    ap.add_argument('--prepared', type=Path, required=True,
                    help='prepared workspace holding shader-cache/shader-cache.pack')
    ap.add_argument('--work', type=Path, default=ROOT / 'build' / 'dkb-prep' / 'bench')
    ap.add_argument('--seconds', type=float, default=20)
    ap.add_argument('--repeat', type=int, default=2)
    ap.add_argument('--profile-source', type=Path, default=ROOT / 'build' / 'profile-run-latest')
    ap.add_argument('--input-script', type=Path, help='TOTK_INPUT_SCRIPT route for each run')
    ap.add_argument('--fresh-exe', action='store_true',
                    help='copy dkb.exe into each run directory, so the GPU driver cache is keyed to a fresh path')
    args = ap.parse_args()
    exe = args.build / ('dkb.exe' if os.name == 'nt' else 'dkb')
    pack = args.prepared / 'shader-cache' / 'shader-cache.pack'
    if not exe.is_file():
        sys.exit(f'{exe} is missing; build first')
    if not pack.is_file():
        sys.exit(f'{pack} is missing; prepare and pack the workspace first')
    tool = load_tool()
    work = args.work.resolve()
    work.mkdir(parents=True, exist_ok=True)
    runs = []
    for case in ('baseline', 'prepared'):
        for i in range(args.repeat):
            run = work / f'{case}-{i}'
            if run.exists():
                shutil.rmtree(run)
            (run / 'shader-cache').mkdir(parents=True)
            if case == 'prepared':
                shutil.copyfile(pack, run / 'shader-cache' / 'shader-cache.pack')
            extra = {'TOTK_LIB_PREBUILD': '0'}
            if args.input_script:
                extra['TOTK_INPUT_SCRIPT'] = str(args.input_script.resolve())
            run_exe = exe
            if args.fresh_exe:
                run_exe = run / ('dkb.exe' if os.name == 'nt' else 'dkb')
                shutil.copyfile(exe, run_exe)
                # The copied exe needs the build's totk.ini (absolute game paths) and its DLL directory.
                ini = exe.parent / 'totk.ini'
                if ini.is_file():
                    shutil.copyfile(ini, run / 'totk.ini')
                for extra_file in ('romfs-index.bin', 'romfs_config.h'):
                    if (exe.parent / extra_file).is_file():
                        shutil.copyfile(exe.parent / extra_file, run / extra_file)
                extra['PATH'] = str(exe.parent) + os.pathsep + os.environ.get('PATH', '')
            result = tool.run_game(run_exe, run, args.seconds, args.profile_source, None, False, extra)
            result.update(case=case, run=i)
            runs.append(result)
            print(json.dumps(result, indent=1), flush=True)
    summary = {}
    for case in ('baseline', 'prepared'):
        rows = [r for r in runs if r['case'] == case and r.get('exit') == 0]
        def med(key):
            vals = [r[key] for r in rows if r.get(key) is not None]
            return statistics.median(vals) if vals else None
        summary[case] = dict(
            runs=len(rows),
            translated=med('translated'),
            guest_start_ms=med('guest_start_ms'),
            slowest_xlate_ms=med('slowest_xlate_ms'),
            xlate_blocked_pct=med('xlate_blocked_pct'),
            mean_fps=med('mean_fps'),
            low1_fps=med('low1_fps'),
        )
    print(json.dumps(dict(seconds=args.seconds, repeat=args.repeat, summary=summary), indent=1))
    return 0 if all(r.get('exit') == 0 for r in runs) else 1


if __name__ == '__main__':
    sys.exit(main())
