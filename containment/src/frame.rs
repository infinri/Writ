//! J-2 framing on `launch.sock`: a 4-byte big-endian payload length, a 1-byte frame type
//! (`0x01` control, `0x02` data) and the payload. The length is checked against the maximum
//! for the frame type **before** any payload byte is buffered (control 64 KiB, data 1 MiB).
//! The launcher accepts only control frames from `writ`; a data frame closes the connection
//! (J-11.2, L7).

/// Control frame type.
pub const CONTROL: u8 = 0x01;
/// Data frame type.
pub const DATA: u8 = 0x02;
/// Largest control payload.
pub const CONTROL_MAX: usize = 64 * 1024;
/// Largest data payload.
pub const DATA_MAX: usize = 1024 * 1024;
/// Header bytes (length and type).
pub const HEADER: usize = 5;

/// One decoded frame.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Frame {
    /// A control frame (one strict JSON document).
    Control(Vec<u8>),
    /// A data frame.
    Data(Vec<u8>),
}

/// Why a byte stream is not acceptable (the connection is closed).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum FrameError {
    /// A frame type other than control or data.
    UnknownType(u8),
    /// A length above the maximum for the frame type.
    TooLong,
    /// A data frame where only control frames are accepted.
    DataRefused,
}

/// The encoded frame, `None` when the payload exceeds the maximum for `kind` or `kind` is
/// unknown.
pub fn encode(kind: u8, payload: &[u8]) -> Option<Vec<u8>> {
    let max = match kind {
        CONTROL => CONTROL_MAX,
        DATA => DATA_MAX,
        _ => return None,
    };
    if payload.len() > max {
        return None;
    }
    let len = u32::try_from(payload.len()).ok()?;
    let mut out = Vec::with_capacity(HEADER + payload.len());
    out.extend_from_slice(&len.to_be_bytes());
    out.push(kind);
    out.extend_from_slice(payload);
    Some(out)
}

/// Incremental decoder for a non-blocking connection.
#[derive(Debug)]
pub struct Decoder {
    buf: Vec<u8>,
    accept_data: bool,
}

impl Decoder {
    /// A decoder; `accept_data` false refuses data frames (the launcher's side).
    pub fn new(accept_data: bool) -> Decoder {
        Decoder {
            buf: Vec::new(),
            accept_data,
        }
    }

    /// Bytes buffered and not yet decoded.
    pub fn pending(&self) -> usize {
        self.buf.len()
    }

    fn header(&self) -> Result<Option<(u8, usize)>, FrameError> {
        if self.buf.len() < HEADER {
            return Ok(None);
        }
        let len = u32::from_be_bytes([self.buf[0], self.buf[1], self.buf[2], self.buf[3]]);
        let len = usize::try_from(len).map_err(|_| FrameError::TooLong)?;
        let kind = self.buf[4];
        let max = match kind {
            CONTROL => CONTROL_MAX,
            DATA if self.accept_data => DATA_MAX,
            DATA => return Err(FrameError::DataRefused),
            other => return Err(FrameError::UnknownType(other)),
        };
        if len > max {
            return Err(FrameError::TooLong);
        }
        Ok(Some((kind, len)))
    }

    /// Append received bytes and return every complete frame. The header of a frame is
    /// validated as soon as it is complete, so no more than one maximum payload is ever
    /// buffered.
    pub fn feed(&mut self, bytes: &[u8]) -> Result<Vec<Frame>, FrameError> {
        let mut out = Vec::new();
        let mut rest = bytes;
        loop {
            // Take only what the current frame needs, so the header check runs before any
            // payload byte beyond the header is buffered.
            let need = match self.header()? {
                None => HEADER - self.buf.len(),
                Some((_, len)) => HEADER + len - self.buf.len(),
            };
            if need == 0 {
                if let Some((kind, len)) = self.header()? {
                    let payload = self.buf[HEADER..HEADER + len].to_vec();
                    self.buf.clear();
                    out.push(if kind == CONTROL {
                        Frame::Control(payload)
                    } else {
                        Frame::Data(payload)
                    });
                }
                continue;
            }
            if rest.is_empty() {
                return Ok(out);
            }
            let take = need.min(rest.len());
            self.buf.extend_from_slice(&rest[..take]);
            rest = &rest[take..];
        }
    }
}

