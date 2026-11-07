# Testing DKB Recomp

This guide is for running DKB Recomp on your own PC and reporting what happens. You build it from source with your own
copy of the game. Nothing from the game is in this repository, and nothing you build from it may be shared: not the
executable, not the generated code, not the shader cache.

## What you need

- Windows 10 or 11 on an x86-64 CPU with AVX2.
- 16 GB of RAM. The game uses about 13 GB, so close other large programs first.
- A GPU with Vulkan 1.3 and a current driver.
- About 15 GB free for the build (the packed RomFS is about 9 GB), besides the space your dump takes.
- *Donkey Kong Bananza* base version 1.0.0, dumped from your own Switch 2: the ExeFS (`main`, `sdk`, `main.npdm`) and
  the RomFS as a folder. Update 458752 is not supported yet. This project does not explain how to dump the game.
  If your dump is a single `.nup` file (the container Eglantine and Eden load), `tools/extract_nup.py` writes both
  folders from its base package: `python tools\extract_nup.py <game>.nup --list` shows the packages, and
  `python tools\extract_nup.py <game>.nup --out build\extracted\dkb100-dump` writes `exefs` and `romfs` there, checking
  every file's SHA-256. Pass those two folders to the commands in the README.

## Build

Install the toolchain from [building.md](building.md) (MSYS2 UCRT64 Clang, Ninja, Python 3.12 in `.venv`, .NET SDK),
then run the commands in the README, "Building from source". The executable ends up in `build\dkb100\dkb.exe`.

The generation command must include `--patches recomp\patches-dkb100.txt`. This enables the controller setup patch
that bypasses the game's HID server proxy, which the host does not implement. If you generated the code without
that argument, rerun the README's generation command with it and then rebuild with `tools\build_game.py`.
Rebuilding alone leaves the unpatched generated code in place and the game stops during controller setup.

## Run

```
build\dkb100\dkb.exe
```

Run it from inside the repository: it finds the shader translator (`build\xlate\totk_xlate.exe`) by looking up from
its own folder. The first run translates every shader it meets and is slow; later runs use the cache.

Useful settings, as environment variables (`set NAME=value` in the same Command Prompt):

