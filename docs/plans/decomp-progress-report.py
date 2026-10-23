#!/usr/bin/env python3
"""Read-only progress evidence. Run with the repository environment / PYTHONPATH=src."""
import argparse
import json
from pathlib import Path

from decompnet.progress_metrics import format_report, report


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('database', type=Path)
    p.add_argument('--project', default='slus-01252', help='project slug')
    p.add_argument('--baseline', type=Path, help='explicit frozen comparison database')
    p.add_argument('--since', type=float, help='inclusive Unix timestamp; default all observed history')
    p.add_argument('--until', type=float, help='exclusive Unix timestamp; default now')
    p.add_argument('--minimum-score-drop', type=int, default=100)
    p.add_argument('--activity-intervals', type=Path, help='JSON list: agent_id, start, end, source (evidence reference)')
    p.add_argument('--output', type=Path, help='new JSON evidence file; existing output is never overwritten')
    args = p.parse_args()
    result = report(args.database, args.project, baseline_database=args.baseline, since=args.since, until=args.until,
                    minimum_score_drop=args.minimum_score_drop,
                    activity_intervals=json.loads(args.activity_intervals.read_text()) if args.activity_intervals else None)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open('x') as out:
            json.dump(result, out, indent=2)
            out.write('\n')
    else:
        print(json.dumps(result, indent=2))
    print(format_report(result), file=__import__('sys').stderr)


if __name__ == '__main__':
    main()
