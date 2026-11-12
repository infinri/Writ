//! Shared test support: fixture paths, a raw-span case loader and a
//! deterministic generator (SplitMix64) for property-style tests.

#![allow(dead_code, clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::path::{Path, PathBuf};
use writ_kernel::json::{self, Value};

pub fn fixtures_dir() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../m1/fixtures")
}

/// Raw byte spans of the root members of one JSON object line. Test-only
/// structural scanner: values are not interpreted, so `input` reaches the
/// command as raw text exactly as the parity driver sends it.
pub fn root_members(line: &[u8]) -> Vec<(String, Vec<u8>)> {
    let mut i = 0;
    let ws = |i: &mut usize| {
        while *i < line.len() && matches!(line[*i], b' ' | b'\t' | b'\n' | b'\r') {
            *i += 1;
        }
    };
    let skip_string = |i: &mut usize| {
        assert_eq!(line[*i], b'"');
        *i += 1;
        loop {
            match line[*i] {
                b'\\' => *i += 2,
                b'"' => {
                    *i += 1;
                    return;
                }
                _ => *i += 1,
            }
        }
    };
    ws(&mut i);
    assert_eq!(line[i], b'{', "case line is an object");
    i += 1;
    let mut out = Vec::new();
    loop {
        ws(&mut i);
        if line[i] == b'}' {
            break;
        }
        let key_start = i;
        skip_string(&mut i);
        let key = match json::parse(&line[key_start..i]).unwrap() {
            Value::Str(s) => s,
            other => panic!("member name {other:?}"),
        };
        ws(&mut i);
        assert_eq!(line[i], b':');
        i += 1;
        ws(&mut i);
        let start = i;
        let mut nest = 0usize;
        loop {
            match line[i] {
                b'"' => skip_string(&mut i),
                b'{' | b'[' => {
                    nest += 1;
                    i += 1;
                }
                b'}' | b']' if nest > 0 => {
                    nest -= 1;
                    i += 1;
                }
                b',' | b'}' | b']' if nest == 0 => break,
                _ => i += 1,
            }
            if nest == 0
                && matches!(
                    line.get(i),
                    Some(b',' | b'}' | b' ' | b'\n' | b'\r' | b'\t') | None
                )
            {
                break;
            }
        }
        out.push((key, line[start..i].to_vec()));
        ws(&mut i);
        match line[i] {
            b',' => i += 1,
            b'}' => break,
            c => panic!("unexpected byte {c}"),
        }
    }
    out
}

pub struct Case {
    pub id: String,
    pub input: Vec<u8>,
    pub expect: Value,
    pub compare: String,
    pub fields: Vec<String>,
}

pub fn load_cases(file: &Path) -> Vec<Case> {
    let text = std::fs::read(file).unwrap();
    let mut cases = Vec::new();
    for line in text
        .split(|b| *b == b'\n')
        .filter(|l| !l.iter().all(u8::is_ascii_whitespace))
    {
        let members = root_members(line);
        let get = |k: &str| members.iter().find(|(n, _)| n == k).map(|(_, v)| v.clone());
        let id = match json::parse(&get("id").unwrap()).unwrap() {
            Value::Str(s) => s,
            other => panic!("id {other:?}"),
        };
        let input = match (get("input"), get("input_text")) {
            (Some(raw), None) => raw,
            (None, Some(raw)) => match json::parse(&raw).unwrap() {
                Value::Str(s) => s.into_bytes(),
                other => panic!("{id}: input_text {other:?}"),
            },
            _ => panic!("{id}: exactly one of input, input_text"),
        };
        let expect = json::parse(&get("expect").unwrap()).unwrap();
        let compare = match get("compare").map(|raw| json::parse(&raw).unwrap()) {
            None => "exact".to_string(),
            Some(Value::Str(s)) => s,
            Some(other) => panic!("{id}: compare {other:?}"),
        };
        let fields = match get("fields").map(|raw| json::parse(&raw).unwrap()) {
            None => Vec::new(),
            Some(Value::Arr(a)) => a
                .into_iter()
                .map(|f| match f {
                    Value::Str(s) => s,
                    other => panic!("{id}: field {other:?}"),
                })
                .collect(),
            Some(other) => panic!("{id}: fields {other:?}"),
        };
        cases.push(Case {
            id,
            input,
            expect,
            compare,
            fields,
        });
    }
    cases
}

/// Cases of one fixture set by name.
pub fn set_cases(name: &str) -> Vec<Case> {
    load_cases(&fixtures_dir().join(format!("sets/{name}.jsonl")))
}

/// The parsed input of a case (`input` raw text or `input_text` content).
pub fn case_input(case: &Case) -> Option<Value> {
    json::parse(&case.input).ok()
}

/// SplitMix64: a small deterministic generator for reproducible property tests.
pub struct Rng(u64);

impl Rng {
    pub fn new(seed: u64) -> Rng {
        Rng(seed)
    }

    pub fn next(&mut self) -> u64 {
        self.0 = self.0.wrapping_add(0x9E37_79B9_7F4A_7C15);
        let mut z = self.0;
        z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
        z ^ (z >> 31)
    }

