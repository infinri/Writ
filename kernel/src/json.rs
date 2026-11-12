//! Strict JSON parsing (D-017, D-018) and the D-006 canonical form.
//!
//! The parser is the only input path of every kernel command. It works on
//! bytes and rejects, reading left to right and reporting the first problem:
//! duplicate member names at any depth (compared after escape decoding, before
//! any map is built); numbers other than `-?(0|[1-9][0-9]*)` (fractions and
//! exponents are `NUMBER_NOT_INTEGER`, `-0` is `NUMBER_NEGATIVE_ZERO`,
//! magnitudes above 2^53-1 are `NUMBER_OUT_OF_RANGE`); lone surrogates;
//! non-JSON literals such as `NaN` and `Infinity`; trailing content; and any
//! value or member name deeper than 64. Depth is traversal depth (D-018 A1):
//! the root is at depth 0, and every array element, member name and member
//! value is at its parent's depth plus 1.
//!
//! The canonical form (D-006) sorts member names by code point (identical to
//! UTF-8 byte order), has no whitespace, escapes only `"`, `\` and control
//! characters, and admits integers within +/-(2^53-1) only. The digest is
//! `sha256:<64 lowercase hex>` of the canonical UTF-8 bytes.

use sha2::{Digest as _, Sha256};
use std::collections::BTreeMap;
use std::fmt;

/// Largest integer magnitude admitted anywhere (2^53-1).
pub const MAX_SAFE_INTEGER: i64 = 9_007_199_254_740_991;

/// Deepest admitted traversal depth.
pub const MAX_DEPTH: usize = 64;

/// Object members. Names are unique by construction and iterate in code point
/// order, which is the canonical order; input member order is never kept, so
/// it can never be significant (D-021 item 7).
pub type Map = BTreeMap<String, Value>;

/// A JSON value inside the D-006 domain: no floats. Equality is JSON equality
/// (objects are maps, arrays are ordered).
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Value {
    /// `null`.
    Null,
    /// `true` or `false`.
    Bool(bool),
    /// An integer. Values produced by [`parse`] are within +/-(2^53-1).
    Int(i64),
    /// A string of Unicode scalar values (no lone surrogates).
    Str(String),
    /// An array.
    Arr(Vec<Value>),
    /// An object.
    Obj(Map),
}

/// Strict parsing failure. The code strings are part of the command contracts.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum JsonError {
    /// A member name repeated in one object (after escape decoding).
    DuplicateKey,
    /// A number with a fraction or an exponent.
    NumberNotInteger,
    /// The number `-0`.
    NumberNegativeZero,
    /// An integer outside +/-(2^53-1).
    NumberOutOfRange,
    /// Anything that is not JSON text, including `NaN`, `Infinity` and trailing content.
    InvalidJson,
    /// A `\u` escape of an unpaired surrogate.
    LoneSurrogate,
    /// A value or member name deeper than [`MAX_DEPTH`].
    DepthExceeded,
}

impl JsonError {
    /// Contract code of the error.
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

impl fmt::Display for JsonError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.code())
    }
}

impl std::error::Error for JsonError {}

/// A value outside the D-006 domain, met while canonicalizing a value that was
/// built in memory rather than parsed.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum CanonicalError {
    /// An integer outside +/-(2^53-1).
    NumberOutOfRange,
    /// A value or member name deeper than [`MAX_DEPTH`].
    DepthExceeded,
}

impl CanonicalError {
    /// Contract code of the error (shared with [`JsonError`]).
    pub fn code(self) -> &'static str {
        match self {
            CanonicalError::NumberOutOfRange => JsonError::NumberOutOfRange.code(),
            CanonicalError::DepthExceeded => JsonError::DepthExceeded.code(),
        }
    }
}

impl fmt::Display for CanonicalError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.code())
    }
}

impl std::error::Error for CanonicalError {}

/// A `sha256:<64 lowercase hex>` digest.
#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct Digest(String);

impl Digest {
    /// Digest of raw bytes.
    pub fn of_bytes(bytes: &[u8]) -> Digest {
        Digest(format!("sha256:{}", hex(&Sha256::digest(bytes))))
    }

    /// The full `sha256:<hex>` text.
    pub fn as_str(&self) -> &str {
        &self.0
    }

