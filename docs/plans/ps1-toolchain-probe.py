"""Run the four frozen compiler reductions on installed, explicit PS1 lanes.

Usage: .venv/bin/python docs/plans/ps1-toolchain-probe.py NEW_OUTPUT_DIR
  [--lane gcc-2.7.2-psx@2.34] [--lane gcc-2.8.1-psx@2.34/O1]
  [--original-lane-json PATH]

An original ASPSX lane must be supplied as a local CompilerConfig JSON (cmd,
ro_paths, sandbox etc.). It is never downloaded or substituted with maspsx.
The probe does not read or write the project database and never installs tools.
"""
import argparse
import json
from pathlib import Path

from decompnet import toolchains
from decompnet.compiler import CompilerConfig
from decompnet.project import compiler_config, toolchain_id, with_toolchain
from decompnet.ps1_diagnostics import run_microprobes, analyze_microprobes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    parser.add_argument('--lane', action='append')
    parser.add_argument('--original-lane-json', type=Path)
    parser.add_argument('--analyze-only', action='store_true', help='derive disassembly/diffs from saved artifacts; no compiles')
    args = parser.parse_args()
    if args.analyze_only:
        report = analyze_microprobes(args.output)
        print(json.dumps({'output_dir': report['output_dir'], 'compiles': 0,
                          'final_code_outcomes': [r['final_code']['state'] for r in report['results']]}, indent=2))
        return
    specs = args.lane or ['gcc-2.7.2-psx@2.34', 'gcc-2.7.2-psx@2.77',
                          'gcc-2.8.1-psx@2.34/O1', 'gcc-2.8.1-psx@2.77/O1']
    lanes, unavailable = {}, []
    for spec in specs:
        tid = toolchain_id(spec)
        try:
            tc = toolchains.get(tid)
            if tc.platform != 'PS1':
                raise ValueError('Only PS1 compiler lanes are supported.')
            # compiler_config normally ensures dependencies; preflight every required
            # supported dependency so this diagnostic cannot trigger an installation.
            for dep in [tid] + [d for d in tc.requires if tc.via_wsl or toolchains.get(d).supported]:
                if toolchains.installed(dep, tc.via_wsl) is None:
                    raise ValueError(f'{dep} is not installed; skipped without downloading')
            recipe = with_toolchain({'toolchain': tid, 'flags': ['-O2', '-G8']}, spec)
            lane_name = spec.replace('/', '-').replace(':', '-')
            if lane_name in lanes:
                raise ValueError('Duplicate lane.')
            lanes[lane_name] = compiler_config(Path.cwd(), {'compiler': recipe})
        except (toolchains.ToolchainError, ValueError) as exc:
            unavailable.append({'lane': spec, 'reason': str(exc)})
    if args.original_lane_json:
        lanes['original-aspsx-local'] = CompilerConfig(**json.loads(args.original_lane_json.read_text()))
    if not lanes:
        parser.error('No installed lanes available: ' + json.dumps(unavailable))
    report = run_microprobes(args.output, lanes)
    report['unavailable_lanes'] = unavailable
    if args.original_lane_json:
        report['original_aspsx'] = 'Explicit local lane requested; inspect its command, fingerprints and outcomes.'
    else:
        report['original_aspsx'] = 'Unavailable: no local original-ASPSX command supplied; no original assembler ran.'
    (args.output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'output_dir': report['output_dir'], 'outcomes': report['results'],
                      'unavailable_lanes': unavailable, 'original_aspsx': report['original_aspsx']}, indent=2))


if __name__ == '__main__':
    main()
