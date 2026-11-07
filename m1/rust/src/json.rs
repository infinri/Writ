//! Strict JSON parser (D-017, D-018) and D-006 canonical serializer.
//!
//! The parser is the normative input path for every command. It works on
//! bytes, rejects duplicate member names (compared after escape decoding)
//! before any map is built, accepts only integer numbers spelled
//! `-?(0|[1-9][0-9]*)` within +/-(2^53-1), rejects `-0`, lone surrogates,
//! non-JSON literals and anything nested deeper than 64 (traversal depth:
//! root at 0, array elements, member names and member values at parent + 1).
//! Errors are reported for the first problem met reading left to right.

use sha2::{Digest, Sha256};
use std::collections::HashSet;

pub const MAX_SAFE: i64 = 9_007_199_254_740_991;
pub const MAX_DEPTH: usize = 64;

/// Equality is JSON equality (RFC 8259, D-006): objects are unordered maps,
/// so member order never affects a comparison; arrays stay ordered.
#[derive(Clone, Debug)]
pub enum Value {
    Null,
    Bool(bool),
    Int(i64),
    Str(String),
    Arr(Vec<Value>),
    /// Members in input order; names are unique (enforced by the parser and
    /// by every constructor in this crate).
    Obj(Vec<(String, Value)>),
}

impl PartialEq for Value {
    fn eq(&self, other: &Value) -> bool {
        match (self, other) {
            (Value::Null, Value::Null) => true,
            (Value::Bool(a), Value::Bool(b)) => a == b,
            (Value::Int(a), Value::Int(b)) => a == b,
            (Value::Str(a), Value::Str(b)) => a == b,
            (Value::Arr(a), Value::Arr(b)) => a == b,
            // Names are unique within an object, so equal sizes plus every
            // member of `a` found with an equal value in `b` is map equality.
            (Value::Obj(a), Value::Obj(b)) => {
                a.len() == b.len() && a.iter().all(|(k, v)| b.iter().any(|(k2, v2)| k2 == k && v2 == v))
            }
            _ => false,
        }
    }
}

impl Eq for Value {}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum JsonError {
    DuplicateKey,
    NumberNotInteger,
    NumberNegativeZero,
    NumberOutOfRange,
    InvalidJson,
    LoneSurrogate,
    DepthExceeded,
}

impl JsonError {
    pub fn code(self) -> &'static str {
        match self {
            JsonError::DuplicateKey => "DUPLICATE_KEY",
            JsonError::NumberNotInteger => "NUMBER_NOT_INTEGER",
            JsonError::NumberNegativeZero => "NUMBER_NEGATIVE_ZERO",
            JsonError::NumberOutOfRange => "NUMBER_OUT_OF_RANGE",
            JsonError::InvalidJson => "INVALID_JSON",
            JsonError::LoneSurrogate => "LONE_SURROGATE",
            JsonError::DepthExceeded => "DEPTH_EXCEEDED",
        }
    }
}

type R<T> = Result<T, JsonError>;

struct Parser<'a> {
    b: &'a [u8],
    i: usize,
    max_depth: usize,
    /// When set, the value of this root member is not parsed; its byte span is
    /// recorded instead (used only for the protocol envelope).
    skip_member: Option<&'a str>,
    skipped: Option<(usize, usize)>,
}

/// Parse a complete JSON text strictly.
pub fn parse(bytes: &[u8]) -> R<Value> {
    let mut p = Parser { b: bytes, i: 0, max_depth: MAX_DEPTH, skip_member: None, skipped: None };
    p.ws();
    let v = p.value(0)?;
    p.ws();
    if p.i != p.b.len() {
        return Err(JsonError::InvalidJson);
    }
    Ok(v)
}

