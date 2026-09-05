# Pacman E2E 실패 분석과 재검증

## 관찰된 실패

`thread_02646d27`은 2026-09-05 13:53 KST에 production을 시작했다.
고정 정책 세 개를 실행했지만 실제 RL 학습이나 연구 노드 생성까지 진행하지 못했다.
14:33에는 연구자가 거절한 qualification을 원본과 동일하게 재등록했고,
음식 회수율 0.03을 평가 기준으로 넣었다. 이 기준의 고정 맵·seed 분리나
학습 고착 해결과의 관계는 입증되지 않았다. 최종 원고도 없다.

## 재현과 원인

| 실패 | 재현 / 원인 | 수정과 확인 |
|---|---|---|
| 거절 의견 소실 | 수신 확인된 operator 응답을 다음 resume prompt가 제외했다. | 소비 여부와 관계없이 결정 기록 전달. 수정 전 회귀 테스트 실패, 수정 후 통과. |
| 답변 대기 중 실행 | 미응답 decision_request가 있어도 deterministic advance와 Codex spawn을 호출했다. | 질문이 미응답이면 연구 dispatch를 기다리고 UI에 awaiting_input 표시. 답변 후 재개를 테스트했다. |
| 평가 기준 자체 등록 | MCP가 caller의 `registered_by=supervisor_bootstrap`을 받아 활성 envelope에 저장했다. | MCP는 제안만 저장. 연구자 frontend가 등록 주체를 지정하고 frozen blind contract 변경도 거부. API 재현과 TestClient로 확인. |
| 부적절한 문헌 연결 재승인 | canonical runner 검증을 과학적 역할 승인으로 사용했다. | 실행 검증 뒤 별도 연구자 검토. qualification, dossier, 후보 상세 자료, 실행 결합 hash를 승인에 연결. 거절 기록과 달라진 자료에 이전 승인을 재사용할 수 없다. 계약 compiler도 승인을 요구한다. |
| 단순 대조군을 정확히 등록할 수 없음 | 모든 역할의 source_ids에 문헌 인용을 강제했다. | naive/random은 문헌 주장 없는 자체 대조군으로 등록 가능. current_best_known은 문헌 근거 필수. 정책 코드와 방법은 하네스가 작성·선정한다. |
| 모델 호출 전 반복 실패 | 임시 작업 폴더가 Git 저장소 밖인데 `--skip-git-repo-check`가 없었다. | 실제 실패 복사본에서 CodexCliError를 확인하고 CLI 옵션 추가. Git 오류가 사라진 실제 호출 확인. |
| 정상 추출 조건 거부 | 생성된 intervention의 `without replacement`가 금지형 주장 정규식에 걸렸다. | `without` 단독을 금지 증거로 사용하지 않음. 실제 응답을 그대로 재생해 direction_ready 확인. |
| 실패 원인 유실 | 생성기의 모든 예외를 설명 없는 generation_retry로 변환했다. | 오류 유형과 메시지를 명령 영수증에 저장. 재시도 영수증도 같은 오류를 재구성한다. |

Git 오류와 어휘 오탐을 재현한 복사본은
`runs/audits/pacman-generation-failure/thread_copy`에 있다.
`diagnostic_result.json`, `diagnostic_after_fix.json`,
`direction_raw_response.json`, `verified_replay.json`으로 입력과 결과를 대조할 수 있다.
이 복사본의 방향은 잘못된 기존 계약을 사용한 진단 산출물이므로 실제 연구 성과로 사용하지 않는다.

## 재검증 조건

Pacman은 하네스 수정 사이클의 검증 사례다. 게임 규칙·I/O·시뮬레이터·맵만
입력으로 제공한다. 이전 정책, learner, checkpoint, 이번 진단의 방향과 코드는
새 연구에 제공하지 않는다. 연구자 역할은 운영 에이전트가 맡고, 연구 에이전트는
Sol low로 유지한다. 연구자의 개입은 별도로 기록한다.

통과 기준은 프로세스 생존이나 HTML 생성이 아니다. 실제 학습과 적합한 비교,
불확실성 및 독립 평가, 출처와 수치가 추적되는 원고, 제출용 패키지까지
실행한 증거가 필요하다. 사용자가 추후 제공할 서로 다른 문제 두 개는 추가
최종 검증이며 Pacman에서 성공했다고 그 결과까지 추정하지 않는다.

## 아직 해결하지 않은 경계

- 연구자 검토 경로는 독립된 자동 과학 심사를 대체하지 않는다.
- 셸 쓰기와 실험 프로세스의 작업 폴더 밖 쓰기는 제한했다. 호스트 전체 읽기 접근과
  모든 MCP 경로의 권한 분리까지 검증한 것은 아니다.
- 원문과 구현의 대응, holdout의 독립성, placeholder 없는 연구 계약,
  제출 패키지와 terminal gate의 연결은 새 E2E에서 계속 검증해야 한다.
- 자연어 방향의 나머지 어휘 검사는 의미적 판단을 완전히 대체하지 못한다.
- 실패 실행의 반복을 놓친 운영 공백도 있었다. 이번 재검증에서는 질문과
  실행 실패를 확인하며 진행해야 한다.

## 새 실행의 첫 production cycle에서 추가로 확인한 실패

`thread_07b175cc`는 grilling과 connector를 정상 완료했다. 첫 production
cycle은 문헌 메타데이터와 사전 실행 구조를 읽은 뒤 구현을 시작하지 않고
종료했다. 이때 하네스가 구현 부재를 `hard_external_block`으로 반환했다.
실제로 접근할 수 없는 외부 자원이 확인된 것은 아니었다.