/// Output stream tag of a launcher data frame.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Stream {
    /// The job's stdout.
    Stdout,
    /// The job's stderr.
    Stderr,
}

impl Stream {
    fn tag(self) -> u8 {
        match self {
            Stream::Stdout => 1,
            Stream::Stderr => 2,
        }
    }

    /// `stdout` or `stderr`.
    pub fn name(self) -> &'static str {
        match self {
            Stream::Stdout => "stdout",
            Stream::Stderr => "stderr",
        }
    }
}

/// A launcher output data payload: stream tag, `job_id` length, `job_id` (echoed in every
/// frame, J-11.3), then the bytes. `None` when `job_id` is longer than 255 bytes or the payload
/// exceeds the data maximum.
pub fn output_payload(stream: Stream, job_id: &str, bytes: &[u8]) -> Option<Vec<u8>> {
    let id = job_id.as_bytes();
    let id_len = u8::try_from(id.len()).ok()?;
    let mut p = Vec::with_capacity(2 + id.len() + bytes.len());
    p.push(stream.tag());
    p.push(id_len);
    p.extend_from_slice(id);
    p.extend_from_slice(bytes);
    if p.len() > DATA_MAX {
        return None;
    }
    Some(p)
}

/// Decode an output data payload: (stream, job_id, bytes).
pub fn parse_output(payload: &[u8]) -> Option<(Stream, &str, &[u8])> {
    let (&tag, rest) = payload.split_first()?;
    let stream = match tag {
        1 => Stream::Stdout,
        2 => Stream::Stderr,
        _ => return None,
    };
    let (&n, rest) = rest.split_first()?;
    let n = usize::from(n);
    if rest.len() < n {
        return None;
    }
    let id = std::str::from_utf8(&rest[..n]).ok()?;
    Some((stream, id, &rest[n..]))
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
mod tests {
    use super::*;

    #[test]
    fn round_trip_in_single_bytes() {
        let mut wire = encode(CONTROL, b"{\"a\":1}").unwrap();
        wire.extend(encode(CONTROL, b"").unwrap());
        let mut d = Decoder::new(false);
        let mut frames = Vec::new();
        for b in &wire {
            frames.extend(d.feed(std::slice::from_ref(b)).unwrap());
        }
        assert_eq!(
            frames,
            vec![
                Frame::Control(b"{\"a\":1}".to_vec()),
                Frame::Control(vec![])
            ]
        );
        assert_eq!(d.pending(), 0);
    }

    #[test]
    fn data_frame_from_writ_is_refused_at_its_header() {
        let mut d = Decoder::new(false);
        // Only the header is needed to refuse: no payload byte is buffered.
        assert_eq!(d.feed(&[0, 0, 0, 3, DATA]), Err(FrameError::DataRefused));
        let mut d = Decoder::new(true);
        assert_eq!(
            d.feed(&encode(DATA, b"xyz").unwrap()).unwrap(),
            vec![Frame::Data(b"xyz".to_vec())]
        );
    }

    #[test]
    fn oversized_and_unknown_frames_are_refused_before_allocation() {
        let mut d = Decoder::new(false);
        let len = u32::try_from(CONTROL_MAX + 1).unwrap().to_be_bytes();
        assert_eq!(
            d.feed(&[len[0], len[1], len[2], len[3], CONTROL]),
            Err(FrameError::TooLong)
        );
        assert_eq!(d.pending(), HEADER);
        let mut d = Decoder::new(true);
        assert_eq!(
            d.feed(&[0xff, 0xff, 0xff, 0xff, DATA]),
            Err(FrameError::TooLong)
        );
        let mut d = Decoder::new(true);
        assert_eq!(d.feed(&[0, 0, 0, 0, 7]), Err(FrameError::UnknownType(7)));
        assert!(encode(CONTROL, &vec![b' '; CONTROL_MAX + 1]).is_none());
        assert!(encode(9, b"").is_none());
    }

    #[test]
    fn output_payload_round_trip_and_bounds() {
        let p = output_payload(Stream::Stderr, "run-1", b"hi").unwrap();
        assert_eq!(
            parse_output(&p),
            Some((Stream::Stderr, "run-1", &b"hi"[..]))
        );
        assert!(output_payload(Stream::Stdout, &"a".repeat(256), b"").is_none());
        assert!(parse_output(&[3, 0]).is_none());
        assert!(parse_output(&[1, 5, b'a']).is_none());
    }
}
