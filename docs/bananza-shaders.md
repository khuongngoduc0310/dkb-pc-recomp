# Bananza shaders (base 1.0.0)

Notes on the NVN 2 shader format and on how DKB Recomp turns the game's Ampere shaders into SPIR-V. The plan is in
[bananza-plan.md](bananza-plan.md); open shader work is in the issues labelled
[area: shaders](https://github.com/Lucas-blip-png/dkb-pc-recomp/issues?q=is%3Aopen+label%3A%22area%3A+shaders%22).

## Where the shaders are

- RomFS `Shader/`: `SilverStarMaterial...Nin_Ounce_NVN2.bfsha.zs` (12.2 MB zstd, most of them),
  `Project...release.sarc.zs` (660 KB) and `PhotoFilter...bfsha.zs` (14 KB). "Ounce" is the Switch 2 code name.
- `bfsha` is an `FSHA` container with `BNSH` inside (and `grsc`), the same format family as on Switch 1. What changes is
  the GPU code inside the BNSH: Ampere instead of Maxwell.
- `Lib/sead/primitive_renderer/primitive_drawer_nvn_shader.bin` (7168 bytes) and `..._nvn2_shader.bin` (6912 bytes)
  are the same shader compiled for NVN 1 (Maxwell) and NVN 2 (Ampere): a Rosetta stone, since TotK Recomp's Maxwell
  translator already understands the first. Both carry the NVN control magic `0x98761234` at +0x10; the next field is
  `0x01` for NVN 1 and `0xC9` for NVN 2.

## The NVN 2 control section

The layout differs completely from NVN 1's (`tools/ctlcmp.py` compares the sead pair; `tools/ctlstats.py` and
`tools/sphstats.py` read the whole corpus). Fields known so far, over the 2,656 stages of the game's corpus
(`DKB_SHADER_DUMP`, see `runtime/gfx_shader.c`, `gfx_shader_nvn2_dump`):

| Offset | Meaning |
| --- | --- |
| +0x00 | Magic `0x98761234` |
| +0x04 | Version `0xC9` (201); NVN 1 has 1 |
| +0x08 | `0xB` (2,626 stages) or `0x7` (30); not the stage |
| +0x0C | `0x000E0170`, constant (target, probably SM86) |
| +0x20..+0x2C | 128-bit hash (1,907 distinct) |
| +0x30 | Control size, `0xD8` in the game (`0x930` in sead); repeated at +0x98, +0xC4, +0xC8. 182 stages leave it 0 and are treated as `0xD8` |
| +0x38 | Code size |
| +0x40, +0x44 | Inline constants (bank 1) follow the code from +0x40 to +0x44 (`runtime/nvn2_lower.cpp`) |
| +0x4C | Small (0x24..0x48): likely the register count |
| +0x54, +0x60 | Multiples of 0x1000 |
| +0x64 | Compute shared memory size |
| +0xA4 | `0x1000000`, constant |
| +0xB1, +0xB2, +0xB3, +0xB6 | Compute bank counts: uniform buffers, storage buffers, textures, images (`NVN2_C0_LAYOUT` in `runtime/gfx_shader.c`) |

The Ampere code starts right away, without NVN 1's `0x12345678` header, as 128-bit instructions (SM70 and later).
Graphics stages begin with a version 4 SPH (32 words, 0x80 bytes, as in Mesa NAK `sph.rs`): word 0 bits 0-4 are the
SPH type (1 VTG, 2 PS), bits 5-9 the version (4), bits 10-13 the shader type (1 vertex, 5 pixel). Compute stages have
no SPH. The corpus holds 1,460 vertex, 1,138 pixel and 58 compute stages.

`nvdisasm -b SM86 -raw` (CUDA toolkit 13.4.92) disassembles the code after the SPH cleanly. The sead vertex shader:
`ALD.96 a[0x80]` / `ALD a[0xa0]`, two 4x4 matrices from `c[0x4]` and `c[0x3]` (`FMUL`/`FFMA`/`FADD .FTZ`),
`AST.128 a[0x80]` (colour), `AST.128 a[0x70]` (position), `EXIT`. Attributes keep the classic layout (a[0x70] position,
a[0x80] and up generic), so Ryujinx's Maxwell mapping applies.

## From Ampere to SPIR-V

