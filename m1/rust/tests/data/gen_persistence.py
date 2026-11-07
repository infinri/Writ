# Generates persistence.jsonl (two broker-scenario inputs for tests/ledger_persistence.rs)
# from the frozen fixture case b-15-grant-single-use:
#   python3 m1/rust/tests/data/gen_persistence.py m1/fixtures/sets/broker-scenarios.jsonl > m1/rust/tests/data/persistence.jsonl 2>/dev/null
import json, hmac, hashlib, copy, sys
fx = [json.loads(l) for l in open(sys.argv[1])]
case = next(c for c in fx if c["id"] == "b-15-grant-single-use")
initial = case["input"]["initial"]
approve = case["input"]["steps"][0]
canon = lambda v: json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
key = bytes.fromhex(initial["ingress_keys"]["writ-host-adapter:fixture"])
P = approve["for_request"]
W = copy.deepcopy(P)
W["action"]["kind"] = "file.write"
W["action"]["resource"] = {"canonical_resource_id": "workspace:fixture-tenant/fixture-project/a.txt", "uri": "file:///work/a.txt"}
def req(base, idem):
    r = copy.deepcopy(base); r["idempotency_key"] = idem; return r
def ex(r, grant=None, fault=None, bad=False):
    mac = hmac.new(key, canon(r).encode(), hashlib.sha256).hexdigest()
    if bad: mac = ("0" if mac[0] != "0" else "1") + mac[1:]
    op = {"op": "execute", "request": r, "seal": {"adapter_id": "writ-host-adapter:fixture", "mac": mac}}
    if grant: op["grant_ref"] = grant
    if fault: op["fault"] = fault
    return op
def ap(label):
    a = copy.deepcopy(approve); a["grant_ref"] = label; return a
first = [ap("g1"), ap("g2"), ex(req(P, "idem-a"), "g1", "crash_after_reserve"), ex(req(W, "idem-w1")),
         ex(req(W, "idem-w2"), fault="crash_after_dispatch"), ex(req(W, "idem-x"), bad=True), {"op": "restart_broker"},
         {"op": "present_permit", "of_step": 3, "request": req(W, "idem-w1")}, ex(req(W, "idem-w2"))]
second = [ex(req(P, "idem-a"), "g1"), ex(req(W, "idem-w1")), ex(req(W, "idem-w2")), ex(req(P, "idem-c"), "g1"), {"op": "reconcile"}]
for steps in (first, second):
    print(canon({"initial": initial, "steps": steps}))
# canonical-form cross-check input for `proto canonical`
for r in (req(P, "idem-a"), req(W, "idem-w1")):
    print(json.dumps({"id": "c", "input_text": json.dumps(r)}), file=sys.stderr)
