-- ============================================================================
-- fix_mojibake.sql  —  修复 seed 时因 mysql client 默认 latin1 导致的双重编码
--
-- 典型症状：display_name "王大海" 显示为 "çŽ‹å¤§æµ·"
-- 成因：seed SQL 文件是 UTF-8，但 mysql 客户端把它当 latin1 读取，
--       每个 CJK 字符的 3 字节被当作 3 个 latin1 字符再编码为 utf8mb4 存储。
-- 修复：CONVERT(CAST(CONVERT(col USING latin1) AS BINARY) USING utf8mb4)
--       将 utf8mb4 bytes 反 decode 为原始 latin1，再 cast 成 binary 取回
--       真正的 UTF-8 字节序列，最后告诉 MySQL 这些字节就是 utf8mb4。
--
-- 安全机制：
--   1. 只更新"看起来像 mojibake"的行（col LIKE '%ç%' OR '%å%' …）。
--   2. 额外防护：要求反解码结果非 NULL —— 否则说明该行并非标准 mojibake
--      Chinese，跳过不动。防止 NOT NULL 列被置空导致事务回滚。
--   3. START TRANSACTION 包裹；最后 COMMIT，中断则自动回滚。
--
-- 用法：
--   docker exec -i jobbridge-mysql mysql --default-character-set=utf8mb4 \
--       -u jobbridge -pjobbridge jobbridge < backend/sql/fix_mojibake.sql
-- ============================================================================

START TRANSACTION;

-- ---- user 表：display_name / company / contact_person / blocked_reason ----
UPDATE `user` SET display_name =
  CONVERT(CAST(CONVERT(display_name USING latin1) AS BINARY) USING utf8mb4)
  WHERE display_name IS NOT NULL
    AND (display_name LIKE '%ç%' OR display_name LIKE '%å%' OR display_name LIKE '%æ%'
         OR display_name LIKE '%è%' OR display_name LIKE '%é%' OR display_name LIKE '%ê%'
         OR display_name LIKE '%ã%' OR display_name LIKE '%â%')
    AND CONVERT(CAST(CONVERT(display_name USING latin1) AS BINARY) USING utf8mb4) IS NOT NULL;

UPDATE `user` SET company =
  CONVERT(CAST(CONVERT(company USING latin1) AS BINARY) USING utf8mb4)
  WHERE company IS NOT NULL
    AND (company LIKE '%ç%' OR company LIKE '%å%' OR company LIKE '%æ%'
         OR company LIKE '%è%' OR company LIKE '%é%' OR company LIKE '%ê%'
         OR company LIKE '%ã%' OR company LIKE '%â%')
    AND CONVERT(CAST(CONVERT(company USING latin1) AS BINARY) USING utf8mb4) IS NOT NULL;

UPDATE `user` SET contact_person =
  CONVERT(CAST(CONVERT(contact_person USING latin1) AS BINARY) USING utf8mb4)
  WHERE contact_person IS NOT NULL
    AND (contact_person LIKE '%ç%' OR contact_person LIKE '%å%' OR contact_person LIKE '%æ%'
         OR contact_person LIKE '%è%' OR contact_person LIKE '%é%' OR contact_person LIKE '%ê%'
         OR contact_person LIKE '%ã%' OR contact_person LIKE '%â%')
    AND CONVERT(CAST(CONVERT(contact_person USING latin1) AS BINARY) USING utf8mb4) IS NOT NULL;

UPDATE `user` SET blocked_reason =
  CONVERT(CAST(CONVERT(blocked_reason USING latin1) AS BINARY) USING utf8mb4)
  WHERE blocked_reason IS NOT NULL
    AND (blocked_reason LIKE '%ç%' OR blocked_reason LIKE '%å%' OR blocked_reason LIKE '%æ%'
         OR blocked_reason LIKE '%è%' OR blocked_reason LIKE '%é%' OR blocked_reason LIKE '%ê%'
         OR blocked_reason LIKE '%ã%' OR blocked_reason LIKE '%â%')
    AND CONVERT(CAST(CONVERT(blocked_reason USING latin1) AS BINARY) USING utf8mb4) IS NOT NULL;

-- ---- job 表：自由文本列 ----
UPDATE `job` SET dorm_condition =
  CONVERT(CAST(CONVERT(dorm_condition USING latin1) AS BINARY) USING utf8mb4)
  WHERE dorm_condition IS NOT NULL
    AND (dorm_condition LIKE '%ç%' OR dorm_condition LIKE '%å%' OR dorm_condition LIKE '%æ%'
         OR dorm_condition LIKE '%è%' OR dorm_condition LIKE '%é%' OR dorm_condition LIKE '%ã%')
    AND CONVERT(CAST(CONVERT(dorm_condition USING latin1) AS BINARY) USING utf8mb4) IS NOT NULL;

