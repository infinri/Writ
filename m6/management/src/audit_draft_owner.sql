(SELECT tenant, project, audit_id AS sequence,
        'draft-' || audit_id AS event_id, 'management.mutation' AS event_type,
        strftime('%Y-%m-%dT%H:%M:%SZ',recorded_at) AS occurred_at,
        NULL AS trace_id, NULL AS request_id, subject AS principal_id,
        'draft.' || operation AS action_kind, 'committed' AS status,
        NULL AS policy_version,
        json_object('source_audit_id',audit_id,'draft_id',draft,
                    'state_revision',revision,'previous_revision',previous_revision,
                    'request_digest',request_digest,'content_digest',content_digest) AS payload
 FROM draft_audit)
