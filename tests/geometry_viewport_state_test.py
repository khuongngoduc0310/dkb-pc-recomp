"""CPU-only semantic controls for PR67's translator-owned viewport-mask state.

Run against unchanged PR67 for a negative control with the same expectations.
Only authored shaders and a bounded scalar SPIR-V interpreter are used. The
zero/one cull expectation is the existing patch's behavior, not hardware proof.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import unittest

import geometry_passthrough_fixture as portable
import geometry_viewport_fixture as fixture

ROOT = Path(__file__).resolve().parents[1]
POSITIONS = [tuple(0x3e800001 + vertex * 32 + c for c in range(4)) for vertex in range(3)]
COLORS = [tuple(0x3f000001 + vertex * 32 + c for c in range(4)) for vertex in range(3)]
CASES = ('no_ast_r254', 'preserve_r254', 'guest_cannot_unmask', 'zero', 'one', 'never',
         'predicated', 'predicated_inverse', 'helper_store', 'helper_guest_write', 'indexed')


def check_result(result, value, culled=False):
    if result['record'] != (value, *([fixture.CANARY] * 5)) or result['writes'] != [(0, value)]:
        raise AssertionError(f'guest record/canary mismatch: expected {value:#x}, actual {result["record"]}')
    expected = [(fixture.CLIPPED if culled else POSITIONS[v], COLORS[v]) for v in range(3)]
    if result['emitted'] != expected or result['events'] != ['store', 'emit', 'emit', 'emit', 'cut']:
        raise AssertionError(f'position/color/emission oracle mismatch: culled={culled}, actual={result["emitted"]}')


class ViewportState(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        args.out.resolve().relative_to((ROOT / 'build').resolve())
        args.out.mkdir(parents=True, exist_ok=False)
        env = {k: v for k, v in os.environ.items() if k.upper() in {
            'SYSTEMROOT', 'WINDIR', 'PATH', 'TEMP', 'TMP', 'USERPROFILE', 'APPDATA', 'LOCALAPPDATA',
            'PROGRAMFILES', 'PROGRAMFILES(X86)', 'PROGRAMDATA', 'SYSTEMDRIVE'}}
        env['DOTNET_PROCESSOR_COUNT'] = '2'
        jobs = []
        for case in CASES:
            code = args.out / (case + '.code')
            code.write_bytes(fixture.program(case))
            job = dict(key=case, stage='gs', code=str(code.resolve()), c1='', debug=False,
                       geometry_version=2, gs_input='triangles',
                       gs_passthrough_skip=portable.skip_words([fixture.LOCATION]))
            jobs.append(dict(job=job, out=str(args.out.resolve())))
        run = subprocess.run([str(args.xlate.resolve()), '--serve'], env=env,
                             input=''.join(json.dumps(job) + '\n' for job in jobs),
                             capture_output=True, text=True, timeout=60)
        (args.out / 'translator.log').write_text(run.stdout + run.stderr, encoding='utf-8')
        if run.returncode or len(run.stdout.splitlines()) != len(CASES):
            raise AssertionError(run.stdout + run.stderr)
        cls.replies = dict(zip(CASES, run.stdout.splitlines()))
        cls.modules, cls.validations = {}, 0
        for case in CASES:
            if cls.replies[case] != 'ok ' + case:
                if case != 'indexed':
                    raise AssertionError(cls.replies[case])
                continue
            spv = args.out / (case + '.spv')
            validation = subprocess.run([str(args.spirv_val.resolve()), '--target-env', 'vulkan1.3', str(spv)],
                                        capture_output=True, text=True, timeout=15)
            (args.out / (case + '.val.log')).write_text(validation.stdout + validation.stderr, encoding='utf-8')
            if validation.returncode:
                raise AssertionError(validation.stdout + validation.stderr)
            cls.validations += 1
            report = json.loads((args.out / (case + '.res.json')).read_text(encoding='utf-8'))
            cls.modules[case] = fixture.ScalarModule(spv.read_bytes(), report)

    def run_case(self, case, predicate=0):
        return self.modules[case].run(POSITIONS, COLORS, predicate)

    def test_no_ast_guest_register_cannot_cull(self):
        check_result(self.run_case('no_ast_r254'), 0)
        self.assertNotIn(6, self.modules['no_ast_r254'].variables.values())

    def test_mask_store_preserves_guest_r254(self):
        check_result(self.run_case('preserve_r254'), fixture.VALUE)

    def test_guest_register_cannot_overwrite_mask(self):
        check_result(self.run_case('guest_cannot_unmask'), fixture.VALUE, True)

    def test_initialized_zero_and_one_keep_body_and_color(self):
        check_result(self.run_case('zero'), fixture.VALUE, True)
        check_result(self.run_case('one'), fixture.VALUE)

    def test_predicated_updates_and_skipped_store_default(self):
        for case, inverted in (('predicated', False), ('predicated_inverse', True)):
            module = self.modules[case]
            self.assertTrue(any(op == 250 for op, _ in module.ops), 'dynamic predicate must retain actual control flow')
            for value in (0, 1, 2, 0xffffffff):
                with self.subTest(case=case, predicate=value):
                    check_result(self.run_case(case, value), fixture.VALUE, bool(value & 1) != inverted)
        check_result(self.run_case('never'), fixture.VALUE)

    def test_private_state_and_guest_registers_survive_helper_calls(self):
        for case in ('helper_store', 'helper_guest_write'):
            with self.subTest(case=case):
                module = self.modules[case]
                self.assertGreater(len(module.functions), 1)
                self.assertTrue(any(op == 57 for op, _ in module.ops))
                check_result(self.run_case(case), fixture.VALUE, True)

    def test_mask_state_is_a_distinct_private_u32(self):
        for case in CASES:
            if case in ('no_ast_r254', 'indexed'):
                continue
            module = self.modules[case]
            private = [o for op, o in module.ops if op == 59 and o[2] == 6]
            self.assertEqual(len(private), 1, case)
            pointer_type = module.types[private[0][0]]
            self.assertEqual(pointer_type[0], 32)
            self.assertEqual(module.types[pointer_type[1][1]], (21, (32, 0)))

    def test_indexed_ast_is_rejected_without_outputs(self):
        self.assertTrue(self.replies['indexed'].startswith('fail indexed '), self.replies['indexed'])
        self.assertFalse((args.out / 'indexed.spv').exists())
        self.assertFalse((args.out / 'indexed.res.json').exists())

    def test_oracle_and_interpreter_fail_loudly(self):
        result = self.run_case('one')
        check_result(result, fixture.VALUE)
        lost_store = dict(result, record=(1, *([fixture.CANARY] * 5)))
        with self.assertRaisesRegex(AssertionError, 'record/canary'):
            check_result(lost_store, fixture.VALUE)
        lost_position = dict(result, emitted=[(fixture.CLIPPED, COLORS[v]) for v in range(3)])
        with self.assertRaisesRegex(AssertionError, 'position/color'):
            check_result(lost_position, fixture.VALUE)
        module = self.modules['one']
        block = next(iter(module.functions[module.entry]['blocks'].values()))
        block.insert(0, (0xffff, ()))
        try:
            with self.assertRaisesRegex(AssertionError, 'unsupported fixture opcode'):
                self.run_case('one')
        finally:
            block.pop(0)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--xlate', type=Path, required=True)
    parser.add_argument('--spirv-val', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args, rest = parser.parse_known_args()
    result = unittest.main(argv=[__file__] + rest, exit=False).result
    if args.out.is_dir():
        evidence = dict(tests=result.testsRun, failures=len(result.failures), errors=len(result.errors),
                        skipped=len(result.skipped), complete_spirv_validations=getattr(ViewportState, 'validations', 0),
                        translator_sha256=hashlib.sha256(args.xlate.read_bytes()).hexdigest(),
                        translator_identity=args.xlate.with_suffix('.id').read_text().strip(),
                        failure_details=[{'test': str(test), 'traceback': text} for test, text in result.failures],
                        error_details=[{'test': str(test), 'traceback': text} for test, text in result.errors],
                        sources={name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in (
                            'tests/geometry_viewport_fixture.py', 'tests/geometry_viewport_state_test.py',
                            'tools/xlate/patches/0018-native-geometry-passthrough-viewport-mask.patch')},
                        gpu_executed=False,
                        limits='Authored acyclic scalar SPIR-V only; SSBO store/input leaf checks, not general Output/Private/Function store bounds, hardware viewport or Vulkan memory semantics.')
        (args.out / 'result.json').write_text(json.dumps(evidence, indent=2) + '\n', encoding='utf-8')
    raise SystemExit(not result.wasSuccessful())
