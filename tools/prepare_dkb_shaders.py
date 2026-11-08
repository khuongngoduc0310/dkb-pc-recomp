"""Offline preparation of the DKB (NVN 2 / Ampere) shader cache from the runtime's own capture.

The runtime records every (lowered program, answers) pair it gives the translator:

    shader-programs.bin  "TOTKSPRG": stage, length, c1 length, code hash, 0x880 control, code, c1
    shader-answers.bin   "TOTKSANS": stage, code hash, input id, 148-byte answers, priority

The programs are exactly nvn2_lower's output, so they need no second lowering implementation. This tool copies the
records into a private workspace, asks dkb.exe --seed-dkb-shaders to write the seed files gfx_shader_prepare_seed reads,
and lets the existing --prepare-shaders translate and warm them. A native DKB_SHADER_DUMP folder is optional: the seed
step lowers every native stage through nvn2_lower and reports which recorded programs it still reproduces (coverage and
lowering drift), without dropping recorded variants from the seed.

It never writes the build's shader cache or its records: pass --build for the source and --work for a directory under
build/. Verification re-derives code hash, use mask, input id, cache key, the stored input and the reflection fields
without trusting the runtime, and runs spirv-val on the prepared modules.

    .venv\\Scripts\\python tools\\prepare_dkb_shaders.py --build build\\dkb100 --work build\\dkb-prep\\corpus
    .venv\\Scripts\\python tools\\prepare_dkb_shaders.py --build build\\dkb100 --work build\\dkb-prep\\one --one <64 hex>
    ... --dump build\\dkb-prep\\dump --run-seconds 60 [--trace <64 hex>] [--pack]

Game data and every derived file stay in the ignored build/ tree. Nothing here is safe to commit or share.
"""
import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTROL = 0x880
PRG_MAGIC = b'TOTKSPRG'
ANS_MAGIC = b'TOTKSANS'
PRP_MAGIC = b'TOTKSPRP'
USE_MAGIC = 0x4D554B54
USE_VERSION = 2
KEY_VERSION = 5
IN_VERSION = 1
GS_IN_VERSION = 2
ANS_PACKED = 147
GS_ANS_PACKED = 148
PRP_REC = 36
STAGE_NAMES = {0: 'vs', 1: 'tcs', 2: 'tes', 3: 'gs', 4: 'ps', 5: 'cs'}
GFX_STAGE_VS = 0
GFX_STAGE_GS = 3
GFX_STAGE_FS = 4
GFX_STAGE_CS = 5


def u32(data, at):
    return struct.unpack_from('<I', data, at)[0]


def sha(*parts):
    h = hashlib.sha256()
    for p in parts:
        h.update(p)
    return h.digest()


def check_content(path):
    """Refuse an obvious game container even when a caller points --build at one by mistake."""
    try:
        head = Path(path).read_bytes()[:8]
    except OSError:
        return
    if head[:4] in (b'BNSH', b'FSHA', b'SARC', b'BAHS') or head[:4] == b'NSO0':
        sys.exit(f'{path} looks like game data, not a build directory')


def read_header(path, magic):
    data = Path(path).read_bytes()
    if len(data) < 16:
        raise ValueError(f'{path}: shorter than a record header')
    got, version, rec_size = struct.unpack_from('<8sII', data, 0)
    if got != magic:
        raise ValueError(f'{path}: expected {magic.decode()}, found {got!r}')
    return version, rec_size, data[16:]


def program_records(body):
    """Parse whole TOTKSPRG records. A torn tail is ignored (and reported by the caller)."""
    out, at = [], 0
    while at + 44 + CONTROL <= len(body):
        stage, length, c1_len = struct.unpack_from('<III', body, at)
        size = 44 + CONTROL + length + c1_len
        if size > len(body) - at or length > (64 << 20) or c1_len > length:
            break
        code = body[at + 44 + CONTROL:at + 44 + CONTROL + length]
        out.append(dict(off=at, stage=stage, length=length, c1_len=c1_len, hash=body[at + 12:at + 44],
                        control=body[at + 44:at + 44 + CONTROL], code=code,
                        c1=body[at + 44 + CONTROL + length:at + size], raw=body[at:at + size]))
        at += size
    return out, at


def answer_records(body, rec_size):
    out, at = [], 0
    while at + rec_size <= len(body):
        r = body[at:at + rec_size]
        answers = bytearray(r[68:68 + ANS_PACKED])
        if rec_size >= 217:
            answers.append(r[68 + ANS_PACKED])
        else:
            answers.append(0)                              # v1 had no geometry selector
        out.append(dict(off=at, stage=u32(r, 0), hash=r[4:36], iid=r[36:68], answers=bytes(answers),
                        priority=r[rec_size - 1], raw=r))
        at += rec_size
    return out


def code_hash(program, answer=None):
    """gfx_shader.c build_code_hash over a lowered program record."""
    h = hashlib.sha256()
    h.update(struct.pack('<I', program['stage']))
    h.update(program['control'])
    h.update(struct.pack('<Q', program['length']))
    h.update(program['code'])
    h.update(struct.pack('<I', len(program['c1'])))
    h.update(program['c1'])
    if program['stage'] == GFX_STAGE_GS:
        meta = program['control'][0x800:0x808] == b'TKGSMETA'
        input_sel = answer['answers'][ANS_PACKED] if answer else 0
        h.update(b'TKGS')
        h.update(struct.pack('<I', 2 if meta else 1))
        h.update(struct.pack('<I', input_sel))
    return h.digest()


def fmz_code_hash(digest):
    """The code hash gfx_shader.c records for a stage translated with the FMZ rule: sha256("FMZ1" + plain)."""
    return sha(b'FMZ1', digest)


