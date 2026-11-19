"""Check the reviewed, isolated D-051 Cargo graph and query OSV without exceptions."""

import hashlib
import json
from pathlib import Path
import subprocess
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "kernel/tools"))
from check_deps import locked, osv  # noqa: E402

LOCK_SHA256 = "a45cbdf020655444dcd54e6b7d76b09da54a907e4b7e68d4e06b21dc716120f5"
GRAPH_SHA256 = "1924f64b55749631d5fc08f30d6324376d5d9ee1faf83b207b6cd122d6e226d4"


def graph_sha256():
    metadata = json.loads(subprocess.check_output([
        "cargo", "metadata", "--format-version", "1", "--locked", "--all-features",
        "--manifest-path", str(HERE / "Cargo.toml"),
    ], cwd=HERE))
    packages = {pkg["id"]: pkg for pkg in metadata["packages"]}
    labels = {key: f"{pkg['name']}@{pkg['version']}" for key, pkg in packages.items()}
    graph = {}
    for node in metadata["resolve"]["nodes"]:
        key = node["id"]
        pkg = packages[key]
        graph[labels[key]] = {
            "source": pkg["source"],
            "license": pkg["license"],
            "features": sorted(node["features"]),
            "dependencies": sorted([
                dependency["name"], labels[dependency["pkg"]],
                sorted((edge["kind"] or "normal", edge["target"] or "*")
                       for edge in dependency["dep_kinds"]),
            ] for dependency in node["deps"]),
        }
    wire = json.dumps(graph, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(wire).hexdigest()


def main():
    lock = HERE / "Cargo.lock"
    lock_hash = hashlib.sha256(lock.read_bytes()).hexdigest()
    graph_hash = graph_sha256()
    if (lock_hash, graph_hash) != (LOCK_SHA256, GRAPH_SHA256):
        sys.exit(f"D-051 dependency review required (lock {lock_hash}, graph {graph_hash})")
    packages = locked(lock)
    findings = osv(packages)
    if findings:
        sys.exit(f"D-051 dependency advisories: {findings}")
    print(f"D-051: exact lock and all-target features verified, {len(packages)} registry packages, no OSV advisories")


if __name__ == "__main__":
    main()