/// Parse the protocol envelope, leaving the raw bytes of root member
/// `member` unparsed so that it can be parsed strictly as its own document
/// (equivalent to `input_text`). Returns the envelope and the raw span.
/// The envelope itself is held to every strict rule, including the depth
/// limit (the skipped member does not count towards it).
pub fn parse_envelope<'a>(bytes: &'a [u8], member: &'a str) -> R<(Value, Option<&'a [u8]>)> {
    let mut p = Parser { b: bytes, i: 0, max_depth: MAX_DEPTH, skip_member: Some(member), skipped: None };
    p.ws();
    let v = p.value(0)?;
    p.ws();
    if p.i != p.b.len() {
        return Err(JsonError::InvalidJson);
    }
    let raw = p.skipped.map(|(s, e)| &bytes[s..e]);
    Ok((v, raw))
}

impl<'a> Parser<'a> {
    fn ws(&mut self) {
        while self.i < self.b.len() && matches!(self.b[self.i], b' ' | b'\t' | b'\n' | b'\r') {
            self.i += 1;
        }
    }

    fn peek(&self) -> Option<u8> {
        self.b.get(self.i).copied()
    }

    fn value(&mut self, depth: usize) -> R<Value> {
        if depth > self.max_depth {
            return Err(JsonError::DepthExceeded);
        }
        match self.peek() {
            None => Err(JsonError::InvalidJson),
            Some(b'{') => self.object(depth),
            Some(b'[') => self.array(depth),
            Some(b'"') => Ok(Value::Str(self.string()?)),
            Some(b't') => self.literal(b"true", Value::Bool(true)),
            Some(b'f') => self.literal(b"false", Value::Bool(false)),
            Some(b'n') => self.literal(b"null", Value::Null),
            Some(c) if c == b'-' || c.is_ascii_digit() => self.number(),
            _ => Err(JsonError::InvalidJson),
        }
    }

    fn literal(&mut self, word: &[u8], v: Value) -> R<Value> {
        if self.b[self.i..].starts_with(word) {
            self.i += word.len();
            Ok(v)
        } else {
            Err(JsonError::InvalidJson)
        }
    }

    fn number(&mut self) -> R<Value> {
        let start = self.i;
        let neg = self.peek() == Some(b'-');
        if neg {
            self.i += 1;
        }
        let int_start = self.i;
        match self.peek() {
            Some(b'0') => self.i += 1,
            Some(c) if c.is_ascii_digit() => {
                while matches!(self.peek(), Some(c) if c.is_ascii_digit()) {
                    self.i += 1;
                }
            }
            _ => return Err(JsonError::InvalidJson),
        }
        let int_end = self.i;
        // A leading zero followed by more digits is not a JSON number.
        if &self.b[int_start..int_end] == b"0" && matches!(self.peek(), Some(c) if c.is_ascii_digit()) {
            return Err(JsonError::InvalidJson);
        }
        let mut non_integer = false;
        if self.peek() == Some(b'.') {
            self.i += 1;
            if !matches!(self.peek(), Some(c) if c.is_ascii_digit()) {
                return Err(JsonError::InvalidJson);
            }
            while matches!(self.peek(), Some(c) if c.is_ascii_digit()) {
                self.i += 1;
            }
            non_integer = true;
        }
        if matches!(self.peek(), Some(b'e') | Some(b'E')) {
            self.i += 1;
            if matches!(self.peek(), Some(b'+') | Some(b'-')) {
                self.i += 1;
            }
            if !matches!(self.peek(), Some(c) if c.is_ascii_digit()) {
                return Err(JsonError::InvalidJson);
            }
            while matches!(self.peek(), Some(c) if c.is_ascii_digit()) {
                self.i += 1;
            }
            non_integer = true;
        }
        if non_integer {
            return Err(JsonError::NumberNotInteger);
        }
        let digits = &self.b[int_start..int_end];
        if neg && digits == b"0" {
            return Err(JsonError::NumberNegativeZero);
        }
        if digits.len() > 16 {
            return Err(JsonError::NumberOutOfRange);
        }
        let mut n: i64 = 0;
        for &d in digits {
            n = n * 10 + i64::from(d - b'0');
        }
        if n > MAX_SAFE {
            return Err(JsonError::NumberOutOfRange);
        }
        let _ = start;
        Ok(Value::Int(if neg { -n } else { n }))
    }

