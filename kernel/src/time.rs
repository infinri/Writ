//! Timestamps. The kernel never reads a clock: every instant is an input.
//!
//! Two textual forms are accepted: the strict Writ form
//! `YYYY-MM-DDTHH:MM:SSZ` (harness `now`, acceptance records and broker state,
//! D-022) and RFC 3339 `date-time` (contract `format: date-time`, for example
//! `deadline_at`). Both require a valid proleptic Gregorian calendar time with
//! years 0000 to 9999, hours at most 23 and minutes and seconds at most 59 (no
//! leap second).

use std::fmt;

/// An instant: seconds since 1970-01-01T00:00:00Z plus nanoseconds. Ordering
/// is chronological.
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct Timestamp {
    secs: i64,
    nanos: u32,
}

const SECONDS_PER_DAY: i64 = 86_400;

fn days_from_civil(year: i64, month: i64, day: i64) -> i64 {
    let y = if month <= 2 { year - 1 } else { year };
    let era = (if y >= 0 { y } else { y - 399 }) / 400;
    let yoe = y - era * 400;
    let mp = (month + 9) % 12;
    let doy = (153 * mp + 2) / 5 + day - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146_097 + doe - 719_468
}

fn civil_from_days(days: i64) -> (i64, i64, i64) {
    let z = days + 719_468;
    let era = (if z >= 0 { z } else { z - 146_096 }) / 146_097;
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    (if m <= 2 { y + 1 } else { y }, m, d)
}

fn is_leap(year: i64) -> bool {
    (year % 4 == 0 && year % 100 != 0) || year % 400 == 0
}

fn days_in_month(year: i64, month: i64) -> i64 {
    match month {
        1 | 3 | 5 | 7 | 8 | 10 | 12 => 31,
        4 | 6 | 9 | 11 => 30,
        2 if is_leap(year) => 29,
        2 => 28,
        _ => 0,
    }
}

fn number(b: &[u8], at: usize, len: usize) -> Option<i64> {
    let part = b.get(at..at + len)?;
    part.iter().try_fold(0i64, |n, c| {
        c.is_ascii_digit().then(|| n * 10 + i64::from(c - b'0'))
    })
}

/// Seconds of `YYYY-MM-DD?HH:MM:SS` (the separator at index 10 is checked by the caller).
fn date_time_prefix(b: &[u8]) -> Option<i64> {
    if b.get(4) != Some(&b'-')
        || b.get(7) != Some(&b'-')
        || b.get(13) != Some(&b':')
        || b.get(16) != Some(&b':')
    {
        return None;
    }
    let (year, month, day) = (number(b, 0, 4)?, number(b, 5, 2)?, number(b, 8, 2)?);
    let (hour, minute, second) = (number(b, 11, 2)?, number(b, 14, 2)?, number(b, 17, 2)?);
    if !(1..=12).contains(&month)
        || day < 1
        || day > days_in_month(year, month)
        || hour > 23
        || minute > 59
        || second > 59
    {
        return None;
    }
    Some(days_from_civil(year, month, day) * SECONDS_PER_DAY + hour * 3600 + minute * 60 + second)
}

impl Timestamp {
    /// The strict Writ form: exactly `YYYY-MM-DDTHH:MM:SSZ` (uppercase `T`
    /// and `Z`, no fraction, no offset) and a valid calendar time.
    pub fn parse_strict(s: &str) -> Option<Timestamp> {
        let b = s.as_bytes();
        if b.len() != 20 || b.get(10) != Some(&b'T') || b.get(19) != Some(&b'Z') {
            return None;
        }
        date_time_prefix(b).map(|secs| Timestamp { secs, nanos: 0 })
    }

    /// RFC 3339 `date-time`: optional fraction (digits beyond nanoseconds are
    /// truncated), `Z`, `z` or a `+HH:MM` / `-HH:MM` offset; `T` or `t`.
    pub fn parse_rfc3339(s: &str) -> Option<Timestamp> {
        let b = s.as_bytes();
        if !matches!(b.get(10), Some(b'T' | b't')) {
            return None;
        }
        let base = date_time_prefix(b)?;
        let mut i = 19;
        let mut nanos: u32 = 0;
        if b.get(i) == Some(&b'.') {
            i += 1;
            let start = i;
            while b.get(i).is_some_and(u8::is_ascii_digit) {
                if i - start < 9 {
                    nanos = nanos * 10 + u32::from(b.get(i).map_or(0, |c| c - b'0'));
                }
                i += 1;
            }
            let count = i - start;
            if count == 0 {
                return None;
            }
            for _ in count..9 {
                nanos *= 10;
            }
        }
        let offset = match b.get(i..)? {
            [b'Z' | b'z'] => 0,
            rest @ [sign @ (b'+' | b'-'), _, _, b':', _, _] => {
                let (hours, minutes) = (number(rest, 1, 2)?, number(rest, 4, 2)?);
                if hours > 23 || minutes > 59 {
                    return None;
                }
                let seconds = hours * 3600 + minutes * 60;
                if *sign == b'+' {
                    seconds
                } else {
                    -seconds
                }
            }
            _ => return None,
        };
        Some(Timestamp {
            secs: base - offset,
            nanos,
        })
    }

