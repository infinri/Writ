// M1-00 toolchain smoke test: threads, integer arithmetic, std only.
fn main() {
    let handles: Vec<_> = (1u64..=10).map(|n| std::thread::spawn(move || n)).collect();
    let sum: u64 = handles.into_iter().map(|h| h.join().unwrap()).sum();
    assert_eq!(sum, 55);
    println!("rust-smoke ok sum={sum}");
}
