"""Edit all F1 settings before starting dkb.exe.

    .venv\\Scripts\\python.exe tools\\launch_dkb.py
    .venv\\Scripts\\python.exe tools\\launch_dkb.py --exe build\\dkb100\\dkb.exe

The selected values are written to the totk.ini beside the executable. Settings whose
environment overrides are changed in the launcher have those overrides removed only from
the game process it starts.
"""
import argparse
import json
import math
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EXE = ROOT / 'build' / 'dkb100' / 'dkb.exe'
EXAMPLE_INI = ROOT / 'totk.ini.example'
COLOR_PRESETS = {
    'default': ('0', '100', '100', '1'),
    'vivid': ('0', '115', '135', '0.95'),
    'clarity': ('2', '110', '115', '1'),
}
COLOR_KEYS = ('COLOR_BRIGHTNESS', 'COLOR_CONTRAST', 'COLOR_SATURATION', 'COLOR_GAMMA')
SPECIAL_LABELS = {
    'LOD': 'LOD', 'GPU': 'GPU', 'FPS': 'FPS', 'FOV': 'FOV', 'ASTC': 'ASTC', 'GPL': 'GPL',
    'VK': 'Vulkan', 'A2C': 'A2C', 'XLATE': 'Translator', 'NVN': 'NVN',
}


def normalize_key(key):
    key = key.strip().upper()
    return key[5:] if key.startswith('TOTK_') else key


def load_schema(exe):
    exe = Path(exe).resolve()
    if not exe.is_file():
        raise FileNotFoundError(exe)
    run = subprocess.run([str(exe), '--settings-schema'], cwd=str(exe.parent), capture_output=True, text=True,
                         encoding='utf-8', errors='replace', timeout=20)
    for line in reversed(run.stdout.splitlines()):
        if line.startswith('{'):
            try:
                schema = json.loads(line)
            except json.JSONDecodeError:
                continue
            if run.returncode == 0 and isinstance(schema.get('settings'), list) and isinstance(schema.get('bindings'), list):
                return schema
    detail = (run.stderr + '\n' + run.stdout).strip()[-2000:]
    raise RuntimeError(f'{exe} cannot provide the F1 settings catalog. Rebuild it with this repository version.\n{detail}')


def _line_ending(line):
    return line[len(line.rstrip('\r\n')):]


def _active_key(line):
    body = line.rstrip('\r\n')
    if not body.strip() or body.lstrip().startswith(('#', ';', '[')):
        return None
    match = re.match(r'^(\s*)((?:totk_)?[a-z0-9_]+)(\s*=\s*)(.*?)(\s*)$', body, re.IGNORECASE)
    return (normalize_key(match.group(2)), match) if match else None


def _commented_key(line):
    body = line.rstrip('\r\n')
    match = re.match(r'^\s*[#;]\s*((?:totk_)?[a-z0-9_]+)\s*=.*$', body, re.IGNORECASE)
    return normalize_key(match.group(1)) if match else None


