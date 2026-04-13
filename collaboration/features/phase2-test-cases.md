# Phase 2 Full Coverage Test Cases

> Scope: infrastructure adapters and contract skeleton for Phase 2
> Role: QA
> Updated: 2026-04-12

## 1. Test Scope

Phase 2 only verifies:

- LLM provider factory, request/response adaptation, fallback, retry, prompt contract
- Local storage behavior and key/path contract
- WeCom crypto, callback parsing, client wrapper behavior
- Redis message contract and `wecom_inbound_event` state contract
- Mockability, reproducibility, and Phase 3/4 boundary control

Phase 2 does not verify:

- Business services
- Webhook route
- Worker implementation
- Admin API
- Real external WeCom or LLM effect quality

## 2. Entry / Exit Criteria

### Entry

- Requirement docs reviewed
- Phase 2 code delivered
- Unit and integration tests runnable locally
- Redis and MySQL available for integration verification

### Exit

- Critical and high severity defects are either fixed or explicitly blocked
- Phase 2 contract, storage, WeCom, and LLM coverage completed
- Pressure test executed with reproducible command and results

## 3. Test Case Matrix

### 3.1 LLM Factory

| ID | Scenario | Preconditions | Steps | Expected Result | Priority |
|---|---|---|---|---|---|
| LLM-F-001 | Default provider resolved from settings | `settings.llm_provider=qwen` | Call `get_intent_extractor()` and `get_reranker()` | Qwen implementations returned | P0 |
| LLM-F-002 | Explicit provider override works | None | Call factory with `provider='doubao'` | Doubao implementations returned | P0 |
| LLM-F-003 | Unknown provider rejected | None | Call factory with unknown provider | Clear `ValueError` raised | P0 |
| LLM-F-004 | Service layer only depends on abstraction | None | Import factory plus ABCs only | No direct provider import needed | P1 |

### 3.2 LLM Provider Behavior

| ID | Scenario | Preconditions | Steps | Expected Result | Priority |
|---|---|---|---|---|---|
| LLM-P-001 | Intent normal response mapping | Mock valid OpenAI-compatible response | Call `extract()` | `IntentResult` fields mapped correctly | P0 |
| LLM-P-002 | Rerank normal response mapping | Mock valid OpenAI-compatible response | Call `rerank()` | `RerankResult` fields mapped correctly | P0 |
| LLM-P-003 | Invalid JSON fallback | Mock non-JSON content | Call `extract()` / `rerank()` | Fallback result returned, `raw_response` preserved | P0 |
| LLM-P-004 | Unknown intent fallback | Mock JSON with unsupported intent | Call `extract()` | Intent downgraded to `chitchat` | P0 |
| LLM-P-005 | Timeout retry once | Mock first timeout, second success | Call helper/provider | Exactly one retry triggered | P0 |
| LLM-P-006 | Timeout after retry fails predictably | Mock timeout twice | Call provider | Unified timeout path reached | P0 |
| LLM-P-007 | HTTP status error path | Mock HTTP 4xx/5xx | Call provider | Unified provider exception raised | P1 |
| LLM-P-008 | No hardcoded final user reply in fallback | Mock parse failure | Inspect fallback result | No service-layer user reply text hardcoded | P1 |
| LLM-P-009 | Mock-only acceptance for Doubao | No real API key | Run mocked tests | Doubao passes under mock | P1 |

### 3.3 Prompt Contract

| ID | Scenario | Preconditions | Steps | Expected Result | Priority |
|---|---|---|---|---|---|
| LLM-T-001 | Intent prompt constant exists | None | Inspect prompts module | Constant exists and non-empty | P0 |
| LLM-T-002 | Rerank prompt constant exists | None | Inspect prompts module | Constant exists and non-empty | P0 |
| LLM-T-003 | Version tag present | None | Inspect prompt text / constants | Version and date present | P0 |
| LLM-T-004 | Token budget documented | None | Inspect prompt text / constants | Input/output budget present | P0 |
| LLM-T-005 | Strict JSON output mandated | None | Inspect prompt text | JSON-only and no markdown fence | P0 |
| LLM-T-006 | DTO field alignment | None | Compare prompt fields with DTOs | Fields align with `IntentResult` / `RerankResult` | P0 |
| LLM-T-007 | Fallback rules documented | None | Inspect prompt text | Parse fail / unknown intent / timeout fallback documented | P1 |

