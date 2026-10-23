"""Bounded PS1 compiler probes and conservative, read-only call-boundary evidence.

These diagnostics cannot accept a match or prove an ABI. C declarations are retained
verbatim; the deliberately small lexical reader does not resolve typedefs, macros,
conditional preprocessing, function pointers, or K&R definitions.
"""
from __future__ import annotations

import difflib
import hashlib
import itertools
import json
import re
import shutil
import subprocess
from dataclasses import asdict
from pathlib import Path

from . import compiler


def _digest(data: str | bytes) -> str:
    return hashlib.sha256(data.encode() if isinstance(data, str) else data).hexdigest()


def microcases() -> dict[str, str]:
    """The four frozen reductions from docs/plans/slus-reload-repros.py, unchanged."""
    locals_ = '\n'.join(f'  int x{i} = in[{i}];' for i in range(10))
    updates = '\n'.join(f'    x{i} += step({i});' for i in range(10))
    uses = '+'.join(f'x{i}' for i in range(10))
    stack = ('extern int step(int);\nint probe(int *out,int *in,int n) {\n' + locals_ +
             '\n RESERVE\n while(n--) {\n' + updates + '\n }\n *out=' + uses + '; return x0;\n}\n')
    grid = '''extern void draw(int,int,int,int,unsigned int,int);
void probe(const unsigned char *s,int bx,int by) {
 int i; unsigned int ch;
 for(i=0;i<102;i++) {
  ch=*s++;
  if(ch && ch<96) draw(bx+REMAINDER*14+2,by+(i/17)*14-1,15,15,ch,0);
 }
}
'''
    return {'stack_plain': stack.replace('RESERVE', ''),
            'stack_reserve': stack.replace('RESERVE', 'volatile int reserved[2];'),
            'grid_plain': grid.replace('REMAINDER', '(i%17)'),
            'grid_quotient': grid.replace('REMAINDER', '(i-(i/17)*17)')}


def tool_fingerprints(cfg: compiler.CompilerConfig) -> dict:
    """Hash local files actually named by the recipe and its mounted tool directories.

    No version commands or downloads are needed. Hash helpers as well as the driver:
    the same GCC driver with a different cc1 or maspsx is a different experiment.
    WSL/container-only files are explicitly outside this host-side fingerprint.
    """
    candidates = set()
    executable = shutil.which(cfg.cmd[0]) if cfg.cmd else None
    if executable:
        candidates.add(Path(executable).resolve())
    def add_file(value):
        try:
            if '\n' not in value and len(value) < 1024 and Path(value).is_file():
                candidates.add(Path(value).resolve())
        except OSError:
            pass  # a shell fragment, not a filesystem path
    for arg in cfg.cmd:
        add_file(arg)
    # Recognize paths embedded in the trusted shell recipe (including paths with spaces).
    for arg in cfg.cmd:
        for value in re.findall(r'"([^"\n]+)"', arg):
            if value.startswith('/'):
                add_file(value)
    for directory in cfg.ro_paths:
        root = Path(directory)
        if not root.is_dir():
            continue
        for name in ('gcc', 'cc1', 'cpp', 'as', 'maspsx.py', 'mips-ps2-decompals-as',
                     '.decompnet-toolchain.json'):
            if (root / name).is_file():
                candidates.add((root / name).resolve())
        # maspsx imports its package, not just the entrypoint script.
        if (root / 'maspsx.py').is_file():
            candidates.update(p.resolve() for p in root.rglob('*.py') if '__pycache__' not in p.parts)
    return {'files': {str(p): {'sha256': _digest(p.read_bytes()), 'bytes': p.stat().st_size}
                      for p in sorted(candidates)},
            'scope': 'host-local recipe files; container/WSL contents not fingerprinted',
            'config': asdict(cfg)}


def _assembly_summary(text: str) -> dict:
    return {'sha256': _digest(text), 'lines': len(text.splitlines()),
            'frames': re.findall(r'\.frame[^\n]+', text),
            'hi_sequence': [line.strip() for line in text.splitlines()
                            if re.search(r'\b(mfhi|sra)\b', line)]}