def fmz_stage_set(mode, log):
    """Stages the prepare run translates with the FMZ rule, mirroring gfx_shader.c's selection.

    ``auto`` follows the runtime: no TOTK_FMZ_STAGES value means all vertex stages, off means none, and all means
    every stage. A stage name, sel: list or hash: list selects individual programs by their native NVN 2 origin,
    which the lowered records do not carry, so an offline check cannot reproduce it.
    """
    if mode == 'auto':
        value = os.environ.get('DKB_FMZ_STAGES')
        if value is None:
            value = os.environ.get('TOTK_FMZ_STAGES')
        value = (value or '').strip()
        if not value:
            return {GFX_STAGE_VS}
        low = value.lower()
        if low == 'off':
            return set()
        if low == 'all':
            return {0, 1, 2, 3, 4, 5}
        sys.exit('the FMZ list in TOTK_FMZ_STAGES selects individual stages; pass --fmz vertex or --fmz off to '
                 'check the prepared cache offline, or play-test the narrowed list')
    if mode == 'off':
        return set()
    if mode == 'vertex':
        return {GFX_STAGE_VS}
    return {0, 1, 2, 3, 4, 5}


def masked_answers(answer):
    """gfx_shader.c mask_answers: slots/attrs/fouts outside the mask are zero; local and shared always survive, and
    depth_minus_w is not copied at all (it is not in the input id)."""
    a = answer['answers']
    slots, attrs, fouts = answer['slots'], answer['attrs'], answer['fouts']
    out = bytearray(ANS_PACKED)
    for i in range(32):
        if slots >> i & 1:
            out[i] = a[i]
            out[32 + i] = a[32 + i]
            out[64 + i] = a[64 + i]
        if attrs >> i & 1:
            out[96 + i] = a[96 + i]
    for i in range(8):
        if fouts >> i & 1:
            out[128 + i] = a[128 + i]
    out[136:142] = a[136:142]
    out[142:146] = a[142:146]
    return bytes(out)


def input_id(answer):
    return sha(b'TKSI', struct.pack('<I', IN_VERSION), answer['hash'], masked_answers(answer))


def cache_key(effective, answer):
    return sha(b'TKSK', struct.pack('<I', KEY_VERSION), effective, input_id(answer)).hex()


def use_name(effective, code_digest):
    return sha(b'TKSU', effective, code_digest).hex()


def read_use(cache, effective, code_digest):
    path = cache / (use_name(effective, code_digest) + '.use')
    if not path.is_file():
        return None
    data = path.read_bytes()
    if len(data) != 20:
        return None
    magic, version, slots, attrs, fouts = struct.unpack('<5I', data)
    if magic != USE_MAGIC or version != USE_VERSION:
        return None
    return slots, attrs, fouts


# ---- effective translator identity (gfx_shader.c gfx_shader_set_host / set_geometry_host)

def base_identity(translator):
    exe = Path(translator)
    ident = exe.with_suffix('.id')
    if ident.is_file():
        text = ident.read_text(encoding='ascii', errors='replace').strip()
        if re.fullmatch(r'[0-9A-Fa-f]{64}', text):
            return bytes.fromhex(text)
    if not exe.is_file():
        sys.exit(f'translator {exe} and its .id are missing; pass --translator')
    return hashlib.sha256(exe.read_bytes()).digest()


def effective_identity(translator, log):
    ident = base_identity(translator)
    m = re.search(r'gfx_shader: host options (.*?): own shader cache keys', log)
    if m:
        ident = sha(ident, b'TKHO ' + m.group(1).encode('ascii', 'replace'))
    m = re.search(r'translator subgroup vs/fs/cs/gs (\d+)/(\d+)/(\d+)/(\d+)', log)
    if m and int(m.group(4)) != 32:
        ident = sha(ident, b'TKHG', struct.pack('<I', int(m.group(4))))
    return ident


# ---- record selection / workspace

def write_records(work, programs, answers):
    work.mkdir(parents=True, exist_ok=True)
    pb = struct.pack('<8sII', PRG_MAGIC, 1, 0) + b''.join(p['raw'] for p in programs)
    ab = struct.pack('<8sII', ANS_MAGIC, 2, 217) + b''.join(a['raw'] for a in answers)
    (work / 'shader-programs.bin').write_bytes(pb)
    (work / 'shader-answers.bin').write_bytes(ab)


def copy_records(source, work, one):
    progs_path, ans_path = source / 'shader-programs.bin', source / 'shader-answers.bin'
    check_content(progs_path)
    check_content(ans_path)
    pver, prec, pbody = read_header(progs_path, PRG_MAGIC)
    aver, arec, abody = read_header(ans_path, ANS_MAGIC)
    if pver != 1 or prec != 0:
        sys.exit(f'{progs_path}: unsupported program record version {pver}/{prec}')
    if arec not in (216, 217):
        sys.exit(f'{ans_path}: unsupported answer record size {arec}')
    progs, whole = program_records(pbody)
    answers = answer_records(abody, arec)
    if one:
        one = one.lower()
        programs = [p for p in progs if p['hash'].hex() == one]
        answers = [a for a in answers if a['hash'].hex() == one]
        if not programs:
            sys.exit(f'no program record with code hash {one}')
        if not answers:
            sys.exit(f'program {one} has no answer record')
    else:
        programs, answers = progs, answers
    work.mkdir(parents=True, exist_ok=True)
    pb = struct.pack('<8sII', PRG_MAGIC, 1, 0) + b''.join(p['raw'] for p in programs)
    ab = struct.pack('<8sII', ANS_MAGIC, aver, arec) + b''.join(a['raw'] for a in answers)
    (work / 'shader-programs.bin').write_bytes(pb)
    (work / 'shader-answers.bin').write_bytes(ab)
    # A private copy of the pipeline keys would name stale input ids; do not copy it by default.
    return programs, answers, whole