def update_ini_text(text, updates, sections):
    """Apply key -> string/None updates while preserving all unrelated lines and comments."""
    lines = text.splitlines(keepends=True)
    newline = next((ending for line in lines if (ending := _line_ending(line))), '\n')
    for raw_key, value in updates.items():
        key = normalize_key(raw_key)
        active = [(i, found[1]) for i, line in enumerate(lines) if (found := _active_key(line)) and found[0] == key]
        if value is None or value == '':
            for i, _match in reversed(active):
                del lines[i]
            continue
        if '\n' in value or '\r' in value:
            raise ValueError(f'{key} cannot contain a newline')
        if active:
            i, match = active[-1]
            lines[i] = match.group(1) + match.group(2) + match.group(3) + value + match.group(5) + _line_ending(lines[i])
            continue

        comment_at = next((i for i, line in enumerate(lines) if _commented_key(line) == key), None)
        if comment_at is not None:
            lines.insert(comment_at + 1, f'{key.lower()} = {value}{newline}')
            continue

        section = sections.get(key)
        section_start, section_end = None, len(lines)
        if section:
            wanted = section.casefold()
            for i, line in enumerate(lines):
                match = re.match(r'^\s*\[([^]]+)\]\s*$', line.rstrip('\r\n'))
                if match:
                    if match.group(1).strip().casefold() == wanted:
                        section_start = i
                    elif section_start is not None:
                        section_end = i
                        break
        if section_start is not None:
            if section_end and not lines[section_end - 1].endswith(('\n', '\r')):
                lines[section_end - 1] += newline
            lines.insert(section_end, f'{key.lower()} = {value}{newline}')
            continue

        if lines and not lines[-1].endswith(('\n', '\r')):
            lines[-1] += newline
        if lines and lines[-1].strip():
            lines.append(newline)
        if section:
            lines.extend((f'[{section}]{newline}', f'{key.lower()} = {value}{newline}'))
        else:
            lines.append(f'{key.lower()} = {value}{newline}')
    return ''.join(lines)


def write_ini_updates(path, updates, sections, example=None):
    path = Path(path)
    exists = path.is_file()
    if not exists and not updates:
        return path
    source = path if exists else Path(example) if example and Path(example).is_file() else None
    if source:
        raw = source.read_bytes()
        bom = raw.startswith(b'\xef\xbb\xbf')
        text = raw[3:].decode('utf-8') if bom else raw.decode('utf-8')
    else:
        bom, text = False, ''
    updated = update_ini_text(text, updates, sections)
    if exists and updated == text:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_bytes((b'\xef\xbb\xbf' if bom else b'') + updated.encode('utf-8'))
    os.replace(tmp, path)
    return path


def launch_environment(environment=None, clear_keys=()):
    env = dict(os.environ if environment is None else environment)
    remove = {f'{prefix}_{normalize_key(key)}'.upper() for key in clear_keys for prefix in ('TOTK', 'DKB')}
    for key in list(env):
        if key.upper() in remove:
            del env[key]
    return env


def start_game(exe, updates, sections, environment=None, example=None):
    exe = Path(exe).resolve()
    if not exe.is_file():
        raise FileNotFoundError(exe)
    write_ini_updates(exe.with_name('totk.ini'), updates, sections, example)
    return subprocess.Popen([str(exe)], cwd=str(exe.parent), env=launch_environment(environment, updates.keys()))


def _friendly(key):
    words = key.split('_')
    return ' '.join(SPECIAL_LABELS.get(word, word.title()) for word in words)


def _description(example, key):
    if not Path(example).is_file():
        return ''
    comments = []
    pattern = re.compile(r'^\s*[#;]\s*(?:totk_)?' + re.escape(key) + r'\s*=.*$', re.IGNORECASE)
    for line in Path(example).read_text(encoding='utf-8-sig').splitlines():
        stripped = line.strip()
        if not stripped:
            comments.clear()
            continue
        if stripped.startswith('['):
            comments.clear()
            continue
        if stripped.startswith(('#', ';')):
            if pattern.match(line):
                return ' '.join(comments[-3:])
            comments.append(stripped[1:].strip())
            continue
        comments.clear()
    return ''


def _choices(spec):
    menu_values = spec.get('menu_values')
    if menu_values is not None:
        return menu_values.split('|'), bool(spec.get('menu_values_editable'))
    values = spec.get('values')
    if not values or '..' in values:
        return None, False
    choices = values.split('|')
    if spec.get('default') == '' and '' not in choices:
        choices.insert(0, '')
    return choices, False