### 3.4 Storage

| ID | Scenario | Preconditions | Steps | Expected Result | Priority |
|---|---|---|---|---|---|
| STO-F-001 | Default provider is local | `settings.oss_provider=local` | Call `get_storage()` | `LocalStorage` returned | P0 |
| STO-F-002 | Unknown storage provider rejected | None | Call `get_storage('bad')` | Clear `ValueError` raised | P0 |
| STO-B-001 | Save creates file | Temp dir available | Save bytes to nested key | File exists on disk | P0 |
| STO-B-002 | Save returns stable URL | Temp dir available | Save bytes | URL matches configured prefix + key | P0 |
| STO-B-003 | Exists reflects file status | Temp dir available | Check before and after save/delete | Boolean behavior correct | P0 |
| STO-B-004 | Delete existing file | Temp dir available | Save then delete | Returns `True`, file removed | P0 |
| STO-B-005 | Delete missing file | Temp dir available | Delete absent file | Returns `False` | P1 |
| STO-B-006 | Multi-level key works | Temp dir available | Save nested key | Intermediate dirs auto-created | P1 |
| STO-S-001 | Reject relative traversal | Temp dir available | Save `../../../etc/passwd` | `ValueError` raised | P0 |
| STO-S-002 | Reject Unix absolute path | Temp dir available | Save `/etc/passwd` | `ValueError` raised | P0 |
| STO-S-003 | Reject Windows absolute path | Windows environment | Save `C:/temp/evil.txt` or `C:\\temp\\evil.txt` | Must be rejected and remain under base dir | P0 |
| STO-S-004 | URL generation rejects invalid key | Temp dir available | Call `get_url()` with invalid absolute key | Should reject or sanitize consistently | P1 |

### 3.5 WeCom Crypto

| ID | Scenario | Preconditions | Steps | Expected Result | Priority |
|---|---|---|---|---|---|
| WEC-C-001 | Valid signature accepted | Known token/timestamp/nonce/encrypt | Verify signature | Returns `True` | P0 |
| WEC-C-002 | Invalid signature rejected | Known inputs | Verify wrong signature | Returns `False` | P0 |
| WEC-C-003 | Encrypt/decrypt round trip | Valid AES key and corp id | Encrypt then decrypt | Original plaintext restored | P0 |
| WEC-C-004 | Wrong corp id rejected | Valid ciphertext | Decrypt with wrong corp id | Controlled exception raised | P0 |
| WEC-C-005 | Long payload decrypts correctly | Large message | Encrypt/decrypt | Success without truncation | P1 |
| WEC-C-006 | Malformed ciphertext handled predictably | Tampered ciphertext | Call `decrypt_message()` | Controlled `ValueError`-style path, no low-level leak | P0 |
| WEC-C-007 | Invalid padding handled predictably | Tampered last block | Call `decrypt_message()` | Controlled exception, no partial plaintext | P0 |

### 3.6 WeCom Callback

| ID | Scenario | Preconditions | Steps | Expected Result | Priority |
|---|---|---|---|---|---|
| WEC-B-001 | Parse base XML | Valid XML | Call `parse_xml()` | Dict returned correctly | P0 |
| WEC-B-002 | Extract encrypt field | Callback XML contains `Encrypt` | Call helper | Correct ciphertext extracted | P0 |
| WEC-B-003 | Missing encrypt rejected | XML without `Encrypt` | Call helper | `ValueError` raised | P0 |
| WEC-B-004 | Text message mapped | Valid text XML | Parse message | Required fields complete | P0 |
| WEC-B-005 | Image message mapped | Valid image XML | Parse message | `media_id` and content mapped | P0 |
| WEC-B-006 | Optional field missing is stable | XML missing optional field | Parse message | Empty string/default used predictably | P1 |
| WEC-B-007 | Invalid `CreateTime` handling | XML with bad time | Parse message | Controlled exception or explicit fallback | P1 |

