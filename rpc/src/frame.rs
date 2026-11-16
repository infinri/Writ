//! Frames (J-2): `u32` big-endian payload length, one frame-type byte, the payload.

use std::io::{self, Read, Write};
use std::time::{Duration, Instant};

use writ_kernel::json::{self, Value};

/// Largest control payload accepted on any endpoint (64 KiB).
pub const MAX_CONTROL_BYTES: u32 = 64 * 1024;
/// Largest ingest data payload accepted on any endpoint (1 MiB).
pub const MAX_DATA_BYTES: u32 = 1024 * 1024;
/// Header length: 4 length bytes and 1 type byte.
pub const HEADER_BYTES: usize = 5;

/// The frame type byte.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum FrameType {
    /// `0x01`: one strict JSON object.
    Control,
    /// `0x02`: ingest data bytes (`snapshot.ingest.file`, J-4).
    Data,
}

impl FrameType {
    /// The wire byte.
    pub fn byte(self) -> u8 {
        match self {
            FrameType::Control => 0x01,
            FrameType::Data => 0x02,
        }
    }

    /// The frame type of `b`, if any.
    pub fn of(b: u8) -> Option<FrameType> {
        match b {
            0x01 => Some(FrameType::Control),
            0x02 => Some(FrameType::Data),
            _ => None,
        }
    }
}

/// The frame types and maximum lengths an endpoint accepts.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct FrameLimits {
    control: u32,
    data: Option<u32>,
}

impl FrameLimits {
    /// Limits with at most `control` control bytes and, when `data` is `Some`, data frames
    /// of at most that many bytes. `None` when a bound is zero or above the J-2 maximum.
    pub fn new(control: u32, data: Option<u32>) -> Option<FrameLimits> {
        let control_ok = control > 0 && control <= MAX_CONTROL_BYTES;
        let data_ok = data.is_none_or(|d| d > 0 && d <= MAX_DATA_BYTES);
        (control_ok && data_ok).then_some(FrameLimits { control, data })
    }

    /// `host.sock`: control and data frames at the J-2 maxima.
    pub fn host() -> FrameLimits {
        FrameLimits {
            control: MAX_CONTROL_BYTES,
            data: Some(MAX_DATA_BYTES),
        }
    }

    /// `admin.sock`: control frames only.
    pub fn admin() -> FrameLimits {
        FrameLimits {
            control: MAX_CONTROL_BYTES,
            data: None,
        }
    }

    /// The maximum payload of `kind` here; `None` when the type is not accepted.
    pub fn max(&self, kind: FrameType) -> Option<u32> {
        match kind {
            FrameType::Control => Some(self.control),
            FrameType::Data => self.data,
        }
    }
}

/// A received frame.
#[derive(Clone, Debug, PartialEq)]
pub enum Frame {
    /// A strict JSON object.
    Control(Value),
    /// Data bytes.
    Data(Vec<u8>),
}

/// Why a frame was refused. Every variant closes the connection with the generic error,
/// except [`FrameError::Closed`] (the peer closed between frames).
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum FrameError {
    /// End of stream before the first header byte: an orderly close.
    Closed,
    /// End of stream inside a header or a payload.
    Truncated,
    /// A type byte that is not a frame type.
    UnknownType(u8),
    /// A frame type this endpoint does not accept.
    TypeNotAccepted(FrameType),
    /// A length above this endpoint's maximum for the type (checked before allocation).
    TooLarge {
        /// The frame type.
        kind: FrameType,
        /// The announced length.
        len: u32,
    },
    /// A control payload that is not UTF-8.
    Utf8,
    /// A control payload that is not one strict JSON object.
    Json,
    /// The read failed (timeouts included) or the transport refused the bytes (ancillary
    /// data); the kind only.
    Io(io::ErrorKind),
}

/// Read exactly `buf.len()` bytes. `Ok(false)` when the stream ends before the first byte
/// and `allow_clean_end`; [`FrameError::Truncated`] when it ends later.
fn fill<R: Read + ?Sized>(
    r: &mut R,
    buf: &mut [u8],
    allow_clean_end: bool,
) -> Result<bool, FrameError> {
    let mut got = 0;
    while got < buf.len() {
        match r.read(&mut buf[got..]) {
            Ok(0) if got == 0 && allow_clean_end => return Ok(false),
            Ok(0) => return Err(FrameError::Truncated),
            Ok(n) => got += n,
            Err(e) if e.kind() == io::ErrorKind::Interrupted => {}
            Err(e) => return Err(FrameError::Io(e.kind())),
        }
    }
    Ok(true)
}

