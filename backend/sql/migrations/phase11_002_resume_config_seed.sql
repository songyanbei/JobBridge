-- Phase 11 pre-cutover configuration seed.  The runner preserves this
-- explicit transaction as one resumable unit and checkpoints only COMMIT.
START TRANSACTION;

INSERT IGNORE INTO `system_config`
 (`config_key`,`config_value`,`value_type`,`description`)
VALUES
 ('ttl.resume.days','30','int','简历业务有效期（天）'),
 ('ttl.resume.candidate.days','7','int','简历候选版本保留期（天）'),
 ('resume.replacement.rollout.allowlist','{"revision":1,"userids":[]}','json','简历替换隐藏 allowlist');

-- Lock the concurrent INSERT IGNORE winner before validation.  A legacy
-- pre-freeze spelling is never guessed or silently migrated: its presence is
-- an explicit fail-closed operator decision.
SELECT `config_key` FROM `system_config`
WHERE `config_key` IN (
 'ttl.resume.days','ttl.resume.candidate.days',
 'resume.replacement.rollout.allowlist','rollout.resume_replacement.allowlist'
) FOR UPDATE;

SET @phase11_config_blockers :=
 (SELECT IF(COUNT(*)=1,0,1) FROM `system_config` WHERE `config_key`='ttl.resume.days')
 + (SELECT COUNT(*) FROM `system_config` WHERE `config_key`='ttl.resume.days'
    AND NOT (`value_type`='int' AND `config_value` REGEXP '^[0-9]+$'
      AND CAST(`config_value` AS UNSIGNED) BETWEEN 1 AND 3650
      AND CAST(CAST(`config_value` AS UNSIGNED) AS CHAR)=`config_value`))
 + (SELECT IF(COUNT(*)=1,0,1) FROM `system_config` WHERE `config_key`='ttl.resume.candidate.days')
 + (SELECT COUNT(*) FROM `system_config` WHERE `config_key`='ttl.resume.candidate.days'
    AND NOT (`value_type`='int' AND `config_value` REGEXP '^[0-9]+$'
      AND CAST(`config_value` AS UNSIGNED) BETWEEN 1 AND 365
      AND CAST(CAST(`config_value` AS UNSIGNED) AS CHAR)=`config_value`))
 + (SELECT IF(COUNT(*)=1,0,1) FROM `system_config`
    WHERE `config_key`='resume.replacement.rollout.allowlist')
 + (SELECT COUNT(*) FROM `system_config`
    WHERE `config_key`='rollout.resume_replacement.allowlist')
 + (SELECT COUNT(*) FROM `system_config`
    WHERE `config_key`='resume.replacement.rollout.allowlist'
      AND NOT (`value_type`='json' AND JSON_VALID(`config_value`)
        AND JSON_TYPE(CAST(`config_value` AS JSON))='OBJECT'
        AND JSON_LENGTH(JSON_KEYS(CAST(`config_value` AS JSON)))=2
        AND JSON_CONTAINS_PATH(CAST(`config_value` AS JSON),'all','$.revision','$.userids')
        AND JSON_TYPE(JSON_EXTRACT(CAST(`config_value` AS JSON),'$.revision')) IN ('INTEGER','UNSIGNED INTEGER')
        AND CAST(JSON_UNQUOTE(JSON_EXTRACT(CAST(`config_value` AS JSON),'$.revision')) AS DECIMAL(20,0))
          BETWEEN 1 AND 18446744073709551615
        AND JSON_TYPE(JSON_EXTRACT(CAST(`config_value` AS JSON),'$.userids'))='ARRAY'
        AND NOT EXISTS (
          SELECT 1 FROM JSON_TABLE(CAST(`config_value` AS JSON),'$.userids[*]'
            COLUMNS (`userid` VARCHAR(256) PATH '$', `item` JSON PATH '$')) AS members
          WHERE JSON_TYPE(`item`)<>'STRING' OR `userid`<>TRIM(`userid`)
            OR CHAR_LENGTH(`userid`)=0 OR CHAR_LENGTH(`userid`)>64
        )
        AND (SELECT COUNT(*) FROM JSON_TABLE(CAST(`config_value` AS JSON),'$.userids[*]'
          COLUMNS (`userid` VARCHAR(256) PATH '$')) AS all_members)
          = (SELECT COUNT(DISTINCT `userid`) FROM JSON_TABLE(CAST(`config_value` AS JSON),'$.userids[*]'
            COLUMNS (`userid` VARCHAR(256) PATH '$')) AS unique_members)
      ));

SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='phase11_config_seed_invalid';
COMMIT;