def normalize_v1_answer(raw):
    return raw[:68 + ANS_PACKED] + b'\x00' + raw[-1:]


def merge_captures(sources, work, one):
    """Merge captures by code hash and input id (the first source wins), writing a v2 workspace.

    A capture from a newer session can be merged with an older corpus so coverage grows without re-lowering or
    retranslating the old records. Input ids are translator-independent while the use masks stay the same; when a
    translator changes them, the new session's records win.
    """
    programs, answers, seen_p, seen_a = [], [], set(), set()
    for source in sources:
        source = Path(source)
        pver, prec, pbody = read_header(source / 'shader-programs.bin', PRG_MAGIC)
        apath = source / 'shader-answers.bin'
        if not apath.is_file():                            # a prepared workspace keeps its full capture here
            apath = source / 'source-answers.bin'
        aver, arec, abody = read_header(apath, ANS_MAGIC)
        if pver != 1 or prec != 0 or arec not in (216, 217):
            sys.exit(f'{source}: unsupported capture format {pver}/{prec}/{arec}')
        progs, _ = program_records(pbody)
        ans = answer_records(abody, arec)
        for p in progs:
            if p['hash'] not in seen_p:
                seen_p.add(p['hash'])
                programs.append(p)
        for a in ans:
            if a['iid'] not in seen_a:
                seen_a.add(a['iid'])
                if arec != 217:
                    a = dict(a, raw=normalize_v1_answer(a['raw']))
                answers.append(a)
    if one:
        one = one.lower()
        programs = [p for p in programs if p['hash'].hex() == one]
        answers = [a for a in answers if a['hash'].hex() == one]
        if not programs or not answers:
            sys.exit(f'{one}: merged capture has no program/answer record')
    write_records(work, programs, answers)
    return programs, answers


def merge_dumps(sources, work):
    """Copy several DKB_SHADER_DUMP folders into one (by file name), so one seed run can verify all of them."""
    out = work / 'dump-merged'
    out.mkdir(parents=True, exist_ok=True)
    seen, copied = set(), 0
    for src in sources:
        src = Path(src)
        for name in os.listdir(src):
            if name in seen or not (name.endswith('.bin') or name.endswith('.ctl')):
                continue
            seen.add(name)
            shutil.copyfile(src / name, out / name)
            copied += 1
    print(f'merged {copied} dump files from {len(sources)} folder(s) into {out}')
    return out


def seed_selected(work):
    """Code hashes in the workspace's seed-programs.bin, or None when the seed step has not run."""
    p = work / 'seed-programs.bin'
    if not p.is_file():
        return None
    ver, rec, body = read_header(p, PRG_MAGIC)
    progs, _ = program_records(body)
    return {x['hash'] for x in progs}


def run_dkb(exe, mode, env, verbose=False):
    cmd = [str(exe), mode]
    if verbose:
        print('+', ' '.join(cmd), flush=True)
        r = subprocess.run(cmd, cwd=str(ROOT), env=env)
        return r.returncode, ''
    r = subprocess.run(cmd, cwd=str(ROOT), env=env, capture_output=True, text=True)
    if r.stdout:
        print(r.stdout, end='' if r.stdout.endswith('\n') else '\n', flush=True)
    if r.returncode and r.stderr:
        print(r.stderr, end='' if r.stderr.endswith('\n') else '\n', flush=True)
    return r.returncode, r.stdout + r.stderr


def work_env(work, dump, extra=None):
    env = dict(os.environ)
    env['TOTK_PIPELINE_CACHE'] = str((work / 'pipeline-cache.bin').resolve())
    env['TOTK_SHADER_CACHE'] = str((work / 'shader-cache').resolve())
    env.pop('DKB_SHADER_DUMP', None)
    env.pop('DKB_SHADER_VERIFIED_ONLY', None)
    if dump:
        env['DKB_SHADER_DUMP'] = str(Path(dump).resolve())
    if extra:
        env.update(extra)
    return env


# ---- cache pack reader (tools/pack_shader_cache.py layout), for a packed --reference

class Pack:
    KINDS = {'.spv': 1, '.res.json': 2, '.use': 3, '.in': 4}

    def __init__(self, path):
        self.path = Path(path)
        self.f = open(self.path, 'rb')
        head = self.f.read(16)
        if len(head) != 16 or head[:8] != b'TKSPACK1':
            raise ValueError(f'{path}: not a cache pack')
        count = struct.unpack_from('<I', head, 8)[0]
        raw = self.f.read(48 * count)
        if len(raw) != 48 * count:
            raise ValueError(f'{path}: truncated index')
        self.ix = {}
        for i in range(count):
            name, kind, size, off = struct.unpack_from('<32sIIQ', raw, 48 * i)
            self.ix[(kind, name)] = (off, size)

    def read(self, kind, name):
        e = self.ix.get((kind, bytes(name)))
        if not e:
            return None
        self.f.seek(e[0])
        data = self.f.read(e[1])
        return data if len(data) == e[1] else None

    def close(self):
        self.f.close()


# ---- preparation manifest (next to the seeds; no game bytes, only identities and counts)