/// Read one frame under `limits`: header, type and length checks, then exactly `len`
/// payload bytes into a buffer of that size, then (control) UTF-8 and strict JSON object.
pub fn read_frame<R: Read + ?Sized>(r: &mut R, limits: &FrameLimits) -> Result<Frame, FrameError> {
    let mut header = [0u8; HEADER_BYTES];
    if !fill(r, &mut header, true)? {
        return Err(FrameError::Closed);
    }
    frame_after_header(r, header, limits)
}

/// A reader whose blocking-read timeout can be set before each read (a socket with
/// `SO_RCVTIMEO`), for [`read_frame_timed`].
pub trait TimedRead: Read {
    /// Make the next reads time out after `timeout` (never zero).
    fn set_read_timeout(&mut self, timeout: Duration) -> io::Result<()>;
}

/// Reads under an absolute deadline: each read gets the time left as its timeout.
struct Bounded<'r, R: TimedRead + ?Sized> {
    inner: &'r mut R,
    deadline: Instant,
}

impl<R: TimedRead + ?Sized> Read for Bounded<'_, R> {
    fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
        let left = self.deadline.saturating_duration_since(Instant::now());
        if left.is_zero() {
            return Err(io::Error::from(io::ErrorKind::TimedOut));
        }
        self.inner.set_read_timeout(left)?;
        self.inner.read(buf)
    }
}

/// [`read_frame`] with the J-2 deadlines (M5a-13 L10): at most `idle` waiting for the first
/// byte of a frame, then the **whole** frame (rest of the header and the payload) within
/// `request` of that byte, whatever the per-read progress. A peer that drips one byte at a
/// time cannot hold a connection (and its cap slot) beyond `idle + request` per frame. A
/// timeout is [`FrameError::Io`] (`TimedOut` or `WouldBlock`) and closes the connection.
pub fn read_frame_timed<R: TimedRead + ?Sized>(
    r: &mut R,
    limits: &FrameLimits,
    idle: Duration,
    request: Duration,
) -> Result<Frame, FrameError> {
    if idle.is_zero() || request.is_zero() {
        return Err(FrameError::Io(io::ErrorKind::InvalidInput));
    }
    r.set_read_timeout(idle)
        .map_err(|e| FrameError::Io(e.kind()))?;
    let mut header = [0u8; HEADER_BYTES];
    if !fill(r, &mut header[..1], true)? {
        return Err(FrameError::Closed);
    }
    let mut bounded = Bounded {
        inner: r,
        deadline: Instant::now() + request,
    };
    fill(&mut bounded, &mut header[1..], false)?;
    frame_after_header(&mut bounded, header, limits)
}

fn frame_after_header<R: Read + ?Sized>(
    r: &mut R,
    header: [u8; HEADER_BYTES],
    limits: &FrameLimits,
) -> Result<Frame, FrameError> {
    let len = u32::from_be_bytes([header[0], header[1], header[2], header[3]]);
    let kind = FrameType::of(header[4]).ok_or(FrameError::UnknownType(header[4]))?;
    let max = limits.max(kind).ok_or(FrameError::TypeNotAccepted(kind))?;
    if len > max {
        return Err(FrameError::TooLarge { kind, len });
    }
    // `len <= max <= MAX_DATA_BYTES`: the allocation is bounded by the endpoint limit.
    let mut payload =
        vec![0u8; usize::try_from(len).map_err(|_| FrameError::TooLarge { kind, len })?];
    fill(r, &mut payload, false)?;
    match kind {
        FrameType::Data => Ok(Frame::Data(payload)),
        FrameType::Control => {
            if std::str::from_utf8(&payload).is_err() {
                return Err(FrameError::Utf8);
            }
            match json::parse(&payload) {
                Ok(v @ Value::Obj(_)) => Ok(Frame::Control(v)),
                _ => Err(FrameError::Json),
            }
        }
    }
}

/// Encode one frame. `None` when the payload does not fit the J-2 maximum of its type.
pub fn encode(kind: FrameType, payload: &[u8]) -> Option<Vec<u8>> {
    let max = match kind {
        FrameType::Control => MAX_CONTROL_BYTES,
        FrameType::Data => MAX_DATA_BYTES,
    };
    let len = u32::try_from(payload.len()).ok().filter(|l| *l <= max)?;
    let mut out = Vec::with_capacity(HEADER_BYTES + payload.len());
    out.extend_from_slice(&len.to_be_bytes());
    out.push(kind.byte());
    out.extend_from_slice(payload);
    Some(out)
}

/// Write one frame (one `write_all`). A payload over the J-2 maximum is `InvalidInput`.
pub fn write_frame<W: Write + ?Sized>(
    w: &mut W,
    kind: FrameType,
    payload: &[u8],
) -> io::Result<()> {
    let bytes =
        encode(kind, payload).ok_or_else(|| io::Error::from(io::ErrorKind::InvalidInput))?;
    w.write_all(&bytes)?;
    w.flush()
}

