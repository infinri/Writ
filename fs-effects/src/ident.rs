//! Persisted identities (J-7.2, L-d): `{fs_uuid, ino, btime, size, digest}` as canonical
//! text. `st_dev` and `mnt_id` are never persisted. The inode generation is not read (no
//! safe wrapper for `FS_IOC_GETVERSION` in the reviewed `rustix` features); recorded limitation.

use writ_broker::writ_kernel::json::Digest;
use writ_hostsys::fsfx::Observed;

/// The persisted identity of one file, with its content digest.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Identity {
    /// Filesystem UUID (verified at open).
    pub fs_uuid: String,
    /// Inode number.
    pub ino: u64,
    /// Birth time seconds.
    pub btime_sec: i64,
    /// Birth time nanoseconds.
    pub btime_nsec: u32,
    /// Size in bytes.
    pub size: u64,
    /// Content digest (`sha256:<hex>`).
    pub digest: String,
}

const PREFIX: &str = "fsid/1";

impl Identity {
    /// The identity of `o` with content `digest`.
    pub fn of(fs_uuid: &str, o: &Observed, digest: &str) -> Identity {
        Identity {
            fs_uuid: fs_uuid.to_string(),
            ino: o.ino,
            btime_sec: o.btime_sec,
            btime_nsec: o.btime_nsec,
            size: o.size,
            digest: digest.to_string(),
        }
    }

    /// Canonical text.
    pub fn text(&self) -> String {
        format!(
            "{PREFIX} {} {} {}.{:09} {} {}",
            self.fs_uuid, self.ino, self.btime_sec, self.btime_nsec, self.size, self.digest
        )
    }

    /// Parse canonical text.
    pub fn parse(text: &str) -> Option<Identity> {
        let parts: Vec<&str> = text.split(' ').collect();
        let [p, uuid, ino, btime, size, digest] = parts.as_slice() else {
            return None;
        };
        if *p != PREFIX {
            return None;
        }
        let (sec, nsec) = btime.split_once('.')?;
        let id = Identity {
            fs_uuid: (*uuid).to_string(),
            ino: ino.parse().ok()?,
            btime_sec: sec.parse().ok()?,
            btime_nsec: nsec.parse().ok()?,
            size: size.parse().ok()?,
            digest: (*digest).to_string(),
        };
        (id.text() == text).then_some(id)
    }

    /// `true` when `o` is the same file (inode and birth time; the filesystem is the
    /// adapter's, checked by `st_dev` and mount id at every observation).
    pub fn same_file(&self, o: &Observed) -> bool {
        self.ino == o.ino && self.btime_sec == o.btime_sec && self.btime_nsec == o.btime_nsec
    }
}

/// A recorded prior state of a target: absent, or one file.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Prior {
    /// No target.
    Absent,
    /// A regular single-link file.
    File(Identity),
}

impl Prior {
    /// Canonical text (`absent` or the identity).
    pub fn text(&self) -> String {
        match self {
            Prior::Absent => "absent".into(),
            Prior::File(i) => i.text(),
        }
    }

    /// Parse canonical text.
    pub fn parse(text: &str) -> Option<Prior> {
        if text == "absent" {
            Some(Prior::Absent)
        } else {
            Identity::parse(text).map(Prior::File)
        }
    }
}

/// The D-006 content digest of `bytes` (`sha256:<hex>`).
pub fn digest(bytes: &[u8]) -> String {
    Digest::of_bytes(bytes).as_str().to_string()
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
mod tests {
    use super::*;

    #[test]
    fn identity_text_round_trips_and_rejects_variants() {
        let i = Identity {
            fs_uuid: "u-1".into(),
            ino: 12,
            btime_sec: 1_700_000_000,
            btime_nsec: 5,
            size: 3,
            digest: digest(b"abc"),
        };
        assert_eq!(Identity::parse(&i.text()), Some(i.clone()));
        assert_eq!(Prior::parse("absent"), Some(Prior::Absent));
        assert_eq!(Prior::parse(&i.text()), Some(Prior::File(i.clone())));
        let t = i.text();
        assert_eq!(Identity::parse(&t.replace(".000000005", ".5")), None);
        assert_eq!(Identity::parse(&format!("{t} x")), None);
        assert_eq!(Identity::parse(&t.replace("fsid/1", "fsid/2")), None);
    }
}