def _choice_label(value, key):
    if value == '':
        if key == 'INPUT_RECORD':
            return 'Off'
        return 'Automatic / default'
    if key == 'INPUT_RECORD' and value == '@default':
        return 'On (logs/input-record.txt)'
    labels = {
        'default': 'Default', 'game': 'Game default', 'auto': 'Automatic',
        'xbox_prompts': 'Xbox prompts', 'zh-Hans': 'Simplified Chinese', 'zh-Hant': 'Traditional Chinese',
        'en-US': 'English', 'immediate': 'Immediate', 'fifo': 'VSync', 'mailbox': 'Mailbox',
        'strict': 'Strict', 'any': 'Any', 'fullscreen': 'Fullscreen', 'fsr': 'FSR', 'bilinear': 'Bilinear',
        'docked': 'Docked', 'handheld': 'Handheld', 'vivid': 'Vivid', 'clarity': 'Clarity', 'custom': 'Custom',
    }
    if value in labels:
        return labels[value]
    if value == '0':
        return 'Off'
    if value == '1':
        return 'On'
    if key == 'ASYNC_PREEMPT' and value == '2':
        return 'Priority inheritance'
    if key in ('WINDOW_SIZE', 'OUTPUT_RES') and re.fullmatch(r'\d+x\d+', value):
        return value.replace('x', ' × ')
    return value


def _validate_binding(action, value, schema):
    if not value.strip():
        return
    keys = {x.casefold() for x in schema.get('binding_keys', [])}
    pads = {x.casefold() for x in schema.get('binding_pads', [])}
    stick = action.startswith(('LSTICK_', 'RSTICK_'))
    key_count = pad_count = 0
    key_default = pad_default = False
    tokens = [token.strip() for token in value.split(',')]
    if 'none' in (token.casefold() for token in tokens):
        if len(tokens) != 1 or tokens[0].casefold() != 'none':
            raise ValueError(f'{action}: none must be used by itself')
        return
    for token in tokens:
        low = token.casefold()
        if low.startswith('key:'):
            name = low[4:]
            if name not in keys | {'default'}:
                raise ValueError(f'{action}: unknown key binding {token!r}')
            key_default |= name == 'default'
            key_count += name != 'default'
        elif low.startswith('pad:') and not stick:
            name = low[4:]
            if name not in pads | {'default'}:
                raise ValueError(f'{action}: unknown controller binding {token!r}')
            pad_default |= name == 'default'
            pad_count += name != 'default'
        else:
            raise ValueError(f'{action}: expected key:<name>, pad:<name>, or none')
    if key_default and key_count or pad_default and pad_count:
        raise ValueError(f'{action}: default cannot be combined with explicit bindings for the same device')
    if key_count > 2 or pad_count > 1:
        raise ValueError(f'{action}: F1 supports at most two keys and one controller button')


def _validate_setting(spec, value, schema):
    if value == '':
        return
    choices, editable = _choices(spec)
    if choices is not None and not editable and value not in choices:
        raise ValueError(f'{spec["key"]}: choose one of {", ".join(choices)}')
    accepted = spec.get('values') or ''
    if '..' in accepted:
        lo, hi = accepted.split('..', 1)
        try:
            number = float(value)
        except ValueError as error:
            raise ValueError(f'{spec["key"]}: enter a number from {lo} to {hi}') from error
        if not math.isfinite(number) or number < float(lo) or number > float(hi):
            raise ValueError(f'{spec["key"]}: enter a number from {lo} to {hi}')
    if spec['key'] in ('WINDOW_SIZE', 'OUTPUT_RES'):
        match = re.fullmatch(r'(\d{3,5})x(\d{3,5})', value)
        if not match or not (640 <= int(match.group(1)) <= 7680 and 360 <= int(match.group(2)) <= 4320):
            raise ValueError(f'{spec["key"]}: enter a width x height such as 1920x1080')
    if spec['key'] == 'GPU_INDEX' and (not value.isdecimal() or int(value) > 7):
        raise ValueError('GPU_INDEX: enter an available zero-based adapter index from 0 to 7, or leave it blank for Auto')


