"""Original Maxwell viewport-state controls and a bounded scalar SPIR-V interpreter.

No game input, Vulkan loader or GPU. Decoder fields follow pinned InstAst,
InstR2pR, InstCal and InstOp.GetAbsoluteAddress; no predicate/dataflow is guessed.
"""
import struct

import geometry_shader_fixture as geometry
import geometry_passthrough_fixture as portable

CANARY = 0xa5a5a5a5
VALUE = 0x12345678
LOCATION = 6
CLIPPED = (0x40000000, 0x40000000, 0x40000000, 0x3f800000)


def store(register):
    return 0xeed8000000000000 | (4 << 48) | (1 << 45) | (10 << 8) | geometry.PT | register


def mask_store(predicate=7, inverted=False, indexed=False):
    word = geometry.ast(0, 0x3a0, words=1) & ~(15 << 16)
    word |= predicate << 16 | int(inverted) << 19
    if indexed:
        word = (word & ~(255 << 8)) | (12 << 8)
    return word


def program(case):
    prefix = [geometry.movc(10, 3, 0), geometry.movc(11, 3, 1), geometry.movi(8, VALUE)]
    helper = []
    if case == 'no_ast_r254':
        body = [geometry.movi(254, 0), store(254)]
    elif case == 'preserve_r254':
        body = [geometry.movi(254, VALUE), geometry.movi(0, 1), mask_store(), store(254)]
    elif case == 'guest_cannot_unmask':
        body = [geometry.movi(0, 0), mask_store(), geometry.movi(254, 1), store(8)]
    elif case in ('zero', 'one', 'never', 'indexed'):
        body = [geometry.movi(0, int(case == 'one')), geometry.movi(12, 4),
                mask_store(inverted=case == 'never', indexed=case == 'indexed'), store(8)]
    elif case in ('predicated', 'predicated_inverse'):
        # R2P.R: SrcA bits8..15, SrcB20..27, ByteSel0, Ccpr0; mask1 updates P0 only.
        body = [geometry.movc(2, 3, 2), geometry.movi(1, 1),
                0x5cf0000000000000 | geometry.PT | (2 << 8) | (1 << 20),
                geometry.movi(0, 0), mask_store(0, case == 'predicated_inverse'), store(8)]
    elif case in ('helper_store', 'helper_guest_write'):
        body = [geometry.movi(254, VALUE)]
        if case == 'helper_guest_write':
            body += [geometry.movi(0, 0), mask_store()]
        body += ['call', store(254 if case == 'helper_store' else 8)]
        helper = ([geometry.movi(0, 0), mask_store()] if case == 'helper_store' else [geometry.movi(254, 1)])
        helper += [0xe32000000000000f | geometry.PT]  # RET, CccAlways
    else:
        raise ValueError(case)
    ops = prefix + body + [geometry.EXIT]
    if helper:
        call = ops.index('call')
        # framed puts a scheduler before each three instructions. CAL's signed
        # byte delta is relative to the following physical instruction word.
        address = lambda index: 8 * (index + index // 3 + 1)
        delta = address(len(ops)) - address(call) - 8
        ops[call] = 0xe260000000000000 | ((delta & 0xffffff) << 20)
        ops += helper
    header = bytearray(portable.program()[:0x50])
    struct.pack_into('<I', header, 0, struct.unpack_from('<I', header)[0] | (1 << 16))
    return bytes(header) + geometry.framed(ops)


class ScalarModule:
    """Execute only the authored scalar, acyclic, void-call control programs.

    Loads require initialized leaves; SSBO stores require owned record leaves.
    Output/Private/Function stores rely on these validated static fixture shapes,
    without a general type-shape or store-bounds proof. Unsupported operations,
    result types, loops, recursion and excessive steps fail loudly. This is not a
    Vulkan memory model, general engine, float evaluator or hardware viewport oracle.
    """
    def __init__(self, data, report):
        self.ops = list(geometry.instructions(data))
        self.types = {o[0]: (op, o[1:]) for op, o in self.ops if 19 <= op <= 33}
        self.constants = {o[1]: o[2] for op, o in self.ops if op == 43 and len(o) == 3}
        self.constants.update({o[1]: op == 41 for op, o in self.ops if op in (41, 42)})
        self.variables = {o[1]: o[2] for op, o in self.ops if op == 59}
        self.locations = {o[0]: o[2] for op, o in self.ops if op == 71 and o[1] == 30}
        self.members = {(o[0], o[1]): o[3] for op, o in self.ops if op == 72 and o[2] == 11}
        bindings = {o[0]: o[2] for op, o in self.ops if op == 71 and o[1] == 33}
        resources = report['sbufs']
        if len(resources) != 1 or (resources[0]['cb_slot'], resources[0]['cb_offset'], resources[0]['flags']) != (3, 0, 1):
            raise AssertionError('one writable fixed c3-pointer SSBO required')
        self.storage = self.unique([var for var, binding in bindings.items() if binding == resources[0]['binding']])
        cb = report['cbufs']
        if len(cb) > 1 or any(r['slot'] != 3 for r in cb):
            raise AssertionError('unexpected constant resource')
        self.uniform = self.unique([var for var, binding in bindings.items() if cb and binding == cb[0]['binding']]) if cb else None
        self.functions = {}
        current = None
        for op, o in self.ops:
            if op == 54:
                if current is not None or self.types[o[0]][0] != 19:
                    raise AssertionError('only nonnested void functions')
                current = {'params': [], 'blocks': {}, 'locals': []}
                self.functions[o[1]] = current
            elif op == 55:
                if self.types[o[0]][0] != 32:
                    raise AssertionError('only pointer call parameters')
                current['params'].append(o[1])
            elif op == 248 and current is not None:
                block = current['blocks'].setdefault(o[0], [])
            elif op == 56:
                current = None
            elif current is not None:
                block.append((op, o))
                if op == 59:
                    current['locals'].append(o[1])
        self.entry = self.unique([o[1] for op, o in self.ops if op == 15 and o[0] == 3])

    @staticmethod
    def unique(items):
        if len(items) != 1:
            raise AssertionError('expected unique fixture interface')
        return items[0]

    def run(self, positions, colors, predicate=0):
        values = dict(self.constants)
        pointers = {var: (var,) for var in self.variables}
        memory = {}
        def position(storage):
            matches = []
            for op, o in self.ops:
                if op != 59 or o[2] != storage:
                    continue
                type_id = self.types[o[0]][1][1]
                if self.types[type_id][0] == 28:
                    type_id = self.types[type_id][1][0]
                matches += [(o[1], member) for (structure, member), builtin in self.members.items()
                            if structure == type_id and builtin == 0]
            return matches
        input_position = position(1)
        if len(input_position) > 1:
            raise AssertionError('ambiguous input Position')
        for var, member in input_position:
            for vertex, vector in enumerate(positions):
                for component, value in enumerate(vector):
                    memory[var, vertex, member, component] = value
        for var, location in self.locations.items():
            if self.variables[var] == 1:
                if location != LOCATION:
                    raise AssertionError('unexpected user input')
                for vertex, vector in enumerate(colors):
                    for component, value in enumerate(vector):
                        memory[var, vertex, component] = value
        if self.uniform is not None:
            for word, value in enumerate((0, 2, predicate, 0)):
                memory[self.uniform, 0, 0, word] = value
        for word in range(6):
            memory[self.storage, 0, word] = CANARY
        output_position, member = self.unique(position(3))
        output_color = self.unique([var for var, location in self.locations.items()
                                    if location == LOCATION and self.variables[var] == 3])
        emitted, writes, events, calls = [], [], [], []
        steps = 0

        def execute(function, arguments=()):
            nonlocal steps
            if function in calls or len(calls) >= 8:
                raise AssertionError('recursive or excessive fixture calls')
            calls.append(function)
            f = self.functions[function]
            if len(arguments) != len(f['params']):
                raise AssertionError('call parameter count')
            pointers.update(zip(f['params'], arguments))
            for var in f['locals']:
                for key in list(memory):
                    if key[0] == var:
                        del memory[key]
            label, previous = next(iter(f['blocks'])), None
            while True:
                block = f['blocks'][label]
                phis = {}
                for op, o in block:
                    if op == 245:
                        sources = [value for value, parent in zip(o[2::2], o[3::2]) if parent == previous]
                        phis[o[1]] = values[self.unique(sources)]
                values.update(phis)
                next_label = None
                for op, o in block:
                    steps += 1
                    if steps > 10000:
                        raise AssertionError('fixture step bound')
                    if op in (61, 83, 124, 168, 169, 170, 171, 199, 245) and self.types[o[0]] not in (
                            (20, ()), (21, (32, 0)), (21, (32, 1)), (22, (32,))):
                        raise AssertionError('unsupported nonscalar fixture result type')
                    if op in (245, 247):
                        continue
                    if op == 59:
                        if o[2] != 7 or len(o) != 3:
                            raise AssertionError('unsupported function variable')
                    elif op == 65:
                        pointers[o[1]] = pointers[o[2]] + tuple(values[i] for i in o[3:])
                    elif op == 61:
                        if pointers[o[2]] not in memory:
                            raise AssertionError('undefined or out-of-range fixture load')
                        values[o[1]] = memory[pointers[o[2]]]
                    elif op == 62:
                        p = pointers[o[0]]
                        if p[0] == self.storage:
                            if p not in memory:
                                raise AssertionError('out-of-range fixed storage write')
                            writes.append((p[-1], values[o[1]])); events.append('store')
                        elif self.variables.get(p[0]) not in (3, 6, 7):
                            raise AssertionError('unexpected write storage class')
                        memory[p] = values[o[1]]
                    elif op in (83, 124):
                        values[o[1]] = values[o[2]]
                    elif op in (170, 171):
                        values[o[1]] = (values[o[2]] == values[o[3]]) == (op == 170)
                    elif op == 199:
                        values[o[1]] = values[o[2]] & values[o[3]]
                    elif op == 168:
                        values[o[1]] = not values[o[2]]
                    elif op == 169:
                        values[o[1]] = values[o[3] if values[o[2]] else o[4]]
                    elif op == 57:
                        execute(o[2], [pointers[arg] for arg in o[3:]])
                    elif op == 249:
                        next_label = o[0]; break
                    elif op == 250:
                        next_label = o[1] if values[o[0]] else o[2]; break
                    elif op == 253:
                        calls.pop(); return
                    elif op == 218:
                        emitted.append((tuple(memory[output_position, member, c] for c in range(4)),
                                        tuple(memory[output_color, c] for c in range(4))))
                        events.append('emit')
                        for key in list(memory):
                            if self.variables.get(key[0]) == 3:
                                del memory[key]
                    elif op == 219:
                        events.append('cut')
                    else:
                        raise AssertionError('unsupported fixture opcode ' + str(op))
                if next_label is None:
                    raise AssertionError('unterminated fixture block')
                previous, label = label, next_label
        execute(self.entry)
        return {'record': tuple(memory[self.storage, 0, word] for word in range(6)),
                'writes': writes, 'emitted': emitted, 'events': events, 'steps': steps}
