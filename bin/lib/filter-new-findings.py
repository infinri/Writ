"""Keep only the analysis findings a proposed edit ADDS.

Used by hooks/scripts/pre-validate-file.sh so that pre-existing violations in
untouched code cannot block an unrelated edit to the same file.

Usage:
    FILE_NEW=<proposed content> FILE_OLD=<file on disk> \
        python3 filter-new-findings.py <proposed.json> <baseline.json>

Findings are fingerprinted on the offending line's TEXT rather than its number:
an edit shifts later line numbers, which would otherwise make every downstream
violation look new. Matching is a multiset, so adding one more violation of a
rule that already appears is still reported.
"""

import json
import os
import sys
from collections import Counter


def load(path):
    try:
        with open(path) as handle:
            return json.load(handle)
    except Exception:
        return []


def source_lines(path):
    try:
        with open(path) as handle:
            return handle.read().splitlines()
    except Exception:
        return []


def fingerprint(finding, lines):
    line_no = finding.get('line') or 0
    text = lines[line_no - 1].strip() if 0 < line_no <= len(lines) else ''
    return (finding.get('tool'), finding.get('rule'), finding.get('message'), text)


def main():
    new_lines = source_lines(os.environ.get('FILE_NEW', ''))
    old_lines = source_lines(os.environ.get('FILE_OLD', ''))

    baseline = Counter(
        fingerprint(f, old_lines)
        for f in load(sys.argv[2])
        if f.get('severity') == 'error'
    )

    kept = []
    for finding in load(sys.argv[1]):
        if finding.get('severity') != 'error':
            kept.append(finding)
            continue
        key = fingerprint(finding, new_lines)
        if baseline[key] > 0:
            baseline[key] -= 1
        else:
            kept.append(finding)

    print(json.dumps(kept))


if __name__ == '__main__':
    main()
