//! Bind the disposable evidence records to the exact compiled sources and fixture tests.
use std::{
    env, fs,
    path::{Path, PathBuf},
    process::Command,
};

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let out = PathBuf::from(env::var_os("OUT_DIR").ok_or("missing build output")?);
    // Only source identities enter the library; fixture signing keys stay outside it.
    hash_sources(
        "fixtures/mediation.sources",
        &out.join("mediation-source-ids"),
    )?;
    hash_sources(
        "fixtures/host-coverage.sources",
        &out.join("host-source-ids"),
    )?;
    for (source, name) in [
        ("fixtures/mediation-result.json", "mediation-result.json"),
        ("fixtures/mediation.seal", "mediation.seal"),
        (
            "fixtures/host-coverage-result.json",
            "host-coverage-result.json",
        ),
        ("fixtures/host-coverage.seal", "host-coverage.seal"),
        (
            "../../docs/contracts/proposed/examples/host-coverage.valid.json",
            "host-coverage.json",
        ),
    ] {
        println!("cargo:rerun-if-changed={source}");
        fs::write(out.join(name), fs::read(source).unwrap_or_default())?;
    }
    let corpus = "../../writ-corpus.cypher";
    println!("cargo:rerun-if-changed={corpus}");
    let corpus = fs::read_to_string(corpus).unwrap_or_default();
    let mut entries = corpus.lines().filter(|row| {
        row.starts_with("CREATE (:Rule {")
            && row.contains("rule_id: 'DOC-ONBOARD-001'")
            && row.contains("mandatory: false")
            && row.contains("statement: 'Advisory only.")
    });
    let entry = entries.next().unwrap_or("");
    fs::write(
        out.join("advisory-entry"),
        if entries.next().is_none() { entry } else { "" },
    )?;
    Ok(())
}

fn hash_sources(manifest: &str, target: &Path) -> Result<(), Box<dyn std::error::Error>> {
    println!("cargo:rerun-if-changed={manifest}");
    let sources = fs::read_to_string(manifest).unwrap_or_default();
    for path in sources.lines() {
        println!("cargo:rerun-if-changed={path}");
    }
    let digest_list = if sources.is_empty() {
        Vec::new()
    } else {
        Command::new("sha256sum")
            .args(sources.lines())
            .output()
            .ok()
            .filter(|result| result.status.success())
            .map_or_else(Vec::new, |result| result.stdout)
    };
    fs::write(target, digest_list)?;
    Ok(())
}