| Setting | Effect |
| --- | --- |
| `TOTK_PROFILE_DIR=<folder>` | Use this folder for the save and settings. A new, empty folder gives a fresh start, which is what reports should use. |
| `TOTK_GPU_NAME=<text>` | Use the first GPU whose name contains this text, on machines with more than one. |
| `TOTK_GPU=null` | Run without a picture (no Vulkan). Useful to tell CPU and OS bugs from graphics bugs. |
| `TOTK_XLATE_PROCS=<n>` | Shader translator processes at run time (default 2). Lower it if RAM runs out. |
| `TOTK_TRACE=perf` | Log the frame time, what holds frames back and the GPU time every 60 frames. For stalls and low frame rates. |
| `TOTK_PERF_SLOW_GPU_MS=<ms>` | With `TOTK_TRACE=perf`, a replay part that runs this long on the GPU (default 100) gets a line with its draw and dispatch counts and the NVN 2 names of its stages, a second line with the arguments of its indirect draws as read back afterwards, and a third saying whether a shader in that part wrote the largest one's arguments, and which. |
| `TOTK_FIX_INDIRECT_RESET=0` | Draws the terrain's indirect-record reset program as a shader instead of the host doing its stores (#48). The shader's stores are lost, so the records keep zero or stale contents; for comparisons only. |
| `TOTK_EXPERIMENTAL_GEOMETRY=1` | Replays draws with an ordinary geometry stage through it (#52). Off by default; other geometry draws are rejected once in the log. |
| `TOTK_NATIVE_GS=0` (or `DKB_NATIVE_GS=0`) | Turns off the replay of the game's native passthrough geometry stages on the portable path. That replay draws the destructible terrain (#45) and is on by default; with it off the terrain is black. The varyings are inferred, not read from the hardware skip mask. |
| `DKB_GEOMETRY_CONTEXT=1` | Logs the first 12 draws with a geometry stage and their buffers (#58). `DKB_GEOMETRY_CONTEXT_ORIGIN=<hex>` keeps only draws with that stage origin. |
| `TOTK_TRACE=codec` | Log each call of the video decoder that moves a buffer or a picture. For guide movies that stop or show a flat colour. |
| `DKB_SHADER_TRACE=<64-hex>` | Log the first cache outcome (memory, disk or translated) for that shader code hash. For proving one offline-prepared shader loads without translating. |

The settings keep TotK Recomp's `TOTK_` names, so its fixes apply without a rename. Each one can also be written with
`DKB_` in its place (`DKB_GPU=null`); when both are set, the `DKB_` one wins.

Command-line options: `--seconds S` stops the run after S seconds of game time, and `--shot frame.png` with it saves
that frame. A shot is for your own debugging; do not post it if it shows game content.

To edit every F1-menu setting before starting the game, double-click `Launch DKB.cmd` or run
`.venv\Scripts\python.exe tools\launch_dkb.py`. The launcher writes changed values into the `totk.ini` beside `dkb.exe`
and starts the game. Settings marked “After restart” take effect on that start; settings marked unsupported are not
implemented by that game build. Environment overrides for changed settings are removed from the child game process.
`--exe PATH` selects another build.

## Preparing shaders offline

The first run translates shaders while it plays, and a translator rebuild invalidates every cache key at once. After a
session has captured the variants it met, prepare them ahead of play (the records are in the build folder):

```text
.venv\Scripts\python tools\prepare_dkb_shaders.py --build build\dkb100 --work build\dkb-prep\corpus --pack
```

It keeps the build's working `shader-cache` untouched by using `--work` for the records, seeds, translations and the
packed cache. To use the prepared cache, start the game with the two paths the tool printed:

```text
set TOTK_PIPELINE_CACHE=<work>\pipeline-cache.bin
set TOTK_SHADER_CACHE=<work>\shader-cache
build\dkb100\dkb.exe
```

A short proof with one shader, and a run that reports its cache outcome:

```text
.venv\Scripts\python tools\prepare_dkb_shaders.py --build build\dkb100 --work build\dkb-prep\one --one <64-hex code hash> --dump <DKB_SHADER_DUMP folder> --verified-only --reference <runtime shader-cache folder>
.venv\Scripts\python tools\prepare_dkb_shaders.py --build build\dkb100 --work build\dkb-prep\one --run-only --run-seconds 20 --run-env TOTK_LIB_PREBUILD=0 --trace <64-hex code hash>
```

The strict corpus uses one or more native dumps and keeps only records whose lowered bytes they reproduce:

```text
.venv\Scripts\python tools\prepare_dkb_shaders.py --build build\dkb100 --work build\dkb-prep\strict --merge-captures <fresh capture> build\dkb100 --dump <DKB_SHADER_DUMP folder> [another dump] --verified-only --pack
```

If a route still translates shaders its dump does not cover, capture that route's dump and merge it:
`--run-only --run-seconds 20 --run-env DKB_SHADER_DUMP=<folder>`; then rerun the strict command with both dump folders.
On the tutorial route this took the strict set from 1,060 to 1,074 and the translations from 14 to 0.

Recorded routes can be captured without hand-play. Use the profile the route was recorded with and a fresh workspace:

```text
set TOTK_INPUT_SCRIPT=build\face-route.txt
set TOTK_PROFILE_DIR=<copy of build\profile-face-front>
set DKB_SHADER_DUMP=build\dkb-prep\route-face\dump
set TOTK_PIPELINE_CACHE=build\dkb-prep\route-face\pipeline-cache.bin
set TOTK_SHADER_CACHE=build\dkb-prep\route-face\shader-cache
build\dkb100\dkb.exe --seconds 70
```

`build\terrain-route.txt` with `profile-terrain-test4` and `--seconds 240` is the longer route. These two captures raised
the strict set from 1,074 to 1,952 of 3,038. The first replays found two GS and four FS variants outside the dump; the GS
matcher now scans packed GS records by code, and capturing the face replay added the four FS stages. Replaying the same
terrain route with `profile-run-latest` captured 6,258 stages and raised the strict set to 2,746; the same route with
`profile-face-front` added two more, for 2,748 of 3,039. The other profiles reach the same opening area, so the
remaining 291 need content those routes do not meet. With the resulting
2,748-entry cache the face, terrain and `profile-run-latest` terrain routes translate 0 shaders with 0 shader-xlate
stalls; the full 3,039-entry cache also translates 0 on the face route. `--coverage` writes `coverage.json` and splits
the 291 excluded records into 170 vertex, 2 geometry and 119 fragment. None of those code hashes appears among the
lowered stages of the merged dumps, so the tail is capture coverage, not a matcher difference. The strict corpus packs
to 229 MiB; the full one is 250 MiB. The labelled launchers are `build\dkb-prep\play-strict.cmd` (2,748 verified, the
recommended one) and `play-full.cmd` (3,039 recorded, 291 unverified).

`--merge-captures` joins sessions by code hash and input id (first source wins). Each preparation writes
`build\dkb-prep\<workspace>\play.cmd`, which sets `TOTK_SHADER_CACHE` and `TOTK_PIPELINE_CACHE` for that workspace and
starts `dkb.exe`; this is the recommended way to play a prepared cache without changing the build's own cache.
`--run-only` uses an existing workspace without seeding or translating again; `--no-verify` skips only the verifier.

To repeat the shader-translation comparison with fresh shader/pipeline caches per run:

```text
.venv\Scripts\python tools\bench_dkb_shaders.py --prepared build\dkb-prep\routes-strict8 --repeat 3 --seconds 70 --profile-source build\profile-face-front --input-script build\face-route.txt --fresh-exe
```

It reports medians for a baseline that translates on demand and for the packed prepared cache, with
`TOTK_LIB_PREBUILD=0`. `--input-script` replays a recorded route. `--fresh-exe` copies `dkb.exe`, `totk.ini` and the
RomFS index into each run, so caches keyed to the exe path (AMD's per-exe pipeline cache) start fresh; NVIDIA's driver
cache may persist across paths, so frame times are indicative and the translation counts are the clean metric.

On the 70 s face route, three fresh-exe runs per case produced medians of 465 translations in the baseline and 0 in the
strict cache. Guest start was 131 against 136 ms and mean fps 60.1 against 60.0; the 1 % low was 55.6 against 57.1 fps.
Earlier sessions had lower baseline 1 % lows (near 30 fps), which is the driver's own cache varying, not the prepared
cache. Reports should give translation counts, xlate stalls and guest start first.

With the same 10,992 entries and three runs per format, the packed cache started the guest in 133–174 ms with 28–70 ms of
prewarm; the loose cache started in 126–330 ms with 25–223 ms. After the first run the packed range was 173–174 ms and
68–70 ms, the loose range 206–330 ms and 104–223 ms; the spread is the file system's, but the pack is never slower. Both
translated 0 shaders.

`--dump` lowers every native stage in the dump again through the runtime's `nvn2_lower` and reports coverage and
lowering drift; it never drops recorded variants. `--reference` compares each prepared `.spv`, `.res.json` and `.in`
byte-for-byte with a runtime cache from the same build and GPU, loose or packed. Use it on the same draw (the `--one`
proof); a whole-corpus cache from another session can differ in the translator's draw-specific `key` field and in
answers outside the use mask. On the 1,037 entries the `profile-run-latest` capture translated, every `.spv` and every
control/code/c1 byte was identical; 510 `.res.json`/`.in` pairs matched exactly and 527 differed only in those
draw-specific fields. Preparation is specific to the translator build and the GPU/driver host answers, so rerun it
after either changes. The workspaces hold game-derived files: keep them under `build/` and out of commits and reports.
Details: [bananza-shaders.md](bananza-shaders.md), “Preparing the cache offline”.

## What to report

Open an issue with the issue form, or comment on [#3](https://github.com/Lucas-blip-png/dkb-pc-recomp/issues/3)
(GPU testing) or [#7](https://github.com/Lucas-blip-png/dkb-pc-recomp/issues/7) (playing further):

- the commit you built (`git rev-parse --short HEAD`);
- GPU, driver version and Windows version;
- how far the game got and what you did just before it stopped;
- the log, `logs\totk.log` next to `dkb.exe`, as an attachment. After a crash, copy it before you start the game
  again: the next run renames it to `totk.1.log`.

Never attach game files, keys, builds or the shader cache, and never paste key values into a log or an issue.
