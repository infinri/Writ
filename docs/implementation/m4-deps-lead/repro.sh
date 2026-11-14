#!/bin/bash
D=/tmp/claude-1001/-home-lucio-saldivar-workspaces-Writ/80ea5938-c3a4-4206-9970-b00f5cc3393d/scratchpad/repro
cd /home/lucio.saldivar/workspaces/Writ
[ -d $D/wt ] || git worktree add -q --detach $D/wt 3b7cb76
cd $D/wt
source /home/lucio.saldivar/workspaces/Writ/m1/tools/env.sh
export CARGO_TARGET_DIR=$D/target
git log --oneline -1
cargo build --locked --release -q 2>&1 | tail -2; echo "release build rc=${PIPESTATUS[0]}"
cargo test --locked --workspace --no-run -q 2>&1 | tail -2; echo "test build rc=${PIPESTATUS[0]}"
cnt(){ cargo tree --locked "$@" --prefix none 2>/dev/null | awk '{print $1" "$2}' | sort -u; }
for c in writ-kernel writ-broker writ-ledger-sqlite writ-approval; do
  echo "$c normal/build=$(cnt -p $c -e normal,build | wc -l) with-dev=$(cnt -p $c -e normal,build,dev | wc -l)"
done
cnt --workspace -e normal,build > $D/combined-release.txt
cnt --workspace -e normal,build,dev > $D/combined-test.txt
echo "combined release=$(wc -l < $D/combined-release.txt) combined test=$(wc -l < $D/combined-test.txt) lock=$(grep -c '^name = ' Cargo.lock)"
for p in openssl openssl-sys native-tls aws-lc-sys aws-lc-rs ring rustls; do cargo tree --locked --workspace -e normal,build,dev -i $p >/dev/null 2>&1 && echo "PRESENT $p"; done
echo "native: $(grep -E '^links|links = ' -r $HOME/.local/writ-m1-toolchains/cargo-home/registry/src/*/ 2>/dev/null | head -0)"
cargo metadata --locked --format-version 1 2>/dev/null | python3 -c "
import json,sys; m=json.load(sys.stdin); ids=set()
for n in m['resolve']['nodes']: ids.add(n['id'])
print('links crates:', sorted({p['name']+' '+p['version']+' links='+str(p['links']) for p in m['packages'] if p.get('links')}))"
echo "== rsa paths"; cargo tree --locked --workspace -e normal,build,dev -i rsa --prefix depth 2>/dev/null | head -6
echo "== tokio in standalone kernel/broker/ledger:"; for c in writ-kernel writ-broker writ-ledger-sqlite; do cnt -p $c -e normal,build | grep -E '^(tokio|hyper) ' && echo "  in $c"; done; echo done
