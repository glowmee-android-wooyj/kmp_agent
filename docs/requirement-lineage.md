# 원본·승인 기준과 파생 명세의 일치 검토

격리 개발 소스에만 적용한다. 설치본·과거 원장은 변경하지 않는다. 별도 역할·상태 원장을 만들지 않고 Tasks artifact/snapshot/verification/agent_run/issue/review를 재사용한다.

## 입력 계약

프로젝트 루트 `requirement-lineage.json`:
- schema_version=1, scope=PRODUCT 또는 HARNESS_TRIAL, purpose.
- sources: id/kind/path/sha256/anchor. USER_ORIGINAL은 문자 그대로의 요청·결정, APPROVED_REQUIREMENTS는 approval.source/anchor/approved_sha256로 USER_ORIGINAL 승인·기준 지정을 연결한 요구, AGENT_DERIVED는 파생 설계·시험 명세. 파일명이나 에이전트 CONFIRMED는 권한 근거가 아니다. 해시는 내용 고정이지 사용자 권한 증명이 아니다.
- requirements: id/source/anchor/expected(값·동작 객체). 원문의 모든 요구 목록을 만든다. 추론·제안은 승인 요구로 승격하지 않는다.
- derived_items: id/source/anchor/source_ids/expected/excluded. 원본별 전체 기대값과 비교 가능한 단위로 분리한다. 원본 누락, 출처 없는 추가, 값/동작 변경, 제외를 계산한다.
- decisions: delta_id/status/source/anchor/expected/actual. APPROVED는 USER_ORIGINAL과 정확한 변경 전후 값이 필요하다. 구체 합의를 일반 기술 위임으로 바꾸지 않는다. 실제 승인 의미·출처 진위는 독립 QA가 확인한다.

PRODUCT의 미승인 변경은 보완/사용자 결정 대기로 반환한다. HARNESS_TRIAL은 purpose, USER_ORIGINAL 출처를 가리키는 scope_authorization, product_pass_claim=false, 각 차이의 TRIAL_ONLY+reason+정확한 전후 값을 요구한다. 제품의 원래 조건은 여전히 미충족이다. 제한 시험을 제품 변경 승인이나 제품 PASS로 바꾸지 않는다.

## 구조 비교와 인간 승인