UPDATE `job` SET shift_pattern =
  CONVERT(CAST(CONVERT(shift_pattern USING latin1) AS BINARY) USING utf8mb4)
  WHERE shift_pattern IS NOT NULL
    AND (shift_pattern LIKE '%ç%' OR shift_pattern LIKE '%å%' OR shift_pattern LIKE '%æ%'
         OR shift_pattern LIKE '%è%' OR shift_pattern LIKE '%é%' OR shift_pattern LIKE '%ã%')
    AND CONVERT(CAST(CONVERT(shift_pattern USING latin1) AS BINARY) USING utf8mb4) IS NOT NULL;

UPDATE `job` SET work_hours =
  CONVERT(CAST(CONVERT(work_hours USING latin1) AS BINARY) USING utf8mb4)
  WHERE work_hours IS NOT NULL
    AND (work_hours LIKE '%ç%' OR work_hours LIKE '%å%' OR work_hours LIKE '%æ%'
         OR work_hours LIKE '%è%' OR work_hours LIKE '%é%' OR work_hours LIKE '%ã%')
    AND CONVERT(CAST(CONVERT(work_hours USING latin1) AS BINARY) USING utf8mb4) IS NOT NULL;

UPDATE `job` SET experience_required =
  CONVERT(CAST(CONVERT(experience_required USING latin1) AS BINARY) USING utf8mb4)
  WHERE experience_required IS NOT NULL
    AND (experience_required LIKE '%ç%' OR experience_required LIKE '%å%' OR experience_required LIKE '%æ%'
         OR experience_required LIKE '%è%' OR experience_required LIKE '%é%' OR experience_required LIKE '%ã%')
    AND CONVERT(CAST(CONVERT(experience_required USING latin1) AS BINARY) USING utf8mb4) IS NOT NULL;

UPDATE `job` SET rebate =
  CONVERT(CAST(CONVERT(rebate USING latin1) AS BINARY) USING utf8mb4)
  WHERE rebate IS NOT NULL
    AND (rebate LIKE '%ç%' OR rebate LIKE '%å%' OR rebate LIKE '%æ%'
         OR rebate LIKE '%è%' OR rebate LIKE '%é%' OR rebate LIKE '%ã%')
    AND CONVERT(CAST(CONVERT(rebate USING latin1) AS BINARY) USING utf8mb4) IS NOT NULL;

UPDATE `job` SET min_duration =
  CONVERT(CAST(CONVERT(min_duration USING latin1) AS BINARY) USING utf8mb4)
  WHERE min_duration IS NOT NULL
    AND (min_duration LIKE '%ç%' OR min_duration LIKE '%å%' OR min_duration LIKE '%æ%'
         OR min_duration LIKE '%è%' OR min_duration LIKE '%é%' OR min_duration LIKE '%ã%')
    AND CONVERT(CAST(CONVERT(min_duration USING latin1) AS BINARY) USING utf8mb4) IS NOT NULL;

UPDATE `job` SET job_sub_category =
  CONVERT(CAST(CONVERT(job_sub_category USING latin1) AS BINARY) USING utf8mb4)
  WHERE job_sub_category IS NOT NULL
    AND (job_sub_category LIKE '%ç%' OR job_sub_category LIKE '%å%' OR job_sub_category LIKE '%æ%'
         OR job_sub_category LIKE '%è%' OR job_sub_category LIKE '%é%' OR job_sub_category LIKE '%ã%')
    AND CONVERT(CAST(CONVERT(job_sub_category USING latin1) AS BINARY) USING utf8mb4) IS NOT NULL;

UPDATE `job` SET raw_text =
  CONVERT(CAST(CONVERT(raw_text USING latin1) AS BINARY) USING utf8mb4)
  WHERE raw_text IS NOT NULL
    AND (raw_text LIKE '%ç%' OR raw_text LIKE '%å%' OR raw_text LIKE '%æ%'
         OR raw_text LIKE '%è%' OR raw_text LIKE '%é%' OR raw_text LIKE '%ã%')
    AND CONVERT(CAST(CONVERT(raw_text USING latin1) AS BINARY) USING utf8mb4) IS NOT NULL;

UPDATE `job` SET description =
  CONVERT(CAST(CONVERT(description USING latin1) AS BINARY) USING utf8mb4)
  WHERE description IS NOT NULL
    AND (description LIKE '%ç%' OR description LIKE '%å%' OR description LIKE '%æ%'
         OR description LIKE '%è%' OR description LIKE '%é%' OR description LIKE '%ã%')
    AND CONVERT(CAST(CONVERT(description USING latin1) AS BINARY) USING utf8mb4) IS NOT NULL;

UPDATE `job` SET audit_reason =
  CONVERT(CAST(CONVERT(audit_reason USING latin1) AS BINARY) USING utf8mb4)
  WHERE audit_reason IS NOT NULL
    AND (audit_reason LIKE '%ç%' OR audit_reason LIKE '%å%' OR audit_reason LIKE '%æ%'
         OR audit_reason LIKE '%è%' OR audit_reason LIKE '%é%' OR audit_reason LIKE '%ã%')
    AND CONVERT(CAST(CONVERT(audit_reason USING latin1) AS BINARY) USING utf8mb4) IS NOT NULL;