def _range_note(spec):
    if spec.get('key') == 'GPU_INDEX':
        return 'Vulkan adapter index (0–7); blank selects Auto'
    values = spec.get('values') or ''
    return f'Range: {values.replace("..", " to ")}' if '..' in values else ''


def persisted_setting_value(key, value, exe):
    if key == 'INPUT_RECORD' and value == '@default':
        return str(Path(exe).resolve().parent / 'logs' / 'input-record.txt')
    return value


class ScrollPage:
    def __init__(self, parent, tk, ttk):
        self.frame = ttk.Frame(parent)
        self.canvas = tk.Canvas(self.frame, highlightthickness=0)
        self.scrollbar = ttk.Scrollbar(self.frame, orient='vertical', command=self.canvas.yview)
        self.body = ttk.Frame(self.canvas, padding=(8, 8, 16, 8))
        self.window = self.canvas.create_window((0, 0), window=self.body, anchor='nw')
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.pack(side='left', fill='both', expand=True)
        self.scrollbar.pack(side='right', fill='y')
        self.body.bind('<Configure>', lambda _e: self.canvas.configure(scrollregion=self.canvas.bbox('all')))
        self.canvas.bind('<Configure>', lambda e: self.canvas.itemconfigure(self.window, width=e.width))
        self.canvas.bind('<MouseWheel>', lambda e: self.canvas.yview_scroll(-int(e.delta / 120), 'units'))