`nvnProgramSetShaders` → `runtime/nvn2_lower.cpp` → Eglantine's Ampere-to-Maxwell lowering
(`runtime/third_party/eglantine/`) → Maxwell code plus a synthetic NVN 1 control section → TotK Recomp's translator
(`totk_xlate`, Ryujinx's shader translator with the patches in `tools/xlate/patches/`) → SPIR-V. Offline, the same path
runs with `tools/dkb_lower_test.cpp` and `totk_xlate jobs.json`.

Fixes on top of Eglantine are marked "DKB Recomp" in its files. The ones that mattered most:

- closing the lowered program with a group of Maxwell `EXIT`s (855 `IndexOutOfRange` in the translator's decoder);
- the self-branch end marker, which otherwise became `while (true)`;
- bit 32 of `BRA` is the `.U` flag, not part of the offset (173 stages failed on branch alignment);
- `ULDC.U8/S8/U16/S16`, `I2F.U16` from a uniform register, `I2F.U8` of any byte, `FRND` with a constant buffer
  source, `S2R SR_THREAD_KILL`.

Result: every stage of the first 3D scene lowers and translates (4,820 of 4,820). The translator side also clamps every
shared and local memory index (patch 0013), because the game's neural upscaler reads shared memory out of bounds
([#9](https://github.com/Lucas-blip-png/dkb-pc-recomp/issues/9)).

## Preparing the cache offline

Every `gfx_shader_get` the runtime answers is recorded as a lowered program plus the answers the draw state gave it
(`shader-programs.bin`, `shader-answers.bin` next to `pipeline-keys.bin`). `tools/prepare_dkb_shaders.py` turns that
capture into the seed files `gfx_shader_prepare_seed` already understands, then runs the existing `--prepare-shaders`
translation and pipeline warm:

```text
.venv\Scripts\python tools\prepare_dkb_shaders.py --build build\dkb100 --work build\dkb-prep\corpus
.venv\Scripts\python tools\prepare_dkb_shaders.py --build build\dkb100 --work build\dkb-prep\strict --merge-captures <fresh capture> build\dkb100 --dump <dump> --verified-only --pack
.venv\Scripts\python tools\prepare_dkb_shaders.py --build build\dkb100 --work build\dkb-prep\one --one <64-hex code hash>
... --dump build\dkb-prep\dump      # verify native stages through nvn2_lower
... --reference <runtime cache>     # byte-compare with a runtime translation (A/B proof)
... --coverage                      # list selected/excluded by stage; writes coverage.json
... --pack                          # fold the verified cache with tools/pack_shader_cache.py
... --run-only --run-seconds 60 --run-env TOTK_LIB_PREBUILD=0 --trace <code hash>
```

The workspace is private: the tool always points `TOTK_PIPELINE_CACHE` and `TOTK_SHADER_CACHE` into `--work`, copies
the records instead of editing them, and never touches the build's working `shader-cache`. `--seed-dkb-shaders` (runtime
`gfx_shader.c`) writes `seed-programs.bin` and `seed-answers.bin` from the recorded lowered programs, optionally
lowering every `<fnv>.bin`/`.ctl` of a `DKB_SHADER_DUMP` folder again through the same `nvn2_lower`: matched stages prove
the lowering still reproduces the recorded bytes, unmatched ones are reported as coverage/lowering drift and never
drop a recorded variant. `--prepare-shaders` then produces the cache and `shader-prepared.bin` through the normal
`gfx_shader_get` path, and `--warm-shaders` rebuilds the driver's pipeline cache if the driver evicted it.

`--verified-only` keeps only records whose lowered bytes the dump reproduces, which is the strict corpus. Replaying the
recorded face and terrain routes (`TOTK_INPUT_SCRIPT=build\face-route.txt` with `profile-face-front`, and
`terrain-route.txt` with `profile-terrain-test4`, each with a fresh `DKB_SHADER_DUMP`) raised the verified set from
1,074 to **1,952 of 3,038**; 1,086 records were still excluded. Replaying the same terrain route with
`profile-run-latest` captured 6,258 stages and raised the set to 2,746; the same route with `profile-face-front` added
two more, for **2,748 of 3,039**. The other profiles reach the same opening area, so the remaining 291 need content
those routes do not meet. The face capture held 4,949 native stages and 381 variants, the terrain capture 4,989 stages
and 734 variants, and all runs had 0 shader failures and 0 validation errors. `--dump` accepts several folders and
merges them, so every captured route adds to the same strict set.
`--merge-captures` joins captures by code hash and input id (first source wins); it also accepts a prepared workspace,
whose full capture is kept in `source-answers.bin`. Each successful preparation writes `dkb-prepare.json` (translator
and effective identities, host options, dump coverage, seed counts, excluded records, cache and pack) and a `play.cmd`
that sets the two cache paths and starts `dkb.exe` without touching the build's own cache.

The first strict replays found two GS and four FS variants outside the dump. The GS cause was a matcher bug: a packed
native-GS record is not found by the control+code map, and two packed variants can share one native stage's code. The
seed adapter now scans every GS record by code; both verified. Capturing the face replay added the four FS stages. With
the 2,748-entry strict cache the face (70 s), terrain (240 s) and `profile-run-latest` terrain routes translated
0 shaders, had 0 xlate stalls and ran at 60 fps; the full 3,039-entry cache also translates 0 on the face route. The
verifier reports 0 warnings: the two GS mask notices came from the Python parser, which now admits a lowered native GS
body through the same rule the runtime uses. `--coverage` classifies the remaining 291 records by stage (170 vertex,
2 geometry, 119 fragment) and writes `coverage.json`. Lowering all 6,886 merged dump stages reproduced none of those
code hashes, so the tail is capture coverage, not a matcher difference; those variants need sessions that meet their
native stages. The strict corpus prepares in 19.6 s plus 1.5 s of pipeline warm and packs to 229 MiB; the full
3,039-record cache is 250 MiB (21.9 s + 1.6 s). The labelled launchers in `build\dkb-prep` are `play-strict.cmd`
(2,748 verified) and `play-full.cmd` (3,039 recorded, 291 unverified).

The cache key includes the translator identity and the device's host answers (`gfx_shader_set_host`: float64, storage
alignment, subgroup sizes, gather bias). Preparation therefore runs on the play machine with the same translator,
GPU/driver and `totk.ini` settings; a translator rebuild or a GPU/driver change re-keys everything and the preparation
has to be repeated. The records themselves are game-derived: keep the workspace under `build/` and out of commits and
public reports.

A native dump alone cannot supply the answers (texture/image types, vertex attributes and colour outputs come from the
draw state), so the first play always captures them; offline preparation covers the corpus the records hold. The
`DKB_SHADER_TRACE=<64-hex code hash>` diagnostic logs the first cache outcome (memory, disk or translated) for a shader
and is how a single prepared shader is shown to load without retranslating.

## Replaying a failed translation locally

Set `DKB_SHADER_FAILURE_DUMP` to an absolute path under an ignored `build/` directory before launching the game.
Each failed translator request gets an exclusively claimed `capture-<process>-<sequence>` directory there, holding
`<key>.request.json`, `<key>.failed.code` and `<key>.error.txt`.
The request preserves the actual draw/host answers and inline constants; the code is the lowered Maxwell input.
Normal cache cleanup stays enabled. Existing packets stay unchanged, including when multiple runtimes share the
dump directory; an incomplete older packet does not block a new capture. Native Ampere input capture remains
the separate `DKB_SHADER_DUMP` option.

Replay one packet without the game or GPU:

```text
python tools/replay_shader_failure.py build/shader-failures/<capture>/<key>.request.json --translator build/xlate/totk_xlate.exe --out build/shader-replay
```

The tool sets `DMSH_STACK=1` for the translator child and retains its reply and stack output in `reply.txt` and
`stderr.txt` under `--out`. It uses the captured sibling `.failed.code` file, so the original temporary cache path may be gone.
Use the same translator executable/identity when reproducing a report. Packets and replay output contain game-derived
code or constants: keep them local and out of commits and public attachments. The option is off by default.

## Constant banks

- **c0, driver bank.** Graphics stages use NVN 1's layout (vertex storage buffers at 0x110, pixel at 0x510). Compute
  is packed per program from the control counts (`runtime/nvn2_c0.h`): a 0x10 head, the eight root tables the game
  pushes with `nvnCommandBufferPushComputeConstants`, each padded to 16 bytes (word counts at control +0xa9..+0xb0),
  the uniform buffer and storage buffer tables, then texture and image handles. The upscaler's root table 0 holds its
  two image rectangles (#20).
- **c1**: the inline constants after the code.
- **c2**: combined texture handles at 0x20 + 8 × slot, as in NVN 1.
- **c24**: read only by full-screen blit shaders (3 vertex, 2 pixel), at offsets 0x0 to 0xc. Maxwell has 18 banks, so
  the lowering moves it to bank 17, and the host fills bank 17 with a blit block computed from the render area
  (`runtime/vk_exec_draw.c`, the `slot == 17` branch; Eglantine's `BlitConstantsPass`). NVN 2 push constants are
  written as Ampere root tables, one per stage, and bank 24 is the likely place shaders read them from; if so, the game
  supplies these values itself. See [#2](https://github.com/Lucas-blip-png/dkb-pc-recomp/issues/2).

## Still to handle

From Eglantine's `NX2_NOTES` and our runs: ASTC with the Switch 2 GOB swizzle (TotK Recomp's deswizzle is the Switch 1
one), mesh and task shaders (`ISBEWR`/`ISBERD`), 8192×8192×68 ASTC virtual textures (sparse allocation), and `IPA`
interpolation modes. The NVN 2 command buffer calls the host does not record yet are listed in
[#6](https://github.com/Lucas-blip-png/dkb-pc-recomp/issues/6).