-- ---- resume 表：自由文本列 ----
UPDATE `resume` SET work_experience =
  CONVERT(CAST(CONVERT(work_experience USING latin1) AS BINARY) USING utf8mb4)
  WHERE work_experience IS NOT NULL
    AND (work_experience LIKE '%ç%' OR work_experience LIKE '%å%' OR work_experience LIKE '%æ%'
         OR work_experience LIKE '%è%' OR work_experience LIKE '%é%' OR work_experience LIKE '%ã%')
    AND CONVERT(CAST(CONVERT(work_experience USING latin1) AS BINARY) USING utf8mb4) IS NOT NULL;

UPDATE `resume` SET ethnicity =
  CONVERT(CAST(CONVERT(ethnicity USING latin1) AS BINARY) USING utf8mb4)
  WHERE ethnicity IS NOT NULL
    AND (ethnicity LIKE '%ç%' OR ethnicity LIKE '%å%' OR ethnicity LIKE '%æ%'
         OR ethnicity LIKE '%è%' OR ethnicity LIKE '%é%' OR ethnicity LIKE '%ã%')
    AND CONVERT(CAST(CONVERT(ethnicity USING latin1) AS BINARY) USING utf8mb4) IS NOT NULL;

UPDATE `resume` SET taboo =
  CONVERT(CAST(CONVERT(taboo USING latin1) AS BINARY) USING utf8mb4)
  WHERE taboo IS NOT NULL
    AND (taboo LIKE '%ç%' OR taboo LIKE '%å%' OR taboo LIKE '%æ%'
         OR taboo LIKE '%è%' OR taboo LIKE '%é%' OR taboo LIKE '%ã%')
    AND CONVERT(CAST(CONVERT(taboo USING latin1) AS BINARY) USING utf8mb4) IS NOT NULL;

UPDATE `resume` SET raw_text =
  CONVERT(CAST(CONVERT(raw_text USING latin1) AS BINARY) USING utf8mb4)
  WHERE raw_text IS NOT NULL
    AND (raw_text LIKE '%ç%' OR raw_text LIKE '%å%' OR raw_text LIKE '%æ%'
         OR raw_text LIKE '%è%' OR raw_text LIKE '%é%' OR raw_text LIKE '%ã%')
    AND CONVERT(CAST(CONVERT(raw_text USING latin1) AS BINARY) USING utf8mb4) IS NOT NULL;

UPDATE `resume` SET description =
  CONVERT(CAST(CONVERT(description USING latin1) AS BINARY) USING utf8mb4)
  WHERE description IS NOT NULL
    AND (description LIKE '%ç%' OR description LIKE '%å%' OR description LIKE '%æ%'
         OR description LIKE '%è%' OR description LIKE '%é%' OR description LIKE '%ã%')
    AND CONVERT(CAST(CONVERT(description USING latin1) AS BINARY) USING utf8mb4) IS NOT NULL;

UPDATE `resume` SET audit_reason =
  CONVERT(CAST(CONVERT(audit_reason USING latin1) AS BINARY) USING utf8mb4)
  WHERE audit_reason IS NOT NULL
    AND (audit_reason LIKE '%ç%' OR audit_reason LIKE '%å%' OR audit_reason LIKE '%æ%'
         OR audit_reason LIKE '%è%' OR audit_reason LIKE '%é%' OR audit_reason LIKE '%ã%')
    AND CONVERT(CAST(CONVERT(audit_reason USING latin1) AS BINARY) USING utf8mb4) IS NOT NULL;

-- ---- conversation_log / audit_log ----
UPDATE `conversation_log` SET content =
  CONVERT(CAST(CONVERT(content USING latin1) AS BINARY) USING utf8mb4)
  WHERE content IS NOT NULL
    AND msg_type = 'text'
    AND (content LIKE '%ç%' OR content LIKE '%å%' OR content LIKE '%æ%'
         OR content LIKE '%è%' OR content LIKE '%é%' OR content LIKE '%ã%')
    AND CONVERT(CAST(CONVERT(content USING latin1) AS BINARY) USING utf8mb4) IS NOT NULL;

UPDATE `audit_log` SET reason =
  CONVERT(CAST(CONVERT(reason USING latin1) AS BINARY) USING utf8mb4)
  WHERE reason IS NOT NULL
    AND (reason LIKE '%ç%' OR reason LIKE '%å%' OR reason LIKE '%æ%'
         OR reason LIKE '%è%' OR reason LIKE '%é%' OR reason LIKE '%ã%')
    AND CONVERT(CAST(CONVERT(reason USING latin1) AS BINARY) USING utf8mb4) IS NOT NULL;

-- 查看修复后的样本
SELECT '=== user 样本 ===' AS _;
SELECT external_userid, display_name, company, contact_person FROM `user` LIMIT 10;

SELECT '=== job 样本 ===' AS _;
SELECT id, owner_userid, LEFT(raw_text, 40) AS raw_text_head FROM `job` LIMIT 5;

SELECT '=== resume 样本 ===' AS _;
SELECT id, owner_userid, LEFT(raw_text, 40) AS raw_text_head FROM `resume` LIMIT 5;

COMMIT;