class App:
    def __init__(self, root, exe):
        import tkinter as tk
        from tkinter import ttk
        self.tk, self.ttk, self.root = tk, ttk, root
        self.exe = Path(exe).resolve()
        self.schema = load_schema(self.exe)
        self.example = self.exe.with_name('totk.ini.example')
        if not self.example.is_file():
            self.example = EXAMPLE_INI
        self.setting_fields = {}
        self.binding_fields = {}
        self._color_sync = False
        root.title('DKB Settings Launcher')
        root.geometry('980x780')
        root.minsize(760, 580)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(1, weight=1)

        top = ttk.Frame(root, padding=10)
        top.grid(row=0, column=0, sticky='ew')
        top.columnconfigure(1, weight=1)
        ttk.Label(top, text='Game executable').grid(row=0, column=0, sticky='w', padx=(0, 8))
        self.exe_var = tk.StringVar(value=str(self.exe))
        ttk.Entry(top, textvariable=self.exe_var, state='readonly').grid(row=0, column=1, sticky='ew')
        ttk.Button(top, text='Browse…', command=self.browse).grid(row=0, column=2, padx=(8, 0))

        self.notebook = ttk.Notebook(root)
        self.notebook.grid(row=1, column=0, sticky='nsew', padx=10)
        self.notice = ttk.Label(root, text='Unsupported options are shown with a warning; changes remain in totk.ini.',
                                foreground='#8a5a00', padding=(10, 5))
        self.notice.grid(row=2, column=0, sticky='w')
        bottom = ttk.Frame(root, padding=10)
        bottom.grid(row=3, column=0, sticky='ew')
        bottom.columnconfigure(0, weight=1)
        self.status = tk.StringVar(value='')
        ttk.Label(bottom, textvariable=self.status).grid(row=0, column=0, sticky='w')
        ttk.Button(bottom, text='Close', command=root.destroy).grid(row=0, column=1, padx=(0, 8))
        ttk.Button(bottom, text='Launch', command=self.launch_game).grid(row=0, column=2)
        self.build_tabs()

    def _has_changes(self):
        return any(self._raw(field) != field['initial'] for field in self.setting_fields.values()) or any(
            field['var'].get().strip() != field['initial'] for field in self.binding_fields.values())

    def browse(self):
        from tkinter import filedialog, messagebox
        if self._has_changes() and not messagebox.askyesno(
                'DKB Settings Launcher', 'Discard unsaved launcher changes and choose another game executable?'):
            return
        chosen = filedialog.askopenfilename(title='Choose dkb.exe', filetypes=[('Game executable', 'dkb.exe'),
                                                                                ('Executables', '*.exe')])
        if not chosen:
            return
        try:
            schema = load_schema(chosen)
        except Exception as error:
            messagebox.showerror('DKB Settings Launcher', str(error))
            return
        self.exe = Path(chosen).resolve()
        self.exe_var.set(str(self.exe))
        self.schema = schema
        self.example = self.exe.with_name('totk.ini.example')
        if not self.example.is_file():
            self.example = EXAMPLE_INI
        self.build_tabs()

    def _field_row(self, parent, spec, binding=False):
        tk, ttk = self.tk, self.ttk
        key = spec['key']
        current = spec.get('current', '') or ''
        row = ttk.Frame(parent, padding=(4, 3))
        row.pack(fill='x')
        row.columnconfigure(1, weight=1)
        label_key = key[5:] if binding else key
        ttk.Label(row, text=_friendly(label_key)).grid(row=0, column=0, sticky='w', padx=(0, 12))
        if binding:
            var = tk.StringVar(value=current)
            ttk.Entry(row, textvariable=var, width=50).grid(row=0, column=1, sticky='ew')
            field = {'var': var, 'initial': current, 'spec': spec, 'kind': 'binding'}
            self.binding_fields[key] = field
            note = 'Empty = default. Use key:<name>, pad:<name>, or none.'
        else:
            choices, editable = _choices(spec)
            if choices is not None:
                raw_to_label, label_to_raw = {}, {}
                shown = []
                for value in choices:
                    name = _choice_label(value, key)
                    if name in label_to_raw and label_to_raw[name] != value:
                        name = f'{name} ({value})'
                    shown.append(name)
                    raw_to_label[value] = name
                    label_to_raw[name] = value
                if current not in raw_to_label:
                    name = f'Current value: {current}'
                    shown.append(name)
                    raw_to_label[current] = name
                    label_to_raw[name] = current
                var = tk.StringVar(value=raw_to_label[current])
                widget = ttk.Combobox(row, textvariable=var, values=shown,
                                      state='normal' if editable else 'readonly', width=32)
                widget.grid(row=0, column=1, sticky='ew')
                field = {'var': var, 'initial': current, 'spec': spec, 'kind': 'choice',
                         'label_to_raw': label_to_raw, 'editable': editable}
                self.setting_fields[key] = field
                if key == 'COLOR_PRESET':
                    widget.bind('<<ComboboxSelected>>', lambda _e: self._preset_changed())
                note = ''
            else:
                var = tk.StringVar(value=current)
                ttk.Entry(row, textvariable=var, width=36).grid(row=0, column=1, sticky='ew')
                field = {'var': var, 'initial': current, 'spec': spec, 'kind': 'text'}
                self.setting_fields[key] = field
                note = _range_note(spec)
            if key in COLOR_KEYS:
                var.trace_add('write', lambda *_args, k=key: self._color_changed(k))
        notes = [note] if note else []
        if not binding and not spec.get('live', False):
            notes.append('After restart')
        if spec.get('from_env'):
            notes.append('Environment overrides current value; editing removes that override for this launch')
        if not binding and not spec.get('supported', True):
            notes.append('Not applied by this game build')
        if notes:
            ttk.Label(row, text=' · '.join(notes), foreground='#8a5a00', wraplength=820).grid(
                row=1, column=1, sticky='w', pady=(2, 0))
        desc = _description(self.example, label_key)
        if desc:
            ttk.Label(row, text=desc, foreground='#555555', wraplength=820).grid(
                row=2, column=1, sticky='w', pady=(2, 0))

    def build_tabs(self):
        for tab in self.notebook.tabs():
            self.notebook.nametowidget(tab).destroy()
        self.setting_fields.clear()
        self.binding_fields.clear()
        tabs = ('Screen', 'Game', 'Sound', 'Controls', 'Menu', 'Advanced')
        pages, groups = {}, {}
        for tab in tabs:
            page = ScrollPage(self.notebook, self.tk, self.ttk)
            self.notebook.add(page.frame, text=tab)
            pages[tab], groups[tab] = page.body, {}
        for spec in self.schema.get('settings', []):
            tab = spec.get('tab', 'Advanced')
            group = spec.get('group', '')
            parent = groups.get(tab, {}).get(group)
            if parent is None:
                parent = self.ttk.LabelFrame(pages.get(tab, pages['Advanced']), text=group or tab, padding=6)
                parent.pack(fill='x', expand=True, pady=(0, 8))
                groups.setdefault(tab, {})[group] = parent
            self._field_row(parent, spec)
        bindings = self.ttk.LabelFrame(pages['Controls'], text='Button and stick bindings', padding=6)
        bindings.pack(fill='x', expand=True, pady=(0, 8))
        self.ttk.Label(bindings, text='Examples: key:e, pad:east; key:default; none',
                       foreground='#555555').pack(anchor='w', padx=4, pady=(0, 4))
        for spec in self.schema.get('bindings', []):
            self._field_row(bindings, spec, binding=True)

    def _raw(self, field):
        value = field['var'].get().strip()
        if field.get('kind') == 'choice':
            return field['label_to_raw'].get(value, value)
        return value

    def _set_raw(self, key, value):
        field = self.setting_fields.get(key)
        if not field:
            return
        if field.get('kind') == 'choice':
            reverse = {raw: label for label, raw in field['label_to_raw'].items()}
            field['var'].set(reverse.get(value, f'Current value: {value}'))
        else:
            field['var'].set(value)

    def _preset_changed(self):
        if self._color_sync or 'COLOR_PRESET' not in self.setting_fields:
            return
        preset = self._raw(self.setting_fields['COLOR_PRESET'])
        values = COLOR_PRESETS.get(preset)
        if values:
            self._color_sync = True
            for key, value in zip(COLOR_KEYS, values):
                self._set_raw(key, value)
            self._color_sync = False

    def _color_changed(self, _key):
        if self._color_sync or 'COLOR_PRESET' not in self.setting_fields:
            return
        if self._raw(self.setting_fields['COLOR_PRESET']) in COLOR_PRESETS:
            self._color_sync = True
            self._set_raw('COLOR_PRESET', 'custom')
            self._color_sync = False

    def _collect_updates(self):
        updates, sections = {}, {}
        for key, field in self.setting_fields.items():
            value = self._raw(field)
            if value != field['initial']:
                _validate_setting(field['spec'], value, self.schema)
                value = persisted_setting_value(key, value, self.exe_var.get())
                updates[key] = value or None
                sections[key] = field['spec'].get('section') or 'performance'
        for key, field in self.binding_fields.items():
            value = field['var'].get().strip()
            if value != field['initial']:
                _validate_binding(key[5:], value, self.schema)
                updates[key] = value or None
                sections[key] = 'input'
        return updates, sections

    def launch_game(self):
        from tkinter import messagebox
        try:
            exe = Path(self.exe_var.get().strip()).resolve()
            updates, sections = self._collect_updates()
            example = exe.with_name('totk.ini.example')
            if not example.is_file():
                example = EXAMPLE_INI
            process = start_game(exe, updates, sections, os.environ, example)
        except Exception as error:
            messagebox.showerror('DKB Settings Launcher', str(error))
            return
        self.status.set(f'Launched (PID {process.pid}). Settings saved to {exe.with_name("totk.ini")}')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--exe', type=Path, default=DEFAULT_EXE, help='game executable (default: %(default)s)')
    args = parser.parse_args(argv)
    import tkinter as tk
    from tkinter import messagebox
    root = tk.Tk()
    try:
        App(root, args.exe)
    except Exception as error:
        messagebox.showerror('DKB Settings Launcher', str(error), parent=root)
        root.destroy()
        return 1
    root.mainloop()
    return 0


if __name__ == '__main__':
    sys.exit(main())