def write_manifest(work, exe, translator, effective, log, dump, verified_only, programs, answers, selected, excluded,
                   prepared_copies):
    base = base_identity(translator)
    host = re.search(r'gfx_shader: host options (.*?): own shader cache keys', log)
    sub = re.search(r'translator subgroup vs/fs/cs/gs (\d+)/(\d+)/(\d+)/(\d+)', log)
    cache = work / 'shader-cache'
    pack = cache / 'shader-cache.pack'
    m = dict(
        version=1,
        created=time.strftime('%Y-%m-%dT%H:%M:%S'),
        exe=str(exe),
        translator=str(translator),
        translator_id=base.hex(),
        effective_id=effective.hex(),
        host_options=host.group(1) if host else None,
        subgroup=('/'.join(sub.groups()) if sub else None),
        dump=str(dump) if dump else None,
        verified_only=bool(verified_only),
        programs=len(programs),
        answers=len(answers),
        selected=len(selected),
        excluded=excluded,
        prepared=prepared_copies,
        cache=str(cache),
        pack=str(pack) if pack.is_file() else None,
        pack_size=pack.stat().st_size if pack.is_file() else None,
    )
    (work / 'dkb-prepare.json').write_text(json.dumps(m, indent=1) + '\n', encoding='utf-8')
    return m


def read_manifest(work):
    p = work / 'dkb-prepare.json'
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return None


def record_pack(work):
    """Update an existing manifest after --verify-only --pack, which does not rewrite the rest of it."""
    m = read_manifest(work)
    if m is None:
        return
    pack = work / 'shader-cache' / 'shader-cache.pack'
    m['pack'] = str(pack) if pack.is_file() else None
    m['pack_size'] = pack.stat().st_size if pack.is_file() else None
    (work / 'dkb-prepare.json').write_text(json.dumps(m, indent=1) + '\n', encoding='utf-8')


def write_launcher(work, exe):
    """A double-clickable launcher that uses this workspace's cache without touching the build's working cache."""
    p = work / 'play.cmd'
    lines = [
        '@echo off',
        'setlocal',
        f'set "TOTK_SHADER_CACHE={work / "shader-cache"}"',
        f'set "TOTK_PIPELINE_CACHE={work / "pipeline-cache.bin"}"',
        f'"{exe}" %*',
    ]
    p.write_text('\r\n'.join(lines) + '\r\n', encoding='ascii', errors='replace')
    return p



# ---- reflection checks

