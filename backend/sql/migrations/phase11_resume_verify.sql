SELECT JSON_OBJECT(
 'config_anomaly_count',
   (SELECT IF(COUNT(*)=1,0,1) FROM `system_config` WHERE `config_key`='ttl.resume.days')
 + (SELECT IF(COUNT(*)=1,0,1) FROM `system_config` WHERE `config_key`='ttl.resume.candidate.days')
 + (SELECT IF(COUNT(*)=1,0,1) FROM `system_config` WHERE `config_key`='resume.replacement.rollout.allowlist')
 + (SELECT COUNT(*) FROM `system_config`
   WHERE (`config_key`='ttl.resume.days' AND NOT (`value_type`='int'
       AND `config_value` REGEXP '^[0-9]+$'
       AND CAST(`config_value` AS DECIMAL(20,0)) BETWEEN 1 AND 3650
       AND CAST(CAST(`config_value` AS DECIMAL(20,0)) AS CHAR)=`config_value`))
     OR (`config_key`='ttl.resume.candidate.days' AND NOT (`value_type`='int'
       AND `config_value` REGEXP '^[0-9]+$'
       AND CAST(`config_value` AS DECIMAL(20,0)) BETWEEN 1 AND 365
       AND CAST(CAST(`config_value` AS DECIMAL(20,0)) AS CHAR)=`config_value`))
     OR (`config_key`='resume.replacement.rollout.allowlist' AND NOT (
       `value_type`='json' AND JSON_VALID(`config_value`)
       AND JSON_TYPE(CAST(`config_value` AS JSON))='OBJECT'
       AND JSON_LENGTH(JSON_KEYS(CAST(`config_value` AS JSON)))=2
       AND JSON_CONTAINS_PATH(CAST(`config_value` AS JSON),'all','$.revision','$.userids')
       AND JSON_TYPE(JSON_EXTRACT(CAST(`config_value` AS JSON),'$.revision')) IN ('INTEGER','UNSIGNED INTEGER')
       AND CAST(JSON_UNQUOTE(JSON_EXTRACT(CAST(`config_value` AS JSON),'$.revision')) AS DECIMAL(20,0))
         BETWEEN 1 AND 18446744073709551615
       AND JSON_TYPE(JSON_EXTRACT(CAST(`config_value` AS JSON),'$.userids'))='ARRAY'
       AND NOT EXISTS (SELECT 1 FROM JSON_TABLE(CAST(`config_value` AS JSON),'$.userids[*]'
         COLUMNS (`userid` VARCHAR(256) PATH '$', `item` JSON PATH '$')) members
         WHERE JSON_TYPE(`item`)<>'STRING' OR `userid`<>TRIM(`userid`)
           OR CHAR_LENGTH(`userid`)=0 OR CHAR_LENGTH(`userid`)>64)
       AND (SELECT COUNT(*) FROM JSON_TABLE(CAST(`config_value` AS JSON),'$.userids[*]'
         COLUMNS (`userid` VARCHAR(256) PATH '$')) all_members)
         = (SELECT COUNT(DISTINCT `userid`) FROM JSON_TABLE(CAST(`config_value` AS JSON),'$.userids[*]'
           COLUMNS (`userid` VARCHAR(256) PATH '$')) unique_members)
     ))
     OR `config_key`='rollout.resume_replacement.allowlist'),
 'lifecycle_anomaly_count', SUM(CASE
   WHEN deleted_at IS NULL AND audit_status='passed' AND (activated_at IS NULL OR expires_at IS NULL OR candidate_expires_at IS NOT NULL) THEN 1
   WHEN deleted_at IS NULL AND audit_status IN ('pending','rejected') AND (activated_at IS NOT NULL OR expires_at IS NOT NULL OR candidate_expires_at IS NULL) THEN 1
   ELSE 0 END),
 'unresolved_media_issue_count', (SELECT COUNT(*) FROM resume_media_isolation_issue WHERE status <> 'resolved'),
 'media_missing_count', (SELECT COUNT(*) FROM phase11_resume_media_key_scan s
   LEFT JOIN media_asset_lifecycle m ON SHA2(m.object_key,256)=s.key_hash
   LEFT JOIN resume_media_isolation_issue i ON i.resume_id=s.resume_id
     AND i.key_hash=s.key_hash AND i.status<>'resolved'
   WHERE s.reference_kind='valid' AND m.id IS NULL AND i.id IS NULL),
 'media_illegal_binding_count', (SELECT COUNT(*) FROM phase11_resume_media_key_scan s
   JOIN resume r ON r.id=s.resume_id
   JOIN media_asset_lifecycle m ON SHA2(m.object_key,256)=s.key_hash
   LEFT JOIN resume_media_isolation_issue i ON i.resume_id=s.resume_id
     AND i.key_hash=s.key_hash AND i.status<>'resolved'
   WHERE s.reference_kind='valid' AND i.id IS NULL
     AND (m.owner_userid<>r.owner_userid OR m.entity_type<>'resume'
     OR m.entity_id<>r.id OR (r.deleted_at IS NULL AND m.state<>'attached')
     OR (r.deleted_at IS NOT NULL AND m.state NOT IN ('delete_pending','deleted')))),
 'media_resolution_not_applied_count', (SELECT COUNT(*)
   FROM resume_media_isolation_issue i
   WHERE i.status='resolved' AND (
     (i.issue_type='shared_reference' AND EXISTS (
       SELECT 1 FROM phase11_resume_media_key_scan mine
       WHERE mine.resume_id=i.resume_id AND mine.key_hash=i.key_hash
         AND mine.reference_kind='valid' AND EXISTS (
           SELECT 1 FROM phase11_resume_media_key_scan other
           WHERE other.key_hash=mine.key_hash AND other.reference_kind='valid'
             AND other.resume_id<>mine.resume_id)))
     OR (i.issue_type='duplicate_reference' AND EXISTS (
       SELECT 1 FROM phase11_resume_media_key_scan s
       WHERE s.resume_id=i.resume_id AND s.key_hash=i.key_hash
         AND s.reference_kind='valid' AND s.reference_count>1))
     OR (i.issue_type IN ('invalid_json','invalid_reference') AND EXISTS (
       SELECT 1 FROM phase11_resume_media_key_scan s
       WHERE s.resume_id=i.resume_id AND s.key_hash=i.key_hash
         AND s.reference_kind='invalid'))
     OR (i.issue_type='owner_binding_conflict' AND EXISTS (
       SELECT 1 FROM phase11_resume_media_key_scan s JOIN resume r ON r.id=s.resume_id
       JOIN media_asset_lifecycle m ON SHA2(m.object_key,256)=s.key_hash
       WHERE s.resume_id=i.resume_id AND s.key_hash=i.key_hash
         AND (m.owner_userid<>r.owner_userid OR m.entity_type<>'resume' OR m.entity_id<>r.id)))
     OR (i.issue_type='illegal_lifecycle_state' AND EXISTS (
       SELECT 1 FROM phase11_resume_media_key_scan s JOIN resume r ON r.id=s.resume_id
       JOIN media_asset_lifecycle m ON SHA2(m.object_key,256)=s.key_hash
       WHERE s.resume_id=i.resume_id AND s.key_hash=i.key_hash
         AND ((r.deleted_at IS NULL AND m.state<>'attached')
           OR (r.deleted_at IS NOT NULL AND m.state NOT IN ('delete_pending','deleted')))))
   )),
 'media_invalid_json_count', (SELECT COUNT(*) FROM resume WHERE images IS NOT NULL AND NOT JSON_VALID(images)),
 'orphan_cleanup_pending_count', (SELECT COUNT(*) FROM target_cleanup_task t
   LEFT JOIN resume r ON r.id=t.target_id
   WHERE t.target_type='resume' AND r.id IS NULL AND t.status <> 'succeeded'),
 'orphan_without_cleanup_count', (SELECT COUNT(DISTINCT targets.target_id) FROM (
   SELECT target_id FROM recommendation_impression WHERE target_type='resume'
   UNION ALL SELECT j.target_id FROM recommendation_request q
     JOIN JSON_TABLE(q.served_top_ids,'$[*]' COLUMNS(target_id BIGINT PATH '$' NULL ON ERROR)) j
     WHERE q.direction='search_worker'
   UNION ALL SELECT j.target_id FROM recommendation_request q
     JOIN JSON_TABLE(COALESCE(q.shadow_top_ids,JSON_ARRAY()),'$[*]' COLUMNS(target_id BIGINT PATH '$' NULL ON ERROR)) j
     WHERE q.direction='search_worker'
   UNION ALL SELECT j.target_id FROM recommendation_search_attempt a
     JOIN recommendation_request q ON q.request_id=a.request_id AND q.direction='search_worker'
     JOIN JSON_TABLE(a.candidate_ids,'$[*]' COLUMNS(target_id BIGINT PATH '$' NULL ON ERROR)) j
   UNION ALL SELECT j.target_id FROM recommendation_search_attempt a
     JOIN recommendation_request q ON q.request_id=a.request_id AND q.direction='search_worker'
     JOIN JSON_TABLE(a.precision_pool_ids,'$[*]' COLUMNS(target_id BIGINT PATH '$' NULL ON ERROR)) j
   UNION ALL SELECT j.target_id FROM recommendation_delivery d
     JOIN JSON_TABLE(d.recommendation_context,'$.items[*]'
       COLUMNS(target_type VARCHAR(16) PATH '$.target_type' NULL ON ERROR,
               target_id BIGINT PATH '$.target_id' NULL ON ERROR)) j
     WHERE j.target_type='resume'
   UNION ALL SELECT j.target_id FROM conversation_log c
     JOIN recommendation_delivery d ON d.delivery_id=c.recommendation_delivery_id
     JOIN JSON_TABLE(d.recommendation_context,'$.items[*]'
       COLUMNS(target_type VARCHAR(16) PATH '$.target_type' NULL ON ERROR,
               target_id BIGINT PATH '$.target_id' NULL ON ERROR)) j
     WHERE j.target_type='resume'
   UNION ALL SELECT j.target_id FROM wecom_outbound_outbox o
     JOIN recommendation_delivery d ON d.delivery_id=o.recommendation_delivery_id
     JOIN JSON_TABLE(d.recommendation_context,'$.items[*]'
       COLUMNS(target_type VARCHAR(16) PATH '$.target_type' NULL ON ERROR,
               target_id BIGINT PATH '$.target_id' NULL ON ERROR)) j
     WHERE j.target_type='resume'
   UNION ALL SELECT target_id FROM recommendation_exposure_daily WHERE target_type='resume'
   UNION ALL SELECT target_id FROM event_log WHERE target_type='resume'
 ) targets LEFT JOIN resume r ON r.id=targets.target_id
 LEFT JOIN target_cleanup_task t ON t.target_type='resume' AND t.target_id=targets.target_id
 WHERE targets.target_id IS NOT NULL AND r.id IS NULL AND t.id IS NULL),
 'deleted_cleanup_pending_count', (SELECT COUNT(*) FROM target_cleanup_task t
   JOIN resume r ON r.id=t.target_id
   WHERE t.target_type='resume' AND r.deleted_at IS NOT NULL AND t.status <> 'succeeded'),
 'deleted_without_cleanup_count', (SELECT COUNT(*) FROM resume r
   LEFT JOIN target_cleanup_task t ON t.target_type='resume' AND t.target_id=r.id
   WHERE r.deleted_at IS NOT NULL AND t.id IS NULL)
) AS verification_summary FROM resume;