    fn hex4(&mut self) -> R<u32> {
        if self.i + 4 > self.b.len() {
            return Err(JsonError::InvalidJson);
        }
        let mut v = 0u32;
        for k in 0..4 {
            let c = self.b[self.i + k];
            let d = match c {
                b'0'..=b'9' => c - b'0',
                b'a'..=b'f' => c - b'a' + 10,
                b'A'..=b'F' => c - b'A' + 10,
                _ => return Err(JsonError::InvalidJson),
            };
            v = v * 16 + u32::from(d);
        }
        self.i += 4;
        Ok(v)
    }

    fn string(&mut self) -> R<String> {
        // Caller guarantees the opening quote.
        self.i += 1;
        let mut out: Vec<u8> = Vec::new();
        loop {
            let c = match self.peek() {
                None => return Err(JsonError::InvalidJson),
                Some(c) => c,
            };
            match c {
                b'"' => {
                    self.i += 1;
                    break;
                }
                b'\\' => {
                    self.i += 1;
                    let e = self.peek().ok_or(JsonError::InvalidJson)?;
                    self.i += 1;
                    match e {
                        b'"' => out.push(b'"'),
                        b'\\' => out.push(b'\\'),
                        b'/' => out.push(b'/'),
                        b'b' => out.push(0x08),
                        b'f' => out.push(0x0c),
                        b'n' => out.push(b'\n'),
                        b'r' => out.push(b'\r'),
                        b't' => out.push(b'\t'),
                        b'u' => {
                            let u = self.hex4()?;
                            let cp = if (0xD800..0xDC00).contains(&u) {
                                // High surrogate: must be followed by an escaped low surrogate.
                                if self.b[self.i..].starts_with(b"\\u") {
                                    let save = self.i;
                                    self.i += 2;
                                    let lo = self.hex4()?;
                                    if (0xDC00..0xE000).contains(&lo) {
                                        0x10000 + ((u - 0xD800) << 10) + (lo - 0xDC00)
                                    } else {
                                        self.i = save;
                                        return Err(JsonError::LoneSurrogate);
                                    }
                                } else {
                                    return Err(JsonError::LoneSurrogate);
                                }
                            } else if (0xDC00..0xE000).contains(&u) {
                                return Err(JsonError::LoneSurrogate);
                            } else {
                                u
                            };
                            let ch = char::from_u32(cp).ok_or(JsonError::InvalidJson)?;
                            let mut buf = [0u8; 4];
                            out.extend_from_slice(ch.encode_utf8(&mut buf).as_bytes());
                        }
                        _ => return Err(JsonError::InvalidJson),
                    }
                }
                c if c < 0x20 => return Err(JsonError::InvalidJson),
                _ => {
                    out.push(c);
                    self.i += 1;
                }
            }
        }
        String::from_utf8(out).map_err(|_| JsonError::InvalidJson)
    }

    fn array(&mut self, depth: usize) -> R<Value> {
        self.i += 1;
        let mut items = Vec::new();
        self.ws();
        if self.peek() == Some(b']') {
            self.i += 1;
            return Ok(Value::Arr(items));
        }
        loop {
            self.ws();
            items.push(self.value(depth + 1)?);
            self.ws();
            match self.peek() {
                Some(b',') => self.i += 1,
                Some(b']') => {
                    self.i += 1;
                    return Ok(Value::Arr(items));
                }
                _ => return Err(JsonError::InvalidJson),
            }
        }
    }

    fn object(&mut self, depth: usize) -> R<Value> {
        self.i += 1;
        let mut members: Vec<(String, Value)> = Vec::new();
        let mut seen: HashSet<String> = HashSet::new();
        self.ws();
        if self.peek() == Some(b'}') {
            self.i += 1;
            return Ok(Value::Obj(members));
        }
        loop {
            self.ws();
            if self.peek() != Some(b'"') {
                return Err(JsonError::InvalidJson);
            }
            if depth + 1 > self.max_depth {
                return Err(JsonError::DepthExceeded);
            }
            let name = self.string()?;
            if !seen.insert(name.clone()) {
                return Err(JsonError::DuplicateKey);
            }
            self.ws();
            if self.peek() != Some(b':') {
                return Err(JsonError::InvalidJson);
            }
            self.i += 1;
            self.ws();
            let skip = depth == 0 && self.skip_member == Some(name.as_str());
            let v = if skip {
                let s = self.i;
                self.skip_value()?;
                self.skipped = Some((s, self.i));
                Value::Null
            } else {
                self.value(depth + 1)?
            };
            members.push((name, v));
            self.ws();
            match self.peek() {
                Some(b',') => self.i += 1,
                Some(b'}') => {
                    self.i += 1;
                    return Ok(Value::Obj(members));
                }
                _ => return Err(JsonError::InvalidJson),
            }
        }
    }