기존 `structurally_ready`는 링크·값·변경 결정의 구조 비교 결과로 유지한다. 로컬 `USER_ORIGINAL`/`APPROVED_REQUIREMENTS` 라벨이나 anchor는 실제 인간 권한을 증명하지 않는다. `authorization_ready`와 `human_authorization`을 별도로 표시한다. PRODUCT는 source kind·APPROVED 주장 유무와 관계없이 `human_authorization.question_id/task_id`가 현재 계약의 source·requirements·derived_items·decisions와 모든 source 파일 해시를 승인한 typed `HUMAN_APPROVAL`에 연결돼야 한다. purpose는 `requirement_lineage`, subject는 `approval_subject(contract)`다. 질문·원본·대상 입력·명시 artifact·작업 소유와 범위는 소비 시 다시 검사한다. [원본 권한과 구현 진행](approval-authority.md#원본-권한-승인과-구현-진행)을 구분하므로 허용된 구현 코드 변경만으로 이 원본 승인을 무효화하지 않으며, 기존 개설 스냅샷과 native 응답은 보존한다. 정확한 코드 상태의 승인·검증을 대신하는 것은 아니다.

PRODUCT의 `require_review()`는 구조 비교가 통과해도 인간 권한이 없거나 오래됐거나 다른 task 것이면 차단한다. HARNESS_TRIAL에서 승인 주장이 없을 때만 `NOT_REQUIRED`를 허용한다. PRODUCT 작업에 HARNESS_TRIAL 계약을 사용하는 것은 차단한다. source kind를 낮춰 인간 승인 조건을 피할 수 없다. HARNESS_TRIAL이나 독립 requirements QA PASS를 인간 제품 승인으로 승격하지 않는다. 기존 비교 이력을 다시 쓰지 않고 현재 실행 경계에서 검사한다. [승인·검토 권한](approval-authority.md)의 native 및 same-OS 한계를 따른다.

아직 승인되지 않은 입력·계약 변경은 기존 질문을 고치지 않고 `supersedes_question_id`로 명시적인 대체 승인을 요청한다. 소비자는 원래 question ID에서 유효한 승인 체인만 해석하고 현재 계보 subject·source 해시·task를 다시 비교한다. 새 대체 질문이 미응답·거절·무효이거나 분기가 모호하면 진행하지 않는다. 이미 정확한 새 기술 제안을 승인한 경우에는 아래 전환 경로를 사용한다. 과거 구조 비교와 저장 상태는 덮어쓰지 않는다.

## 승인된 기술 계약을 기존 시도에 전환

T1 입력에 연결된 작업에서 정확한 T2 제안을 먼저 승인한 경우 `transition-approved-contract`를 사용한다. 새 동의를 받거나 기존 질문의 `decision_contract`·개설 checkpoint·native 응답을 수정하지 않는다. 동일 목적의 이전 질문을 대체한다면 T2 질문에 이미 등록된 명시적 `supersedes_question_id` 연결을 사용한다. 전환 명령이 승인 체인을 새로 만들지는 않는다. 아직 적용하지 않은 후보 파일은 일반 계보 JSON 또는 `{"target_path":"requirement-lineage.json","after":{...}}` 형식이며, 후보의 `human_authorization`은 해당 승인과 대상 작업을 가리킨다.

```json
{
  "task_id": "IMPLEMENT",
  "agent_run_id": "ORIGINAL_WORK_EXECUTION_ID",
  "question_id": "APPROVED_TECHNICAL_PROPOSAL",
  "candidate": "docs/guard-inputs/approved-contract.json",
  "before_sha256": "CURRENT_CANONICAL_FILE_SHA256",
  "reason": "승인한 기술 제안을 기존 시도에 적용",
  "evidence": "docs/guard-inputs/transition-evidence.json"
}
```

```sh
rtk proxy python3 .kmp-agent/agent.py task --run-id ORIGINAL_RUN --operation transition-approved-contract --data docs/guard-inputs/transition.json
```

동일 파일을 `APPROVED_REQUIREMENTS`와 `AGENT_DERIVED`에 연결한 후보도 전체 subject와 파일 해시의 정확한 native 승인이 있을 때만 허용한다. `USER_ORIGINAL`과 파생 출처의 중복은 계속 거부한다.

원래 checkpoint가 있는 `BLOCKED` work 시도와 현재 native 루트 연결이 필요하다. 정확한 승인 subject·출처 파일 해시·현재 T1 입력을 검증하고 실제로 달라진 기술 정책·관련 파생 항목·기술 출처만 전환한다. 비기술 요구·원본 출처·기존 변경 결정·설정 및 P/D/API 기준·코드 범위는 보존한다. 현재 계보 파일과 동결 작업 입력·기술 계약은 하나의 복구 가능한 트랜잭션으로 반영한다. 기존 실행의 시작 입력·baseline·승인 기록은 보존하고 전후 연결 근거를 추가한다. 후보나 파일이 승인 범위와 다르면 canonical 파일을 바꾸지 않고 거부한다.

트랜잭션 도중 중단되면 하네스의 일반 작업 조회·변경은 미완료 journal 때문에 차단된다. 로컬 `.harness/contract-transition-transactions/txn-*/journal.json`에서 중단된 transaction ID를 확인하고 `recovery.json`에 `transaction_id`와 `reason`을 제공한다.

```sh
rtk proxy python3 .kmp-agent/agent.py task --run-id ORIGINAL_RUN --operation recover-contract-transition --data docs/guard-inputs/recovery.json
```

이 명령은 미완료 전환을 전환 전 상태로 복구한다. 알 수 없는 파일 변경이나 손상된 복구 snapshot은 덮어쓰지 않는다. 이미 `COMMITTED`인 기록은 되돌리지 않는다. 복구 후에도 승인 원본·후보·현재 입력을 확인하고 전환을 다시 명시적으로 실행한다.

전환은 작업을 자동 재개하거나 과거 QA를 새 계약의 PASS로 승격하지 않는다. 변경된 입력에 필요한 검증을 다시 수행하고 기존 `resume` 경로를 사용한다. 아직 승인하지 않은 변경은 위의 명시적 대체 승인 절차를 따른다. 이 기능은 미배포 변경이며 기존 설치본에는 자동 반영되지 않는다.

전환 전에 만든 명령 continuation은 이전 입력 범위에 묶여 있으므로 새 계약에서 명령을 실행할 수 없다. 해당 명령 run이 열려 있다면 명령·자식 프로세스가 끝난 것을 확인하고 `finish --end-reason handoff`로 종료한다. 작업 재개 후 원래 task run에서 `continue-command-run`의 `previous_run_id`에 그 마지막 명령 run을 지정해 새 범위로 연결한다. [종료된 상위 실행 뒤 명령 로그 연결](execution-policy.md#종료된-상위-실행-뒤-명령-로그-이어가기)을 따르며 기존 명령 기록은 보존한다.

기술 계약 검토는 제출한 work에 정식 연결된 continuation의 빌드·테스트 로그도 인정한다. 연결 증거·native 바인딩·현재 계약 범위와 제출 코드 지문, 동일 명령 run의 시작·종료 기록을 다시 검증하며 임의의 다른 run 로그는 인정하지 않는다. 이는 명령 근거의 연결 검사이며 독립 QA나 제품 전체 PASS를 대신하지 않는다.

## 실행

부모 개발 task inputs에 계약·모든 출처를 넣어 동결한다.

```sh
rtk proxy python3 -B -m harness.requirement_lineage compare --root /project
rtk proxy python3 -B -m harness.requirement_lineage configure-qa --root /project --run-id RUN --task-id REQUIREMENTS-QA --target-task-id DEVELOPMENT
rtk proxy python3 -B -m harness.cli --target /project qa --run-id RUN --task-id REQUIREMENTS-QA
rtk proxy python3 -B -m harness.cli --target /project development --run-id RUN --task-id DEVELOPMENT
```

stage=requirements는 기존 verification 역할/QA.execute의 독립 프로세스·한도·결과 회수·반환 루프를 사용한다. 원문·승인 문서·파생 명세·구조 비교를 모두 전달하고 전체 원본 목록 자체의 누락도 읽어 검토하도록 필수 criteria를 등록한다. 누락·모순은 baseline_gap/baseline_contradiction으로 planning에 반환한다. QA가 정책을 수정하지 않는다.

실제 development와 직접 run_child(run_codex)는 모델 호출 전에 동일 부모 대상의 동결 계약·필수 기준·현재 입력/검토 범위·독립 QA 실행 시작/종료·유효 보고를 검사한다. 없으면 기존 task에 BLOCKED 이유/다음 행동과 이벤트를 기록한다. 계약이 있는 구현/재검증 QA도 선행 요구사항 QA를 검사한다.

기존 명시 synthetic worker 단위 시험은 유지하되 독립 QA로 인정하지 않는다. 계약 없는 과거 QA는 LEGACY_UNCERTIFIED. 실제 신규 development는 계약 없이 실행하지 않는다. 외부 메인 에이전트의 직접 파일 편집/하네스 밖 명령까지 통제하는 기능은 아니다. 설치본에는 미적용이다.

QA 보고는 claim_scope / requirement_lineage / execution_path_validation / product_requirements_fulfillment을 분리한다. 작업 QA PASS만으로 실행 경로나 제품 전체를 PASS로 승격하지 않는다. 플랫폼·영역·통합 인수는 별도다.

선택 실제 모델 시험은 [별도 로컬 companion](examples-separation.md)으로 분리한다. 실제 시험 허가 원문과 합성 명세를 구분하고 구현·수리 전 이 QA 계약을 사용하며, 개별 실행 기록은 로컬에 보존한다. [UI 비해당 계약](ui-applicability.md)도 현재 계보와 동결 입력에 연결하며 시각 검증을 PASS로 바꾸지 않는다.

## 한계

구조 검사는 명시된 링크·값 차이·승인 참조·신선도를 확인한다. 자연어 의미·원본 인벤토리 완전성·사용자 출처 진위·승인 범위는 독립 QA의 직접 검토가 필요하다. source kind 라벨만으로 진위를 증명하지 않는다. 합성 상태 회귀 시험과 실제 모델 검토를 구분한다. 실제 모델 시험은 각 격리 실행의 로컬 검증 근거에 결과·차단·미실행 범위를 기록하며, 한 실행의 성공을 일반적인 의미 이해 성능이나 제품 전체 PASS로 확대하지 않는다.

위 실행 명령은 해당 로컬 개발 소스를 이미 명시 선택한 격리 프로젝트에서만 사용한다. 실제 제품의 설치본 선택을 바꾸지 않는다. requirements QA는 기준 문서 범위만 고정하므로 정상적인 후속 구현 코드 변경만으로 원본 대조 QA를 무효화하지 않는다. 원본·파생·승인·계약 변경은 재검토를 요구한다.
