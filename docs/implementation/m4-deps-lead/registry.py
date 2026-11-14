import json, re, sys, time, urllib.request
D = sys.argv[1]
lock = open(D + '/wt/Cargo.lock').read()
pkgs = []
for b in lock.split('[[package]]')[1:]:
    g = lambda k: (re.search(rf'^{k} = "([^"]+)"', b, re.M) or [None, None])[1]
    if g('source') and 'registry' in g('source'):
        pkgs.append((g('name'), g('version'), g('checksum')))
UA = {'User-Agent': 'writ-dependency-review (generic client)'}
def get(url, data=None):
    req = urllib.request.Request(url, data=data, headers={**UA, **({'Content-Type': 'application/json'} if data else {})})
    return json.load(urllib.request.urlopen(req, timeout=60))
rows, bad, young = [], [], []
cache = {}
for n, v, c in pkgs:
    if n not in cache:
        cache[n] = {x['num']: x for x in get(f'https://crates.io/api/v1/crates/{n}/versions?per_page=100')['versions']}
        time.sleep(0.2)
    x = cache[n].get(v)
    if x is None:
        bad.append((n, v, 'version not found on page')); continue
    if x['checksum'] != c: bad.append((n, v, 'checksum mismatch'))
    if x.get('yanked'): bad.append((n, v, 'yanked'))
    rows.append({'name': n, 'version': v, 'checksum': c, 'created_at': x['created_at'], 'license': x.get('license')})
    if x['created_at'] > '2026-10-31T00:00:00Z': young.append((n, v, x['created_at']))
osv = get('https://api.osv.dev/v1/querybatch', json.dumps({'queries': [{'package': {'name': n, 'ecosystem': 'crates.io'}, 'version': v} for n, v, _ in pkgs]}).encode())
hits = [(pkgs[i][0], pkgs[i][1], [v['id'] for v in r.get('vulns', [])]) for i, r in enumerate(osv['results']) if r.get('vulns')]
json.dump(rows, open(D + '/packages-lead.json', 'w'), indent=1)
print('registry packages', len(pkgs), 'verified', len(rows), 'problems', bad)
print('osv hits', hits)
print('published after 2026-10-31:', sorted(young, key=lambda r: r[2]))
lic = {}
for r in rows: lic.setdefault(r['license'], []).append(r['name'] + ' ' + r['version'])
for k in sorted(lic, key=str):
    if k not in ('MIT', 'MIT OR Apache-2.0', 'Apache-2.0 OR MIT', 'MIT/Apache-2.0'): print('licence', k, ':', len(lic[k]), lic[k][:6])