    /// Structural skip of one value (envelope only): balanced brackets outside
    /// strings. The skipped bytes are parsed strictly afterwards.
    fn skip_value(&mut self) -> R<()> {
        let mut nest: usize = 0;
        loop {
            let c = self.peek().ok_or(JsonError::InvalidJson)?;
            match c {
                b'"' => {
                    self.i += 1;
                    loop {
                        let d = self.peek().ok_or(JsonError::InvalidJson)?;
                        self.i += 1;
                        if d == b'\\' {
                            self.i += 1;
                        } else if d == b'"' {
                            break;
                        }
                    }
                }
                b'{' | b'[' => {
                    nest += 1;
                    self.i += 1;
                }
                b'}' | b']' => {
                    if nest == 0 {
                        return Ok(());
                    }
                    nest -= 1;
                    self.i += 1;
                }
                b',' if nest == 0 => return Ok(()),
                _ => self.i += 1,
            }
            if nest == 0 && matches!(self.peek(), Some(b',') | Some(b'}') | Some(b']') | None) {
                // Trailing whitespace of a scalar is consumed by the caller.
                return Ok(());
            }
        }
    }
}

// ---------------------------------------------------------------------------
// Canonical form (D-006): keys sorted by code point (UTF-8 byte order is
// identical), no whitespace, minimal escaping, integers only.

#[derive(Debug, PartialEq, Eq)]
pub struct CanonError;

fn check_depth(v: &Value, depth: usize) -> Result<(), CanonError> {
    if depth > MAX_DEPTH {
        return Err(CanonError);
    }
    match v {
        Value::Int(n) if n.abs() > MAX_SAFE => Err(CanonError),
        Value::Arr(a) => a.iter().try_for_each(|x| check_depth(x, depth + 1)),
        Value::Obj(m) => {
            if !m.is_empty() && depth + 1 > MAX_DEPTH {
                return Err(CanonError);
            }
            m.iter().try_for_each(|(_, x)| check_depth(x, depth + 1))
        }
        _ => Ok(()),
    }
}

