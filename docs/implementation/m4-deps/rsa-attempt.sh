#!/usr/bin/env bash
# D-041 DR-3: attempt a patched rsa resolution and record the evidence. Run from the repository
# root with the M1 toolchain on PATH. Leaves Cargo.lock unchanged (the update fails).
set -u
UA="writ-dependency-review"
echo "== advisory RUSTSEC-2023-0071 (api.osv.dev)"
curl -s -A "$UA" https://api.osv.dev/v1/vulns/RUSTSEC-2023-0071 | python3 -I -c 'import json,sys; d=json.load(sys.stdin); print(d["id"], d["modified"], d["summary"]); print("ranges:", json.dumps(d["affected"][0]["ranges"])); print(d["details"].split("### References")[0])'
echo "== rsa releases (crates.io)"
curl -s -A "$UA" https://crates.io/api/v1/crates/rsa | python3 -I -c 'import json,sys; [print(v["num"], v["created_at"], "yanked" if v["yanked"] else "") for v in json.load(sys.stdin)["versions"][:8]]'
echo "== crypto-glue latest release and its rsa requirement"
curl -s -A "$UA" https://crates.io/api/v1/crates/crypto-glue | python3 -I -c 'import json,sys; d=json.load(sys.stdin)["crate"]; print(d["max_version"], d["updated_at"])'
curl -s -A "$UA" https://crates.io/api/v1/crates/crypto-glue/0.2.2/dependencies | python3 -I -c 'import json,sys; [print(d["crate_id"], d["req"], "features", d["features"], "optional", d["optional"]) for d in json.load(sys.stdin)["dependencies"] if d["crate_id"]=="rsa"]'
echo "== locked rsa and its dependents"
cargo tree --locked --workspace --target all -e normal,build,dev -i rsa
echo "== cargo update -p rsa --precise 0.10.0"
cp Cargo.lock Cargo.lock.rsa-attempt
cargo update -p rsa --precise 0.10.0 2>&1; echo "exit $?"
mv Cargo.lock.rsa-attempt Cargo.lock
echo "== upstream crypto-glue pull requests (GitHub API)"
curl -s -A "$UA" "https://api.github.com/repos/kanidm/crypto-glue/pulls?state=all&per_page=10" | python3 -I -c 'import json,sys; [print(p["number"], p["state"], p["created_at"], p["merged_at"], p["title"]) for p in json.load(sys.stdin)]'
echo "== RSA private-key operations referenced by the webauthn crates (static search)"
R=$(dirname "$(cargo metadata --locked --format-version 1 | python3 -I -c 'import json,sys; print([p["manifest_path"] for p in json.load(sys.stdin)["packages"] if p["name"]=="webauthn-rs-core"][0])')")/..
grep -rnE "RsaPrivateKey|RS256PrivateKey|RS256SigningKey|RS384SigningKey|rsa::new_key|oaep_sha256|Oaep|pkcs1v15::SigningKey" \
  "$R"/webauthn-rs-0.6.2 "$R"/webauthn-rs-core-0.6.2 "$R"/webauthn-rs-proto-0.6.2 "$R"/webauthn-attestation-ca-0.6.2 "$R"/webauthn-authenticator-rs-0.6.2 | sed "s#$R/##"
echo "== rsa items imported by webauthn-rs-core"
grep -rn "rsa::" "$R"/webauthn-rs-core-0.6.2/src | sed "s#$R/##"
echo "== crypto-glue rsa module (private-key helpers exported, unused by the webauthn crates)"
grep -nE "RsaPrivateKey|fn new_key|fn oaep" "$R"/crypto-glue-0.2.2/src/lib.rs
echo "== webauthn-rs algorithm configuration"
grep -nE "algorithms|secure_algs" "$R"/webauthn-rs-0.6.2/src/lib.rs
grep -nE -A10 "pub fn secure_algs" "$R"/webauthn-rs-proto-0.6.2/src/cose.rs
grep -nE -B2 -A10 "let alg_valid" "$R"/webauthn-rs-core-0.6.2/src/core.rs
grep -nE -A3 "pub fn cred_algorithm" "$R"/webauthn-rs-0.6.2/src/interface.rs | head -5