/// Write `value` as a control frame in canonical JSON.
pub fn write_control<W: Write + ?Sized>(w: &mut W, value: &Value) -> io::Result<()> {
    let text = json::canonical(value).map_err(|_| io::Error::from(io::ErrorKind::InvalidInput))?;
    write_frame(w, FrameType::Control, text.as_bytes())
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
mod tests {
    use super::*;
    use std::io::Cursor;

    /// A reader that counts how many bytes were consumed.
    struct Counting<'a> {
        inner: Cursor<&'a [u8]>,
        read: usize,
    }

    impl Read for Counting<'_> {
        fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
            let n = self.inner.read(buf)?;
            self.read += n;
            Ok(n)
        }
    }

    fn frame(len: u32, kind: u8, payload: &[u8]) -> Vec<u8> {
        let mut v = len.to_be_bytes().to_vec();
        v.push(kind);
        v.extend_from_slice(payload);
        v
    }

    fn read(bytes: &[u8], limits: FrameLimits) -> Result<Frame, FrameError> {
        read_frame(&mut Cursor::new(bytes), &limits)
    }

    #[test]
    fn a_control_frame_round_trips_as_a_strict_object() {
        let v = json::obj([("op", json::s("status.receipt"))]);
        let mut out = Vec::new();
        write_control(&mut out, &v).unwrap();
        assert_eq!(&out[..5], &[0, 0, 0, 23, 0x01]);
        assert_eq!(read(&out, FrameLimits::host()), Ok(Frame::Control(v)));
    }

    #[test]
    fn oversized_lengths_are_refused_before_any_payload_is_read_or_allocated() {
        for (len, kind, limits) in [
            (MAX_CONTROL_BYTES + 1, 0x01, FrameLimits::host()),
            (u32::MAX, 0x01, FrameLimits::host()),
            (MAX_DATA_BYTES + 1, 0x02, FrameLimits::host()),
            (u32::MAX, 0x02, FrameLimits::host()),
            (101, 0x01, FrameLimits::new(100, None).unwrap()),
        ] {
            // The announced payload is absent: only the header may be consumed.
            let bytes = frame(len, kind, b"");
            let mut r = Counting {
                inner: Cursor::new(&bytes),
                read: 0,
            };
            let got = read_frame(&mut r, &limits);
            assert!(
                matches!(got, Err(FrameError::TooLarge { len: l, .. }) if l == len),
                "{got:?}"
            );
            assert_eq!(r.read, HEADER_BYTES);
        }
        // At the maximum the frame is accepted.
        let payload = vec![b' '; (MAX_CONTROL_BYTES - 2) as usize];
        let mut body = b"{".to_vec();
        body.extend_from_slice(&payload);
        body.push(b'}');
        assert!(matches!(
            read(&frame(MAX_CONTROL_BYTES, 0x01, &body), FrameLimits::host()),
            Ok(Frame::Control(_))
        ));
    }

    #[test]
    fn unknown_and_unaccepted_types_are_refused() {
        for t in [0x00, 0x03, 0x7f, 0xff] {
            assert_eq!(
                read(&frame(2, t, b"{}"), FrameLimits::host()),
                Err(FrameError::UnknownType(t))
            );
        }
        assert_eq!(
            read(&frame(1, 0x02, b"x"), FrameLimits::admin()),
            Err(FrameError::TypeNotAccepted(FrameType::Data))
        );
        assert_eq!(
            read(&frame(1, 0x02, b"x"), FrameLimits::host()),
            Ok(Frame::Data(b"x".to_vec()))
        );
    }

    #[test]
    fn truncation_and_orderly_close_are_distinguished() {
        assert_eq!(read(b"", FrameLimits::host()), Err(FrameError::Closed));
        assert_eq!(
            read(&[0, 0, 0], FrameLimits::host()),
            Err(FrameError::Truncated)
        );
        assert_eq!(
            read(&frame(5, 0x01, b"{}"), FrameLimits::host()),
            Err(FrameError::Truncated)
        );
    }

    #[test]
    fn control_payloads_must_be_one_strict_utf8_json_object() {
        for bad in [
            &b""[..],
            b"[]",
            b"\"x\"",
            b"1",
            b"{\"a\":1,\"a\":2}",
            b"{\"a\":1} {}",
            b"{'a':1}",
            b"{\"a\":01}",
            b"\xef\xbb\xbf{}",
        ] {
            let got = read(&frame(bad.len() as u32, 0x01, bad), FrameLimits::host());
            assert!(
                matches!(got, Err(FrameError::Json | FrameError::Utf8)),
                "{bad:?}: {got:?}"
            );
        }
        assert_eq!(
            read(&frame(4, 0x01, b"{\xff}x"), FrameLimits::host()),
            Err(FrameError::Utf8)
        );
    }

    #[test]
    fn io_errors_are_reported_by_kind() {
        struct Failing;
        impl Read for Failing {
            fn read(&mut self, _: &mut [u8]) -> io::Result<usize> {
                Err(io::Error::from(io::ErrorKind::WouldBlock))
            }
        }
        assert_eq!(
            read_frame(&mut Failing, &FrameLimits::host()),
            Err(FrameError::Io(io::ErrorKind::WouldBlock))
        );
    }

    #[test]
    fn limits_are_bounded_by_the_j2_maxima() {
        assert!(FrameLimits::new(0, None).is_none());
        assert!(FrameLimits::new(MAX_CONTROL_BYTES + 1, None).is_none());
        assert!(FrameLimits::new(1, Some(0)).is_none());
        assert!(FrameLimits::new(1, Some(MAX_DATA_BYTES + 1)).is_none());
        assert!(encode(FrameType::Control, &vec![0; MAX_CONTROL_BYTES as usize + 1]).is_none());
        assert!(encode(FrameType::Data, &vec![0; MAX_DATA_BYTES as usize]).is_some());
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
mod deadline_tests {
    use super::*;
    use std::os::unix::net::UnixStream;
    use std::thread;

    struct Timed(UnixStream);

    impl Read for Timed {
        fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
            self.0.read(buf)
        }
    }

    impl TimedRead for Timed {
        fn set_read_timeout(&mut self, timeout: Duration) -> io::Result<()> {
            self.0.set_read_timeout(Some(timeout))
        }
    }

    /// Send `frame` one byte every `gap`.
    fn drip(frame: Vec<u8>, gap: Duration) -> (Timed, thread::JoinHandle<()>) {
        let (a, mut b) = UnixStream::pair().unwrap();
        let t = thread::spawn(move || {
            for byte in frame {
                if b.write_all(&[byte]).is_err() {
                    return;
                }
                thread::sleep(gap);
            }
            thread::sleep(Duration::from_millis(500));
        });
        (Timed(a), t)
    }

    /// M5a-13 L10: a slow-drip peer whose every read arrives within the idle timeout is
    /// still cut off at the per-request deadline (the plain reader would wait it out).
    #[test]
    fn a_slow_drip_frame_is_cut_off_at_the_request_deadline() {
        let frame = encode(FrameType::Control, br#"{"op":"readiness"}"#).unwrap();
        let gap = Duration::from_millis(40);
        let (mut r, t) = drip(frame.clone(), gap);
        let started = Instant::now();
        let got = read_frame_timed(
            &mut r,
            &FrameLimits::admin(),
            Duration::from_secs(5),
            Duration::from_millis(200),
        );
        assert!(
            matches!(
                got,
                Err(FrameError::Io(
                    io::ErrorKind::TimedOut | io::ErrorKind::WouldBlock
                ))
            ),
            "{got:?}"
        );
        assert!(started.elapsed() < Duration::from_millis(600));
        drop(r);
        t.join().unwrap();
        // The same drip read without the request deadline completes (the old behaviour:
        // every single read is within the idle timeout).
        let (mut r, t) = drip(frame, gap);
        r.0.set_read_timeout(Some(Duration::from_secs(5))).unwrap();
        assert!(matches!(
            read_frame(&mut r, &FrameLimits::admin()),
            Ok(Frame::Control(_))
        ));
        t.join().unwrap();
    }

    #[test]
    fn a_prompt_frame_and_an_idle_close_behave_as_before() {
        let frame = encode(FrameType::Control, br#"{"op":"readiness"}"#).unwrap();
        let (mut r, t) = drip(frame, Duration::ZERO);
        assert!(matches!(
            read_frame_timed(
                &mut r,
                &FrameLimits::admin(),
                Duration::from_secs(1),
                Duration::from_secs(1)
            ),
            Ok(Frame::Control(_))
        ));
        t.join().unwrap();
        // Nothing arrives within the idle timeout.
        let (a, _b) = UnixStream::pair().unwrap();
        let mut r = Timed(a);
        assert!(matches!(
            read_frame_timed(
                &mut r,
                &FrameLimits::admin(),
                Duration::from_millis(100),
                Duration::from_secs(1)
            ),
            Err(FrameError::Io(_))
        ));
        // A clean close between frames is still Closed.
        let (a, b) = UnixStream::pair().unwrap();
        drop(b);
        let mut r = Timed(a);
        assert_eq!(
            read_frame_timed(
                &mut r,
                &FrameLimits::admin(),
                Duration::from_secs(1),
                Duration::from_secs(1)
            ),
            Err(FrameError::Closed)
        );
    }
}