fn write_str(out: &mut String, s: &str) {
    out.push('"');
    for ch in s.chars() {
        match ch {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{08}' => out.push_str("\\b"),
            '\u{0c}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push('"');
}

fn write_value(out: &mut String, v: &Value) {
    match v {
        Value::Null => out.push_str("null"),
        Value::Bool(true) => out.push_str("true"),
        Value::Bool(false) => out.push_str("false"),
        Value::Int(n) => out.push_str(&n.to_string()),
        Value::Str(s) => write_str(out, s),
        Value::Arr(a) => {
            out.push('[');
            for (k, x) in a.iter().enumerate() {
                if k > 0 {
                    out.push(',');
                }
                write_value(out, x);
            }
            out.push(']');
        }
        Value::Obj(m) => {
            let mut sorted: Vec<&(String, Value)> = m.iter().collect();
            sorted.sort_by(|a, b| a.0.as_bytes().cmp(b.0.as_bytes()));
            out.push('{');
            for (k, (name, x)) in sorted.into_iter().enumerate() {
                if k > 0 {
                    out.push(',');
                }
                write_str(out, name);
                out.push(':');
                write_value(out, x);
            }
            out.push('}');
        }
    }
}

/// Canonical text of a value inside the D-006 domain.
pub fn canonical(v: &Value) -> Result<String, CanonError> {
    check_depth(v, 0)?;
    let mut s = String::new();
    write_value(&mut s, v);
    Ok(s)
}

/// Serialization used for protocol output (same encoding, no domain check).
pub fn to_text(v: &Value) -> String {
    let mut s = String::new();
    write_value(&mut s, v);
    s
}

pub fn sha256_hex(bytes: &[u8]) -> String {
    let d = Sha256::digest(bytes);
    hex(&d)
}

pub fn hex(bytes: &[u8]) -> String {
    const H: &[u8; 16] = b"0123456789abcdef";
    let mut s = String::with_capacity(bytes.len() * 2);
    for b in bytes {
        s.push(H[(b >> 4) as usize] as char);
        s.push(H[(b & 15) as usize] as char);
    }
    s
}

/// `sha256:<hex>` of the canonical form.
pub fn digest(v: &Value) -> Result<String, CanonError> {
    Ok(format!("sha256:{}", sha256_hex(canonical(v)?.as_bytes())))
}

// ---------------------------------------------------------------------------
// Small accessors and constructors.

impl Value {
    pub fn get(&self, key: &str) -> Option<&Value> {
        match self {
            Value::Obj(m) => m.iter().find(|(k, _)| k == key).map(|(_, v)| v),
            _ => None,
        }
    }
    pub fn as_str(&self) -> Option<&str> {
        match self {
            Value::Str(s) => Some(s),
            _ => None,
        }
    }
    pub fn as_int(&self) -> Option<i64> {
        match self {
            Value::Int(n) => Some(*n),
            _ => None,
        }
    }
    pub fn as_bool(&self) -> Option<bool> {
        match self {
            Value::Bool(b) => Some(*b),
            _ => None,
        }
    }
    pub fn as_arr(&self) -> Option<&Vec<Value>> {
        match self {
            Value::Arr(a) => Some(a),
            _ => None,
        }
    }
    pub fn as_obj(&self) -> Option<&Vec<(String, Value)>> {
        match self {
            Value::Obj(m) => Some(m),
            _ => None,
        }
    }
    pub fn is_null(&self) -> bool {
        matches!(self, Value::Null)
    }
    pub fn str_at(&self, key: &str) -> Option<&str> {
        self.get(key).and_then(Value::as_str)
    }
    /// Copy of an object without one member.
    pub fn without(&self, key: &str) -> Value {
        match self {
            Value::Obj(m) => Value::Obj(m.iter().filter(|(k, _)| k != key).cloned().collect()),
            other => other.clone(),
        }
    }
}

pub fn s(x: &str) -> Value {
    Value::Str(x.to_string())
}

/// Build an object; later entries with an existing name replace it.
pub fn obj(entries: Vec<(&str, Value)>) -> Value {
    let mut m: Vec<(String, Value)> = Vec::new();
    for (k, v) in entries {
        if let Some(slot) = m.iter_mut().find(|(n, _)| n == k) {
            slot.1 = v;
        } else {
            m.push((k.to_string(), v));
        }
    }
    Value::Obj(m)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn err(t: &str) -> &'static str {
        parse(t.as_bytes()).unwrap_err().code()
    }

    #[test]
    fn known_vector() {
        let v = parse("{\"b\": [1, true, null], \"a\": \"\u{e9}\\n\", \"c\": {\"z\": 0, \"y\": \"x\"}}".as_bytes()).unwrap();
        assert_eq!(canonical(&v).unwrap(), "{\"a\":\"\u{e9}\\n\",\"b\":[1,true,null],\"c\":{\"y\":\"x\",\"z\":0}}");
        assert_eq!(digest(&v).unwrap(), "sha256:c25cf6d8af15edaf7f9ef3f86172767d36bfdfb090c8eebac9abecca9737980b");
    }

    #[test]
    fn duplicates_including_escaped() {
        assert_eq!(err(r#"{"a":1,"a":1}"#), "DUPLICATE_KEY");
        assert_eq!(err(r#"{"a":1,"\u0061":2}"#), "DUPLICATE_KEY");
        assert_eq!(err(r#"[{"k":1},{"k":2,"k":3}]"#), "DUPLICATE_KEY");
        assert!(parse(br#"[{"k":1},{"k":2}]"#).is_ok());
    }

    #[test]
    fn numbers() {
        assert_eq!(err("1.0"), "NUMBER_NOT_INTEGER");
        assert_eq!(err("1e3"), "NUMBER_NOT_INTEGER");
        assert_eq!(err("-0"), "NUMBER_NEGATIVE_ZERO");
        assert_eq!(err("9007199254740992"), "NUMBER_OUT_OF_RANGE");
        assert_eq!(err("-9007199254740992"), "NUMBER_OUT_OF_RANGE");
        assert_eq!(err("18446744073709551623"), "NUMBER_OUT_OF_RANGE");
        assert_eq!(err("01"), "INVALID_JSON");
        assert_eq!(err("-01"), "INVALID_JSON");
        assert_eq!(err("+1"), "INVALID_JSON");
        assert_eq!(err("1."), "INVALID_JSON");
        assert_eq!(parse(b"-9007199254740991").unwrap(), Value::Int(-MAX_SAFE));
    }

    #[test]
    fn literals_and_garbage() {
        for t in ["NaN", "Infinity", "[-Infinity]", "{\"a\":NaN}", "[1,]", "{} {}", "tru", "\"\\x\"", "\"a\u{1}\""] {
            assert_eq!(err(t), "INVALID_JSON", "{t}");
        }
        assert_eq!(err(r#""\ud800""#), "LONE_SURROGATE");
        assert_eq!(err(r#""\udc00""#), "LONE_SURROGATE");
        assert_eq!(parse(br#""\ud83d\ude00""#).unwrap(), Value::Str("\u{1F600}".into()));
    }

    #[test]
    fn depth_is_traversal_depth() {
        let nest = |n: usize, inner: &str| format!("{}{}{}", "[".repeat(n), inner, "]".repeat(n));
        assert!(parse(nest(65, "").as_bytes()).is_ok());
        assert_eq!(err(&nest(66, "")), "DEPTH_EXCEEDED");
        assert!(parse(nest(64, "0").as_bytes()).is_ok());
        assert_eq!(err(&nest(65, "0")), "DEPTH_EXCEEDED");
        assert_eq!(err(&nest(64, "{\"k\":0}")), "DEPTH_EXCEEDED");
        assert!(parse(nest(64, "{}").as_bytes()).is_ok());
    }

    #[test]
    fn canonical_escaping_and_order() {
        let v = parse(br#"{"b":"\u00e9","a":"\/","\u00e9":0,"c":"\u0001\t"}"#).unwrap();
        assert_eq!(canonical(&v).unwrap(), "{\"a\":\"/\",\"b\":\"\u{e9}\",\"c\":\"\\u0001\\t\",\"\u{e9}\":0}");
    }

    #[test]
    fn envelope_keeps_input_raw() {
        let line = br#"{"id":"x","input":{"a":[1,2],"b":"}"}}"#;
        let (env, raw) = parse_envelope(line, "input").unwrap();
        assert_eq!(env.str_at("id"), Some("x"));
        assert_eq!(raw.unwrap(), br#"{"a":[1,2],"b":"}"}"#);
    }

    #[test]
    fn object_equality_ignores_member_order() {
        let a = parse(br#"{"a":1,"b":{"x":[1,{"p":true,"q":null}],"y":"s"}}"#).unwrap();
        let b = parse(br#"{"b":{"y":"s","x":[1,{"q":null,"p":true}]},"a":1}"#).unwrap();
        assert_eq!(a, b);
        assert_ne!(a, parse(br#"{"a":1,"b":{"x":[{"p":true,"q":null},1],"y":"s"}}"#).unwrap(), "arrays stay ordered");
        assert_ne!(a, parse(br#"{"a":1}"#).unwrap());
        assert_ne!(parse(br#"{"a":1}"#).unwrap(), parse(br#"{"a":1,"b":2}"#).unwrap());
        assert_ne!(parse(br#"{"a":1}"#).unwrap(), parse(br#"{"b":1}"#).unwrap());
    }
}