def run_microprobes(output_dir: Path, lanes: dict[str, compiler.CompilerConfig]) -> dict:
    """Compile exactly four fixed cases per explicit lane into a new artifact directory.

    This runs the configured real compiler/assembler command through the existing
    sandbox runner. It never installs tools or touches a project/database. Failure,
    timeout and missing assembly stay in the report. No original target exists.
    """
    if not 1 <= len(lanes) <= 12:
        raise ValueError('Supply 1-12 explicit compiler lanes.')
    if any(not re.fullmatch(r'[A-Za-z0-9_.@-]+', name) for name in lanes):
        raise ValueError('Lane names must be safe directory names.')
    out = Path(output_dir).resolve()
    out.mkdir(parents=True, exist_ok=False)
    sources = microcases()
    manifest = {'schema': 1, 'scope': 'compiler reductions only; no original target or match claim',
                'sources': sources, 'source_sha256': {n: _digest(s) for n, s in sources.items()},
                'lanes': {n: tool_fingerprints(cfg) for n, cfg in lanes.items()},
                'requested_compiles': len(lanes) * len(sources)}
    (out / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    results = []
    for lane, cfg in lanes.items():
        for name, source in sources.items():
            work = out / lane / name
            work.mkdir(parents=True)
            inc = work / 'include'
            inc.mkdir()
            for directory in cfg.include_dirs:
                shutil.copytree(directory, inc, dirs_exist_ok=True)
            (inc / 'context.h').write_text('')
            prefix = ''
            if cfg.psx_gte:
                from .psx_gte import header_path
                shutil.copyfile(header_path(), inc / 'psx_gte.h')
                prefix = '#include "psx_gte.h"\n'
            (work / 'input.c').write_text(prefix + source, newline='\n')
            # Relative paths work in the production none/bwrap/docker/WSL runner.
            argv = [a.replace('{src}', 'input.c').replace('{obj}', 'output.o')
                    .replace('{include}', 'include') for a in cfg.cmd]
            (work / 'command.json').write_text(json.dumps(argv, indent=2) + '\n')
            row = {'lane': lane, 'case': name, 'state': 'running'}
            results.append(row)
            # Persist each reservation and outcome; never silently retry interrupted probes.
            (out / 'results.json').write_text(json.dumps(results, indent=2) + '\n')
            try:
                proc = compiler.run_in(cfg, work, argv, timeout=cfg.timeout,
                                       inputs=['input.c', 'include'],
                                       outputs=['output.o', 'output.o.s', 'output.o.ms'])
                log = proc.stdout + proc.stderr
                row.update(returncode=proc.returncode,
                           state='ok' if proc.returncode == 0 and (work / 'output.o').is_file() else 'failed')
            except (subprocess.TimeoutExpired, OSError, RuntimeError) as exc:
                log = str(exc)
                row.update(state='timeout' if isinstance(exc, subprocess.TimeoutExpired) else 'failed',
                           error=type(exc).__name__)
            (work / 'compiler.log').write_text(log)
            row['assembly'] = {}
            for suffix in ('s', 'ms'):
                file = work / f'output.o.{suffix}'
                if file.is_file():
                    assembly = file.read_text(errors='replace')
                    row['assembly'][suffix] = _assembly_summary(assembly)
            if (work / 'output.o').is_file():
                row['object_sha256'] = _digest((work / 'output.o').read_bytes())
            (out / 'results.json').write_text(json.dumps(results, indent=2) + '\n')
    return analyze_microprobes(out)


def analyze_microprobes(output_dir: Path) -> dict:
    """Derive final-code evidence/diffs from a saved run without recompiling it."""
    from .elfobj import extract_function
    from .scoring import disassemble
    out = Path(output_dir).resolve()
    manifest = json.loads((out / 'manifest.json').read_text())
    results = json.loads((out / 'results.json').read_text())
    lanes, sources = list(manifest['lanes']), manifest['sources']
    if len(results) > 48 or any(not re.fullmatch(r'[A-Za-z0-9_.@-]+', n) for n in lanes):
        raise ValueError('Invalid saved probe bounds or lane names.')
    assemblies = {}
    for row in results:
        lane, name = row['lane'], row['case']
        if lane not in lanes or name not in microcases():
            raise ValueError('Saved probe contains an unknown lane/case.')
        work = out / lane / name
        for suffix in ('s', 'ms'):
            file = work / f'output.o.{suffix}'
            if file.is_file():
                assemblies[lane, name, suffix] = file.read_text(errors='replace')
        obj = work / 'output.o'
        if not obj.is_file():
            row['final_code'] = {'state': 'unavailable'}
            continue
        try:
            function = extract_function(obj.read_bytes(), 'probe')
            if function is None:
                raise ValueError('ELF object has no probe function')
            text = ''.join(f'{i.offset:04x}: {i.text}\n' for i in disassemble(function))
            (work / 'probe.disasm').write_text(text)
            (work / 'probe.bin').write_bytes(function.code)
            (work / 'probe.relocations.json').write_text(json.dumps([r.to_json() for r in function.relocs], indent=2) + '\n')
            row['final_code'] = {'state': 'ok', 'bytes': len(function.code),
                                 'raw_code_sha256': _digest(function.code),
                                 'normalized_code_sha256': _digest(function.normalized()),
                                 'code_and_relocations_fingerprint': function.fingerprint(),
                                 'disassembly': 'probe.disasm',
                                 'scope': 'production ELF extraction and relocation-symbolized disassembly'}
            assemblies[lane, name, 'disasm'] = text
        except Exception as exc:
            # Retain successful compiler exits separately from failed ELF inspection.
            row['final_code'] = {'state': 'inspection_failed', 'error': type(exc).__name__, 'message': str(exc)}
    (out / 'results.json').write_text(json.dumps(results, indent=2) + '\n')
    differences = []
    comparisons = [(a, case, b, case) for a, b in itertools.combinations(lanes, 2) for case in sources]
    comparisons += [(lane, a, lane, b) for lane in lanes
                    for a, b in [('stack_plain', 'stack_reserve'), ('grid_plain', 'grid_quotient')]]
    diff_dir = out / 'diffs'
    diff_dir.mkdir(exist_ok=True)
    for left, case_left, right, case_right in comparisons:
        for suffix in ('s', 'ms', 'disasm'):
            a, b = assemblies.get((left, case_left, suffix)), assemblies.get((right, case_right, suffix))
            row = {'left': f'{left}/{case_left}', 'right': f'{right}/{case_right}', 'stage': suffix}
            if a is None or b is None:
                row['state'] = 'unavailable'
            else:
                delta = ''.join(difflib.unified_diff(a.splitlines(True), b.splitlines(True),
                                                    fromfile=row['left'], tofile=row['right']))
                filename = f'{left}-{case_left}__{right}-{case_right}.{suffix}.diff'
                (diff_dir / filename).write_text(delta)
                row.update(state='same' if a == b else 'different', diff=f'diffs/{filename}')
                if suffix == 'disasm':
                    records = {(r['lane'], r['case']): r for r in results}
                    ca = records[left, case_left]['final_code']
                    cb = records[right, case_right]['final_code']
                    row.update(raw_code_equal=ca['raw_code_sha256'] == cb['raw_code_sha256'],
                               normalized_code_equal=ca['normalized_code_sha256'] == cb['normalized_code_sha256'],
                               code_and_relocations_equal=ca['code_and_relocations_fingerprint'] ==
                                                          cb['code_and_relocations_fingerprint'])
            differences.append(row)
    report = {'schema': 1, 'scope': manifest['scope'], 'output_dir': str(out),
              'results': results, 'differences': differences,
              'original_aspsx': 'not run unless explicitly supplied as a configured lane; maspsx is a reimplementation'}
    (out / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    return report


_LEX = re.compile(r'//[^\n]*|/\*.*?\*/|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'', re.S)
_KEYWORDS = {'if', 'while', 'for', 'switch', 'return', 'sizeof', '__attribute__', 'typeof', '_Alignof'}
_DECL = re.compile(r'(?P<ret>(?:\b[A-Za-z_]\w*\s+|\*\s*)+)(?P<name>[A-Za-z_]\w*)\s*'
                   r'\((?P<params>[^();{}]*)\)\s*(?P<end>[;{])')


def _masked(text: str) -> str:
    def mask(match):
        value = match.group()
        # Keep a token for string/character arguments, but hide their commas/parens.
        return ('0' if value[0] in "\"'" else ' ') + ''.join('\n' if c == '\n' else ' ' for c in value[1:])
    text = _LEX.sub(mask, text)
    return re.sub(r'^\s*#[^\n]*(?:\\\n[^\n]*)*', lambda m: ' ' * len(m.group()), text, flags=re.M)


def _declarations(text: str) -> list[dict]:
    masked = _masked(text)
    result = []
    for match in _DECL.finditer(masked):
        ret = match['ret'].strip()
        if any(word in _KEYWORDS for word in ret.split()):
            continue
        # This simple reader accepts top-level declarations/definitions only.
        before = masked[:match.start()]
        if before.count('{') != before.count('}'):
            continue
        params = match['params'].strip()
        count = None if not params else 0 if params == 'void' else len(params.split(','))
        variadic = '...' in params
        if variadic:
            count -= 1
        result.append({'name': match['name'], 'text': text[match.start():match.end()].strip(),
                       'parameters': text[match.start('params'):match.end('params')],
                       'return_type_spelling': re.sub(r'\b(?:extern|static|inline)\b', '', ret).strip(),
                       'arity': count, 'variadic': variadic,
                       'kind': 'definition' if match['end'] == '{' else 'declaration',
                       'line': text.count('\n', 0, match.start()) + 1,
                       'name_offset': match.start('name')})
    return result


def _calls(source: str) -> list[dict]:
    masked = _masked(source)
    declarations = {d['name_offset'] for d in _declarations(source)}
    result = []
    for match in re.finditer(r'\b([A-Za-z_]\w*)\s*\(', masked):
        if match[1] in _KEYWORDS or match.start(1) in declarations:
            continue
        start = match.end()
        depth, commas, pos = 1, 0, start
        while pos < len(masked) and depth:
            char = masked[pos]
            if char in '([{':
                depth += 1
            elif char in ')]}':
                depth -= 1
            elif char == ',' and depth == 1:
                commas += 1
            pos += 1
        if depth:
            continue
        args = masked[start:pos - 1]
        result.append({'name': match[1], 'arity': commas + 1 if args.strip() else 0,
                       'line': source.count('\n', 0, match.start()) + 1,
                       'text': source[match.start():pos]})
    return result


def _assembly_calls(assembly: str) -> list[dict]:
    lines = []
    for line in assembly.splitlines():
        line = re.sub(r'/\*.*?\*/', '', line).split('#', 1)[0].strip()
        if line:
            lines.append(line)
    result = []
    for i, line in enumerate(lines):
        match = re.match(r'jal\s+([A-Za-z_]\w*)\b', line)
        if not match:
            continue
        before = []
        for previous in reversed(lines[max(0, i - 12):i]):
            if previous.endswith(':') or re.match(r'(?:j\w*|b\w*)\s', previous):
                break
            before.append(previous)
        before.reverse()
        delay = lines[i + 1] if i + 1 < len(lines) else ''
        window = before + [delay]
        writes = [s for s in window if re.match(r'\w+\s+\$(?:a[0-3]|[4-7])\s*,', s)
                  and not re.match(r'(?:s[bwh]|b\w*|j\w*)\s', s)]
        stack = [s for s in window if re.match(r'sw\s+[^,]+,\s*(?:0x1[048c]|(?:16|20|24|28))\(\$sp\)', s)]
        result.append({'name': match[1], 'call': line, 'delay_slot': delay,
                       'argument_register_writes': writes, 'possible_stack_argument_stores': stack,
                       'scope': 'local instruction window only; no control-flow/liveness or type proof'})
    return result


def audit_call_boundaries(header: str, source: str, assembly: str = '',
                          matched_callees: dict[str, str] | None = None) -> dict:
    """Return evidence and conservative warnings, never an acceptance decision.

    matched_callees maps callee name to its saved C source or exact declaration.
    A matched byte sequence does not certify its C signature or host-port ABI.
    """
    declarations = [d | {'origin': 'header'} for d in _declarations(header)]
    declarations += [d | {'origin': 'source'} for d in _declarations(source)]
    calls, asm_calls = _calls(source), _assembly_calls(assembly)
    matched = {}
    for name, text in (matched_callees or {}).items():
        found = [d for d in _declarations(text) if d['name'] == name]
        if found:
            matched[name] = found
    warnings = []
    for call in calls:
        name = call['name']
        available = [d for d in declarations if d['name'] == name]
        for origin, records in [('visible declaration', available), ('matched callee source', matched.get(name, []))]:
            for declaration in records:
                arity = declaration['arity']
                if arity is not None and (call['arity'] < arity or
                                         not declaration['variadic'] and call['arity'] != arity):
                    warnings.append({'code': 'call_arity_disagreement', 'confidence': 'lexical evidence',
                                     'callee': name, 'line': call['line'], 'call': call['text'],
                                     'evidence_origin': origin, 'declaration': declaration['text'],
                                     'message': f"Call supplies {call['arity']} arguments; {origin} has "
                                                f"{'at least ' if declaration['variadic'] else ''}{arity}. "
                                                'Preprocessing/type review is required.'})
        if not available:
            warnings.append({'code': 'no_simple_visible_prototype', 'confidence': 'heuristic',
                             'callee': name, 'line': call['line'],
                             'message': 'No supported simple declaration found; check includes/macros and implicit int.'})
        elif all(d['arity'] is None for d in available):
            warnings.append({'code': 'unspecified_parameters', 'confidence': 'lexical evidence',
                             'callee': name, 'line': call['line'],
                             'message': 'C empty parameter list () does not specify zero arguments.'})
        evidence = [a for a in asm_calls if a['name'] == name and a['argument_register_writes']]
        if call['arity'] == 0 and evidence:
            warnings.append({'code': 'empty_call_with_argument_setup', 'confidence': 'heuristic',
                             'callee': name, 'line': call['line'], 'assembly': evidence,
                             'message': 'Empty C call has target call-site argument-register writes nearby. '
                                        'Inspect live values and the delay slot; register writes do not prove arity.'})
    # Preserve exact competing declarations. Text differences can be harmless names/typedefs.
    comparisons = [{'callee': name, 'visible': [d['text'] for d in declarations if d['name'] == name],
                    'matched_source': [d['text'] for d in records]}
                   for name, records in matched.items() if any(c['name'] == name for c in calls)]
    called_names = {c['name'] for c in calls}
    for name in called_names:
        for visible in [d for d in declarations if d['name'] == name]:
            for saved in matched.get(name, []):
                normalize = lambda s: re.sub(r'\s+', '', s)
                if normalize(visible['return_type_spelling']) != normalize(saved['return_type_spelling']):
                    warnings.append({'code': 'return_type_spelling_difference', 'confidence': 'heuristic',
                                     'callee': name, 'visible': visible['text'], 'matched_source': saved['text'],
                                     'message': 'Return type spellings differ; resolve typedefs and inspect return-value '
                                                'use before changing a declaration. This is not an ABI verdict.'})
    # Only relevant exact header declarations belong in a compact function report.
    declarations = [d for d in declarations if d['origin'] == 'source' or d['name'] in called_names]
    for d in declarations:
        d.pop('name_offset', None)
    return {'scope': 'Read-only lexical/assembly audit. No signature, equivalence, or ABI acceptance proof.',
            'limitations': 'No preprocessing, typedef resolution, indirect-call analysis, K&R or function-pointer parsing; '
                           'assembly windows are not dataflow analysis. Matched C can still have a wrong prototype.',
            'input_sha256': {'header': _digest(header), 'source': _digest(source), 'assembly': _digest(assembly)},
            'calls': calls, 'declarations': declarations, 'matched_comparisons': comparisons,
            'assembly_calls': asm_calls, 'warnings': warnings}


def audit_function(net, function, project=None) -> str:
    """Compact read-only network tool wrapper; never creates attempts or changes headers."""
    fn = net._function(function, project)
    ctx = net._context(fn['project_id'], fn['unit'])
    source = fn['matched_source'] or fn['best_source'] or fn['seed'] or ''
    names = sorted({c['name'] for c in _calls(source)})
    matched = {}
    if names:
        placeholders = ','.join('?' for _ in names)
        rows = net.store.all(f"SELECT name,matched_source FROM functions WHERE project_id=? AND status='matched' "
                             f"AND name IN ({placeholders})", fn['project_id'], *names)
        matched = {r['name']: r['matched_source'] for r in rows if r['matched_source']}
    report = audit_call_boundaries(ctx['header'], source, fn['asm'] or '', matched)
    # Keep an interactive tool response bounded. The CLI retains the full evidence.
    call_names = sorted({c['name'] for c in report['calls']})
    shown_names = set(call_names[:30])
    def compact_record(record):
        item = dict(record)
        clipped = []
        for key in ('text', 'call', 'declaration', 'visible', 'matched_source', 'parameters'):
            if isinstance(item.get(key), str) and len(item[key]) > 512:
                item[key] = item[key][:512] + ' [truncated]'
                clipped.append(key)
        if clipped:
            item['truncated_fields'] = clipped
        if 'assembly' in item:
            item['assembly_evidence_total'] = len(item['assembly'])
            item['assembly'] = item['assembly'][:3]
            item['assembly_evidence_omitted'] = item['assembly_evidence_total'] - len(item['assembly'])
        return item
    all_declarations = [d for d in report['declarations'] if d['name'] in call_names]
    declarations = [d for d in all_declarations if d['name'] in shown_names]
    comparisons = []
    for comparison in report['matched_comparisons']:
        if comparison['callee'] in shown_names:
            comparisons.append({'callee': comparison['callee'],
                                'visible': [compact_record({'text': s}) for s in comparison['visible'][:3]],
                                'matched_source': [compact_record({'text': s}) for s in comparison['matched_source'][:3]],
                                'omitted': {'visible': max(0, len(comparison['visible']) - 3),
                                            'matched_source': max(0, len(comparison['matched_source']) - 3)}})
    compact = {'scope': report['scope'], 'limitations': report['limitations'],
               'input_sha256': report['input_sha256'],
               'counts': {'calls': len(report['calls']), 'callees': len(call_names),
                          'warnings': len(report['warnings']), 'relevant_declarations': len(all_declarations)},
               'callees': call_names[:30],
               'warnings': [compact_record(w) for w in report['warnings'][:40]],
               'declarations': [compact_record(d) for d in declarations[:60]],
               'matched_comparisons': comparisons,
               'omitted': {'callees': max(0, len(call_names) - 30),
                           'warnings': max(0, len(report['warnings']) - 40),
                           'declarations': len(all_declarations) - len(declarations[:60]),
                           'matched_comparisons': len(report['matched_comparisons']) - len(comparisons)},
               'full_evidence': 'Run docs/plans/ps1-prototype-audit.py for complete source/header/assembly artifacts.'}
    return json.dumps({'function': fn['name'], 'context_version': ctx['version'],
                       'source_kind': 'matched' if fn['matched_source'] else 'best' if fn['best_source'] else 'seed',
                       **compact}, indent=2)
