//! Time values. The kernel never reads a clock: every instant comes from input.
//! Instants are (seconds since 1970-01-01T00:00:00Z, nanoseconds).

pub type Instant = (i64, u32);

fn days_from_civil(y: i64, m: i64, d: i64) -> i64 {
    let y = if m <= 2 { y - 1 } else { y };
    let era = if y >= 0 { y } else { y - 399 } / 400;
    let yoe = y - era * 400;
    let mp = (m + 9) % 12;
    let doy = (153 * mp + 2) / 5 + d - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146_097 + doe - 719_468
}

fn civil_from_days(z: i64) -> (i64, i64, i64) {
    let z = z + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    (if m <= 2 { y + 1 } else { y }, m, d)
}

fn days_in_month(y: i64, m: i64) -> i64 {
    match m {
        1 | 3 | 5 | 7 | 8 | 10 | 12 => 31,
        4 | 6 | 9 | 11 => 30,
        2 if (y % 4 == 0 && y % 100 != 0) || y % 400 == 0 => 29,
        2 => 28,
        _ => 0,
    }
}

fn digits(b: &[u8], at: usize, n: usize) -> Option<i64> {
    let part = b.get(at..at + n)?;
    let mut v = 0i64;
    for &c in part {
        if !c.is_ascii_digit() {
            return None;
        }
        v = v * 10 + i64::from(c - b'0');
    }
    Some(v)
}

fn date_time_prefix(b: &[u8]) -> Option<i64> {
    if b.len() < 19 || b[4] != b'-' || b[7] != b'-' || b[13] != b':' || b[16] != b':' {
        return None;
    }
    let (y, mo, d) = (digits(b, 0, 4)?, digits(b, 5, 2)?, digits(b, 8, 2)?);
    let (h, mi, s) = (digits(b, 11, 2)?, digits(b, 14, 2)?, digits(b, 17, 2)?);
    if !(1..=12).contains(&mo) || d < 1 || d > days_in_month(y, mo) || h > 23 || mi > 59 || s > 59 {
        return None;
    }
    Some(days_from_civil(y, mo, d) * 86_400 + h * 3600 + mi * 60 + s)
}

/// Writ timestamp (acceptance records, current state): exactly
/// `YYYY-MM-DDTHH:MM:SSZ` and a valid calendar time.
pub fn parse_strict_z(s: &str) -> Option<i64> {
    let b = s.as_bytes();
    if b.len() != 20 || b[10] != b'T' || b[19] != b'Z' {
        return None;
    }
    date_time_prefix(b)
}

/// RFC 3339 `date-time` (contract `format: date-time`): optional fraction,
/// `Z` or a numeric offset.
pub fn parse_rfc3339(s: &str) -> Option<Instant> {
    let b = s.as_bytes();
    if b.len() < 20 || !matches!(b[10], b'T' | b't') {
        return None;
    }
    let base = date_time_prefix(b)?;
    let mut i = 19;
    let mut nanos: u32 = 0;
    if b[i] == b'.' {
        i += 1;
        let start = i;
        while i < b.len() && b[i].is_ascii_digit() {
            if i - start < 9 {
                nanos = nanos * 10 + u32::from(b[i] - b'0');
            }
            i += 1;
        }
        let n = i - start;
        if n == 0 {
            return None;
        }
        for _ in n..9 {
            nanos *= 10;
        }
    }
    let rest = &b[i..];
    let offset = match rest {
        [b'Z'] | [b'z'] => 0,
        [sign @ (b'+' | b'-'), _, _, b':', _, _] => {
            let (oh, om) = (digits(rest, 1, 2)?, digits(rest, 4, 2)?);
            if oh > 23 || om > 59 {
                return None;
            }
            let o = oh * 3600 + om * 60;
            if *sign == b'+' { o } else { -o }
        }
        _ => return None,
    };
    Some((base - offset, nanos))
}

pub fn add_seconds(t: Instant, secs: i64) -> Instant {
    (t.0 + secs, t.1)
}

/// `YYYY-MM-DDTHH:MM:SS[.fraction]Z`.
pub fn format(t: Instant) -> String {
    let days = t.0.div_euclid(86_400);
    let rem = t.0.rem_euclid(86_400);
    let (y, m, d) = civil_from_days(days);
    let mut out = format!("{:04}-{:02}-{:02}T{:02}:{:02}:{:02}", y, m, d, rem / 3600, (rem / 60) % 60, rem % 60);
    if t.1 != 0 {
        let frac = format!("{:09}", t.1);
        out.push('.');
        out.push_str(frac.trim_end_matches('0'));
    }
    out.push('Z');
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn strict_and_rfc3339() {
        assert!(parse_strict_z("2026-11-07T11:00:00Z").is_some());
        assert!(parse_strict_z("2026-02-30T11:00:00Z").is_none());
        assert!(parse_strict_z("2026-11-07T11:00:00+00:00").is_none());
        let a = parse_rfc3339("2026-11-07T12:00:00+01:00").unwrap();
        assert_eq!(a, (parse_strict_z("2026-11-07T11:00:00Z").unwrap(), 0));
        assert_eq!(format(parse_rfc3339("2026-11-05T12:00:30.5Z").unwrap()), "2026-11-05T12:00:30.5Z");
        assert_eq!(format((0, 0)), "1970-01-01T00:00:00Z");
    }
}