def spirv_masks(spv, stage):
    """Input/Output Location masks from the module, with scalar/vector/array spans (reflect_spirv rules)."""
    if len(spv) < 20 or u32(spv, 0) != 0x07230203:
        raise ValueError('not a SPIR-V module')
    words = struct.unpack('<%dI' % (len(spv) // 4), spv)
    bound = min(words[3], 1 << 20)
    kind = [0] * bound          # 0 unknown, 1 float, 2 int
    comps = [0] * bound
    span = [0] * bound
    elem_span = [0] * bound
    length = [0] * bound
    ptr = [0] * bound
    loc = [0] * bound
    ins = outs = 0
    i = 5
    while i < len(words):
        op, n = words[i] & 0xffff, words[i] >> 16
        if not n or i + n > len(words):
            break
        a = words[i + 1:i + n]
        if op == 71 and len(a) >= 3 and a[1] == 30 and a[0] < bound:      # OpDecorate Location
            loc[a[0]] = a[2] + 1 if a[2] < 32 else 0
        elif op in (21, 22) and len(a) >= 2 and a[0] < bound:            # OpTypeInt / OpTypeFloat
            kind[a[0]] = 1 if op == 22 else 2
            comps[a[0]] = 1
            span[a[0]] = 1
        elif op == 23 and len(a) >= 3 and a[0] < bound and a[1] < bound:  # OpTypeVector
            kind[a[0]] = kind[a[1]]
            comps[a[0]] = a[2]
            span[a[0]] = 1 if 2 <= a[2] <= 4 and span[a[1]] else 0
        elif op == 43 and len(a) >= 3 and a[0] < bound:                  # OpConstant
            value = a[2]
            if value > 32:
                value = 32
            length[a[1]] = value
        elif op == 28 and len(a) >= 3 and a[0] < bound and a[1] < bound:  # OpTypeArray
            kind[a[0]] = kind[a[1]]
            comps[a[0]] = comps[a[1]]
            elem_span[a[0]] = span[a[1]]
            n_elem = length[a[2]] if a[2] < bound else 0
            span[a[0]] = min(32, span[a[1]] * n_elem) if span[a[1]] and n_elem else 0
        elif op == 32 and len(a) >= 3 and a[0] < bound:                  # OpTypePointer
            ptr[a[0]] = a[2]
        elif op == 59 and len(a) >= 3:                                    # OpVariable
            type_id, var, sc = a[0], a[1], a[2]
            if sc in (1, 3) and var < bound and loc[var] and type_id < bound:
                t = ptr[type_id] if ptr[type_id] < bound else 0
                slots = span[t]
                if stage == GFX_STAGE_GS and sc == 1 and elem_span[t]:
                    slots = elem_span[t]
                l = loc[var] - 1
                for j in range(slots or 1):
                    if l + j >= 32:
                        break
                    if sc == 1:
                        ins |= 1 << (l + j)
                    else:
                        outs |= 1 << (l + j)
        i += n
    return ins, outs


def check_res_json(text, program, answer):
    problems = []
    try:
        res = json.loads(text)
    except json.JSONDecodeError as e:
        return [f'res.json is not JSON: {e}']
    if res.get('stage') != STAGE_NAMES.get(program['stage']):
        problems.append(f"res.json stage {res.get('stage')!r} != {STAGE_NAMES.get(program['stage'])!r}")
    slots = sum(1 << int(x) for x in res.get('tex_queried', [])) | sum(1 << int(x) for x in res.get('img_queried', []))
    if slots != answer['slots']:
        problems.append(f'res.json queried slots {slots:#x} != use mask {answer["slots"]:#x}')
    lay = program['control'][0x860:0x874]
    if lay[:4] == b'DKC1':
        if res.get('c0_version') != 2:
            problems.append('DKC1 control without c0_version 2 in reflection')
        c0 = [lay[4], lay[5], lay[6], lay[7]]
        if res.get('c0') != c0:
            problems.append(f'res.json c0 {res.get("c0")} != control {c0}')
        roots = list(lay[12:20])
        if res.get('c0_roots') != roots:
            problems.append(f'res.json c0_roots {res.get("c0_roots")} != control {roots}')
        if res.get('storage_candidates_version') != 1:
            problems.append('DKC1 control without storage_candidates_version 1')
    if program['stage'] == GFX_STAGE_GS:
        meta = program['control'][0x800:0x808] == b'TKGSMETA'
        if res.get('geometry_version') != (2 if meta else 1):
            problems.append(f"res.json geometry_version {res.get('geometry_version')} != {2 if meta else 1}")
        if answer['answers'][ANS_PACKED] and res.get('gs_input') not in (
                'points', 'lines', 'lines_adjacency', 'triangles', 'triangles_adjacency'):
            problems.append('res.json gs_input missing')
    return problems


def check_stored_input(path, program, answer):
    data = path.read_bytes()
    if len(data) < 24:
        return ['stored input shorter than its header']
    magic, version, stage, length, c1_len, answers_size = struct.unpack_from('<6I', data, 0)
    gs = program['stage'] == GFX_STAGE_GS
    want_version = GS_IN_VERSION if gs else IN_VERSION
    want_answers = GS_ANS_PACKED if gs else 152
    if magic != 0x49534B54 or version != want_version or stage != program['stage']:
        return ['stored input header mismatch']
    if length != program['length'] or c1_len != len(program['c1']) or answers_size != want_answers:
        return ['stored input bounds mismatch']
    if len(data) != 24 + answers_size + CONTROL + length + c1_len:
        return ['stored input size mismatch']
    at = 24 + answers_size
    if data[at:at + CONTROL] != program['control']:
        return ['stored input control differs from the program record']
    if data[at + CONTROL:at + CONTROL + length] != program['code']:
        return ['stored input code differs from the program record']
    if data[at + CONTROL + length:] != program['c1']:
        return ['stored input c1 differs from the program record']
    return []


def spirv_val(path, tool):
    r = subprocess.run([str(tool), '--target-env', 'vulkan1.3', str(path)], capture_output=True, text=True)
    if r.returncode:
        text = (r.stderr or r.stdout).strip().splitlines()
        return text[0] if text else 'spirv-val failed'
    return None


def find_spirv_val(explicit):
    if explicit:
        return Path(explicit)
    for name in ('spirv-val.exe', 'spirv-val'):
        p = shutil.which(name)
        if p:
            return Path(p)
    for p in (Path(r'C:\VulkanSDK'), Path(r'C:\msys64\ucrt64\bin')):
        if p.is_dir():
            for cand in sorted(p.glob('**/spirv-val.exe'), reverse=True):
                return cand
    return None


# ---- verification

def verify(work, programs, answers, effective, spirv_val_tool, strict_spirv=False, reference=None, fmz_stages=None):
    cache = work / 'shader-cache'
    fmz_stages = fmz_stages or set()
    packed = cache / 'shader-cache.pack'
    if packed.is_file():
        return {'error': f'{packed} is packed; verify before running tools/pack_shader_cache.py'}
    ref_pack = None
    if reference:
        rp = Path(reference) / 'shader-cache.pack'
        if rp.is_file():
            try:
                ref_pack = Pack(rp)
            except (OSError, ValueError):
                ref_pack = None
    prepared = {}
    prp = work / 'shader-prepared.bin'
    if prp.is_file():
        data = prp.read_bytes()
        if len(data) >= 16 and data[:8] == PRP_MAGIC:
            rec = struct.unpack_from('<I', data, 12)[0] or PRP_REC
            for at in range(16, len(data) - rec + 1, rec):
                prepared[data[at + 4:at + 36]] = data[at]
    problems, warnings, spv_jobs = [], [], []
    ref_match = ref_diff = ref_missing = 0
    ref_problems = []
    checked = 0
    for program in programs:
        matches = [a for a in answers if a['hash'] == program['hash']]
        if not matches:
            problems.append(f'{program["hash"].hex()[:16]}: no answer record')
            continue
        plain_hash = code_hash(program, matches[0])
        calc_hash = fmz_code_hash(plain_hash) if program['stage'] in fmz_stages else plain_hash
        if program['stage'] != GFX_STAGE_GS and calc_hash != program['hash'] and plain_hash != program['hash']:
            problems.append(f'{program["hash"].hex()[:16]}: stored code hash does not match the program bytes')
        for answer in matches:
            answer = dict(answer)
            answer['hash'] = calc_hash
            mask = read_use(cache, effective, calc_hash)
            if not mask:
                problems.append(f'{calc_hash.hex()[:16]}: no use mask under the current translator')
                continue
            answer['slots'], answer['attrs'], answer['fouts'] = mask
            iid = input_id(answer)
            key = cache_key(effective, answer)
            missing = [n for n in (key + '.spv', key + '.res.json') if not (cache / n).is_file()]
            if missing:
                problems.append(f'{calc_hash.hex()[:16]}: missing {", ".join(missing)}')
            stored = cache / 'in' / (iid.hex() + '.in')
            if not stored.is_file():
                problems.append(f'{calc_hash.hex()[:16]}: missing in/{iid.hex()[:16]}.in')
            else:
                problems += [f'{calc_hash.hex()[:16]}: {p}' for p in check_stored_input(stored, program, answer)]
            if iid not in prepared:
                problems.append(f'{calc_hash.hex()[:16]}: shader-prepared.bin has no record for the resolved input')
            spv = cache / (key + '.spv')
            res = cache / (key + '.res.json')
            if spv.is_file() and res.is_file():
                problems += [f'{calc_hash.hex()[:16]}: {p}' for p in check_res_json(
                    res.read_text(encoding='utf-8', errors='replace'), program, answer)]
                try:
                    ins, outs = spirv_masks(spv.read_bytes(), program['stage'])
                    if program['stage'] == GFX_STAGE_VS:
                        msg = f'{calc_hash.hex()[:16]}: SPIR-V inputs {ins:#x} != use mask {answer["attrs"]:#x}'
                        if ins != answer['attrs']:
                            (problems if strict_spirv else warnings).append(msg)
                    elif program['stage'] == GFX_STAGE_FS:
                        msg = f'{calc_hash.hex()[:16]}: SPIR-V outputs {outs & 0xff:#x} != use mask {answer["fouts"]:#x}'
                        if (outs & 0xff) != answer['fouts']:
                            (problems if strict_spirv else warnings).append(msg)
                    elif program['stage'] == GFX_STAGE_GS and ins & ~answer['attrs']:
                        # A lowered native passthrough body may read or declare more inputs than it forwards; the
                        # runtime admits that through REFLECT_LOWERED_BODY (the control is TKGSMETA with the
                        # lowered kind at 0x810). Its key, input id and stored input are still checked below.
                        # An ordinary GS keeps the warning.
                        lowered = (program['control'][0x800:0x808] == b'TKGSMETA' and
                                   struct.unpack_from('<I', program['control'], 0x808)[0] == 1 and
                                   struct.unpack_from('<I', program['control'], 0x810)[0] == 3)
                        if not lowered:
                            warnings.append(f'{calc_hash.hex()[:16]}: SPIR-V inputs outside the use mask')
                except ValueError as e:
                    problems.append(f'{calc_hash.hex()[:16]}: {e}')
                spv_jobs.append((spv, calc_hash.hex()[:16]))
            if reference:
                ref = Path(reference)
                for name, local, kind in ((key + '.spv', spv, 1), (key + '.res.json', res, 2),
                                          ('in/' + iid.hex() + '.in', stored, 4)):
                    data = None
                    if ref_pack:
                        data = ref_pack.read(kind, iid if kind == 4 else bytes.fromhex(key))
                    if data is None:
                        other = ref / name
                        data = other.read_bytes() if other.is_file() else None
                    if data is None or not local.is_file():
                        ref_missing += 1
                    elif data == local.read_bytes():
                        ref_match += 1
                    else:
                        ref_diff += 1
                        ref_problems.append(f'{calc_hash.hex()[:16]}: {name} differs from the reference cache')
            checked += 1
    bad_spv = {}
    if spirv_val_tool and spv_jobs:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(16, len(spv_jobs))) as pool:
            for (path, name), err in zip(spv_jobs, pool.map(lambda j: spirv_val(j[0], spirv_val_tool), spv_jobs)):
                if err:
                    bad_spv[name] = err
    if ref_pack:
        ref_pack.close()
    return dict(effective=effective.hex()[:16], programs=len(programs), answers=len(answers), checked=checked,
                prepared=len(prepared), spirv_val_failures=len(bad_spv),
                spirv_val_examples=dict(list(bad_spv.items())[:3]), problems=problems[:40],
                problem_count=len(problems), warnings=warnings[:20], warning_count=len(warnings),
                reference_matches=ref_match, reference_differences=ref_diff, reference_missing=ref_missing,
                reference_problems=ref_problems[:10])


# ---- runs / metrics

def latest_log(work):
    for p in (work / 'logs' / 'totk.log', work / 'stderr.txt'):
        if p.is_file():
            return p.read_text(encoding='utf-8', errors='replace')
    return ''


def metrics(log):
    def last(pattern, cast=float, default=None):
        found = re.findall(pattern, log)
        return cast(found[-1]) if found else default
    return dict(
        guest_start_ms=last(r'guest starts (\d+) ms', int),
        prewarm_wait_ms=last(r'\((\d+) ms of it waiting for the prewarm\)', int),
        translated=last(r'shaders: \d+ keys, (\d+) translated', int),
        prewarm_translated=last(r'prewarm: .*?(\d+) translated,', int),
        xlate_blocked_pct=last(r'shader xlate (\d+)%', int),
        slowest_xlate_ms=last(r'slowest frame: xlate ([\d.]+) ms', float),
        mean_fps=last(r'frametime: exit: \d+ frames in [\d.]+ s, mean ([\d.]+) fps', float),
        low1_fps=last(r'1% low ([\d.]+) fps', float),
    )


def run_game(exe, work, seconds, profile_source, trace=None, verbose=False, extra_env=None):
    profile = work / 'profile'
    if profile_source and Path(profile_source).is_dir():
        if profile.exists():
            shutil.rmtree(profile)
        shutil.copytree(profile_source, profile)
    env = work_env(work, None)
    env['TOTK_PROFILE_DIR'] = str(profile.resolve())
    env['TOTK_TRACE'] = 'perf'
    if trace:
        env['DKB_SHADER_TRACE'] = trace
    if extra_env:
        env.update(extra_env)
    stderr_path = work / 'stderr.txt'
    with open(stderr_path, 'wb') as err:
        cmd = [str(exe), '--seconds', str(seconds)]
        if verbose:
            print('+', ' '.join(cmd), flush=True)
        r = subprocess.run(cmd, cwd=str(ROOT), env=env, stderr=err)
    log = latest_log(work)
    result = metrics(log)
    result['exit'] = r.returncode
    result['log'] = str(work / 'logs' / 'totk.log' if (work / 'logs' / 'totk.log').is_file() else stderr_path)
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--build', type=Path, default=ROOT / 'build' / 'dkb100', help='build with dkb.exe and the capture')
    ap.add_argument('--work', type=Path, required=True, help='private workspace under build/ (records, seeds, cache)')
    ap.add_argument('--records', type=Path, help='capture directory (default: --build)')
    ap.add_argument('--merge-captures', nargs='+', type=Path,
                    help='merge these capture directories by code hash and input id (first wins); instead of --records')
    ap.add_argument('--translator', type=Path, default=ROOT / 'build' / 'xlate' /
                    ('totk_xlate.exe' if os.name == 'nt' else 'totk_xlate'),
                    help='translator whose identity keys the cache (default: build/xlate)')
    ap.add_argument('--one', help='only this 64-hex code hash (the proof-of-concept variant)')
    ap.add_argument('--dump', type=Path, nargs='+',
                    help='one or more DKB_SHADER_DUMP folders to verify the native lowering against')
    ap.add_argument('--verified-only', action='store_true',
                    help='prepare only records the dump reproduces byte for byte (requires --dump)')
    ap.add_argument('--seed-only', action='store_true', help='write seeds, do not translate')
    ap.add_argument('--verify-only', action='store_true', help='verify the workspace, do not seed or translate')
    ap.add_argument('--run-only', action='store_true',
                    help='use the existing workspace, skip seeding/translation/verification (for --run-seconds)')
    ap.add_argument('--no-verify', action='store_true', help='skip verification (for a packed workspace or --run-only)')
    ap.add_argument('--pack', action='store_true', help='fold the verified cache with tools/pack_shader_cache.py')
    ap.add_argument('--fmz', choices=('auto', 'off', 'vertex', 'all'), default='auto',
                    help='FMZ stages the translation used (default: auto, the runtime default of all vertex stages)')
    ap.add_argument('--spirv-val', help='spirv-val executable (default: PATH or the Vulkan SDK)')
    ap.add_argument('--reference', type=Path,
                    help='a runtime shader-cache directory (loose or packed) to compare prepared entries against')
    ap.add_argument('--strict-spirv', action='store_true', help='also require VS input masks to equal the use mask')
    ap.add_argument('--run-seconds', type=float, help='run the game afterwards and print the cold/warm metrics')
    ap.add_argument('--profile-source', type=Path, default=ROOT / 'build' / 'profile-run-latest')
    ap.add_argument('--trace', help='DKB_SHADER_TRACE code hash for the gameplay run')
    ap.add_argument('--run-env', action='append', default=[], metavar='NAME=VALUE',
                    help='extra environment variable for --run-seconds (repeatable; e.g. TOTK_LIB_PREBUILD=0)')
    ap.add_argument('--no-launcher', action='store_true', help='do not write play.cmd into the workspace')
    ap.add_argument('--coverage', action='store_true',
                    help='report selected/excluded records by stage from an existing workspace, then exit')
    ap.add_argument('--verbose', action='store_true')
    args = ap.parse_args()
    if args.run_only:
        if args.pack or args.seed_only:
            sys.exit('--run-only cannot combine with --pack or --seed-only')
        args.verify_only = True
        args.no_verify = True

    build = args.build.resolve()
    work = args.work.resolve()
    if ROOT not in work.parents and work != ROOT:
        sys.exit(f'--work must be under the repository (got {work})')
    if args.coverage:
        work.mkdir(parents=True, exist_ok=True)
        progs, _ = program_records(read_header(work / 'shader-programs.bin', PRG_MAGIC)[2])
        sp = work / 'source-answers.bin'
        apath = sp if sp.is_file() else work / 'shader-answers.bin'
        _, arec, abody = read_header(apath, ANS_MAGIC)
        answers = answer_records(abody, arec)
        has_answers = {a['hash'] for a in answers}
        seed = seed_selected(work) or set()
        names = {GFX_STAGE_VS: 'vertex', GFX_STAGE_GS: 'geometry', GFX_STAGE_FS: 'fragment', GFX_STAGE_CS: 'compute'}
        by = {names[i]: {'selected': 0, 'excluded': 0} for i in range(6) if i in names}
        excluded, without = [], 0
        for p in progs:
            if p['hash'] not in has_answers:
                without += 1
                continue
            slot = by.setdefault(names.get(p['stage'], str(p['stage'])), {'selected': 0, 'excluded': 0})
            if p['hash'] in seed:
                slot['selected'] += 1
            else:
                slot['excluded'] += 1
                excluded.append(dict(hash=p['hash'].hex(), stage=names.get(p['stage'], str(p['stage'])),
                                     length=p['length']))
        report = dict(work=str(work), programs=len(progs), answers=len(answers), selected=len(seed),
                      unique_code_hashes=len({p['hash'] for p in progs}), by_stage=by,
                      without_answers=without, excluded=excluded)
        (work / 'coverage.json').write_text(json.dumps(report, indent=1), encoding='utf-8')
        summary = {k: v for k, v in report.items() if k != 'excluded'}
        summary['excluded'] = len(excluded)
        summary['coverage'] = str(work / 'coverage.json')
        print(json.dumps(summary, indent=1))
        return 0
    exe = build / ('dkb.exe' if os.name == 'nt' else 'dkb')
    if not exe.is_file():
        sys.exit(f'{exe} is missing; build first')
    source = (args.records or build).resolve()
    if work == source:
        sys.exit('--work must differ from the record source; the capture is never modified')
    work.mkdir(parents=True, exist_ok=True)
    dump_dir = merge_dumps(args.dump, work) if args.dump and len(args.dump) > 1 else (args.dump[0] if args.dump else None)
    if args.verified_only and not dump_dir:
        sys.exit('--verified-only needs --dump')
    if args.pack and args.no_verify:
        sys.exit('--pack needs verification; drop --no-verify or run --verify-only first')

    log = ''
    programs, answers = [], []
    all_programs = 0
    if not args.verify_only:
        if args.merge_captures:
            programs, answers = merge_captures([Path(p).resolve() for p in args.merge_captures], work, args.one)
        else:
            programs, answers, whole = copy_records(source, work, args.one)
            if whole != len(read_header(source / 'shader-programs.bin', PRG_MAGIC)[2]):
                print(f'note: the source program file has a torn tail; {len(programs)} whole records copied')
        all_programs = len(programs)
        extra = {'DKB_SHADER_VERIFIED_ONLY': '1'} if args.verified_only else None
        env = work_env(work, dump_dir, extra)
        rc, out = run_dkb(exe, '--seed-dkb-shaders', env, args.verbose)
        log += out
        (work / 'prep.log').write_text(log, encoding='utf-8', errors='replace')
        if rc:
            sys.exit(f'seed-dkb-shaders failed with {rc}')
        sel = seed_selected(work)
        if sel is not None:
            programs = [p for p in programs if p['hash'] in sel]
            answers = [a for a in answers if a['hash'] in sel]
        if args.seed_only:
            print(json.dumps(dict(work=str(work), selected=len(programs), answers=len(answers),
                                  excluded=all_programs - len(programs)), indent=1))
            return 0
        ans = work / 'shader-answers.bin'
        if ans.is_file():
            ans.replace(work / 'source-answers.bin')
        rc, out = run_dkb(exe, '--prepare-shaders', env, args.verbose)
        log += out
        (work / 'prep.log').write_text(log, encoding='utf-8', errors='replace')
        if rc:
            sys.exit(f'prepare-shaders failed with {rc}')
    else:
        programs, _ = program_records(read_header(work / 'shader-programs.bin', PRG_MAGIC)[2])
        all_programs = len(programs)
        sp = work / 'source-answers.bin'
        apath = sp if sp.is_file() else work / 'shader-answers.bin'
        aver, arec, abody = read_header(apath, ANS_MAGIC)
        answers = answer_records(abody, arec)
        sel = seed_selected(work)
        if sel is not None:
            programs = [p for p in programs if p['hash'] in sel]
            answers = [a for a in answers if a['hash'] in sel]
        logpath = work / 'prep.log'
        log = logpath.read_text(encoding='utf-8', errors='replace') if logpath.is_file() else ''

    excluded = max(0, all_programs - len(programs))
    fmz = fmz_stage_set(args.fmz, log)
    manifest = read_manifest(work)
    if manifest and manifest.get('effective_id'):
        effective = bytes.fromhex(manifest['effective_id'])
        base_now = base_identity(args.translator.resolve())
        if manifest.get('translator_id') and manifest['translator_id'] != base_now.hex():
            print('warning: the workspace was prepared with another translator identity; keys will not match')
    else:
        effective = effective_identity(args.translator.resolve(), log)
    if args.no_verify:
        report = dict(skipped=True, work=str(work), programs=len(programs), answers=len(answers),
                      excluded=excluded, effective=effective.hex()[:16])
    else:
        tool = find_spirv_val(args.spirv_val)
        report = verify(work, programs, answers, effective, tool, args.strict_spirv,
                        args.reference.resolve() if args.reference else None, fmz)
        report['work'] = str(work)
        report['spirv_val'] = str(tool) if tool else None
        report['translator'] = str(args.translator.resolve())
        report['excluded'] = excluded
        if args.reference:
            report['reference'] = str(args.reference.resolve())
    print(json.dumps(report, indent=1))
    failed = 0 if args.no_verify else (
        report.get('problem_count', 0) or report.get('spirv_val_failures', 0) or
        report.get('reference_differences', 0) or report.get('error'))
    if args.pack and not failed:
        r = subprocess.run([sys.executable, str(ROOT / 'tools' / 'pack_shader_cache.py'), str(work / 'shader-cache')],
                           cwd=str(ROOT))
        if r.returncode:
            sys.exit('pack_shader_cache.py failed')
        print('packed', work / 'shader-cache' / 'shader-cache.pack')
        record_pack(work)
    prepared = 0
    prp = work / 'shader-prepared.bin'
    if prp.is_file():
        data = prp.read_bytes()
        if len(data) >= 16 and data[:8] == PRP_MAGIC:
            rec = struct.unpack_from('<I', data, 12)[0] or PRP_REC
            prepared = (len(data) - 16) // rec
    write = not args.verify_only and not args.run_only
    if write:
        write_manifest(work, exe, args.translator.resolve(), effective, log, dump_dir, args.verified_only,
                       programs, answers, programs, excluded, prepared)
    if not args.no_launcher and not args.seed_only:
        print('launcher', write_launcher(work, exe))
    if args.run_seconds:
        extra = {}
        for item in args.run_env:
            if '=' not in item:
                sys.exit(f'--run-env wants NAME=VALUE, got {item!r}')
            k, v = item.split('=', 1)
            extra[k] = v
        result = run_game(exe, work, args.run_seconds, args.profile_source, args.trace, args.verbose, extra)
        print(json.dumps(result, indent=1))
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