    /// The instant `seconds` later.
    pub fn plus_seconds(self, seconds: i64) -> Timestamp {
        Timestamp {
            secs: self.secs.saturating_add(seconds),
            nanos: self.nanos,
        }
    }

    /// Seconds since 1970-01-01T00:00:00Z.
    pub fn unix_seconds(self) -> i64 {
        self.secs
    }

    /// Nanoseconds within the second.
    pub fn subsec_nanos(self) -> u32 {
        self.nanos
    }
}

/// `YYYY-MM-DDTHH:MM:SS[.fraction]Z`, UTC, the fraction without trailing zeros.
impl fmt::Display for Timestamp {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let days = self.secs.div_euclid(SECONDS_PER_DAY);
        let rem = self.secs.rem_euclid(SECONDS_PER_DAY);
        let (y, m, d) = civil_from_days(days);
        write!(
            f,
            "{:04}-{:02}-{:02}T{:02}:{:02}:{:02}",
            y,
            m,
            d,
            rem / 3600,
            (rem / 60) % 60,
            rem % 60
        )?;
        if self.nanos != 0 {
            let fraction = format!("{:09}", self.nanos);
            write!(f, ".{}", fraction.trim_end_matches('0'))?;
        }
        f.write_str("Z")
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::*;

    #[test]
    fn strict_form() {
        let t = Timestamp::parse_strict("2026-11-07T11:00:00Z").unwrap();
        assert_eq!(t.to_string(), "2026-11-07T11:00:00Z");
        assert_eq!(
            Timestamp::parse_strict("1970-01-01T00:00:00Z")
                .unwrap()
                .unix_seconds(),
            0
        );
        assert!(Timestamp::parse_strict("2024-02-29T23:59:59Z").is_some());
        assert!(Timestamp::parse_strict("0000-01-01T00:00:00Z").is_some());
        assert!(Timestamp::parse_strict("9999-12-31T23:59:59Z").is_some());
        for bad in [
            "",
            "2026-02-30T00:00:00Z",
            "2025-02-29T00:00:00Z",
            "1900-02-29T00:00:00Z",
            "2026-11-07T23:59:60Z",
            "2026-11-07T24:00:00Z",
            "0000-00-00T00:00:00Z",
            "2026-11-07T11:00:00+00:00",
            "2026-11-07T11:00:00.1Z",
            "2026-11-07t11:00:00z",
            "2026-11-07T11:00:00z",
            "2026-11-07",
            "2026-11-07 11:00:00Z",
            "+026-11-07T11:00:00Z",
            "2026-11-07T11:00:00ZZ",
        ] {
            assert!(Timestamp::parse_strict(bad).is_none(), "{bad}");
        }
    }

    #[test]
    fn rfc3339_form() {
        let a = Timestamp::parse_rfc3339("2026-11-07T12:00:00+01:00").unwrap();
        assert_eq!(a, Timestamp::parse_strict("2026-11-07T11:00:00Z").unwrap());
        assert_eq!(
            Timestamp::parse_rfc3339("2026-11-05T12:00:30.5Z")
                .unwrap()
                .to_string(),
            "2026-11-05T12:00:30.5Z"
        );
        assert_eq!(
            Timestamp::parse_rfc3339("2026-11-05t12:00:30.123456789123z")
                .unwrap()
                .subsec_nanos(),
            123_456_789
        );
        assert!(Timestamp::parse_rfc3339("2026-11-05T12:00:30.Z").is_none());
        assert!(Timestamp::parse_rfc3339("2026-11-05T12:00:30+24:00").is_none());
        assert!(Timestamp::parse_rfc3339("2026-11-05T12:00:30+0100").is_none());
        assert!(Timestamp::parse_rfc3339("2026-11-05T12:00:30").is_none());
        assert!(Timestamp::parse_rfc3339("2026-11-05X12:00:30Z").is_none());
    }

    #[test]
    fn ordering_and_arithmetic() {
        let t = Timestamp::parse_strict("2026-12-31T23:59:00Z").unwrap();
        assert_eq!(t.plus_seconds(300).to_string(), "2027-01-01T00:04:00Z");
        assert!(t < t.plus_seconds(1));
        let f = Timestamp::parse_rfc3339("2026-12-31T23:59:00.5Z").unwrap();
        assert!(t < f && f < t.plus_seconds(1));
        assert_eq!(
            Timestamp::parse_strict("0000-03-01T00:00:00Z")
                .unwrap()
                .to_string(),
            "0000-03-01T00:00:00Z"
        );
    }
}