### 3.7 WeCom Client

| ID | Scenario | Preconditions | Steps | Expected Result | Priority |
|---|---|---|---|---|---|
| WEC-A-001 | Refresh token success | Mock token API | Call `get_access_token()` | Token cached and returned | P0 |
| WEC-A-002 | Refresh token API error | Mock errcode != 0 | Call `get_access_token()` | `WeComError` raised | P0 |
| WEC-A-003 | Cached token reused | Preload valid token | Call `get_access_token()` | No refresh request | P1 |
| WEC-S-001 | `send_text()` success | Mock HTTP post | Call method | Success payload returned | P0 |
| WEC-S-002 | `send_text()` API failure | Mock errcode != 0 | Call method | `WeComError` raised | P0 |
| WEC-S-003 | Invalid agent id rejected early | `agent_id` non-digit | Call `send_text()` | Fast-fail clear config error, no silent `0` fallback | P0 |
| WEC-M-001 | `download_media()` success | Mock binary response | Call method | Binary bytes returned | P0 |
| WEC-M-002 | `download_media()` error JSON | Mock JSON error body | Call method | `WeComError` raised | P0 |
| WEC-E-001 | External contact success | Mock valid response | Call method | Contact dict returned | P0 |
| WEC-E-002 | External contact not found | Mock errcode 84061 | Call method | Returns `None` only in this case | P0 |
| WEC-E-003 | External contact API failure | Mock errcode != 0 and != 84061 | Call method | Raises exception, not `None` | P0 |
| WEC-E-004 | Network failure propagation | Mock connect error | Call method | Exception raised predictably | P1 |

### 3.8 Message Contract

| ID | Scenario | Preconditions | Steps | Expected Result | Priority |
|---|---|---|---|---|---|
| MSG-D-001 | Incoming queue name locked | None | Inspect contract and constants | `queue:incoming` matches | P0 |
| MSG-D-002 | Dead-letter queue name locked | None | Inspect contract and constants | `queue:dead_letter` matches | P0 |
| MSG-D-003 | Rate-limit then dedup then enqueue order documented | Contract doc exists | Inspect doc | Call order explicitly documented | P0 |
| MSG-D-004 | Worker consume order documented | Contract doc exists | Inspect doc | `dequeue -> processing -> done/failed/dead_letter` documented | P0 |
| MSG-D-005 | Retry threshold documented | Contract doc exists | Inspect doc | Max retries = 2 documented | P0 |
| MSG-D-006 | Five inbound event statuses present | ORM + doc available | Inspect model and doc | `received/processing/done/failed/dead_letter` complete | P0 |

### 3.9 Boundary / Non-Functional

| ID | Scenario | Preconditions | Steps | Expected Result | Priority |
|---|---|---|---|---|---|
| NF-D-001 | No service-layer logic in provider/storage/wecom | None | Static review | No Phase 3/4 business orchestration embedded | P0 |
| NF-D-002 | README/runbook updated for Phase 2 | Docs available | Inspect README and test docs | Phase 2 run and verification instructions present | P1 |
| NF-D-003 | Tests reproducible without real external network | None | Execute unit tests | All Phase 2 unit tests pass under mock | P0 |
| NF-D-004 | Pressure script reproducible | Test env ready | Run perf script | JSON summary produced with no manual edits | P0 |

## 4. Suggested Execution Order

1. Prompt and factory unit checks
2. LLM provider mock tests
3. Storage behavior and path security checks
4. WeCom crypto and callback parsing tests
5. WeCom client mock HTTP tests
6. Redis/message contract review and integration tests
7. Phase 2 composite pressure test

## 5. Defect Classification Guidance

- Blocker: contract mismatch that prevents Phase 3/4 direct reuse
- Critical: security escape, wrong exception semantics, or data corruption risk
- High: provider/storage/client behavior inconsistent with requirement but workaround exists
- Medium: delivery doc gap, weak negative coverage, or non-blocking robustness issue
- Low: wording, logging, or non-functional improvement
