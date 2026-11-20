//! Pure high-stakes threshold evaluation over authenticated contributor facts.

/// Authenticated and currently bound contributor facts supplied by the broker.
pub(super) struct QuorumVote<'a> {
    /// Scoped evaluator identity.
    pub evaluator: &'a str,
    /// Key identity.
    pub key: &'a str,
    /// Verification material digest.
    pub material: &'a str,
    /// Authenticated verdict.
    pub verdict: &'a str,
}

/// Require a pair independent on every dimension and reject an eligible failure.
pub(super) fn quorum_passes(
    eligible: &std::collections::BTreeMap<String, String>,
    votes: &[QuorumVote<'_>],
) -> bool {
    if !(2..=16).contains(&eligible.len()) || votes.len() > eligible.len() {
        return false;
    }
    let mut seen = std::collections::BTreeSet::new();
    let mut passes = Vec::new();
    for vote in votes {
        let Some(group) = eligible.get(vote.evaluator) else {
            return false;
        };
        if !seen.insert(vote.evaluator) {
            return false;
        }
        match vote.verdict {
            "pass" => passes.push((vote, group)),
            "abstain" => (),
            _ => return false,
        }
    }
    passes.iter().enumerate().any(|(i, (left, group))| {
        passes[i + 1..].iter().any(|(right, other)| {
            group != other && left.key != right.key && left.material != right.material
        })
    })
}

#[cfg(test)]
mod quorum_tests {
    use super::{quorum_passes, QuorumVote};
    use std::collections::BTreeMap;

    #[test]
    fn independence_requires_a_single_pair_separated_on_all_dimensions() {
        let eligible = BTreeMap::from([
            ("a".into(), "first".into()),
            ("b".into(), "first".into()),
            ("c".into(), "second".into()),
        ]);
        let vote = |evaluator, key, material, verdict| QuorumVote {
            evaluator,
            key,
            material,
            verdict,
        };
        let passes = |v: &[QuorumVote<'_>]| quorum_passes(&eligible, v);
        assert!(passes(&[
            vote("a", "k1", "m1", "pass"),
            vote("c", "k2", "m2", "pass")
        ]));
        for second in [
            vote("a", "k2", "m2", "pass"),
            vote("b", "k2", "m2", "pass"),
            vote("c", "k1", "m2", "pass"),
            vote("c", "k2", "m1", "pass"),
            vote("c", "k2", "m2", "abstain"),
            vote("other", "k2", "m2", "pass"),
        ] {
            assert!(!passes(&[vote("a", "k1", "m1", "pass"), second]));
        }
        for verdict in ["fail", "unknown"] {
            assert!(!passes(&[
                vote("a", "k1", "m1", "pass"),
                vote("c", "k2", "m2", "pass"),
                vote("b", "k3", "m3", verdict)
            ]));
        }
        assert!(!passes(&[vote("a", "k1", "m1", "pass")]));
    }
}
