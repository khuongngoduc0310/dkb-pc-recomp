"""Pre-launch F1 settings editor tests. No game session or Vulkan device."""
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import launch_dkb


class IniSettingsTest(unittest.TestCase):
    def test_updates_values_without_reformatting_other_lines(self):
        text = ('[display]\r\n# keep this comment\r\nfullscreen = 1\r\n\r\n'
                '[performance]\r\n# keep this too\r\nreplay_async = 1\r\n')
        got = launch_dkb.update_ini_text(text, {'FULLSCREEN': '0', 'LOD_DISTANCE': 'quarter'},
                                        {'FULLSCREEN': 'display', 'LOD_DISTANCE': 'performance'})
        self.assertEqual(got, ('[display]\r\n# keep this comment\r\nfullscreen = 0\r\n\r\n'
                               '[performance]\r\n# keep this too\r\nreplay_async = 1\r\n'
                               'lod_distance = quarter\r\n'))

    def test_updates_repeated_key_where_later_value_wins(self):
        text = '[performance]\nreplay_async = 0\nREPLAY_ASYNC = 1\n'
        self.assertEqual(launch_dkb.update_ini_text(text, {'replay_async': '0'}, {'REPLAY_ASYNC': 'performance'}),
                         '[performance]\nreplay_async = 0\nREPLAY_ASYNC = 0\n')

    def test_empty_value_removes_active_key_but_keeps_comment(self):
        text = '[input]\n# motion = 1\nmotion = 0\n'
        self.assertEqual(launch_dkb.update_ini_text(text, {'MOTION': None}, {'MOTION': 'input'}),
                         '[input]\n# motion = 1\n')

    def test_missing_section_is_added_atomically_with_bom(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'totk.ini'
            path.write_bytes(b'\xef\xbb\xbf[display]\r\nfullscreen = 1\r\n')
            launch_dkb.write_ini_updates(path, {'LOD_DISTANCE': 'half'}, {'LOD_DISTANCE': 'performance'})
            self.assertEqual(path.read_bytes(), b'\xef\xbb\xbf[display]\r\nfullscreen = 1\r\n\r\n'
                             b'[performance]\r\nlod_distance = half\r\n')
            self.assertFalse(path.with_name('totk.ini.tmp').exists())

    def test_missing_ini_uses_example_and_preserves_its_comments(self):
        with tempfile.TemporaryDirectory() as tmp:
            example = Path(tmp) / 'example.ini'
            example.write_text('[performance]\n# lod_distance = default\n# keep\n', encoding='utf-8')
            path = Path(tmp) / 'totk.ini'
            launch_dkb.write_ini_updates(path, {'LOD_DISTANCE': 'quarter'}, {'LOD_DISTANCE': 'performance'}, example)
            self.assertEqual(path.read_text(encoding='utf-8'),
                             '[performance]\n# lod_distance = default\nlod_distance = quarter\n# keep\n')

    def test_no_change_does_not_create_ini(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'totk.ini'
            launch_dkb.write_ini_updates(path, {}, {})
            self.assertFalse(path.exists())


class SchemaAndLaunchTest(unittest.TestCase):
    def test_schema_command_parses_json_after_host_log_lines(self):
        schema = {'settings': [{'key': 'LOD_DISTANCE'}], 'bindings': []}
        run = subprocess.CompletedProcess([], 0, '[host] cfg: read settings\n' + json.dumps(schema) + '\n', '')
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / 'dkb.exe'
            exe.write_bytes(b'fake')
            with patch.object(launch_dkb.subprocess, 'run', return_value=run):
                self.assertEqual(launch_dkb.load_schema(exe), schema)

    def test_child_environment_clears_only_edited_setting_aliases(self):
        env = launch_dkb.launch_environment(
            {'TOTK_LOD_DISTANCE': 'half', 'dkb_lod_distance': 'quarter', 'TOTK_TRACE': 'perf'},
            {'LOD_DISTANCE'})
        self.assertEqual(env, {'TOTK_TRACE': 'perf'})

    def test_start_writes_changed_values_and_launches_from_exe_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / 'dkb.exe'
            exe.write_bytes(b'fake executable')
            env = {'TOTK_LOD_DISTANCE': 'default', 'TOTK_PROFILE_DIR': 'profile'}
            with patch.object(launch_dkb.subprocess, 'Popen') as popen:
                child = launch_dkb.start_game(exe, {'LOD_DISTANCE': 'half'}, {'LOD_DISTANCE': 'performance'}, env)
            args, kwargs = popen.call_args
            self.assertEqual(args[0], [str(exe.resolve())])
            self.assertEqual(kwargs['cwd'], str(exe.parent))
            self.assertEqual(kwargs['env'], {'TOTK_PROFILE_DIR': 'profile'})
            self.assertEqual(child, popen.return_value)
            self.assertEqual((Path(tmp) / 'totk.ini').read_text(encoding='utf-8'),
                             '[performance]\nlod_distance = half\n')

    def test_built_bananza_schema_covers_f1_tabs_and_bindings(self):
        exe = ROOT / 'build' / 'dkb100' / 'dkb.exe'
        if not exe.is_file():
            self.skipTest('build/dkb100/dkb.exe is not present')
        schema = launch_dkb.load_schema(exe)
        rows = {row['key']: row for row in schema['settings']}
        menu_source = (ROOT / 'runtime' / 'ui' / 'menu.cpp').read_text(encoding='utf-8')
        menu_keys = set(re.findall(r'"TOTK_([A-Z0-9_]+)"', menu_source))
        menu_keys -= {'FPS60', 'MENU_TEST', 'MENU_TEST_DIR', 'MENU_TEST_SET', 'SAVE'}
        self.assertEqual(set(rows), menu_keys)
        self.assertEqual({row['tab'] for row in rows.values()},
                         {'Screen', 'Game', 'Sound', 'Controls', 'Menu', 'Advanced'})
        input_source = (ROOT / 'runtime' / 'input_map.c').read_text(encoding='utf-8')
        action_table = input_source.split('action_names[ACTION_COUNT] = {', 1)[1].split('};', 1)[0]
        actions = set(re.findall(r'"([A-Z][A-Z0-9_]*)"', action_table))
        self.assertEqual({row['action'] for row in schema['bindings']}, actions)
        self.assertEqual(len(schema['bindings']), 24)
        self.assertFalse(rows['RENDER_SCALE']['supported'])
        self.assertFalse(rows['FPS_LIMIT']['supported'])
        self.assertEqual(rows['INPUT_RECORD']['menu_values'], '|@default')
        self.assertEqual(launch_dkb._choices(rows['LIB_PREBUILD_THREADS'])[0],
                         ['', '1', '2', '4', '6', '8', '12', '16', '24', '32', '48', '64'])

    def test_binding_validation_matches_game_token_grammar(self):
        schema = {'binding_keys': ['e', 'lshift', 'w'], 'binding_pads': ['east', 'south']}
        launch_dkb._validate_binding('A', 'key:e, pad:east', schema)
        launch_dkb._validate_binding('LSTICK_UP', 'key:w', schema)
        launch_dkb._validate_binding('B', 'none', schema)
        with self.assertRaises(ValueError):
            launch_dkb._validate_binding('A', 'pad:unknown', schema)
        with self.assertRaises(ValueError):
            launch_dkb._validate_binding('LSTICK_UP', 'pad:south', schema)
        with self.assertRaises(ValueError):
            launch_dkb._validate_binding('A', 'none, key:e', schema)
        with self.assertRaises(ValueError):
            launch_dkb._validate_binding('A', 'key:e, key:w, key:lshift', schema)
        with self.assertRaises(ValueError):
            launch_dkb._validate_binding('A', 'pad:east, pad:south', schema)

    def test_setting_range_and_choice_validation(self):
        schema = {}
        launch_dkb._validate_setting({'key': 'RUMBLE_CURVE', 'values': '0.2..1.5'}, '0.6', schema)
        launch_dkb._validate_setting({'key': 'LOD_DISTANCE', 'values': 'default|half|quarter'}, 'quarter', schema)
        with self.assertRaises(ValueError):
            launch_dkb._validate_setting({'key': 'RUMBLE_CURVE', 'values': '0.2..1.5'}, '2', schema)
        with self.assertRaises(ValueError):
            launch_dkb._validate_setting({'key': 'LOD_DISTANCE', 'values': 'default|half|quarter'}, 'fifth', schema)
        launch_dkb._validate_setting({'key': 'INPUT_RECORD', 'menu_values': '|@default'}, '@default', schema)
        launch_dkb._validate_setting({'key': 'GPU_INDEX'}, '0', schema)
        with self.assertRaises(ValueError):
            launch_dkb._validate_setting({'key': 'GPU_INDEX'}, '8', schema)

    def test_default_input_record_path_matches_the_log_folder(self):
        exe = ROOT / 'build' / 'dkb100' / 'dkb.exe'
        self.assertEqual(launch_dkb.persisted_setting_value('INPUT_RECORD', '@default', exe),
                         str(exe.resolve().parent / 'logs' / 'input-record.txt'))


if __name__ == '__main__':
    unittest.main()
