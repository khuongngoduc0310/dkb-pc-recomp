"""Original CPU test of the DKB seed adapter: native Ampere dump -> nvn2_lower -> recorded program -> seed files.

The helper authors two tiny Ampere compute stages (an EXIT, and a MOV+EXIT) plus a native passthrough geometry stage
with two packed skip masks, writes them as a DKB_SHADER_DUMP folder, and writes the lowered bytes. This test builds
TOTKSPRG/TOTKSANS pairs from that output and runs the built dkb.exe --seed-dkb-shaders (no GPU, no translator, no
game):

    - the recorded stage verifies exactly against the native dump,
    - the second native stage is reported as unmatched but does not drop the recorded variant,
    - the two packed GS variants verify by code even though their controls differ from the lowered one,
    - the seed program is the lowered program and the seed answer has a zero input id,
    - the source record files are never modified or removed (a torn tail only stops the walk),
    - the no-dump path selects the same record,
    - merge_captures, merge_dumps and the packed-reference reader keep their first-wins and key rules.

Usage: python tests/dkb_prepare_test.py [--exe build\\dkb100\\dkb.exe] [--cxx clang++]
"""
import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import struct
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_tool():
    spec = importlib.util.spec_from_file_location('prepare_dkb_shaders', ROOT / 'tools' / 'prepare_dkb_shaders.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_seed(exe, work, dump=None, strict=False):
    env = {k: v for k, v in os.environ.items()
           if k.upper() in {'SYSTEMROOT', 'WINDIR', 'PATH', 'TEMP', 'TMP', 'USERPROFILE', 'APPDATA', 'LOCALAPPDATA',
                            'SYSTEMDRIVE'}}
    env['TOTK_PIPELINE_CACHE'] = str((work / 'pipeline-cache.bin').resolve())
    if dump:
        env['DKB_SHADER_DUMP'] = str(Path(dump).resolve())
    if strict:
        env['DKB_SHADER_VERIFIED_ONLY'] = '1'
    r = subprocess.run([str(exe), '--seed-dkb-shaders'], cwd=str(ROOT), env=env, capture_output=True, text=True,
                       timeout=120)
    if r.returncode:
        raise AssertionError(f'seed-dkb-shaders failed ({r.returncode})\n{r.stdout}{r.stderr}')
    return r.stdout + r.stderr


def make_records(tool, out):
    ctl = (out / 'a.lowered.ctl').read_bytes()
    code = (out / 'a.lowered.bin').read_bytes()
    if len(ctl) != 0x880:
        raise AssertionError('lowered control is not 0x880 bytes')
    length = tool.u32(ctl, 0x704)
    c1_off = tool.u32(ctl, 0x700)
    if length != len(code) or c1_off != length:
        raise AssertionError(f'lowered bounds: c1 {c1_off:#x}, length {length:#x}, code {len(code):#x}')
    h = hashlib.sha256()
    h.update(struct.pack('<I', 5))
    h.update(ctl)
    h.update(struct.pack('<Q', length))
    h.update(code)
    h.update(struct.pack('<I', 0))
    digest = h.digest()
    program = struct.pack('<III', 5, length, 0) + digest + ctl + code
    answers = bytearray(148)
    struct.pack_into('<HHH', answers, 136, 1, 1, 1)
    answer = struct.pack('<I', 5) + digest + bytes(32) + bytes(answers) + b'\x00'
    assert len(answer) == 217
    work = out / 'work'
    work.mkdir(parents=True, exist_ok=True)
    (work / 'shader-programs.bin').write_bytes(struct.pack('<8sII', b'TOTKSPRG', 1, 0) + program)
    (work / 'shader-answers.bin').write_bytes(struct.pack('<8sII', b'TOTKSANS', 2, 217) + answer)
    return work, program, answer, digest


def check_seeds(work, program, answer, digest):
    pdata = (work / 'seed-programs.bin').read_bytes()
    if pdata[:8] != b'TOTKSPRG' or struct.unpack_from('<I', pdata, 8)[0] != 1:
        raise AssertionError('seed-programs.bin header')
    body = pdata[16:]
    if body != program:
        raise AssertionError('seed program differs from the lowered program record')
    adata = (work / 'seed-answers.bin').read_bytes()
    if adata[:8] != b'TOTKSANS' or struct.unpack_from('<I', adata, 8)[0] != 2:
        raise AssertionError('seed-answers.bin header')
    rec = adata[16:]
    if len(rec) != 217:
        raise AssertionError('seed answer is not one 217-byte record')
    if rec[4:36] != digest:
        raise AssertionError('seed answer code hash differs')
    if rec[36:68] != bytes(32):
        raise AssertionError('seed answer input id is not zero')
    if rec[68:216] != answer[68:216]:
        raise AssertionError('seed answer bytes differ')


def check_gs_seeds(work, programs, answers):
    pdata = (work / 'seed-programs.bin').read_bytes()
    body = pdata[16:]
    if body != b''.join(programs):
        raise AssertionError('GS seed programs differ from the packed records')
    adata = (work / 'seed-answers.bin').read_bytes()
    recs = [adata[16 + i * 217:16 + (i + 1) * 217] for i in range(len(answers))]
    for rec, answer in zip(recs, answers):
        if rec[36:68] != bytes(32):
            raise AssertionError('GS seed input id is not zero')
        if rec[68:216] != answer[68:216]:
            raise AssertionError('GS seed answer bytes differ')


def make_gs_records(tool, out):
    ctl1 = (out / 'gs.packed1.ctl').read_bytes()
    ctl2 = (out / 'gs.packed2.ctl').read_bytes()
    code = (out / 'gs.lowered.bin').read_bytes()
    programs, answers, hashes = [], [], []
    for ctl in (ctl1, ctl2):
        ans = bytearray(148)
        ans[147] = 4                                         # triangles
        pr = dict(stage=3, control=ctl, length=len(code), code=code, c1=b'', hash=b'')
        h = tool.code_hash(pr, {'answers': bytes(ans)})
        program = struct.pack('<III', 3, len(code), 0) + h + ctl + code
        answer = struct.pack('<I', 3) + h + bytes(32) + bytes(ans) + b'\x00'
        programs.append(program), answers.append(answer), hashes.append(h)
    work = out / 'gswork'
    work.mkdir(parents=True, exist_ok=True)
    (work / 'shader-programs.bin').write_bytes(struct.pack('<8sII', b'TOTKSPRG', 1, 0) + b''.join(programs))
    (work / 'shader-answers.bin').write_bytes(struct.pack('<8sII', b'TOTKSANS', 2, 217) + b''.join(answers))
    return work, programs, answers, hashes


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--exe', type=Path, default=ROOT / 'build' / 'dkb100' / ('dkb.exe' if os.name == 'nt' else 'dkb'))
    ap.add_argument('--cxx', default=os.environ.get('CXX') or shutil.which('clang++'))
    ap.add_argument('--out', type=Path, default=ROOT / 'build' / 'test' / 'dkb-prepare')
    args = ap.parse_args()
    if not args.exe.is_file():
        sys.exit(f'{args.exe} is missing; build the game first')
    if not args.cxx:
        sys.exit('pass --cxx with clang++')
    out = args.out.resolve()
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    helper = out / ('dkb_seed_lower.exe' if os.name == 'nt' else 'dkb_seed_lower')
    cmd = [args.cxx, '-std=c++20', '-O1', '-I', str(ROOT / 'runtime' / 'third_party' / 'eglantine'),
           str(ROOT / 'tests' / 'dkb_seed_lower.cpp'), '-o', str(helper)]
    if os.name == 'nt':
        cmd += ['-static-libstdc++', '-static-libgcc']
    build = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    (out / 'compile.log').write_text(build.stdout + build.stderr)
    if build.returncode:
        raise AssertionError(build.stdout + build.stderr)
    lowered = subprocess.run([str(helper), str(out)], capture_output=True, text=True, timeout=60)
    (out / 'lower.log').write_text(lowered.stdout + lowered.stderr)
    if lowered.returncode:
        raise AssertionError(lowered.stdout + lowered.stderr)

    tool = load_tool()
    work, program, answer, digest = make_records(tool, out)

    # 1. dump verification: A matches, B does not, and B does not drop A from the seed.
    text = run_seed(args.exe, work, out / 'dump')
    if '1 verified (1 exact, 0 native GS)' not in text:
        raise AssertionError(f'dump verification did not report one exact match:\n{text}')
    if '2 unmatched' not in text:
        raise AssertionError(f'dump verification did not report the non-matching stages:\n{text}')
    check_seeds(work, program, answer, digest)

    # 2. no dump: the recorded variant is still selected.
    text = run_seed(args.exe, work, None)
    if 'no native dump' not in text:
        raise AssertionError(f'no-dump run did not report its mode:\n{text}')
    check_seeds(work, program, answer, digest)

    # 2b. verified-only: the dump reproduces A, so it is selected.
    text = run_seed(args.exe, work, out / 'dump', strict=True)
    if '1 selected (verified-only), 0 excluded' not in text:
        raise AssertionError(f'strict seed did not select the verified record:\n{text}')
    check_seeds(work, program, answer, digest)

    # 2c. verified-only with a dump that holds only B: nothing is verified, the seed step refuses, and the
    # previous seed files stay as they were.
    dump_b = out / 'dump-b'
    dump_b.mkdir()
    for suffix in ('.ctl', '.bin'):
        shutil.copyfile(out / 'dump' / ('b' + suffix), dump_b / ('b' + suffix))
    seed_before = [(work / n).read_bytes() for n in ('seed-programs.bin', 'seed-answers.bin')]
    env = {k: v for k, v in os.environ.items()
           if k.upper() in {'SYSTEMROOT', 'WINDIR', 'PATH', 'TEMP', 'TMP', 'USERPROFILE', 'APPDATA', 'LOCALAPPDATA',
                            'SYSTEMDRIVE'}}
    env['TOTK_PIPELINE_CACHE'] = str((work / 'pipeline-cache.bin').resolve())
    env['DKB_SHADER_DUMP'] = str(dump_b.resolve())
    env['DKB_SHADER_VERIFIED_ONLY'] = '1'
    r = subprocess.run([str(args.exe), '--seed-dkb-shaders'], cwd=str(ROOT), env=env, capture_output=True, text=True,
                       timeout=120)
    if r.returncode == 0:
        raise AssertionError('strict seed accepted a dump that verifies none of the records')
    if 'no recorded variant is verified' not in (r.stdout + r.stderr):
        raise AssertionError(f'strict seed failure did not explain itself:\n{r.stdout}{r.stderr}')
    if [(work / n).read_bytes() for n in ('seed-programs.bin', 'seed-answers.bin')] != seed_before:
        raise AssertionError('a failed strict seed overwrote the previous seed files')

    # 3. a torn tail stops the walk but never edits or removes the capture.
    before = (work / 'shader-programs.bin').read_bytes()
    with open(work / 'shader-programs.bin', 'ab') as f:
        f.write(b'\xde\xad\xbe\xef\x01\x02\x03')
    torn = (work / 'shader-programs.bin').read_bytes()
    text = run_seed(args.exe, work, out / 'dump')
    if 'torn tail' not in text:
        raise AssertionError(f'torn tail not reported:\n{text}')
    if (work / 'shader-programs.bin').read_bytes() != torn:
        raise AssertionError('the adapter modified the torn capture')
    check_seeds(work, program, answer, digest)
    with open(work / 'shader-programs.bin', 'wb') as f:
        f.write(before)

    # 4. packed native-GS records: two skip masks share one native stage's code; both must verify by code.
    gswork, gsprogs, gsans, gshashes = make_gs_records(tool, out)
    text = run_seed(args.exe, gswork, out / 'dump')
    if '2 verified (0 exact, 2 native GS)' not in text:
        raise AssertionError(f'the GS records were not both verified by code:\n{text}')
    if '2 unmatched' not in text:
        raise AssertionError(f'the compute stages should not match the GS records:\n{text}')
    check_gs_seeds(gswork, gsprogs, gsans)

    # 5. merge captures, v1 normalization, dump merge and packed-reference reader.
    m1, m2 = out / 'merge1', out / 'merge2'
    m1.mkdir(), m2.mkdir()
    (m1 / 'shader-programs.bin').write_bytes(struct.pack('<8sII', b'TOTKSPRG', 1, 0) + program)
    (m1 / 'shader-answers.bin').write_bytes(struct.pack('<8sII', b'TOTKSANS', 2, 217) + answer)
    (m2 / 'shader-programs.bin').write_bytes(struct.pack('<8sII', b'TOTKSPRG', 1, 0) + program)
    changed = bytearray(answer)
    changed[-1] = 2
    (m2 / 'source-answers.bin').write_bytes(struct.pack('<8sII', b'TOTKSANS', 2, 217) + bytes(changed))
    merged_progs, merged_ans = tool.merge_captures([m1, m2], out / 'merged', None)
    if len(merged_progs) != 1 or len(merged_ans) != 1 or merged_ans[0]['raw'][-1] != 0:
        raise AssertionError('merge_captures did not deduplicate by code hash/input id, first source winning')
    v1 = out / 'merge-v1'
    v1.mkdir()
    (v1 / 'shader-programs.bin').write_bytes(struct.pack('<8sII', b'TOTKSPRG', 1, 0) + program)
    (v1 / 'shader-answers.bin').write_bytes(
        struct.pack('<8sII', b'TOTKSANS', 1, 216) + answer[:68 + 147] + answer[-1:])
    _, v1_ans = tool.merge_captures([v1], out / 'merged-v1', None)
    if len(v1_ans) != 1 or len(v1_ans[0]['raw']) != 217 or v1_ans[0]['raw'][147] != 0:
        raise AssertionError('merge_captures did not normalize a v1 answer record')
    dump_all = tool.merge_dumps([out / 'dump', out / 'dump'], out / 'dump-merged')
    if len([n for n in os.listdir(dump_all) if n.endswith('.ctl')]) != 3:
        raise AssertionError('merge_dumps did not deduplicate dump files')
    name = bytes.fromhex('11' * 32)
    data = b'packed test data'
    pack_path = out / 'tiny.pack'
    pack_path.write_bytes(b'TKSPACK1' + struct.pack('<II', 1, 0) +
                          struct.pack('<32sIIQ', name, 1, len(data), 64) + data)
    pack = tool.Pack(pack_path)
    if pack.read(1, name) != data:
        raise AssertionError('Pack did not read a packed entry')
    pack.close()
    cache = out / 'shader-cache'
    cache.mkdir(exist_ok=True)
    shutil.copyfile(pack_path, cache / 'shader-cache.pack')
    (out / 'dkb-prepare.json').write_text('{"pack": null, "pack_size": null}', encoding='utf-8')
    tool.record_pack(out)
    manifest = json.loads((out / 'dkb-prepare.json').read_text(encoding='utf-8'))
    if manifest['pack'] != str(cache / 'shader-cache.pack') or manifest['pack_size'] != pack_path.stat().st_size:
        raise AssertionError('record_pack did not record the pack after a verify-only pack')

    print('dkb prepare: passed (native lowering, exact match, unmatched report, no-dump, verified-only, GS code match, '
          'merge/dump/pack, manifest pack, torn tail)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
