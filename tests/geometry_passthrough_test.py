"""CPU-only portable GS passthrough protocol, copy semantics and side effects.

All shader inputs are authored fixtures. No Vulkan loader, GPU or game executes.
The copy oracle clears output memory after every EmitVertex, requiring each
emitted vertex to receive the selected original input values independently.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import unittest

import geometry_shader_fixture as geometry
import geometry_passthrough_fixture as fixture
from xlate_geometry_test import GeometrySpirv

ROOT = Path(__file__).resolve().parents[1]


class Copies(GeometrySpirv):
    def run_copies(self, positions, attributes):
        values, memory = dict(self.constants), {}
        pointers = {var: (var,) for var in self.variables}
        ip, im = self.position(1)
        oposition, om = self.position(3)
        outputs = {location: self.location(3, location) for location in attributes}
        for vertex, position in enumerate(positions):
            for component, value in enumerate(position):
                memory[ip, vertex, im, component] = value
        for location, vertices in attributes.items():
            iv = self.location(1, location)
            assert self.input_array_length(iv) == 3
            for vertex, vector in enumerate(vertices):
                for component, value in enumerate(vector):
                    memory[iv, vertex, component] = value
        emitted, cuts, reads = [], [], []
        for opcode, operands in self.body:
            if opcode in (248, 253):
                continue
            if opcode == 65:
                pointers[operands[1]] = pointers[operands[2]] + tuple(values[i] for i in operands[3:])
            elif opcode == 61:
                pointer = pointers[operands[2]]
                values[operands[1]] = memory[pointer]
                reads.append(pointer)
            elif opcode == 62:
                memory[pointers[operands[0]]] = values[operands[1]]
            elif opcode in (83, 124):
                values[operands[1]] = values[operands[2]]
            elif opcode == 218:
                emitted.append((tuple(memory[oposition, om, c] for c in range(4)),
                                {loc: tuple(memory[var, c] for c in range(4)) for loc, var in outputs.items()}))
                memory = {p: value for p, value in memory.items() if self.variables[p[0]][0] != 3}
            elif opcode == 219:
                cuts.append(len(emitted))
            else:
                raise AssertionError('Unsupported original copy fixture opcode ' + str(opcode))
        return emitted, cuts, reads


class Passthrough(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        args.out.mkdir(parents=True, exist_ok=False)
        cls.validations = 0
        cls.env = {k: v for k, v in os.environ.items() if k.upper() in {
            'SYSTEMROOT', 'WINDIR', 'PATH', 'TEMP', 'TMP', 'USERPROFILE', 'APPDATA', 'LOCALAPPDATA',
            'PROGRAMFILES', 'PROGRAMFILES(X86)', 'PROGRAMDATA', 'SYSTEMDRIVE'}}
        cls.env['DOTNET_PROCESSOR_COUNT'] = '2'

    def packet(self, key, code=None, **changes):
        path = args.out / (key + '.bin')
        path.write_bytes(fixture.program() if code is None else code)
        job = dict(key=key, stage='gs', code=str(path.resolve()), c1='', debug=False,
                   geometry_version=2, gs_input='triangles', gs_passthrough_skip=fixture.skip_words([6]), sbuf_align=256)
        job.update(changes)
        return job

    def translate(self, jobs, valid=True, translator=None):
        binary = translator or args.xlate
        command = [str(args.dotnet), str(binary)] if binary.suffix == '.dll' else [str(binary)]
        result = subprocess.run(command + ['--serve'], env=self.env,
            input=''.join(json.dumps(dict(job=job, out=str(args.out.resolve()))) + '\n' for job in jobs),
            capture_output=True, text=True, timeout=60)
        (args.out / (jobs[0]['key'] + '.log')).write_text(result.stdout + result.stderr, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stderr)
        replies = result.stdout.splitlines()
        self.assertEqual(len(replies), len(jobs), result.stdout + result.stderr)
        for job, reply in zip(jobs, replies):
            with self.subTest(key=job['key']):
                spv, res = (args.out / (job['key'] + suffix) for suffix in ('.spv', '.res.json'))
                if valid:
                    self.assertEqual(reply, 'ok ' + job['key'])
                    validation = subprocess.run([str(args.spirv_val), '--target-env', 'vulkan1.3', str(spv)],
                                                capture_output=True, text=True, timeout=15)
                    (args.out / (job['key'] + '.val.log')).write_text(validation.stdout + validation.stderr, encoding='utf-8')
                    self.assertEqual(validation.returncode, 0, validation.stdout + validation.stderr)
                    type(self).validations += 1
                    self.assertTrue(res.is_file())
                else:
                    self.assertTrue(reply.startswith('fail ' + job['key'] + ' '), reply)
                    self.assertFalse(spv.exists())
                    self.assertFalse(res.exists())
        return replies

    def test_original_per_vertex_copies_and_exact_masks(self):
        for locations in ([], [6], [31], [0, 6, 31], list(range(32))):
            with self.subTest(locations=locations):
                job = self.packet('copy_' + str(len(locations)) + '_' + str(locations[-1:] or 'none'),
                                  gs_passthrough_skip=fixture.skip_words(locations))
                self.translate([job])
                data = (args.out / (job['key'] + '.spv')).read_bytes()
                module = Copies(data)
                modes = [o[1:] for op, o in module.ops if op == 16]
                for mode in ((22,), (29,), (0, 1), (26, 3)):
                    self.assertIn(mode, modes)
                positions = [tuple(0x3f000001 + v * 16 + c for c in range(4)) for v in range(3)]
                attributes = {loc: [tuple(0x3f800001 + loc * 256 + v * 16 + c for c in range(4))
                                    for v in range(3)] for loc in locations}
                emitted, cuts, reads = module.run_copies(positions, attributes)
                self.assertEqual(emitted, [(positions[v], {loc: attributes[loc][v] for loc in locations}) for v in range(3)])
                self.assertEqual(cuts, [3])
                self.assertEqual(sum(op == 218 for op, _ in module.ops), 3)
                self.assertEqual(sum(op == 219 for op, _ in module.ops), 1)
                ip, member = module.position(1)
                self.assertEqual(module.input_array_length(ip), 3)
                self.assertEqual([p for p in reads if p[0] == ip],
                                 [(ip, v, member, c) for v in range(3) for c in range(4)])
                for loc in locations:
                    iv = module.location(1, loc)
                    self.assertEqual([p for p in reads if p[0] == iv], [(iv, v, c) for v in range(3) for c in range(4)])
                actual_locations = {(sc, module.locations[var]) for var, (sc, _) in module.variables.items()
                                    if var in module.locations and sc in (1, 3)}
                self.assertEqual(actual_locations, {(sc, loc) for sc in (1, 3) for loc in locations})
                self.assertNotIn(b'SPV_NV_geometry_shader_passthrough', data)
                reflection = json.loads((args.out / (job['key'] + '.res.json')).read_text(encoding='utf-8'))
                self.assertEqual(reflection['geometry_version'], 2)
                self.assertEqual(reflection['gs_input'], 'triangles')
                self.assertEqual(reflection['gs_passthrough_skip'], job['gs_passthrough_skip'])
                self.assertEqual(reflection['geometry'], dict(input_vertices=3, output_topology='triangle_strip',
                    invocations=1, max_output_vertices=3, passthrough=False, source_passthrough=True))
                self.assertEqual(reflection['notes'], [])
                self.assertEqual(reflection['sbufs'], [])

    def test_original_storage_side_effect_survives_before_emission(self):
        job = self.packet('store', fixture.program(store=True))
        self.translate([job])
        data = (args.out / 'store.spv').read_bytes()
        ops = list(geometry.instructions(data))
        reflection = json.loads((args.out / 'store.res.json').read_text(encoding='utf-8'))
        self.assertEqual(len(reflection['sbufs']), 1)
        storage = reflection['sbufs'][0]
        self.assertEqual((storage['cb_slot'], storage['cb_offset'], storage['flags']), (3, 0, 1))
        bindings = {o[0]: o[2] for op, o in ops if op == 71 and o[1] == 33}
        buffers = {var for var, binding in bindings.items() if binding == storage['binding']}
        chains = {o[1]: o[2] for op, o in ops if op == 65}
        writes = [index for index, (op, o) in enumerate(ops) if op == 62 and chains.get(o[0]) in buffers]
        emits = [index for index, (op, _) in enumerate(ops) if op == 218]
        self.assertEqual(len(writes), 1)
        self.assertEqual(len(emits), 3)
        self.assertLess(writes[0], emits[0])
        constants = {o[1]: o[2] for op, o in ops if op == 43 and len(o) == 3}
        self.assertEqual(constants[ops[writes[0]][1][1]], 0x1234abcd)
        self.assertEqual(reflection['notes'], [])

    def test_exact_typed_mask_and_supported_components(self):
        invalid = [None, True, {}, '', [], [0] * 7, [0] * 9]
        for bad in (None, True, '4294967295', 1.0, -1, 1 << 32, {}, []):
            words = fixture.skip_words([6]); words[7] = bad; invalid.append(words)
        for word, value in ((0, 0xffffffff), (0, 0), (5, 0), (6, 0xfffffffe), (7, 0x7fffffff)):
            words = fixture.skip_words([6]); words[word] = value; invalid.append(words)
        for nibble in (1, 2, 7, 8, 14):
            words = fixture.skip_words([]); words[1] = (words[1] & ~15) | nibble; invalid.append(words)
        jobs = [self.packet('invalid_mask_' + str(i), gs_passthrough_skip=mask) for i, mask in enumerate(invalid)]
        missing = self.packet('missing_mask'); del missing['gs_passthrough_skip']
        self.translate([missing, *jobs], valid=False)

    def test_contract_stage_version_header_and_compute_rejection(self):
        jobs = []
        for i, version in enumerate((None, 0, 1, 2.0, True, '2', 3, -1, {}, [])):
            jobs.append(self.packet('invalid_version_' + str(i), geometry_version=version))
        for topology in ('points', 'lines', 'lines_adjacency', 'triangles_adjacency'):
            jobs.append(self.packet('unsupported_topology_' + topology, gs_input=topology))
        for name, offset, value in (('invocations', 8, 2 << 24), ('max_vertices', 16, 3),
                                    ('output', 12, 6 << 24), ('missing_passthrough', 0, 1 | 3 << 5 | 4 << 10)):
            code = bytearray(fixture.program()); struct.pack_into('<I', code, offset, value)
            jobs.append(self.packet('header_' + name, bytes(code)))
        for stage in ('vs', 'ps', 'cs'):
            code = geometry.framed([geometry.EXIT]) if stage == 'cs' else geometry.program(stage=stage)
            job = self.packet('field_on_' + stage, code, stage=stage)
            del job['geometry_version'], job['gs_input']
            jobs.append(job)
        for name, value in (('c0', None), ('c0_version', 2), ('c0_roots', [0] * 8)):
            jobs.append(self.packet('compute_' + name, **{name: value}))
        ordinary = self.packet('mask_on_ordinary', geometry.program(), geometry_version=1, gs_input='points')
        self.translate([ordinary, *jobs], valid=False)

    def test_explicit_output_and_emission_forms_reject(self):
        ops = [('user', geometry.ast(0, 0x80)), ('position', geometry.ast(0, 0x70)),
               ('point_size', geometry.ast(0, 0x6c, 1)), ('clip', geometry.ast(0, 0x2c0, 1)),
               ('layer', geometry.ast(0, 0x64, 1)), ('emit', geometry.out_vertex()),
               ('emit_cut', geometry.out_vertex(cut=True)),
               ('predicated', geometry.ast(0, 0x80) & ~(7 << 16)),
               ('physical', geometry.ast(0, 0) & ~(255 << 8) | 1 << 8),
               ('per_patch', geometry.ast(0, 0x80) | 1 << 31)]
        jobs = [self.packet('unsupported_' + name, fixture.program(extra=[op])) for name, op in ops]
        replies = self.translate(jobs, valid=False)
        self.assertTrue(all('no emission and no attribute writes' in reply for reply in replies))

    def test_portable_sph_kind_and_version_are_exact(self):
        jobs = []
        for kind, version in ((0, 3), (2, 3), (31, 3), (1, 0), (1, 1), (1, 2), (1, 4), (1, 31)):
            code = bytearray(fixture.program())
            word0 = struct.unpack_from('<I', code)[0]
            word0 = (word0 & ~(31 | (31 << 5))) | kind | (version << 5)
            struct.pack_into('<I', code, 0, word0)
            jobs.append(self.packet('sph_' + str(kind) + '_' + str(version), bytes(code)))
        replies = self.translate(jobs, valid=False)
        self.assertTrue(all('kind-1 version-3 SPH' in reply for reply in replies))

    def test_legacy_byte_and_reflection_parity(self):
        jobs = []
        for stage in ('vs', 'ps', 'cs'):
            code = geometry.framed([geometry.EXIT]) if stage == 'cs' else geometry.program(stage=stage)
            job = self.packet('legacy_' + stage, code, stage=stage, local=[1, 1, 1], shared=0)
            del job['geometry_version'], job['gs_input'], job['gs_passthrough_skip']
            jobs.append(job)
        for topology in ('points', 'lines', 'lines_adjacency', 'triangles', 'triangles_adjacency'):
            for store in (False, True):
                job = self.packet('legacy_' + topology + '_' + str(store), geometry.program(store=store),
                                  geometry_version=1, gs_input=topology)
                del job['gs_passthrough_skip']; jobs.append(job)
        self.translate(jobs)
        old = [dict(job, key=job['key'] + '_baseline') for job in jobs]
        self.translate(old, translator=args.baseline)
        for current, baseline in zip(jobs, old):
            self.assertEqual((args.out / (current['key'] + '.spv')).read_bytes(),
                             (args.out / (baseline['key'] + '.spv')).read_bytes())
            a, b = (json.loads((args.out / (job['key'] + '.res.json')).read_text(encoding='utf-8'))
                    for job in (current, baseline))
            a.pop('key'); b.pop('key'); self.assertEqual(a, b)

    def test_unchanged_translator_rejects_new_contract(self):
        self.translate([self.packet('old_contract_reject', fixture.program(store=True))], valid=False,
                       translator=args.baseline)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--xlate', type=Path, required=True)
    parser.add_argument('--baseline', type=Path, required=True)
    parser.add_argument('--dotnet', type=Path, default=Path('F:/Codex/dkb-pc-recomp/toolchain/dotnet/dotnet.exe'))
    parser.add_argument('--spirv-val', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args, rest = parser.parse_known_args()
    result = unittest.main(argv=[__file__] + rest, exit=False).result
    evidence = dict(tests=result.testsRun, failures=len(result.failures), errors=len(result.errors),
                    skipped=len(result.skipped), complete_spirv_validations=Passthrough.validations,
                    translator_sha256=hashlib.sha256(args.xlate.read_bytes()).hexdigest(), gpu_executed=False)
    if args.out.is_dir():
        (args.out / 'result.json').write_text(json.dumps(evidence, indent=2) + '\n', encoding='utf-8')
    raise SystemExit(not result.wasSuccessful())
