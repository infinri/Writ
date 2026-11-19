use super::*;

#[test]
fn waiver_exclusion_links_are_bidirectional_transitive_and_issuer_conservative() {
    let conn = Connection::open_in_memory().unwrap();
    conn.execute_batch(
        "CREATE TABLE enrollment_links
           (issuer TEXT, subject TEXT, linked_issuer TEXT, linked_subject TEXT) STRICT;
         INSERT INTO enrollment_links VALUES
           ('operator', 'publisher', 'mirror', 'delegate'),
           ('unrelated-issuer', 'DELEGATE', 'adapter', 'approver'),
           ('authority', 'requester-human', 'adapter', 'requester'),
           ('adapter', 'agent', 'authority', 'agent-owner'),
           ('other', 'unrelated', 'other', 'unrelated-alias');",
    )
    .unwrap();

    let linked = linked_exclusions(
        &conn,
        vec![
            SubjectId::new("operator", "publisher"),
            SubjectId::new("adapter", "requester"),
            SubjectId::new("adapter", "agent"),
        ],
    )
    .ok()
    .unwrap();
    assert_eq!(
        linked,
        vec![
            SubjectId::new("adapter", "approver"),
            SubjectId::new("authority", "agent-owner"),
            SubjectId::new("authority", "requester-human"),
            SubjectId::new("mirror", "delegate"),
            SubjectId::new("unrelated-issuer", "DELEGATE"),
        ],
    );
}
