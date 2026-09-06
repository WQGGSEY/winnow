# Luna 측정에서 드러난 실패 경로와 재검증

목표는 실제 연구에서 강한 결과와 제출 가능한 논문에 도달하는 것이다. 이 기록은 이전 bounded run의 실패를 수정하고 재검증한 범위를 다룬다. 단위 검사나 계획 생성은 그 목표의 달성을 의미하지 않는다.

## 확인한 경로와 변경

| 관측 또는 인접 실패 경로 | 변경 | 검증 범위 |
| --- | --- | --- |
| 실패한 측정의 `operational-invalidity` 대안을 supported로 해석해 계획 전체 거절 | 이전 receipt의 work ID, 실패 종류, unresolved 효과를 생성 전 응답 스키마에 고정. 대안은 과학적 예측이며 실행 오류는 result_kind에 기록 | 실패 receipt 스키마 재생 및 실제 planner 호출 |
| 이전 계획에 적힌 PosixPath 오류가 최신 metrics 배열 오류를 가림 | 최신 outcome을 별도로 제공하고 output 계약을 함께 전달 | 실제 호출 입력 확인; 최신 원인 선택은 live 결과로 판단 |
| `unexpected_observations` 오류가 필요한 구조 없이 invalid만 반환 | 필요한 필드와 타입, 빈 배열 처리, 문자열 배열 금지를 오류에 포함 | 기존 evidence 검증 유지. 잘못된 값을 자동 변환하지 않음 |
| 초기 계획 입력 451,549바이트, 완료된 단일 호출의 input_tokens 146,063 | 큰 이력·후보·분석·소스를 주소 가능한 파일로 이동. targeted read 지침과 실제 experiment plan 경로 유지 | 현재 입력 79,881바이트와 지침 17,324바이트를 무과금 캡처 |
| 모델 변경 뒤 같은 상태의 planned work를 이전 모델 캐시에서 반환 | work에 planning_model을 기록하고 재사용 조건에 포함 | 기존 계획 경로 검사 |
| 가설 생성 중 모델 변경 시 이전 모델의 단계별 캐시와 새 호출 혼합 | 가설 context digest에 모델 포함. 명시적 다른 모델 run 재개는 거절 | 가설 생성·재개 기존 검사 |
| 총 입력/호출 제한 안에서 한 호출이 대부분 예산 소비 | 호출별 초기 입력 상한 추가. 초과 사실을 ledger에 남겨 controller가 종료 | provider 실행 전 초과 거절 재생 |
| 남은 전체 시간보다 긴 nested timeout | 동기 호출 timeout을 남은 deadline으로 제한 | provider에 전달한 timeout 확인 |
| 동기 호출 timeout 이후 detached tool 생존 가능 | 동기 CLI 호출에도 소유 프로세스 추적과 종료 적용 | 실제 detached Python child를 timeout시킨 뒤 생존하지 않음 확인 |
| 축약된 코드만으로 정확한 실행 bytes를 확인하기 어려움 | 구현 발췌와 함께 원본 experiment_plan 경로 제공 | context에 원본 경로 보존 |

과거 scope 없는 대안을 임의로 operational로 재분류하지 않는다. 실패 receipt가 과학적 증거가 되지 않도록 기존 기록은 unresolved로 유지한다. 서로 다른 집단의 eligible count가 하나라도 0일 때 전체 선택 검사를 inconclusive로 처리하는 것은 현재 선언된 공동 검사의 보수적인 동작이다. 부분 집단 결과를 과학적 성공으로 승격시키는 변경은 하지 않았다.

## 비용과 증거의 한계

호출 예산은 초기 UTF-8 입력과 프로세스 호출 수를 측정한다. native CLI 내부의 추가 도구 결과 및 누적 대화 토큰을 정확히 제한하는 토큰 예산은 아니다. 따라서 실제 invocation usage를 별도로 확인하고 wall-clock 종료를 적용한다. 이전의 multi-agent 차단, host skill 탐색 차단, MCP budget 환경 전달도 유지한다.