    /// Uniform in `0..n` (n > 0).
    pub fn below(&mut self, n: usize) -> usize {
        (self.next() % n as u64) as usize
    }

    pub fn chance(&mut self, percent: u64) -> bool {
        self.next() % 100 < percent
    }

    pub fn pick<'a, T>(&mut self, items: &'a [T]) -> &'a T {
        &items[self.below(items.len())]
    }

    pub fn shuffle<T>(&mut self, items: &mut [T]) {
        for i in (1..items.len()).rev() {
            let j = self.below(i + 1);
            items.swap(i, j);
        }
    }
}

const WS: [&str; 6] = ["", "", " ", "\n", "\t", "\r\n "];

fn ws(rng: &mut Rng, out: &mut String, enabled: bool) {
    if enabled {
        out.push_str(rng.pick(&WS));
    }
}

/// A JSON string literal; with `vary`, characters are randomly written as
/// `\u` escapes (surrogate pairs for astral characters) or short escapes.
pub fn render_string(rng: &mut Rng, s: &str, vary: bool) -> String {
    let mut out = String::from("\"");
    for ch in s.chars() {
        let code = u32::from(ch);
        let must = ch == '"' || ch == '\\' || code < 0x20;
        if vary && rng.chance(25) {
            if code >= 0x10000 {
                let v = code - 0x10000;
                out.push_str(&format!(
                    "\\u{:04x}\\u{:04X}",
                    0xD800 + (v >> 10),
                    0xDC00 + (v & 0x3FF)
                ));
            } else if ch == '/' && rng.chance(50) {
                out.push_str("\\/");
            } else {
                out.push_str(&format!("\\u{:04X}", code));
            }
        } else if must {
            match ch {
                '"' => out.push_str("\\\""),
                '\\' => out.push_str("\\\\"),
                '\n' => out.push_str("\\n"),
                _ => out.push_str(&format!("\\u{:04x}", code)),
            }
        } else {
            out.push(ch);
        }
    }
    out.push('"');
    out
}

/// JSON text of `v` with members in random order (when `shuffle`), random
/// insignificant whitespace and random equivalent string escapes (when `vary`).
pub fn render(rng: &mut Rng, v: &Value, shuffle: bool, vary: bool) -> String {
    let mut out = String::new();
    render_into(rng, v, shuffle, vary, &mut out);
    out
}

fn render_into(rng: &mut Rng, v: &Value, shuffle: bool, vary: bool, out: &mut String) {
    ws(rng, out, vary);
    match v {
        Value::Null => out.push_str("null"),
        Value::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        Value::Int(n) => out.push_str(&n.to_string()),
        Value::Str(s) => out.push_str(&render_string(rng, s, vary)),
        Value::Arr(items) => {
            out.push('[');
            for (k, x) in items.iter().enumerate() {
                if k > 0 {
                    out.push(',');
                }
                render_into(rng, x, shuffle, vary, out);
            }
            ws(rng, out, vary);
            out.push(']');
        }
        Value::Obj(m) => {
            let mut members: Vec<(&String, &Value)> = m.iter().collect();
            if shuffle {
                rng.shuffle(&mut members);
            }
            out.push('{');
            for (k, (name, x)) in members.into_iter().enumerate() {
                if k > 0 {
                    out.push(',');
                }
                ws(rng, out, vary);
                out.push_str(&render_string(rng, name, vary));
                ws(rng, out, vary);
                out.push(':');
                render_into(rng, x, shuffle, vary, out);
            }
            ws(rng, out, vary);
            out.push('}');
        }
    }
    ws(rng, out, vary);
}

const ALPHABET: [&str; 14] = [
    "a",
    "b",
    "k",
    "Z",
    "0",
    "/",
    "\"",
    "\\",
    "\n",
    "\u{1}",
    "\u{e9}",
    "\u{2028}",
    "\u{1F600}",
    " ",
];

pub fn random_string(rng: &mut Rng, max: usize) -> String {
    (0..rng.below(max + 1))
        .map(|_| *rng.pick(&ALPHABET))
        .collect()
}

/// A random value inside the D-006 domain with at most `depth` further levels.
pub fn random_value(rng: &mut Rng, depth: usize) -> Value {
    let max = json::MAX_SAFE_INTEGER;
    let choice = if depth == 0 {
        rng.below(4)
    } else {
        rng.below(6)
    };
    match choice {
        0 => Value::Null,
        1 => Value::Bool(rng.chance(50)),
        2 => {
            let any = (rng.next() % (max as u64)) as i64;
            Value::Int(*rng.pick(&[0, 1, -1, 7, 42, max, -max, 1_000_000_007, any, -any]))
        }
        3 => Value::Str(random_string(rng, 6)),
        4 => Value::Arr(
            (0..rng.below(4))
                .map(|_| random_value(rng, depth - 1))
                .collect(),
        ),
        _ => Value::Obj(
            (0..rng.below(4))
                .map(|_| (random_string(rng, 4), random_value(rng, depth - 1)))
                .collect(),
        ),
    }
}
