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
- API 분리는 로컬 셸과 실행 코드의 OS 수준 파일 접근 격리를 뜻하지 않는다.
- 원문과 구현의 대응, holdout의 독립성, placeholder 없는 연구 계약,
  제출 패키지와 terminal gate의 연결은 새 E2E에서 계속 검증해야 한다.
- 자연어 방향의 나머지 어휘 검사는 의미적 판단을 완전히 대체하지 못한다.
- 실패 실행의 반복을 놓친 운영 공백도 있었다. 이번 재검증에서는 질문과
  실행 실패를 확인하며 진행해야 한다.