후보 설명, 강한 결과 도달 가능성, 논문 품질은 이 구조 변경만으로 보장되지 않는다. 모든 가능한 프로그램 버그를 제거했다고 주장하지 않는다. 이번 범위는 이전 측정으로 특정할 수 있었던 위 경로다.

## 가벼운 실제 재검증

`runs/e2e/lab3-game-only/luna-exposure-20260906-225510`에 controller, 예산, 시작 work, 이벤트 offset과 종료 결과를 보관한다. Luna max, supervisor cycle 1회, 최대 300초, 모델 호출 4회, 총 초기 입력 350,000바이트, 호출별 140,000바이트를 적용했다. 연구 코드의 수정·실험 설계는 harness에 맡기며 root가 과학적 해답이나 learner를 작성하지 않는다.

### 실제 결과

- 총 301.1초 후 종료. supervisor와 이 실행의 budget 환경을 가진 모든 하위 프로세스가 종료됐음을 확인했다.
- 호출 예약은 3회, 초기 입력 합계 220,594바이트였다. 이는 청구 토큰 수가 아니다.
- planner 첫 호출은 240초 시간 초과. MCP가 `planning_failed → plan_research_work`를 반환해 동일 단계 재시도를 유도했다. 남은 약 11초 안에 두 번째 planner도 종료됐다.
- current work는 기존 `fd87d2…` 그대로다. 새 계획, 새 실험, 과학적 진전 및 논문은 생성되지 않았다. 실패 효과를 잘못 해석하는 이전 거절은 새 응답 자체가 없었으므로 live에서 해결됐다고 판정할 수 없다.
- planner 완료 usage가 없으므로 총 입력/출력 토큰은 확인하지 못했다. 이번 실행의 상세 내부 도구 기록이 timeout 오류에 보존되지 않는 점도 확인했다.

### 실행 뒤 추가 수정

실제 재시도 실패를 확인하고 bounded transport 실패가 budget을 소진 상태로 기록하게 했다. MCP는 다음 호출을 재권고하지 않고 checkpoint를 반환한다. timeout까지 나온 원시 이벤트를 별도 파일에 보존하며, usage가 없으면 unknown으로 표시한다. 남은 시간을 반영한 실제 timeout을 오류에 기록한다. ledger는 별도 고정 lock과 atomic replace를 사용해 controller가 쓰기 중간의 JSON을 읽는 경로를 막았다.

동기 프로세스 소유권 정리는 코드 검토 중 실제 complete 호출에 연결되도록 정정했다. 이 연결 정정, atomic ledger, timeout trace 보존 및 checkpoint 반환은 이번 유료 실행 뒤 최종 코드에 반영된 항목이다. 실제 detached child 종료 재현, transport timeout 재생, MCP handler 재생으로 확인했으며 **최종 코드 전체에 대한 추가 유료 E2E는 하지 않았다**.

다음 병목은 planner가 참조 자료를 읽고 단일 결정을 반환하는 과정이다. 입력 축소만으로 이 병목이 제거되지 않았다. 다음 유료 실행 전 timeout trace에서 실제 자료 읽기량과 반복 판단을 측정해야 한다. 이번 결과로 publication 수준의 자동 연구가 작동한다고 주장할 수 없다.

최종 정적 점검에서 기존 `handle_run_critic_reviews`의 `validate_named_schema` import 누락도 발견했다. critic 응답이 생기면 NameError가 발생할 경로이므로 같은 모듈의 지역 import 관례에 맞춰 보완했다. 이번 실행이 critic 단계까지 도달한 것은 아니다.

## 회귀 확인

관련 7개 검사 파일에서 121 passed, 3 subtests passed. 변경 Python 모듈의 pyflakes 및 git diff --check 통과. 이 결과는 연구 성공을 뜻하지 않는다.

별도로 실행한 `tests/test_mcp_server.py`는 20 passed, 3 subtests 이후 기존 도구 목록 기대값에 `retrieve_research_source`가 없어 1 failed로 중단됐다. 이번 변경은 해당 도구의 노출을 바꾸지 않았다. 이 실패를 숨기거나 공개 도구를 제거해 통과시키지 않았다.