이를 `preflight_required`로 분리하고 `execute_baseline_preflight` 도구를
추가했다. 에이전트가 node와 experiment_plan, 한 baseline role을 제출하면
하네스가 workspace와 입력 snapshot을 결합하고 LocalRunner로 실행한다.
알고리즘과 source_files의 코드는 에이전트가 작성한다. 역할·방법의 과학적
적합성 승인은 이 도구가 하지 않는다.

일반 연구 실험의 validator가 항상 세 baseline 역할을 요구하는 것도
사전 실행과 충돌했다. 사전 실행 전용 호출에서만 한 역할을 허용한다.
일반 연구 실험은 세 역할을 계속 요구한다. 한 사전 실행의 보고서에 다른
baseline 키까지 복제하면 거부하며, 같은 계획의 재요청은 기존 실행
영수증을 검증해 반환한다. 실제 코드 실행과 재요청 시 재실행하지 않는
동작은 통합 테스트로 확인했다.

새 실행 복사본의 `runs/audits/pacman-preflight-routing/result.json`에서
외부 차단 대신 사전 실행 작업으로 안내하는 것을 확인했다. 원본은
supervisor를 중지한 상태에서 수정한 뒤 재개한다. 이 추가 개입 역시
무개입 연구 성공으로 간주하지 않는다.


## 사전 실행 후 발견한 설정 불일치와 검증기 수정 권한

Sol low가 FMQ 후보와 단순·무작위 기준선 코드를 직접 작성하고 실행했다.
qualification은 실행 당시 Python 경로·환경 설정을 재검증에 전달하지 않아
동일한 receipt를 거부했다. 설정 없는 검증은 실패하고 스레드 설정을 전달한
동일 검증은 성공하는 것으로 원인을 분리했다. 세 receipt를 변경 없이
재검증했다. 연구 에이전트가 작성한 설정 전달 패치는 운영 세션이 검토해
7e92301로 보존했다. 이는 하네스 수리이며 연구 성과가 아니다.

이때 에이전트가 자신의 검증기를 수정할 수 있음도 실제 확인했다. 이후
Codex 셸은 read-only, 승인 요청은 never로 고정하고 하네스 MCP 서버만
명시적으로 허용한다. 최초 probe에서는 MCP도 차단돼 이를 수정했다.
서버별 승인 설정은 공식 설정 참조와 설치된 CLI의 실제 probe로 확인했다.
https://developers.openai.com/codex/config-reference/

LocalRunner는 bubblewrap이 없으면 실행하지 않는다. 실험의 파일 쓰기는
workspace와 임시 디렉터리로 제한하고 입력 파일은 read-only로 다시 마운트한다.
실험 프로세스의 네트워크·PID·IPC도 분리한다. 검증기 경로로 연결한 symlink와
게임 입력의 수정이 실패하면서 결과 파일은 쓰이는 통합 테스트를 실행했다.
WSL CUDA 장치를 연결한 실제 torch CUDA 연산도 성공했다. 문헌 후보 수정은
update_baseline_sources로만 수행하며, 경로는 하네스가 정하고 과학적 승인과
동결된 계약의 수정은 허용하지 않는다.

현재 기준선은 6회 학습, 하나의 학습 seed와 제한된 평가만 수행했다.
FMQ 후보는 즉시 점수 보상을 사용하고 학습 곡선이나 학습 완료 근거를 남기지
않았다. 이 결과는 실행 확인용이며 고착 재현이나 publication 수준의 비교로
승인하지 않았다. 실제 논문 작성까지의 성공 조건은 아직 충족하지 못했다.


## 실제 QMIX 실행과 재개 검증

`n_preflight_qmix_01`은 에이전트가 작성한 코드로 5개 학습 seed 각각
80회를 학습했고 120.15초에 완료했다. 개발 점수 평균은 -1.2667이다.
동일한 30개 개발 게임에 통제군을 재실행한 결과 greedy는 0.0,
random은 -0.9667이었다. 이 수치는 고착 해결이나 대칭성 원인의 증거가 아니다.

검토 패킷 `477d62f061b02a8ff37dfd78638eac11b0a6ae5ad3ec4dc16aae5947cee7ce65`를
거절했다. 전이의 전역 상태와 보상 구간 정렬, checkpoint마다 달라지는 평가
seed와 불완전한 seed 기록, 학습 충분성 및 과제 수행 진단이 해결되지 않았다.
구체적인 연구자 피드백은 baseline review와 operator_interventions.jsonl에 남겼다.
학습기 수정은 연구 에이전트의 몫이며 운영자가 코드를 제공하지 않았다.

이 과정에서 재개 제어를 추가 수정했다.

- 4782a95: selector가 준비 입력과 checkpoint 변경을 반영하고, 질문에 답하면
  idle 600초를 기다리지 않는다. 수정 전 두 회귀 테스트 실패, 수정 후 통과.
- bc25d84: MCP의 대기 시간 기본값 60초와 장기 실험 한도의 불일치를 수정했다.
  실제 QMIX의 120초 호출이 결과 수집까지 완료됐다.
- 135a319: 도구에 실제 node/experiment_plan/dossier 스키마를 공개했다.
  필수 메타데이터와 task_class를 오류마다 추측하던 요청 형식 문제를 줄인다.

bc25d84까지 전체 테스트 1034개와 하위 테스트 20개가 통과했다.
135a319의 관련 테스트 21개도 통과했다. 16:25:22에 연구자 답변 수신 후
다음 cycle이 즉시 시작되는 것을 실제 로그에서 확인했다. 이때 기존 로그가
`idle=10s > 600s`라는 잘못된 이유를 표시해, 답변 수신 재개를 별도로 표시했다.
아직 GoalContract와 실제 방향 연구 노드, 제출 원고는 생성되지 않았다.
