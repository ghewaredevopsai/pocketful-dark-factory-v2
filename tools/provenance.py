#!/usr/bin/env python3
"""Commit -> seat -> room-message provenance for a result repository, from git and room.json.

Usage: provenance.py <result repo>    (expects <repo>/room.json, the full Band session download)
Prints a Markdown table: commit, author (git), first room message that cites the commit (time, sender), subject.
Also prints room-level counts: human messages, agent text messages per seat, tool calls per seat.
"""
import json, subprocess, sys, collections
repo = sys.argv[1]
room = json.load(open(f'{repo}/room.json'))
msgs = sorted(room['messages'], key=lambda m: m['insertedAt'])
def text(m): return m['content'] if isinstance(m['content'], str) else json.dumps(m['content'])
log = subprocess.run(['git', '-C', repo, 'log', '--reverse', '--format=%H|%h|%an|%s'], capture_output=True, text=True).stdout.strip().splitlines()
print('| Commit | Author (git) | First cited in room | By | Subject |')
print('|---|---|---|---|---|')
for line in log:
    H, h, an, subj = line.split('|', 3)
    hit = next((m for m in msgs if m['messageType'] in ('text', 'tool_result', 'tool_call') and (H in text(m) or f' {h}' in text(m) or f'`{h}' in text(m))), None)
    when = hit['insertedAt'][:19].replace('T', ' ') + 'Z' if hit else 'not cited'
    who = hit['senderName'] if hit else '-'
    print(f'| `{h}` | {an} | {when} | {who} | {subj[:80].replace("|", "/")} |')
by = collections.Counter((m['senderType'], m['senderName'], m['messageType']) for m in msgs)
print()
print(f"Room: {len(msgs)} messages, first {msgs[0]['insertedAt'][:19]}Z, last {msgs[-1]['insertedAt'][:19]}Z")
print(f"Human text messages: {sum(v for (t, n, k), v in by.items() if t.lower() == 'user' and k == 'text')}")
for seat in sorted({n for (t, n, k) in by if t.lower() != 'user'}):
    print(f"- {seat}: {by[('Agent', seat, 'text')]} text, {by[('Agent', seat, 'tool_call')]} tool calls")
