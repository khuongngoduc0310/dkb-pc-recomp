"""Original CPU tests of the real shader service TU, hashes, files and parsers.

No GPU or loader. The C fixture includes an unmodified source/header snapshot,
links the real SHA256 and platform file/lock code, blocks translator children and
background threads, and makes preparation workers use their synchronous fallback.
One opt-in provider fails exactly the targeted owned v1-to-v2 migration write;
all other atomic writes delegate to the real platform implementation.
--source-revision runs the same expectations against an older source snapshot.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
import unittest

import geometry_shader_fixture as original

ROOT = Path(__file__).resolve().parents[1]
NAMES = ('points', 'lines', 'lines_adjacency', 'triangles', 'triangles_adjacency')
# Captured from the original fixture on unmodified 840ddb5, not game data. The
# independent hashlib/wire oracle below additionally documents the vector bytes.
LEGACY = {
    0: ('6ac9e5e5fb6b7d12c61b1873795ec36dda86092d1bd1ee797a5f963b2a266dea',
        'bca70f36e6964422738eb8e833da0989d7ec888adeb77257bf7ceb6243ee3a82',
        'd7889500875d1e84f5383f7519d0f77c9205121361fe7c9506f912c967872150',
        '7cf55aba4fd55d57cf55a36d791e8a65fe01892893122370ae154bb8fc63b6a1'),
    4: ('6d05e9686ee2f9e93df8c8c084c61d1ef5ab7fda70e088caaa65db87d02c8c74',
        '8ffc3071b3a58b5bc529475161b73621cb9675ff388fc565744ceba7ca038e4b',
        'a573e40c24c25fe0fed26380732074d23c90a02d30c5809ef44b476e7795c71e',
        '9a1452bf3b5e266fe454de31b26785c2f535c7b6382440212d3898b0a3f87e57'),
    5: ('2faf37fb1a25c83c42263672b9d291a9b7be38ff4a4b68613127114884475d32',
        'bafb52d408a21ff8c3c511e42f7821b8a87e62fc45c42f71756ea04cb0e5880e',
        '60808388abce834ba51f32ef2410f701ef7bf645ef9a30ae65cafbecbcd0744b',
        'fbdd5e4d02c39f59572de9113890534a98eaec3392889255725be9a8354e4a19'),
}

# Upstream #42 intentionally moved output keys to namespace 5 for guarded storage.
# Code hashes, input IDs and answer bytes above stay fixed. These namespace-5
# keys are independently derived from the documented TKSK/TKSP wire protocol;
# both upstream-only baselines are checked, rather than accepting arbitrary new versions.
LEGACY_KEYS_V5 = {
    0: ('ab75ed28a9981e15679a0f640b47e917418e286177fa2ce09603f4a4f19d6db6',
        '48ec83659d4de92333074197855defc49dcf0535fbbbbb35c6424924b6c70ddf'),
    4: ('f497e7660d9376c1ad2782d78acbe37de2c84820effc838040831092c1be52c1',
        'daa682de03417f197c87a4d773fa14a54e89de31bae827756799eae15d4fa61f'),
    5: ('15ca93b36c99c0af313d54bdfa5fc4c9fa4710b22c933f6a4b700e651c899918',
        '66529ec9f94c50e509e0fcd6a4dd2ce81c2565476c9d0ca5d42cf0fb86f5cefe'),
}


class GeometryCache(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        args.out.mkdir(parents=True, exist_ok=False)
        cls.source_dir = args.out / 'source'
        cls.source_dir.mkdir()
        for name in ('gfx_shader.c', 'gfx.h'):
            path = ROOT / 'runtime' / name
            data = (subprocess.check_output(['git', 'show', args.source_revision + ':runtime/' + name], cwd=ROOT)
                    if args.source_revision else path.read_bytes())
            (cls.source_dir / name).write_bytes(data)
        # The new pure writer is a baseline fixture adapter only: it authors the
        # same original input bytes while --source-revision keeps the shader TU old.
        (cls.source_dir / 'nvn_geometry_control.h').write_bytes((ROOT / 'runtime/nvn_geometry_control.h').read_bytes())
        production = (cls.source_dir / 'gfx_shader.c').read_text()
        header = (cls.source_dir / 'gfx.h').read_text()
        parameters = re.search(r'static int build_code_hash\(([^{};]+)\)\s*\{', production).group(1)
        names = [re.findall(r'\w+', p.split('[')[0])[-1] for p in parameters.split(',')]
        mapping = dict(stage='stage', control='control', code='code', code_size='size', c1='c1',
                       c1_len='c1_size', geometry_input='input', hash='hash')
        call = 'build_code_hash(' + ','.join(mapping[n] for n in names) + ')'
        source = (ROOT / 'tests/geometry_shader_cache.c').read_text()
        source = source.replace('@SHADER_SOURCE@', (cls.source_dir / 'gfx_shader.c').resolve().as_posix())
        source = source.replace('@HASH_CALL@', call)
        source = '#define FIXTURE_GEOMETRY ' + str(int('GFX_GEOMETRY_VERSION' in header)) + '\n' + source
        generated = args.out / 'fixture.c'
        generated.write_text(source)
        cls.exe = args.out / ('fixture.exe' if os.name == 'nt' else 'fixture')
        runtime = ROOT / 'runtime'
        platform = 'win32.c' if os.name == 'nt' else 'linux.c'
        libraries = ['-lbcrypt', '-lpdh', '-lpsapi', '-lrstrtmgr', '-lshell32', '-lwinmm'] if os.name == 'nt' else ['-ldl', '-pthread']
        command = [args.cc, '-std=gnu11', '-' + args.opt, '-ffunction-sections', '-fdata-sections',
                   '-I', str(runtime), str(generated), str(runtime / 'sha256.c'),
                   str(runtime / 'platform' / platform), str(runtime / 'platform/common.c'),
                   '-Wl,--gc-sections', *libraries, '-o', str(cls.exe)]
        compiled = subprocess.run(command, capture_output=True, text=True, timeout=90)
        (args.out / 'compile.log').write_text(compiled.stdout + compiled.stderr)
        if compiled.returncode:
            raise AssertionError(compiled.stdout + compiled.stderr)
        cls.cache = args.out / 'cache'
        cls.cache.mkdir()
        cls.control = args.out / 'identity-control.bin'
        cls.control.write_bytes(bytes(i % 251 for i in range(0x880)))
        cls.code = args.out / 'identity-code.bin'
        cls.code.write_bytes(bytes(range(64)))
        cls.modules = args.out / 'modules'
        cls.modules.mkdir()
        jobs = []
        for topology in NAMES:
            path = cls.modules / (topology + '.code')
            path.write_bytes(original.program())
            jobs.append(dict(key=topology, stage='gs', code=str(path.resolve()), c1='', debug=False,
                             geometry_version=1, gs_input=topology))
        for key, stage in (('vertex', 'vs'), ('fragment', 'ps'), ('compute', 'cs')):
            path = cls.modules / (key + '.code')
            path.write_bytes(original.framed([original.EXIT]) if stage == 'cs' else original.program(stage=stage))
            jobs.append(dict(key=key, stage=stage, code=str(path.resolve()), c1='', debug=False, local=[1, 1, 1]))
        run = subprocess.run([args.translator, '--serve'],
            input=''.join(json.dumps(dict(job=j, out=str(cls.modules.resolve()))) + '\n' for j in jobs),
            capture_output=True, text=True, timeout=60)
        (args.out / 'translator.log').write_text(run.stdout + run.stderr)
        if run.returncode or run.stdout.splitlines() != ['ok ' + j['key'] for j in jobs]:
            raise AssertionError(run.stdout + run.stderr)
        for job in jobs:
            subprocess.run([args.spirv_val, '--target-env', 'vulkan1.3', str(cls.modules / (job['key'] + '.spv'))],
                           check=True, timeout=15, capture_output=True)
        spec = importlib.util.spec_from_file_location('pack_shader_cache', ROOT / 'tools/pack_shader_cache.py')
        cls.packer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.packer)

    def run_fixture(self, mode, *parameters, cache=None, subgroup=32):
        directory = cache or self.cache
        self.assertTrue(directory.resolve().is_relative_to(args.out.resolve()))
        directory.mkdir(parents=True, exist_ok=True)
        command = [str(self.exe.resolve()), mode, str(directory.resolve()), *(str(p) for p in parameters)]
        env = {k: v for k, v in os.environ.items() if k.upper() in {
            'SYSTEMROOT', 'WINDIR', 'PATH', 'TEMP', 'TMP', 'USERPROFILE', 'APPDATA', 'LOCALAPPDATA', 'SYSTEMDRIVE'}}
        env['FIXTURE_GS_SUBGROUP'] = str(subgroup)
        run = subprocess.run(command, capture_output=True, text=True, timeout=15, env=env)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        return json.loads(run.stdout.splitlines()[-1])

    def identity(self, stage, topology=0, cache=None):
        return self.run_fixture('identity', stage, topology, self.code.resolve(), self.control.resolve(), cache=cache)

    def test_distinct_geometry_code_use_final_and_provisional_namespaces(self):
        cache = args.out / 'namespaces'
        records = [self.identity(3, topology, cache) for topology in range(1, 6)]
        for field in ('hash', 'use', 'iid', 'key', 'provisional'):
            with self.subTest(field=field):
                self.assertEqual(len({r[field] for r in records}), 5)
        self.assertEqual([r['known'] for r in records], [0] * 5)
        self.run_fixture('learn', records[0]['hash'], cache=cache)
        self.assertEqual([self.identity(3, topology, cache)['known'] for topology in range(1, 6)], [1, 0, 0, 0, 0])

    def test_trace_logs_once_per_target(self):
        cache = args.out / 'trace-cache'
        cache.mkdir(parents=True, exist_ok=True)
        target = '42' * 32
        env = {k: v for k, v in os.environ.items() if k.upper() in {
            'SYSTEMROOT', 'WINDIR', 'PATH', 'TEMP', 'TMP', 'USERPROFILE', 'APPDATA', 'LOCALAPPDATA', 'SYSTEMDRIVE'}}
        env['DKB_SHADER_TRACE'] = target
        run = subprocess.run([str(self.exe.resolve()), 'trace', str(cache.resolve()), target],
                             capture_output=True, text=True, timeout=15, env=env)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertEqual(run.stderr.count('trace 4242'), 1, run.stderr)
        self.assertIn('memory hit', run.stderr)
        self.assertNotIn('translated', run.stderr)
        env.pop('DKB_SHADER_TRACE')
        quiet = subprocess.run([str(self.exe.resolve()), 'trace', str(cache.resolve()), target],
                               capture_output=True, text=True, timeout=15, env=env)
        self.assertEqual(quiet.returncode, 0, quiet.stdout + quiet.stderr)
        self.assertNotIn('gfx_shader: trace', quiet.stderr)

    def test_legacy_hash_input_id_and_answer_wire_vectors(self):
        answers = (bytes(i % 10 + 1 for i in range(32)) * 2 + bytes(i % 50 + 1 for i in range(32)) +
                   bytes(i % 3 for i in range(32)) + bytes(i % 3 for i in range(8)) +
                   struct.pack('<3HIB', 4, 2, 1, 0x11223344, 1))
        self.assertEqual(len(answers), 147)
        masked = bytearray(147)
        for i in (0, 3):
            for start in (0, 32, 64):
                masked[start + i] = answers[start + i]
        for i in (0, 5):
            masked[96 + i] = answers[96 + i]
        masked[128:130] = answers[128:130]
        masked[136:146] = answers[136:146]
        for stage in (0, 4, 5):
            with self.subTest(stage=stage):
                result = self.identity(stage)
                version = result['key_version']
                self.assertIn(version, (4, 5), 'an upstream namespace change needs explicit new golden vectors')
                data = struct.pack('<I', stage) + self.control.read_bytes() + struct.pack('<Q', 64) + self.code.read_bytes()
                data += struct.pack('<I', 8) + bytes.fromhex('123456789abcdef0')
                code_hash = hashlib.sha256(data).digest()
                iid = hashlib.sha256(b'TKSI' + struct.pack('<I', 1) + code_hash + masked).digest()
                key = hashlib.sha256(b'TKSK' + struct.pack('<I', version) + bytes(range(0x80, 0xa0)) + iid).hexdigest()
                provisional = hashlib.sha256(b'TKSP' + struct.pack('<I', version) + bytes(range(0x80, 0xa0)) + code_hash + answers).hexdigest()
                self.assertEqual(result['answers'], answers.hex())
                self.assertEqual((result['hash'], result['iid'], result['key'], result['provisional']),
                                 (code_hash.hex(), iid.hex(), key, provisional))
                frozen = LEGACY[stage] if version == 4 else LEGACY[stage][:2] + LEGACY_KEYS_V5[stage]
                self.assertEqual((result['hash'], result['iid'], result['key'], result['provisional']), frozen)

    def request_packet(self, stage):
        suffix = original.framed([original.EXIT]) if stage == 5 else original.program(stage={0: 'vs', 3: 'gs', 4: 'ps'}[stage])
        code = (b'' if stage == 5 else bytes(0x30)) + suffix
        control = bytearray(0x880)
        struct.pack_into('<II', control, 0x700, len(code), len(code) + 8)
        root = args.out / ('request-' + str(stage))
        root.mkdir(exist_ok=True)
        (root / 'code.bin').write_bytes(code + bytes.fromhex('123456789abcdef0'))
        (root / 'control.bin').write_bytes(control)
        return (root / 'code.bin').resolve(), (root / 'control.bin').resolve()

    def test_actual_geometry_request_json(self):
        code, control = self.request_packet(3)
        for selector, name in enumerate(NAMES, 1):
            with self.subTest(topology=name):
                result = self.run_fixture('request', 3, selector, code, control)
                self.assertTrue(result['accepted'])
                job = result['request']['job']
                self.assertEqual((job['stage'], job['geometry_version'], job['gs_input']), ('gs', 1, name))
                self.assertEqual(job['c1'], '123456789abcdef0')
                self.assertEqual(job['code'], 'original Maxwell.code')
                self.assertNotIn('c0', job)
                self.assertNotIn('c0_version', job)
                self.assertEqual(job.get('subgroup', 32), 32)

    def test_explicit_geometry_subgroup_and_unregistered_host(self):
        code, control = self.request_packet(3)
        result = self.run_fixture('request', 3, 1, code, control, subgroup=64)
        self.assertTrue(result['accepted'])
        self.assertEqual(result['request']['job']['subgroup'], 64)
        self.assertFalse(self.run_fixture('request', 3, 1, code, control, subgroup=0)['accepted'])

    def test_geometry_instruction_span_cannot_be_supplied_by_constant_tail(self):
        code, control = self.request_packet(3)
        for size in (0x50, 0x58, 0x5f):
            with self.subTest(instruction_span=size):
                data = bytearray(control.read_bytes())
                struct.pack_into('<I', data, 0x700, 0x30 + size)
                path = args.out / ('constant-tail-' + str(size) + '.control')
                path.write_bytes(data)
                self.assertFalse(self.run_fixture('request', 3, 1, code, path.resolve())['accepted'])

    def test_missing_unknown_and_non_geometry_selectors_reject(self):
        for stage, selector in ((3, 0), (3, 6), (0, 1), (4, 2), (5, 3)):
            with self.subTest(stage=stage, selector=selector):
                code, control = self.request_packet(stage)
                self.assertFalse(self.run_fixture('request', stage, selector, code, control)['accepted'])

    def module(self, name):
        return ((self.modules / (name + '.spv')).read_bytes(),
                json.loads((self.modules / (name + '.res.json')).read_text()))

    def put_pair(self, cache, key, module, reflection):
        cache.mkdir(parents=True, exist_ok=True)
        (cache / (key + '.spv')).write_bytes(module)
        (cache / (key + '.res.json')).write_text(reflection if isinstance(reflection, str) else json.dumps(reflection))

    def test_actual_loose_packed_private_and_by_iid_loads(self):
        for selector, topology in enumerate(NAMES, 1):
            for mode in ('loose', 'packed', 'private', 'private-packed', 'input', 'input-packed'):
                with self.subTest(topology=topology, mode=mode):
                    cache = args.out / ('load-' + topology + '-' + mode)
                    iid = hashlib.sha256(('original input ' + topology).encode()).hexdigest()
                    key = self.run_fixture('input-key', iid, cache=cache)['key']
                    self.put_pair(cache, key, *self.module(topology))
                    if 'packed' in mode:
                        self.packer.pack(cache, keep=True)
                        # A valid packed-only load must not accidentally read loose files.
                        (cache / (key + '.spv')).unlink()
                        (cache / (key + '.res.json')).unlink()
                    result = self.run_fixture('load', mode, key, iid, cache=cache)
                    self.assertTrue(result['accepted'])
                    self.assertEqual((result['stage'], result['input'], result['invocations'], result['max_vertices']),
                                     (3, selector, 1, 1))

    def test_request_cache_hit_requires_the_requested_topology(self):
        code, control = self.request_packet(3)
        for selector, topology in enumerate(NAMES, 1):
            with self.subTest(topology=topology):
                cache = args.out / ('request-hit-' + topology)
                request = self.run_fixture('request', 3, selector, code, control, cache=cache)
                self.assertTrue(request['accepted'])
                self.run_fixture('learn', request['hash'], cache=cache)
                request = self.run_fixture('request', 3, selector, code, control, cache=cache)
                self.assertTrue(request['final'])
                self.put_pair(cache, request['key'], *self.module(topology))
                self.assertTrue(self.run_fixture('request-load', 3, selector, code, control, cache=cache)['accepted'])
                self.assertTrue(self.run_fixture('request-memory', 3, selector, code, control, cache=cache)['accepted'])
                wrong = NAMES[selector % 5]
                self.put_pair(cache, request['key'], *self.module(wrong))
                self.assertFalse(self.run_fixture('request-load', 3, selector, code, control, cache=cache)['accepted'])
                self.assertFalse(self.run_fixture('request-memory', 3, selector, code, control, cache=cache)['accepted'])
                self.packer.pack(cache, keep=True)
                # An incompatible packed pair must not hide a newer matching loose pair.
                self.put_pair(cache, request['key'], *self.module(topology))
                self.assertTrue(self.run_fixture('request-load', 3, selector, code, control, cache=cache)['accepted'])

    def test_unregistered_geometry_host_rejects_private_cache_admission(self):
        cache = args.out / 'unset-private'
        iid = hashlib.sha256(b'original unset capability').hexdigest()
        key = self.run_fixture('input-key', iid, cache=cache)['key']
        self.put_pair(cache, key, *self.module('points'))
        self.assertTrue(self.run_fixture('load', 'private', key, iid, cache=cache)['accepted'])
        self.assertFalse(self.run_fixture('load', 'private', key, iid, cache=cache, subgroup=0)['accepted'])

    def scope_load(self, name, topology, reflection, mode):
        cache = args.out / ('scope-' + name + '-' + mode)
        iid = hashlib.sha256(('original scoped metadata ' + name).encode()).hexdigest()
        key = self.run_fixture('input-key', iid, cache=cache)['key']
        self.put_pair(cache, key, self.module(topology)[0], reflection)
        if 'packed' in mode:
            self.packer.pack(cache, keep=True)
        return self.run_fixture('load', mode, key, iid, cache=cache)

    def test_nested_geometry_compute_layout_is_ignored_without_inheriting_c0(self):
        for location in ('geometry', 'extra'):
            _, reflection = self.module('points')
            reflection.setdefault(location, {}).update(c0=[0, 0, 0, 0], c0_version=2, c0_roots=[0] * 8)
            for mode in ('loose', 'private-packed', 'input'):
                with self.subTest(location=location, mode=mode):
                    result = self.scope_load('gs-nested-c0-' + location, 'points', reflection, mode)
                    self.assertTrue(result['accepted'], 'extra nested metadata is ignored, not treated as a driver layout')
                    self.assertEqual(result['stage'], 3)
                    self.assertFalse(result['has_c0'], 'a nested compute layout must never become a GS layout')

    def test_storage_contract_version_uses_a_unique_root_field(self):
        for topology, root_version, nested_version, accepted in (
                ('points', 1, 0, False), ('points', 0, 1, True), ('points', None, 1, True),
                ('compute', 2, 1, False), ('compute', 1, 0, True)):
            _, base = self.module(topology)
            base.pop('storage_candidates_version', None)
            # Deliberately place the nested key before the real root key.
            reflection = dict(extra={'storage_candidates_version': nested_version}, **base)
            if root_version is not None:
                reflection['storage_candidates_version'] = root_version
            if topology == 'compute':
                reflection.update(c0=[0, 0, 0, 0], c0_version=2, c0_roots=[0] * 8)
            name = topology + '-root-' + str(root_version) + '-nested-' + str(nested_version)
            for mode in ('loose', 'private-packed', 'input'):
                with self.subTest(case=name, mode=mode):
                    result = self.scope_load(name, topology, reflection, mode)
                    self.assertEqual(result['accepted'], accepted)
                    if accepted:
                        self.assertEqual(result['has_c0'], topology == 'compute')
        _, compute = self.module('compute')
        compute.update(c0=[0, 0, 0, 0], c0_version=2, c0_roots=[0] * 8)
        for first, second in ((0, 1), (1, 0), (1, 1)):
            compute['storage_candidates_version'] = first
            text = json.dumps(compute)[:-1] + ',"storage_candidates_version":' + str(second) + '}'
            for mode in ('loose', 'private-packed'):
                with self.subTest(duplicate=(first, second), mode=mode):
                    self.assertFalse(self.scope_load('duplicate-storage-' + str(first) + str(second), 'compute', text, mode)['accepted'])

    def test_compute_layout_root_fields_are_unique_and_warm_compatible(self):
        _, compute = self.module('compute')
        compute.pop('storage_candidates_version', None)
        nested = dict(c0=[0, 0, 7, 0], c0_version=2, c0_roots=[3] + [0] * 7)
        valid = dict(extra=nested, **compute, storage_candidates_version=1,
                     c0=[0, 0, 2, 0], c0_version=2, c0_roots=[1] + [0] * 7)
        for mode in ('loose', 'private-packed', 'input'):
            with self.subTest(valid_root_layout=mode):
                result = self.scope_load('cs-root-layout', 'compute', valid, mode)
                self.assertTrue(result['accepted'])
                self.assertTrue(result['has_c0'])
                self.assertEqual((result['c0_ssbos'], result['c0_root0']), (2, 1), 'root layout wins over nested metadata')
        for field, value in (('c0', [0, 0, 0, 0]), ('c0_version', 2), ('c0_roots', [0] * 8)):
            for mode in ('loose', 'private-packed'):
                with self.subTest(duplicate=field, mode=mode):
                    text = json.dumps(valid)[:-1] + ',' + json.dumps(field) + ':' + json.dumps(value) + '}'
                    self.assertFalse(self.scope_load('duplicate-' + field, 'compute', text, mode)['accepted'])
        for missing in ('c0', 'c0_version', 'c0_roots'):
            reflection = dict(valid)
            del reflection[missing]
            with self.subTest(missing_root_field=missing):
                self.assertFalse(self.scope_load('missing-root-' + missing, 'compute', reflection, 'private')['accepted'])
        # The actual request-keyed warm path still requires a valid v1 compute contract.
        code, control = self.request_packet(5)
        data = bytearray(control.read_bytes())
        data[0x860:0x868] = b'DKC1' + bytes((0, 0, 2, 0))
        data[0x86c] = 1
        control.write_bytes(data)
        cache = args.out / 'scope-compute-request-warm'
        request = self.run_fixture('request', 5, 0, code, control, cache=cache)
        self.assertTrue(request['accepted'])
        self.run_fixture('learn', request['hash'], cache=cache)
        request = self.run_fixture('request', 5, 0, code, control, cache=cache)
        warm = dict(valid, extra=dict(nested, storage_candidates_version=0))
        self.put_pair(cache, request['key'], self.module('compute')[0], warm)
        for mode in ('request-load', 'request-memory'):
            with self.subTest(valid_compute_warm=mode):
                result = self.run_fixture(mode, 5, 0, code, control, cache=cache)
                self.assertTrue(result['accepted'])
                self.assertTrue(result['has_c0'])
                self.assertEqual((result['c0_ssbos'], result['c0_root0']), (2, 1))

    def malformed_reflections(self):
        _, good = self.module('points')
        text = json.dumps(good)
        for name, tail in [('stage_gs_then_vs', '"stage":"vs"'),
                           ('duplicate_input', '"gs_input":"points"'),
                           ('duplicate_version', '"geometry_version":1'),
                           ('duplicate_geometry', '"geometry":' + json.dumps(good['geometry']))]:
            yield name, text[:-1] + ',' + tail + '}'
        yield 'stage_vs_then_gs', '{"stage":"vs",' + text[1:]
        for name, field, value in [('version0', 'geometry_version', 0), ('version2', 'geometry_version', 2),
            ('version_float', 'geometry_version', 1.0), ('version_string', 'geometry_version', '1'),
            ('version_bool', 'geometry_version', True), ('input_bad', 'gs_input', 'quads')]:
            r = json.loads(json.dumps(good)); r[field] = value
            yield name, r
        for name, field, value in [('pass_true', 'passthrough', True), ('pass_zero', 'passthrough', 0),
            ('pass_string', 'passthrough', 'false'), ('pass_null', 'passthrough', None),
            ('vertices_bad', 'input_vertices', 3), ('invocations0', 'invocations', 0),
            ('invocations256', 'invocations', 256), ('vertices0', 'max_output_vertices', 0),
            ('vertices4096', 'max_output_vertices', 4096), ('output_bad', 'output_topology', 'triangles')]:
            r = json.loads(json.dumps(good)); r['geometry'][field] = value
            yield name, r
        for field in ('geometry_version', 'gs_input', 'geometry'):
            r = json.loads(json.dumps(good)); del r[field]
            yield 'missing_' + field, r

    @staticmethod
    def encode_module(template, operations):
        words = list(struct.unpack('<5I', template[:20]))
        for op, operands in operations:
            words += [((len(operands) + 1) << 16) | op, *operands]
        return struct.pack('<' + 'I' * len(words), *words)

    def malformed_modules(self):
        module, _ = self.module('points')
        operations = list(original.instructions(module))
        for name, mode, replacement in [('wrong_input', 19, (22,)), ('wrong_invocations', 0, (0, 2)),
                                        ('wrong_vertices', 26, (26, 2)), ('wrong_output', 27, (28,))]:
            changed = [(op, (o[0], *replacement) if op == 16 and o[1] == mode else o) for op, o in operations]
            yield name, self.encode_module(module, changed)
        mode = next((op, o) for op, o in operations if op == 16)
        index = next(i for i, (op, _) in enumerate(operations) if op == 16)
        yield 'duplicate_mode', self.encode_module(module, operations[:index] + [mode] + operations[index:])
        yield 'foreign_entry_mode', self.encode_module(module,
            [(op, (o[0] + 1, *o[1:]) if op == 16 and o[1] == 0 else o) for op, o in operations])
        for name, extra in [('pass_capability', (17, (5251,))), ('pass_decoration', (71, (1, 5250))),
                            ('pass_member', (72, (1, 0, 5250)))]:
            yield name, self.encode_module(module, [extra] + operations)
        text = b'SPV_NV_geometry_shader_passthrough\0'
        text += bytes(-len(text) % 4)
        extension = (10, struct.unpack('<' + 'I' * (len(text) // 4), text))
        yield 'pass_extension', self.encode_module(module, [extension] + operations)

    def test_strict_reflection_and_scoped_modes_on_private_and_loose_loads(self):
        module, reflection = self.module('points')
        cases = [(name, module, bad) for name, bad in self.malformed_reflections()]
        cases += [(name, bad, reflection) for name, bad in self.malformed_modules()]
        fragment_module, fragment_reflection = self.module('fragment')
        cases += [('geometry_under_fragment', module, fragment_reflection)]
        fragment_reflection = dict(fragment_reflection, geometry_version=1)
        cases += [('geometry_fields_under_fragment', fragment_module, fragment_reflection)]
        for name, code, info in cases:
            for mode in ('loose', 'private-packed'):
                with self.subTest(case=name, mode=mode):
                    cache = args.out / ('bad-' + name + '-' + mode)
                    iid = hashlib.sha256(('bad input ' + name).encode()).hexdigest()
                    key = self.run_fixture('input-key', iid, cache=cache)['key']
                    self.put_pair(cache, key, code, info)
                    if 'packed' in mode:
                        self.packer.pack(cache, keep=True)
                    self.assertFalse(self.run_fixture('load', mode, key, iid, cache=cache)['accepted'])

    def test_stored_v1_and_v2_input_roundtrips_and_request_reconstruction(self):
        for stage, selector in [(0, 0), (4, 0), (5, 0), *((3, i) for i in range(1, 6))]:
            with self.subTest(stage=stage, input=selector):
                cache = args.out / ('stored-' + str(stage) + '-' + str(selector))
                code, control = self.request_packet(stage)
                written = self.run_fixture('store-input', stage, selector, code, control, cache=cache)
                self.assertTrue(written['accepted'])
                loaded = self.run_fixture('load-input', written['iid'], cache=cache)
                self.assertTrue(loaded['accepted'])
                self.assertTrue(loaded['recreated'])
                self.assertEqual((loaded['stage'], loaded['input'], loaded['iid']), (stage, selector, written['iid']))
                self.assertEqual(loaded['request'], written['request'])
                path = cache / 'in' / (written['iid'] + '.in')
                data = path.read_bytes()
                header = struct.unpack_from('<6I', data)
                expected_size = 148 if stage == 3 else 152
                self.assertEqual(header[:3], (0x49534b54, 2 if stage == 3 else 1, stage))
                self.assertEqual(header[5], expected_size)
                start = 24 + expected_size
                self.assertEqual(data[start:start + 0x880], control.read_bytes())
                self.assertEqual(data[start + 0x880:start + 0x880 + len(code.read_bytes())], code.read_bytes())
                self.assertEqual(data[-8:], bytes.fromhex('123456789abcdef0'))
                if stage != 3:
                    # Legacy padding is not a new selector. Decoding must ignore
                    # old indeterminate padding rather than treat it as geometry.
                    poison = bytearray(data)
                    poison[24 + 142:24 + 144] = b'\xa5\x5a'
                    poison[24 + 149:24 + 152] = b'\xf1\xf2\xf3'
                    path.write_bytes(poison)
                    self.assertEqual(self.run_fixture('load-input', written['iid'], cache=cache)['input'], 0)
                self.packer.pack(cache, keep=True)
                path.unlink()
                self.assertEqual(self.run_fixture('load-input-packed', written['iid'], cache=cache), loaded)

    def test_stored_inputs_reject_invalid_versions_sizes_and_selectors(self):
        code, control = self.request_packet(0)
        seed = args.out / 'stored-corruption-source'
        written = self.run_fixture('store-input', 0, 0, code, control, cache=seed)
        self.assertTrue(written['accepted'])
        legacy = (seed / 'in' / (written['iid'] + '.in')).read_bytes()
        cases = []
        for name, offset, value in [('version', 4, 3), ('answers_size', 20, 151), ('stage', 8, 1),
                                    ('gs_v1', 8, 3), ('length', 12, 0xffffffff)]:
            data = bytearray(legacy); struct.pack_into('<I', data, offset, value); cases.append((name, bytes(data)))
        for name, data in cases:
            with self.subTest(case=name):
                cache = args.out / ('stored-bad-' + name)
                directory = cache / 'in'; directory.mkdir(parents=True)
                (directory / (written['iid'] + '.in')).write_bytes(data)
                self.assertFalse(self.run_fixture('load-input', written['iid'], cache=cache)['accepted'])
        code, control = self.request_packet(3)
        wire = bytes.fromhex(self.identity(0)['answers'])
        data = (struct.pack('<6I', 0x49534b54, 2, 3, len(code.read_bytes()), 8, 148) + wire + b'\x01' +
                control.read_bytes() + code.read_bytes() + bytes.fromhex('123456789abcdef0'))
        iid = hashlib.sha256(b'original v2 stored-input control').hexdigest()
        for name, selector in (('valid', 1), ('absent', 0), ('unknown', 6)):
            with self.subTest(v2_selector=name):
                cache = args.out / ('stored-v2-' + name)
                directory = cache / 'in'; directory.mkdir(parents=True)
                variant = bytearray(data); variant[24 + 147] = selector
                (directory / (iid + '.in')).write_bytes(variant)
                self.assertEqual(self.run_fixture('load-input', iid, cache=cache)['accepted'], selector == 1)

    def record_file(self, version, records):
        wire = bytes.fromhex(self.identity(0)['answers'])
        body = bytearray()
        for stage, selector, priority, token in records:
            body += struct.pack('<I', stage) + hashlib.sha256(('program' + token).encode()).digest()
            body += hashlib.sha256(('input' + token).encode()).digest() + wire
            if version == 2:
                body.append(selector)
            body.append(priority)
        return struct.pack('<8sII', b'TOTKSANS', version, 216 if version == 1 else 217) + body

    def test_answer_v1_v2_read_only_and_atomic_migration_preserve_priorities(self):
        legacy_records = [(0, 0, 2, 'a'), (4, 0, 1, 'b'), (5, 0, 0, 'c')]
        for version, records in ((1, legacy_records), (2, [*legacy_records, (3, 5, 2, 'd')])):
            with self.subTest(version=version):
                cache = args.out / ('answers-' + str(version)); cache.mkdir()
                path = cache / 'shader-answers.bin'
                data = self.record_file(version, records); path.write_bytes(data)
                expected = self.record_file(2, records)
                result = self.run_fixture('answers', path.resolve(), 0, cache=cache)
                self.assertTrue(result['accepted'])
                self.assertEqual(bytes.fromhex(result['body']), expected[16:])
                self.assertEqual(path.read_bytes(), data, 'read-only consumers must not rewrite recordings')
                result = self.run_fixture('answers', path.resolve(), 1, cache=cache)
                self.assertTrue(result['accepted'])
                self.assertEqual(bytes.fromhex(result['body']), expected[16:])
                self.assertEqual(path.read_bytes(), expected)

    def test_actual_record_append_upgrades_before_new_geometry_record(self):
        cache = args.out / 'record-append'; cache.mkdir()
        path = cache / 'shader-answers.bin'
        legacy_records = [(0, 0, 2, 'older')]
        path.write_bytes(self.record_file(1, legacy_records))
        code, control = self.request_packet(3)
        added = self.run_fixture('record-append', 3, 4, code, control, cache=cache)
        self.assertTrue(added['accepted'])
        data = path.read_bytes()
        expected = self.record_file(2, legacy_records)
        self.assertEqual(data[:len(expected)], expected)
        self.assertEqual(len(data), 16 + 217 * 2)
        record = data[len(expected):]
        self.assertEqual(struct.unpack_from('<I', record)[0], 3)
        self.assertEqual(record[4:36].hex(), added['hash'])
        self.assertEqual(record[36:68].hex(), added['iid'])
        self.assertEqual(record[215:], bytes([4, 0]))

    def test_atomic_migration_failure_keeps_v1_and_disables_recording(self):
        cache = args.out / 'migration-write-failure'; cache.mkdir()
        path = cache / 'shader-answers.bin'
        original_file = self.record_file(1, [(0, 0, 2, 'retained-a'), (4, 0, 1, 'retained-b'),
                                             (5, 0, 0, 'retained-c')])
        path.write_bytes(original_file)
        program_path = cache / 'shader-programs.bin'
        original_programs = struct.pack('<8sII', b'TOTKSPRG', 1, 0)
        program_path.write_bytes(original_programs)
        code, control = self.request_packet(0)
        result = self.run_fixture('record-atomic-failure', 0, 0, code, control, cache=cache)
        (cache / 'provider-result.json').write_text(json.dumps(result, indent=2))
        self.assertTrue(result['accepted'])
        self.assertEqual((result['queued_answers'], result['queued_programs']), (0, 0))
        if result['record_version'] == 1:
            # The old source has no migration. A legitimate whole v1 append is
            # the control here, not a fabricated migration-failure expectation.
            self.assertTrue(args.source_revision)
            self.assertEqual((result['migration_attempts'], result['injected_failures'], result['recording']), (0, 0, 1))
            data = path.read_bytes()
            self.assertEqual(data[:len(original_file)], original_file)
            self.assertEqual(len(data), len(original_file) + 216)
            self.assertEqual(struct.unpack_from('<8sII', data), (b'TOTKSANS', 1, 216))
        else:
            self.assertEqual(result['record_version'], 2)
            self.assertEqual((result['migration_attempts'], result['injected_failures'], result['recording']), (1, 1, 0))
            self.assertEqual(path.read_bytes(), original_file, 'failed migration must not replace or append to v1 data')
            self.assertEqual(program_path.read_bytes(), original_programs, 'disabled recording must not publish program writes')

    def test_seed_prepare_and_warm_list_preserve_v1_v2_priorities(self):
        for version, stages in ((1, [(0, 0, 'vertex'), (4, 0, 'fragment'), (5, 0, 'compute')]),
                                (2, [(3, i, name) for i, name in enumerate(NAMES, 1)])):
            with self.subTest(version=version):
                cache = args.out / ('seed-' + str(version))
                records, programs, expected = [], [], []
                for i, (stage, selector, module_name) in enumerate(stages):
                    code, control = self.request_packet(stage)
                    added = self.run_fixture('record-append', stage, selector, code, control, cache=cache)
                    self.assertTrue(added['accepted'])
                    self.put_pair(cache, added['key'], *self.module(module_name))
                    answer_data = (cache / 'shader-answers.bin').read_bytes()
                    # Read the just-appended actual record, retaining the original
                    # runtime wire bytes; only seed priority is authored here.
                    size = struct.unpack_from('<I', answer_data, 12)[0]
                    rec = bytearray(answer_data[-size:]); rec[-1] = (2 - i) % 3
                    if version == 1 and size == 217:
                        del rec[-2]
                    records.append(bytes(rec))
                    expected.append(dict(iid=added['iid'], stage=stage, priority=1 + (2 - i) % 3))
                programs = (cache / 'shader-programs.bin').read_bytes()
                (cache / 'seed-programs.bin').write_bytes(programs)
                (cache / 'seed-answers.bin').write_bytes(struct.pack('<8sII', b'TOTKSANS', version, 216 if version == 1 else 217) + b''.join(records))
                # Warm-list ordering starts with met-in-play records; remove this
                # fixture-created copy to observe prepared priorities independently.
                (cache / 'shader-answers.bin').unlink()
                result = self.run_fixture('prepare', cache=cache)
                self.assertTrue(result['accepted'])
                self.assertEqual(result['entries'], expected)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cc', required=True)
    parser.add_argument('--opt', choices=('O0', 'O2'), default='O2')
    parser.add_argument('--source-revision')
    parser.add_argument('--translator', required=True)
    parser.add_argument('--spirv-val', required=True)
    parser.add_argument('--out', type=Path, default=ROOT / 'build/test/geometry-cache')
    args, rest = parser.parse_known_args()
    result = unittest.main(argv=[__file__, *rest], exit=False).result
    (args.out / 'result.json').write_text(json.dumps(dict(tests=result.testsRun, failures=len(result.failures),
        errors=len(result.errors), skipped=len(result.skipped), source_revision=args.source_revision,
        gpu_executed=False), indent=2))
    raise SystemExit(not result.wasSuccessful())
