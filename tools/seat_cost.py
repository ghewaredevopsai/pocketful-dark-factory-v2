#!/usr/bin/env python3
"""Per-seat token usage and list-price cost from the seats' Claude Code transcripts.

Usage: seat_cost.py --repo <path the run committed to> <transcript-project-dir-or-repo> [...]
A session counts toward the run when its transcript mentions the --repo path. The seat is read from the
mandate heading in the owner instructions ("# implementer"), else from the model (sonnet = coordinator).
Prices: Anthropic list, $/MTok (input, output, cache write 5m = 1.25x input, cache read).
"""
import glob, json, os, re, sys, collections
PRICE = {'claude-opus-5-5': (4.00, 20.00, 5.00, 0.20), 'claude-sonnet-5-5': (2.00, 10.00, 2.50, 0.20)}
SEATS = ('coordinator', 'implementer', 'modeler', 'reviewer')

def proj_dir(repo):
    return os.path.expanduser('~/.claude/projects/' + re.sub(r'[^A-Za-z0-9]', '-', os.path.abspath(repo)))

def seat_of(text):
    m = re.search(r'(?:^|\\n)# (coordinator|implementer|modeler|reviewer)(?:\\n|\s*$)', text[:600000], re.M)
    return m.group(1) if m else None

def main(args):
    run_repo = None
    if args[:1] == ['--repo']: run_repo, args = os.path.abspath(args[1]), args[2:]
    tot = collections.defaultdict(collections.Counter); seen = set()
    for repo in args:
        d = repo if os.path.isdir(repo) and repo.startswith(os.path.expanduser('~/.claude')) else proj_dir(repo)
        for f in glob.glob(d + '/*.jsonl'):
            text = open(f, errors='replace').read()
            if run_repo and not re.search(re.escape(run_repo) + r'(?![A-Za-z0-9_-])', text): continue
            seat = seat_of(text)
            for line in text.splitlines():
                try: e = json.loads(line)
                except Exception: continue
                m = e.get('message') or {}; u = m.get('usage'); mid = m.get('id')
                if not u or not mid or mid in seen: continue
                seen.add(mid); model = m.get('model', '?'); c = tot[(seat or ('coordinator' if 'sonnet' in model else '?'), model)]
                c['calls'] += 1; c['in'] += u.get('input_tokens', 0); c['out'] += u.get('output_tokens', 0)
                c['cw'] += u.get('cache_creation_input_tokens', 0); c['cr'] += u.get('cache_read_input_tokens', 0)
    grand = 0.0
    print('| Seat | Model | API calls | Output tokens | Cache-write tokens | Cache-read tokens | List-price USD |')
    print('|---|---|---|---|---|---|---|')
    for (seat, model), c in sorted(tot.items()):
        p = PRICE.get(model)
        usd = sum(c[k] * r for k, r in zip(('in', 'out', 'cw', 'cr'), p)) / 1e6 if p else 0.0
        grand += usd
        print(f"| {seat} | {model} | {c['calls']:,} | {c['out']:,} | {c['cw']:,} | {c['cr']:,} | ${usd:,.2f} |")
    print(f'| **total** | | | | | | **${grand:,.2f}** |')

main(sys.argv[1:] or ['.'])