    /// The 64 hex characters after the `sha256:` prefix.
    pub fn hex(&self) -> &str {
        self.0.get(7..).unwrap_or("")
    }
}

impl fmt::Display for Digest {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

fn hex(bytes: &[u8]) -> String {
    const DIGITS: &[u8; 16] = b"0123456789abcdef";
    let mut s = String::with_capacity(bytes.len() * 2);
    for b in bytes {
        s.push(char::from(DIGITS[usize::from(b >> 4)]));
        s.push(char::from(DIGITS[usize::from(b & 0x0f)]));
    }
    s
}

/// Parse a complete JSON text strictly.
pub fn parse(bytes: &[u8]) -> Result<Value, JsonError> {
    let mut p = Parser { b: bytes, i: 0 };
    p.ws();
    let v = p.value(0)?;
    p.ws();
    if p.i == p.b.len() {
        Ok(v)
    } else {
        Err(JsonError::InvalidJson)
    }
}

struct Parser<'a> {
    b: &'a [u8],
    i: usize,
}

impl Parser<'_> {
    fn peek(&self) -> Option<u8> {
        self.b.get(self.i).copied()
    }

    fn peek_digit(&self) -> bool {
        self.peek().is_some_and(|c| c.is_ascii_digit())
    }

    fn ws(&mut self) {
        while matches!(self.peek(), Some(b' ' | b'\t' | b'\n' | b'\r')) {
            self.i += 1;
        }
    }

    fn value(&mut self, depth: usize) -> Result<Value, JsonError> {
        if depth > MAX_DEPTH {
            return Err(JsonError::DepthExceeded);
        }
        match self.peek() {
            Some(b'{') => self.object(depth),
            Some(b'[') => self.array(depth),
            Some(b'"') => self.string().map(Value::Str),
            Some(b't') => self.literal(b"true", Value::Bool(true)),
            Some(b'f') => self.literal(b"false", Value::Bool(false)),
            Some(b'n') => self.literal(b"null", Value::Null),
            Some(c) if c == b'-' || c.is_ascii_digit() => self.number(),
            _ => Err(JsonError::InvalidJson),
        }
    }

    fn literal(&mut self, word: &[u8], v: Value) -> Result<Value, JsonError> {
        if self
            .b
            .get(self.i..)
            .is_some_and(|rest| rest.starts_with(word))
        {
            self.i += word.len();
            Ok(v)
        } else {
            Err(JsonError::InvalidJson)
        }
    }

    fn digits(&mut self) {
        while self.peek_digit() {
            self.i += 1;
        }
    }

    fn number(&mut self) -> Result<Value, JsonError> {
        let negative = self.peek() == Some(b'-');
        if negative {
            self.i += 1;
        }
        let start = self.i;
        match self.peek() {
            Some(b'0') => {
                self.i += 1;
                // A leading zero followed by more digits is not a JSON number.
                if self.peek_digit() {
                    return Err(JsonError::InvalidJson);
                }
            }
            Some(c) if c.is_ascii_digit() => self.digits(),
            _ => return Err(JsonError::InvalidJson),
        }
        let end = self.i;
        let mut integer = true;
        if self.peek() == Some(b'.') {
            self.i += 1;
            if !self.peek_digit() {
                return Err(JsonError::InvalidJson);
            }
            self.digits();
            integer = false;
        }
        if matches!(self.peek(), Some(b'e' | b'E')) {
            self.i += 1;
            if matches!(self.peek(), Some(b'+' | b'-')) {
                self.i += 1;
            }
            if !self.peek_digit() {
                return Err(JsonError::InvalidJson);
            }
            self.digits();
            integer = false;
        }
        if !integer {
            return Err(JsonError::NumberNotInteger);
        }
        let digits = self.b.get(start..end).ok_or(JsonError::InvalidJson)?;
        if negative && digits == b"0" {
            return Err(JsonError::NumberNegativeZero);
        }
        // 2^53-1 has 16 digits; anything longer is out of range (and cannot overflow below).
        if digits.len() > 16 {
            return Err(JsonError::NumberOutOfRange);
        }
        let magnitude = digits
            .iter()
            .fold(0i64, |n, d| n * 10 + i64::from(d - b'0'));
        if magnitude > MAX_SAFE_INTEGER {
            return Err(JsonError::NumberOutOfRange);
        }
        Ok(Value::Int(if negative { -magnitude } else { magnitude }))
    }

    fn hex4(&mut self) -> Result<u32, JsonError> {
        let quad = self
            .b
            .get(self.i..self.i + 4)
            .ok_or(JsonError::InvalidJson)?;
        let mut v = 0u32;
        for c in quad {
            let d = char::from(*c).to_digit(16).ok_or(JsonError::InvalidJson)?;
            v = v * 16 + d;
        }
        self.i += 4;
        Ok(v)
    }

    /// A `\u` escape (the `\u` already consumed), pairing a high surrogate
    /// with an immediately following escaped low surrogate.
    fn unicode_escape(&mut self) -> Result<char, JsonError> {
        let unit = self.hex4()?;
        let code_point = match unit {
            0xD800..=0xDBFF => {
                if !self
                    .b
                    .get(self.i..)
                    .is_some_and(|rest| rest.starts_with(b"\\u"))
                {
                    return Err(JsonError::LoneSurrogate);
                }
                self.i += 2;
                let low = self.hex4()?;
                if !(0xDC00..=0xDFFF).contains(&low) {
                    return Err(JsonError::LoneSurrogate);
                }
                0x10000 + ((unit - 0xD800) << 10) + (low - 0xDC00)
            }
            0xDC00..=0xDFFF => return Err(JsonError::LoneSurrogate),
            other => other,
        };
        char::from_u32(code_point).ok_or(JsonError::InvalidJson)
    }

    fn string(&mut self) -> Result<String, JsonError> {
        // The caller saw the opening quote.
        self.i += 1;
        let mut out: Vec<u8> = Vec::new();
        loop {
            let c = self.peek().ok_or(JsonError::InvalidJson)?;
            self.i += 1;
            match c {
                b'"' => break,
                b'\\' => {
                    let e = self.peek().ok_or(JsonError::InvalidJson)?;
                    self.i += 1;
                    let decoded = match e {
                        b'"' => '"',
                        b'\\' => '\\',
                        b'/' => '/',
                        b'b' => '\u{08}',
                        b'f' => '\u{0c}',
                        b'n' => '\n',
                        b'r' => '\r',
                        b't' => '\t',
                        b'u' => self.unicode_escape()?,
                        _ => return Err(JsonError::InvalidJson),
                    };
                    let mut buf = [0u8; 4];
                    out.extend_from_slice(decoded.encode_utf8(&mut buf).as_bytes());
                }
                c if c < 0x20 => return Err(JsonError::InvalidJson),
                c => out.push(c),
            }
        }
        String::from_utf8(out).map_err(|_| JsonError::InvalidJson)
    }

    fn array(&mut self, depth: usize) -> Result<Value, JsonError> {
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

    fn object(&mut self, depth: usize) -> Result<Value, JsonError> {
        self.i += 1;
        let mut members = Map::new();
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
            if depth + 1 > MAX_DEPTH {
                return Err(JsonError::DepthExceeded);
            }
            let name = self.string()?;
            if members.contains_key(&name) {
                return Err(JsonError::DuplicateKey);
            }
            self.ws();
            if self.peek() != Some(b':') {
                return Err(JsonError::InvalidJson);
            }
            self.i += 1;
            self.ws();
            let v = self.value(depth + 1)?;
            members.insert(name, v);
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
}

// ===========================================================================
// Serialization.

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
            c if u32::from(c) < 0x20 => out.push_str(&format!("\\u{:04x}", u32::from(c))),
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
        Value::Arr(items) => {
            out.push('[');
            for (k, x) in items.iter().enumerate() {
                if k > 0 {
                    out.push(',');
                }
                write_value(out, x);
            }
            out.push(']');
        }
        Value::Obj(members) => {
            out.push('{');
            for (k, (name, x)) in members.iter().enumerate() {
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

fn check_domain(v: &Value, depth: usize) -> Result<(), CanonicalError> {
    if depth > MAX_DEPTH {
        return Err(CanonicalError::DepthExceeded);
    }
    match v {
        Value::Int(n) if n.unsigned_abs() > MAX_SAFE_INTEGER.unsigned_abs() => {
            Err(CanonicalError::NumberOutOfRange)
        }
        Value::Arr(items) => items.iter().try_for_each(|x| check_domain(x, depth + 1)),
        Value::Obj(members) => {
            if !members.is_empty() && depth + 1 > MAX_DEPTH {
                return Err(CanonicalError::DepthExceeded);
            }
            members
                .values()
                .try_for_each(|x| check_domain(x, depth + 1))
        }
        _ => Ok(()),
    }
}

/// D-006 canonical text of a value; fails only for values outside the domain.
pub fn canonical(v: &Value) -> Result<String, CanonicalError> {
    check_domain(v, 0)?;
    let mut s = String::new();
    write_value(&mut s, v);
    Ok(s)
}

/// D-006 digest: SHA-256 of the canonical text.
pub fn digest(v: &Value) -> Result<Digest, CanonicalError> {
    canonical(v).map(|text| Digest::of_bytes(text.as_bytes()))
}

impl Value {
    /// Compact text in the canonical encoding without the domain check (used
    /// for command output, whose values are always built inside the domain).
    pub fn to_json_text(&self) -> String {
        let mut s = String::new();
        write_value(&mut s, self);
        s
    }

    /// Member `key` of an object; `None` for a missing member or a non-object.
    pub fn get(&self, key: &str) -> Option<&Value> {
        match self {
            Value::Obj(m) => m.get(key),
            _ => None,
        }
    }

    /// The members of an object.
    pub fn as_obj(&self) -> Option<&Map> {
        match self {
            Value::Obj(m) => Some(m),
            _ => None,
        }
    }

    /// The items of an array.
    pub fn as_arr(&self) -> Option<&[Value]> {
        match self {
            Value::Arr(a) => Some(a),
            _ => None,
        }
    }

    /// The text of a string.
    pub fn as_str(&self) -> Option<&str> {
        match self {
            Value::Str(s) => Some(s),
            _ => None,
        }
    }

    /// The value of an integer.
    pub fn as_int(&self) -> Option<i64> {
        match self {
            Value::Int(n) => Some(*n),
            _ => None,
        }
    }

    /// The value of a boolean.
    pub fn as_bool(&self) -> Option<bool> {
        match self {
            Value::Bool(b) => Some(*b),
            _ => None,
        }
    }

    /// Copy of an object without member `key` (other values are copied unchanged).
    pub fn without(&self, key: &str) -> Value {
        match self {
            Value::Obj(m) => Value::Obj(
                m.iter()
                    .filter(|(k, _)| k.as_str() != key)
                    .map(|(k, v)| (k.clone(), v.clone()))
                    .collect(),
            ),
            other => other.clone(),
        }
    }
}

/// A string value.
pub fn s(text: &str) -> Value {
    Value::Str(text.to_string())
}

/// An object from `(name, value)` pairs; a later pair replaces an earlier one of the same name.
pub fn obj<const N: usize>(members: [(&str, Value); N]) -> Value {
    Value::Obj(
        members
            .into_iter()
            .map(|(k, v)| (k.to_string(), v))
            .collect(),
    )
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
mod tests {
    use super::*;

    fn err(text: &str) -> &'static str {
        match parse(text.as_bytes()) {
            Err(e) => e.code(),
            Ok(v) => panic!("{text} parsed as {v:?}"),
        }
    }

    fn canon(text: &str) -> String {
        canonical(&parse(text.as_bytes()).unwrap()).unwrap()
    }

    fn nest(n: usize, inner: &str) -> String {
        format!("{}{}{}", "[".repeat(n), inner, "]".repeat(n))
    }

    #[test]
    fn d006_known_vector() {
        let v = parse(
            "{\"b\": [1, true, null], \"a\": \"\u{e9}\\n\", \"c\": {\"z\": 0, \"y\": \"x\"}}"
                .as_bytes(),
        )
        .unwrap();
        assert_eq!(
            canonical(&v).unwrap(),
            "{\"a\":\"\u{e9}\\n\",\"b\":[1,true,null],\"c\":{\"y\":\"x\",\"z\":0}}"
        );
        assert_eq!(
            digest(&v).unwrap().as_str(),
            "sha256:c25cf6d8af15edaf7f9ef3f86172767d36bfdfb090c8eebac9abecca9737980b"
        );
        assert_eq!(digest(&v).unwrap().hex().len(), 64);
    }

    #[test]
    fn duplicate_keys_at_every_depth_and_after_escape_decoding() {
        assert_eq!(err(r#"{"a":1,"a":1}"#), "DUPLICATE_KEY");
        assert_eq!(err(r#"{"a":1,"\u0061":2}"#), "DUPLICATE_KEY");
        assert_eq!(err(r#"{"x":{"b":1,"b":2}}"#), "DUPLICATE_KEY");
        assert_eq!(err(r#"[{"k":1},{"k":2,"k":3}]"#), "DUPLICATE_KEY");
        assert!(parse(br#"[{"k":1},{"k":2}]"#).is_ok());
        // Detected when the repeated name is read, before its value.
        assert_eq!(err(r#"{"a":1,"a":1.5}"#), "DUPLICATE_KEY");
    }

    #[test]
    fn number_spellings_and_range() {
        for (t, code) in [
            ("1.0", "NUMBER_NOT_INTEGER"),
            ("1e3", "NUMBER_NOT_INTEGER"),
            ("1E+3", "NUMBER_NOT_INTEGER"),
            ("-0.5", "NUMBER_NOT_INTEGER"),
            ("99999999999999999999.5", "NUMBER_NOT_INTEGER"),
            ("-0", "NUMBER_NEGATIVE_ZERO"),
            ("9007199254740992", "NUMBER_OUT_OF_RANGE"),
            ("-9007199254740992", "NUMBER_OUT_OF_RANGE"),
            ("18446744073709551616", "NUMBER_OUT_OF_RANGE"),
            ("01", "INVALID_JSON"),
            ("-01", "INVALID_JSON"),
            ("+1", "INVALID_JSON"),
            ("1.", "INVALID_JSON"),
            ("1e", "INVALID_JSON"),
            ("-", "INVALID_JSON"),
            (".5", "INVALID_JSON"),
        ] {
            assert_eq!(err(t), code, "{t}");
        }
        assert_eq!(
            parse(b"9007199254740991").unwrap(),
            Value::Int(MAX_SAFE_INTEGER)
        );
        assert_eq!(
            parse(b"-9007199254740991").unwrap(),
            Value::Int(-MAX_SAFE_INTEGER)
        );
        assert_eq!(parse(b"0").unwrap(), Value::Int(0));
    }

    #[test]
    fn literals_strings_and_trailing_content() {
        for t in [
            "NaN",
            "Infinity",
            "[-Infinity]",
            "{\"a\":NaN}",
            "[1,]",
            "{} {}",
            "tru",
            "\"\\x\"",
            "\"a\u{1}\"",
            "",
            " ",
            "{\"a\" 1}",
            "{,}",
            "\u{feff}{}",
            "[1 2]",
            "{\"a\":1,}",
            "'a'",
        ] {
            assert_eq!(err(t), "INVALID_JSON", "{t:?}");
        }
        assert_eq!(err(r#""\ud800""#), "LONE_SURROGATE");
        assert_eq!(err(r#""\udc00""#), "LONE_SURROGATE");
        assert_eq!(err(r#""\ud800\u0041""#), "LONE_SURROGATE");
        assert_eq!(err(r#""\ud800x""#), "LONE_SURROGATE");
        assert_eq!(
            parse(br#""\ud83d\ude00""#).unwrap(),
            Value::Str("\u{1F600}".into())
        );
        assert_eq!(
            parse(b" \t\r\n{ \"a\" : [ 1 , 2 ] } \n").unwrap(),
            obj([("a", Value::Arr(vec![Value::Int(1), Value::Int(2)]))])
        );
        assert_eq!(
            parse(b"\"\xff\"").unwrap_err(),
            JsonError::InvalidJson,
            "invalid UTF-8 inside a string"
        );
    }

    #[test]
    fn depth_is_traversal_depth() {
        assert!(
            parse(nest(65, "").as_bytes()).is_ok(),
            "65 empty arrays: deepest value at 64"
        );
        assert_eq!(err(&nest(66, "")), "DEPTH_EXCEEDED");
        assert!(parse(nest(64, "0").as_bytes()).is_ok());
        assert_eq!(err(&nest(65, "0")), "DEPTH_EXCEEDED");
        assert!(parse(nest(64, "{}").as_bytes()).is_ok());
        assert_eq!(
            err(&nest(64, "{\"k\":0}")),
            "DEPTH_EXCEEDED",
            "member name at depth 65"
        );
        assert!(parse(nest(63, "{\"k\":0}").as_bytes()).is_ok());
        assert_eq!(
            err(&nest(65, "x")),
            "DEPTH_EXCEEDED",
            "depth is checked before the token"
        );
    }

    #[test]
    fn first_problem_left_to_right() {
        assert_eq!(err(r#"[1.5,{"a":1,"a":2}]"#), "NUMBER_NOT_INTEGER");
        assert_eq!(err(r#"[{"a":1,"a":2},1.5]"#), "DUPLICATE_KEY");
        assert_eq!(err(r#"["\ud800",NaN]"#), "LONE_SURROGATE");
        assert_eq!(err(r#"[-0,1e5]"#), "NUMBER_NEGATIVE_ZERO");
    }

    #[test]
    fn canonical_escaping_and_order() {
        assert_eq!(
            canon(r#"{"b":"\u00e9","a":"\/","\u00e9":0,"c":"\u0001\t\b\f\r\"\\"}"#),
            "{\"a\":\"/\",\"b\":\"\u{e9}\",\"c\":\"\\u0001\\t\\b\\f\\r\\\"\\\\\",\"\u{e9}\":0}"
        );
        assert_eq!(
            canon("\"\u{2028}\u{7f}\""),
            "\"\u{2028}\u{7f}\"",
            "only control characters below U+0020 are escaped"
        );
        assert_eq!(
            canon("{\"\u{1F600}\":1,\"\u{FFFF}\":2}"),
            "{\"\u{FFFF}\":2,\"\u{1F600}\":1}",
            "code point order, not UTF-16 order"
        );
    }

    #[test]
    fn canonical_rejects_values_outside_the_domain() {
        assert_eq!(
            canonical(&Value::Int(MAX_SAFE_INTEGER + 1)),
            Err(CanonicalError::NumberOutOfRange)
        );
        assert_eq!(
            canonical(&Value::Int(i64::MIN)),
            Err(CanonicalError::NumberOutOfRange)
        );
        let mut deep = Value::Int(0);
        for _ in 0..65 {
            deep = Value::Arr(vec![deep]);
        }
        assert_eq!(canonical(&deep), Err(CanonicalError::DepthExceeded));
        let mut member = obj([("k", Value::Int(0))]);
        for _ in 0..64 {
            member = Value::Arr(vec![member]);
        }
        assert_eq!(canonical(&member), Err(CanonicalError::DepthExceeded));
        assert_eq!(CanonicalError::DepthExceeded.code(), "DEPTH_EXCEEDED");
    }

    #[test]
    fn object_equality_ignores_member_order() {
        let a = parse(br#"{"a":1,"b":{"x":[1,{"p":true,"q":null}],"y":"s"}}"#).unwrap();
        let b = parse(br#"{"b":{"y":"s","x":[1,{"q":null,"p":true}]},"a":1}"#).unwrap();
        assert_eq!(a, b);
        assert_ne!(
            a,
            parse(br#"{"a":1,"b":{"x":[{"p":true,"q":null},1],"y":"s"}}"#).unwrap(),
            "arrays stay ordered"
        );
        assert_eq!(a.to_json_text(), b.to_json_text());
    }

    #[test]
    fn accessors() {
        let v = parse(br#"{"s":"x","n":3,"b":true,"a":[1],"o":{}}"#).unwrap();
        assert_eq!(v.get("s").and_then(Value::as_str), Some("x"));
        assert_eq!(v.get("n").and_then(Value::as_int), Some(3));
        assert_eq!(v.get("b").and_then(Value::as_bool), Some(true));
        assert_eq!(
            v.get("a").and_then(Value::as_arr).map(<[Value]>::len),
            Some(1)
        );
        assert!(v
            .get("o")
            .and_then(Value::as_obj)
            .is_some_and(Map::is_empty));
        assert_eq!(v.without("s").get("s"), None);
        assert_eq!(Value::Null.get("s"), None);
    }
}
