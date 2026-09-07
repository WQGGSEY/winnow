# 5–7단계 실제 연구 검증

2026-09-07 사용자가 5–7단계 진행을 승인했다. [1–4 완료 기록](luna-research-loop-1-4-result-20260907.md)의 마지막 판단에서 이어간다.
현재 strong result나 제출 논문은 아직 없다.

## 실행 범위

1. Luna max가 학습 코드를 작성하고, 원래 협력 Pacman 문제에 대한 해결책을 실제 대조 실험으로 발전시킨다. 이전 결정적 제어기 파일럿의 음성 결과를 RL 학습 결과로 대체 해석하지 않는다.
2. 원래 목표와 측정 기준을 유지한 채 기준선·통계적 불확실성·재현성·독립 확인을 검증한다. 개발 분석자는 holdout을 읽지 않는다. 최종 확인은 기존 격리된 경로를 사용한다.
3. 근거와 연결된 원고, 그림, 관련 연구 비교, 방법과 재현 자료를 생성하고 검토한다. 기존 ICLR 2027 제출 대상을 유지해 HTML과 공식 형식의 PDF·제출 패키지를 확인한다. 외부 투고는 이 실행의 범위가 아니다.

상위 코딩 에이전트는 harness의 실행·관측·검증·출판 연결을 수정할 수 있다. 연구 해결책이나 학습기를 대신 작성하지 않는다. 개별 배치는 30분, 최대 10회 모델 호출, 최초 요청 총 1 MB와 호출당 280 KB 한도를 사용한다. 요청 bytes는 과금 토큰 수가 아니며, 배치 종료를 연구 완료로 취급하지 않는다.

## 첫 재개

`runs/e2e/lab3-game-only/luna-publication-e2e-20260907-133028/`에서 기존 감독자를 재개했다. 이전 1–4의 비교 완료 정지 경계는 제거했다. `4606f17d…`의 저장된 궤적 분석을 MCP `resolve_research_work`로 수행 중이다. 분석 다음의 연구 방향은 Luna가 결정한다.

프런트엔드의 기존 thread URL은 유지했다. 로컬 GET 응답 200을 확인했다. 과거 실행 상태가 남아 있던 active_thread.json의 설명과 commit 정보를 갱신했다.

## 출판 경로에서 먼저 확인한 결함

그림 등록 API는 `alt_text`와 `caption`을 구분하지만 LaTeX 변환은 figcaption을 버리고 img의 alt만 PDF 캡션으로 사용했다. 실제 PDF 컴파일 및 텍스트 추출로 설명문 누락을 재현했다. `5778b35`에서 원래 설명문과 인라인 서식을 보존하도록 수정했다. 기존 venue export 검사 8개를 실행했다. 이는 렌더링 결함의 확인이며 연구 성과의 근거가 아니다.

## 목표 형식 확인

현재 설정과 [ICLR 2027 저자 지침](https://iclr.cc/Conferences/2027/AuthorGuidelines)을 2026-09-07에 대조했다. 본문 9쪽, 익명 형식, 공식 스타일, 참고문헌 뒤 부록, AI 사용 설명 요구가 현재 설정에 반영돼 있다. [AI 저자 정책](https://iclr.cc/Conferences/2027/AIPolicyForAuthors)도 출판 시 검토할 원문으로 확인했다. 실제 원고의 준수 여부는 원고 생성 후 확인한다.

## 완료된 후속 분석

13:39:58 첫 감독 사이클이 완료 work를 반환했다. 분석 receipt `5ca03c1a…`는 모든 window가 해당 arm의 하나의 최종 endpoint만 참조하므로, conflict/nonconflict의 독립적인 점수 차이를 복원할 수 없다고 판정했다. 유효한 링크 자체를 인과 효과나 반복 표본으로 해석하지 않았다. 같은 B를 반복하지 않고 학습 충분성 또는 보상·전이 제어로 분기하라는 판단이 저장됐다. 후속 `plan_research_work`가 실행 중이다.

WSL 내부 HTTPS 점검은 시간 초과됐지만 Windows의 실제 portproxy 및 Tailscale HTTPS 경로에서 모두 GET 200을 확인했다. 포트 전달 설정은 변경하지 않았다.

## 학습 비교 계획의 계약 충돌과 복구

다음 계획은 identity-free BFS 시범으로 사전 학습한 동일 recurrent learner와 처음부터 학습한 learner에 같은 raw-score fine-tuning을 적용하는 비교였다. 이 연구 내용은 Luna가 생성했다. 두 응답 모두 예측별 관측 목록에는 10개를 선언했지만 전체 목록의 maxItems=8 계약과 충돌해 같은 검증 오류로 거절됐다.

`f85fdef`에서 전체 목록 생성을 모델에 맡기지 않고 예측별 의존성의 합집합으로 계산하게 했다. 개별 의존성과 관측 바인딩은 그대로다. 실제 두 번째 응답을 정상 MCP 계획 경로에 재적용해 work `7ce7801496f430f1bd6aae170c36eb18831184b7864beee74b09bfce9f4084f2`를 등록했다. 입력은 policy metadata와 거절 피드백을 제외하고 같았고, 가설·방법·예측이 바뀌지 않았음을 비교했다. 모델 호출은 0회였다. 재현 자료는 첫 배치의 `count-inventory-recovery/`에 있다.

새 배치 `runs/e2e/lab3-game-only/luna-publication-e2e-20260907-135508/`가 이 학습 비교의 구현 단계부터 진행한다. 학습 결과는 아직 없다.

## 학습 구현과 등록 전 점검

Luna가 recurrent 학습 모듈을 재사용해 교사 시범 초기화 비교 코드를 작성했다. 한 episode 실측 시간에 맞춰 두 조건의 fine-tuning을 각각 24 episodes로 설정하고 learner seed 17, 23을 사용했다. 상위 에이전트는 학습 코드를 수정하지 않았다.

전체 초안은 네 체크포인트와 30개 유효 endpoint를 생성했다. 교사 평균 점수는 0.667, 두 learner 조건은 모두 −35였고 평균 차이는 0이었다. 시범 학습은 3,042개 행과 8 epochs를 사용했다. 이 값들은 등록 전 점검 결과로, 원래 문제의 실패 원인이나 강한 결과를 입증하지 않는다.

소스와 실행 계약은 `n_preflight_7ce7801496f430f1`에 등록됐다. 남은 배치 시간 115초가 독립 검토의 600초보다 짧아 provider 호출 전에 정지했다. `luna-publication-e2e-20260907-142509`에서 감독 LLM 호출 없이 저장 MCP 요청을 재개했고 독립 구현 검토가 진행 중이다.

## 독립 검토 시간 초과와 같은 검사 기록의 재개

14:40 소스 검토가 600초 내 결론을 반환하지 못했다. 해당 배치는 새 실험 없이 체크포인트로 종료했다. 모든 소유 프로세스가 정리된 뒤 `luna-publication-e2e-20260907-144125`에서 재개했다. 이미 완료된 도구 관측과 해시가 고정된 소스를 입력으로 하는 기존 무도구 판단 복구 경로가 선택됐다. 최초 입력은 276,875 bytes이며 이는 과금 토큰 수가 아니다.

`4b7554e`는 앞서 수정한 관측 합집합 계산의 오류 경계를 보완한다. 누락된 필드가 있는 JSON은 KeyError로 빠져 원시 응답이 남지 않았고, 실패 중에도 새 방향 생성이 가능해졌다. 기존 회귀 검사에서 이 경우를 재현한 뒤 원시 응답 보존과 재개 차단을 확인했다. 연구 계획과 진행 중인 정상 검토 내용은 바꾸지 않았다.

## 최종 검토의 지적과 구현 수정

14:52:20 무도구 복구 검토 `fd8f54d4…`가 완료됐다. 최종 판단은 평가 endpoint와 최종 체크포인트 사이의 검증된 연결이 없다는 이유로 거절이었다. 학습 종료 후 평가를 먼저 수행하고 별도로 checkpoint 목록을 만드는 순서로는 각 집계 endpoint의 checkpoint provenance를 확인할 수 없었다. 감독자는 같은 work의 소스 수정으로 자동 진행했다.

검토의 중간 응답은 하나의 지적에 두 개의 유효한 원문 근거를 붙였다. 당시 host 검사는 이를 근거 개수만으로 거절했다. `67e3537`에서 모든 수정 요구에 근거가 존재하는지 확인하면서 각 근거의 경로·인용·범위를 모두 검사하도록 했다. 실제 최종 응답은 근거 하나를 사용해 기존 계약으로 정상 수용됐다. 이 수정이 최종 응답을 복구했다고 주장하지 않는다.

복구 검토 사용량은 input 95,995, output 35,963 tokens이며 reasoning 34,363은 output에 포함된다. 그 전 시간 초과 호출의 사용량은 여기 포함하지 않았다. 실험 실행보다 검토에 많은 시간이 쓰였으며 비용 효율성은 아직 입증되지 않았다.

## 체크포인트 수정의 국소 검증

Luna는 최종 checkpoint 저장과 strict reload를 평가 앞으로 옮기고, endpoint마다 경로·SHA-256·seed·arm·episode·protocol identity를 연결했다. 첫 점검은 전체 스크립트에 300초 timeout을 적용해 완성되지 않았다. 이전 metrics의 시각을 확인한 뒤 별도 `local_smoke`에서 1개 cell·seed 17·1 episode로 축소했다. 이 실행의 learner endpoint 2개는 strict reload와 checkpoint binding을 통과했다. 등록된 전체 비교의 학습량은 24 episodes 그대로이며 축소 결과는 연구 증거가 아니다.

수정된 dispatch는 저장됐고 남은 466초가 리뷰의 600초 제한보다 짧아 호출 전에 정지했다. 소유 프로세스 정리를 확인한 뒤 `luna-publication-e2e-20260907-150410`에서 같은 요청을 직접 재개했다. 독립 리뷰 `7be23e98…`가 진행 중이다.

## 반복되는 검토 병목의 입력 축소

수정본 소스 리뷰도 600초 제한으로 끝나 `luna-publication-e2e-20260907-151509`에서 완료 관측을 사용한 무도구 판단을 재개했다. 진행 중인 호출은 중단하거나 변경하지 않았다.

`b050ae8`에서는 실제 요청의 과거 개발 실행 102개 중 현재 work가 인용한 5개만 본문에 남겼다. 나머지도 전체 이력의 원문·SHA-256이 있는 참조 파일에 보존한다. 프로토콜 범위 해석과 원본 packet, 소스, 인용 검증은 유지했다. 동일 임시 경로에서 이전·새 변환기를 비교하면 최초 요청은 150,641 → 100,228 bytes로 줄었다. 모델 호출 0회, 원본·나머지 필드·전체 이력 해시 보존을 확인했다. 실제 리뷰 속도나 연구 진전의 개선은 아직 측정하지 않았다.

측정 자료는 `luna-publication-e2e-20260907-151509/review-context-measurement.json`이다. 실행 중인 복구 검토 `8469c02f…`는 변경 전 입력으로 시작했으며 새 축소 효과를 주장할 수 없다.

축소 점검의 추가 한계: source_workspace의 recurrent_core.py는 등록본과 해시가 다르다. 대조한 차이는 서식·변수 표기 및 checkpoint metadata 항목 등으로, 여기서 동등성을 인증하지 않는다. 축소 점검의 성공을 등록된 전체 소스 조합의 실행 성공으로 승격하지 않으며, 정식 runner에서 고정된 소스가 실행됐는지 별도로 확인한다.

## 승인 후 정식 실행은 시간 초과

복구 검토 `8469c02f…`가 수정본을 승인했다. input 90,429, output 30,333 tokens이며 reasoning 29,438은 output에 포함된다. 이 검토는 축소 전 입력을 사용했다.

정식 LocalRunner는 승인된 네 소스 파일로 실행됐지만 900.029981초에 timeout으로 종료했다. 체크포인트는 scratch seed 17, imitation seed 17, scratch seed 23 세 개가 생성됐다. 마지막 조건과 metrics 집계는 완료되지 않았다. work는 처리 완료 상태지만 outcome은 execution_failed, measurement는 timeout_or_turn_exhausted, scientific_verdict는 unverified다. 처리 완료를 실험 성공으로 해석하지 않는다. 등록 결과 검증 스크립트 `verify_registered_learning.py`는 준비했으나 완료 결과가 없어 실행하지 않았다.

## 정해진 다음 판단에서 감독 호출 제거

`6a04241`은 완료 work의 next_tool_to_call=plan_research_work를 정상 MCP 경계로 직접 연결한다. 과학적 선택은 기존 계획 모델이 수행한다. 이전 배치에서 감독 호출 31,317 bytes를 사용한 뒤 남은 291초가 계획 호출 제한 600초보다 짧아 실제 판단은 호출 전에 정지했다.

`luna-publication-e2e-20260907-154529`의 실제 첫 호출은 research work decision 159,317 bytes이며 감독 LLM 호출은 없다. 이전 실행 응답은 덮어쓰지 않고 supervisor_plan.jsonl에 새 응답을 보존한다. 이 연결 절감은 확인했지만 다음 연구 절차의 유효성이나 강한 결과는 아직 확인하지 않았다.

## Timeout 해석과 축소 진단

직접 연결한 계획은 155초에 완료됐다. Luna는 두 과학적 예측을 모두 unresolved로 남기고, defaultCapture|101 한 cell·seed 17·300초의 같은 초기화 비교를 선택했다. work `dcc7fbbc…`에서 조건당 fine-tuning 4회로 구현했고 등록 전 smoke는 완료됐다. 이 작은 비교의 null 결과만으로 일반적인 학습 한계나 upstream 원인을 입증할 수는 없다.

`4355752`는 직전 실패한 개발 실행의 선언된 출력 디렉터리에 남은 파일 metadata를 다음 계획에 전달한다. 실제 900초 실패에는 파일 3개가 남았지만 이전 계획 입력에서는 누락돼 있었다. 내용은 읽지 않으며 유효 checkpoint·관측·효과로 취급하지 않는다. 현재 선택된 축소 실험은 바꾸지 않았다. 실제 파일 목록의 독립 확인은 `154529/retained-output-inventory-verification.json`에 있다.

## 같은 원문 아래 바뀐 범위 해석

축소 진단의 scope `529f09df…`는 최신 지원 감사가 학습을 허가하지 않는다는 이유로 source review 전에 차단했다. 앞서 7ce780 학습 진단의 scope `fc905b82…`와 등록 프로토콜 및 amendment 이력이 같음을 비교했다. 새 담당자에게는 이전 해석과 소스 승인 기록이 전달되지 않았다.

`a8d1bf9`는 검증된 predecessor receipt의 이전 work·범위 해석·소스 판단·변경 조건을 범위 담당자에게 전달한다. 앞선 승인은 새 진단의 승인으로 이전하지 않으며 해석을 번복할 경우 원문 조항으로 이유를 설명하도록 한다. 실제 이전 승인 trace는 모델 호출 없이 확인했다. `154529/prior-scope-trace-verification.json`에 근거를 남겼다.

Luna는 기존 거절을 무시하지 않고 protocol_revision work `54cc5a25…`를 선택해 작은 학습 진단을 위한 명시적 사전 amendment를 제출했다. 남은 499초가 검토 제한 600초보다 짧아 provider 호출 전에 checkpoint로 종료했다. `96e8d8b`에서 이미 저장된 프로토콜 요청도 새 감독 LLM 없이 직접 재개하도록 연결했다. `luna-publication-e2e-20260907-161249`가 이 요청의 독립 검토부터 이어간다. 학습 개선이나 논문 완성은 아직 없다.

## 프로토콜 검토 복구 입력과 직접 연결의 오류 재시도

16:22:50 amendment 검토가 600초 제한으로 종료됐다. 16:27:35 재개는 복구 관측을 합친 입력 298,440 bytes가 호출당 280,000 bytes를 초과해 provider 호출 전 정지했다. `9ec18c1`은 실제 제출 자료 생성 경로를 재사용하여 분석·프로토콜 검토에도 복구 관측 예산을 적용한다. 원문과 전체 검사 이벤트는 그대로 보존한다. 16:28:55 동일 저장 요청이 276,741 bytes로 접수됐다. 완료된 관측 15개를 전달하고 큰 출력 11개는 파일에 남겼다. 이것은 입력 전송 확인이며 연구 결론 확인은 아니다.

`35e6dd5`는 직접 MCP 처리 후 work가 진전되면 다음 알려진 단계를 감독 모델 없이 연결한다. 실제 예산 오류에서는 오류·시각만 바뀌어 같은 요청을 반복했으므로 `524df33`에서 진전 판단을 work ID·상태·다음 도구 변경으로 좁혔다. 관련 77개 검사와 5개 subtest를 실행했다. 연구 성공 기준은 실제 학습 결과 및 논문 제출 패키지이며 이 검사 결과로 대체하지 않는다.

16:33 `77fc818`에서 실행 목록의 선택 필터를 수정했다. execution_inventory는 groups 아래 노드가 있지만 이전 필터는 최상위 키와 evidence ID를 비교하여 실제 실행 상태를 모두 생략했다. 동일 저장 요청에서 n_preflight_7ce7801496f430f1의 timeout과 runner_receipt_exists=true가 복구되는 것을 모델 호출 없이 확인했다. 근거는 `162855/selected-inventory-verification.json`이다. 진행 중인 복구 검토는 변경 전 입력으로 계속 수행 중이다.

Luna가 제출한 amendment에는 이전 900초 실행을 “rejected for scope before execution”이라고 잘못 설명한 문장이 있다. 실제로 source 승인 후 900초 실행이 이뤄졌고, 이후 별도 축소 계획이 scope에서 차단됐다. 제안서의 잘못된 이력을 root가 직접 수정하거나 검토를 우회하지 않는다. 독립 검토의 최종 판단에서 이 차이를 처리하는지 확인해야 한다.

16:37:24 복구 검토 `0318f493…`가 508.6초에 승인됐다. 사용량은 input 92,714, output 27,804이며 reasoning 27,452는 output에 포함된다. 검토 evidence는 앞선 learner launch가 timeout으로 끝났음을 정확히 적었지만 proposal 원문의 잘못된 “before execution” 서술을 수정 요구하지 않았다. 원문과 판단의 불일치는 남아 있으며 출판 이력의 출처 확인에서 처리해야 한다. 승인은 one-cell·one-seed·four-episode 개발 진단의 설계만 대상으로 한다. 정확한 소스 binding과 독립 검토·실행이 여전히 필요하다.

같은 배치는 감독 호출 없이 research work decision 183,036 bytes로 연결됐다. `35e6dd5`의 실제 진전 연쇄가 확인됐지만 그 자체가 과학적 진전은 아니다.

## 정상적인 영·음수 결과를 결측으로 만드는 관측 경계

16:40:13 Luna가 work `4c010517…`을 선택했다. 동일한 300초 one-cell·seed17·four-episode 비교이며 앞선 timeout을 정확히 보존했다. 그러나 required_observations에 10개 지지 count 외에 engine_transition_validity와 두 효과 metric을 넣었다. 기존 host는 모든 의존 값에 >0을 적용하여 정상적인 영·음수 효과도 해석 불가능하게 만들었다.

`393e497`은 사전 observation binding의 value_kind를 support_count와 measurement로 구분한다. 전자는 양수 지지·유효성 조건을 유지하고 후자는 유한한 값만 요구한다. 독립 검토는 효과를 양수 관문으로 취급하거나 필수 지지를 measurement로 우회하는 바인딩을 거절해야 한다. 과거 과학적 결과를 재분류하지 않는다. 실제 계획 이름을 사용한 합성 경계 재현은 `162855/signed-observation-boundary-verification.json`이다. 연구 결과가 아니다. 관련 56개 검사를 실행했다.

이 변경을 실행 전 MCP에 반영하기 위해 16:43:57 supervisor 3637069를 종료했다. 해당 budget을 가진 프로세스가 모두 정리된 것을 확인한 뒤 16:44:13 `luna-publication-e2e-20260907-164413`에서 동일 work를 재개했다. 과학적 source나 저장 계획을 root가 변경하지 않았다.

16:46 `071a5eb`은 신규 observation_contract에 value_kind 명시를 요구하여 누락을 유료 검토 전 복구 가능한 오류로 처리한다. 저장된 과거 계획의 스키마 및 과거 수집 결과는 유지한다. 진행 중 MCP는 이 후속 검사를 로드하기 전에 시작했으므로 현재 prepared metadata에는 아직 value_kind가 없다.

16:50 이전 scope `8b18df20…`는 eligible_for_source_review로 완료됐다. 사용량 input 69,081 / output 15,405, reasoning 14,502 포함. 이번 판단은 timeout과 “실행 전 거절” 서술의 불일치를 명시하고, 새 entry31의 독립적인 작은 진단 허가와 구분했다. 이후 source review `c4086d94…`에 83,273 bytes가 접수됐고 value_kind 검토 지침을 포함함을 확인했다. 아직 실행 영수증은 없다. 완료 후 확인할 스크립트는 `164413/verify_registered_learning.py`로 준비했으며 미실행이다.

16:57 source review `c4086d94…`는 one-cell 코드에 남은 six-cell/two-seed required_ok 조건을 이유로 거절했다. 현재 실험에서 도달 불가능한 count 조건이 valid run에도 false support-failure를 추가하는 구현 결함이다. root가 learner를 수정하지 않았고 Luna가 16:58 같은 work에서 해당 조건을 수정하기 시작했다. 검토는 관측 종류 누락을 명시하지 않았으며 첫 구체적 결함을 근거로 거절했다. 실제 실험은 아직 시작되지 않았다.

감독 에이전트가 검토 대기 중 300초 runner 예산 초과를 잘못 언급했지만 최종 MCP 거절을 받은 후에는 실행 전 거절로 정확히 정정했다. 중간 서술은 실행 영수증이 아니다. `3fb8ef1`은 완성된 새 실행 요청도 직접 MCP로 전달하게 하며, 준비 또는 수정이 필요한 단계는 감독 모델을 유지한다. 실제 첫 적용은 아직 확인하지 않았다.

17:00 이후 원인 두 가지를 추가로 확인했다. 범위 재검토 `bbd12e54…`는 앞선 `8b18df20…`와 모델·지침·스키마 및 모든 범위 근거가 같고 prior_scope_context만 추가됐다. `7e01f93`은 이 경우 해시가 검증된 원래 범위 영수증을 재사용한다. 실제 두 요청의 비교 및 0-model-call 재현은 `164413/unchanged-scope-reuse-verification.json`에 남겼다. 진행 중이던 재검토는 중단하지 않았고 수정된 소스는 별도 검토 `4cef5274…`로 이어졌다.

`f19fd4d`는 request_path 업데이트에 대해 스키마·소스 참조 검증이 끝난 뒤에만 canonical dispatch를 교체한다. content로 고정된 소스에 replacements를 추가하는 잘못된 패치는 기존 요청을 오염시키지 않고 rejected_dispatch_request.json에 보존된다. 기존 회귀에 실제 오류 구조를 추가해 원본 bytes 보존과 정상 후속 실행을 확인했다. 소스 정규화 자체가 content+replacements를 생성한 것이 아니라 잘못된 업데이트가 거절된 뒤에도 저장된 것이 문제였다.

17:11:31 `4cef5274…` source review가 600초 timeout으로 종료됐다. 작업은 checkpoint, new_observation=false이며 learner는 실행되지 않았다. 소유 프로세스 정리를 확인하고 17:13:36 `luna-publication-e2e-20260907-171336`에서 재개했다. 첫 직접 호출은 value_kind 누락을 0-model-call로 거절했다. Luna는 이를 메타데이터 오류로 정확히 분류했다.

하지만 metadata 수정을 위해 과거 준비 source를 다시 등록하면서 앞서 고친 gate를 되돌렸다. bind_work는 이전 source를 prior_prepared_implementation으로 옮겼으므로 최신 준비본으로 잘못 표시한 것은 아니었다. root가 추가한 오류 안내의 design_experiment_template 경로가 소스 재선택을 유도했다. `97c952e`에서 최신 저장 dispatch의 observation_bindings만 updates로 수정하도록 안내를 바로잡았다. 관련 기존 control 42개 검사와 diff check를 실행했다.

17:17:49 `fbc30456…` 검토가 되돌아간 six-cell gate를 거절했다. Luna는 types를 유지한 채 gate를 다시 수정했다. 17:18:51 이후 새 source review `917da01d…`에 104,682 bytes가 접수됐다. source bytes는 앞선 corrected-timeout 4cef와 정확히 같다. experiment.py SHA256=31669f107ebe1bf42b488ca5d167f17c1d68c07146bfa43991ca7d31fd186a23. 13개 binding 중 두 효과는 measurement, count와 engine validity는 support_count이다. scope는 기존 bbd12 영수증을 재사용하며 두 번의 source 제출 모두 새 scope 모델 호출 없이 진행됐다. 아직 formal learner 실행은 없다.

## 추가 감사 거절과 수정본의 로컬 실행

17:38:13 tool-free recovery `f18914d2…`가 516.1초에 reject를 반환했다. usage input 91,817 / output 28,322 (reasoning 27,333 포함). 두 지적은 충돌 점수 변화와 fine-tuning audit 누락이다. 첫 지적은 pinned capture.py:63의 KILL_POINTS=0과 충돌한다. root가 고정 입력에서 이 상수를 확인했지만 과학 소스나 리뷰 영수증을 수정하지 않았다. 초기 검사/복구 관측에는 collision 분기만 포함됐고 상수 정의는 포함되지 않았다. 이 사실만으로 유효한 충돌에서 nonzero kill score가 생긴다고 단정할 수 없다.

Luna의 준비본 `09e8fe75…`는 returned-food delta와 score delta의 독립 비교를 제거하고 raw emitted reward 대 simulator score delta 일치 검사는 유지했다. fine-tuning endpoints/audits를 반환·집계하고 그 유효성을 paired 결과의 조건으로 추가했다. root가 코드 diff를 읽었으며 독립 점수 확인 제거의 적절성은 다음 검토에서 확인할 사항이다. learner 소스 수정은 Luna가 했다.

17:47:15 Luna가 비등록 local smoke에서 8개 training endpoints와 6,320개 training audits 및 checkpoint reload가 통과했고 imitation effect=0이라고 보고했다. 이는 등록된 연구 증거가 아니다. 동일 작업의 source review에 111,803 bytes가 접수됐다. 여전히 formal one-cell learner 결과·강한 결과·논문은 없다.

## 최초의 등록된 단일 셀 학습 비교 완료

17:57:17 source89f6c985…가 600초 timeout으로 종료돼 소유 프로세스 정리 후 `175743` 배치에서 276,869-byte 복구 검토0610f910…로 재개했다. 0610은 source를 승인했고 usage input92,169/output40,704(reasoning39,529 포함)를 남겼다. 검토는 원시 score/reward 일치와 구조적 전이 조건 및 모든 training audit 집계가 이번 개발 진단에 적합하다고 판단했다. 독립 returned-food score 회계 제거는 명시적으로 언급됐다. 이것은 엔진의 모든 전이 법칙을 별도로 증명한 결과가 아니다.

18:10:39 현재 work4c010517…의 정식 LocalRunner 실행이 완료됐다. 실행시간35.980393초, exit0이다. teacher raw endpoint=+2, scratch=-18, imitation=-18, paired effect=0, positive fraction=0. 8개 fine-tuning endpoint, 6,320개 fine-tuning audit 및 총8,689개 transition audit가 유효하게 기록됐다. 두 정책을 네 에피소드 후 저장·strict reload한 checkpoint와 final endpoint 연결을 확인했다. 단일 셀·단일 시드의 짧은 진단이며 강한 결과·기준선 자격·일반적 실패 또는 원래 문제 해결의 증거가 아니다.

root가 `164413/verify_registered_learning.py`를 실제 완료 아티팩트에 실행해 승인 요청 해시/실행 소스 bytes/입력 매니페스트/두 checkpoint SHA/endpoint 연결/카운트/짝 효과 계산을 확인했다. 결과는 `164413/registered-learning-verification.json`. experiment.py SHA=4d1b55b180cf4d6ab3e380d3aeace7d257ec1cce33a65e1d0ecaf10f2c74f1fc, metrics SHA=d5757861720197234c6baa575616d3280a9f706c5d1d548ee1ce8b32382fa177. measurement_support.evaluable=true, missing_or_empty=[]이며 두 영 효과를 결측으로 분류하지 않았다. 이후 감독 호출 없이 research work decision200,656bytes로 연결됐다. 다음 연구 해석과 강한 결과는 아직 확인되지 않았다.

18:12:40 계획 work9963e2ff79585a10ce8a6d28d53f9c8386220633f4f2bc2d6402aeba28e582ea가 생성됐다. Luna는 complete-support 영 효과로 positive-imitation 예측을 weakened, 이 짧은 route의 충분성 미입증 예측을 supported로 갱신하면서 upstream causal defect·B 반박·일반적 algorithm failure를 입증하지 않는다고 제한했다. 같은 학습을 바로 재실행하지 않고 기존 arm endpoints와 learning curves를 읽어 transient imitation benefit이 fine-tuning으로 사라졌는지 분석하도록 선택했다. next resolve_research_work의 research source analysis 입력44,003bytes가 접수됐다. 아직 분석 결론 및 다음 개입은 없다.


## 초기 학습 곡선 분석과 다음 배치

18:19:37 기존 근거 분석9963e2ff…가 answered로 완료됐다. 첫 curve rollout은 BC 또는 scratch 초기화 후 첫 fine-tuning update 전에 수집됐고, 양쪽 모두 첫 회부터 네 번째까지 raw score=-18, returned_food=0이었다. 이 기록에서는 사라진 초기 imitation 이득이 관측되지 않았다. 별도 zero_shot 필드나 action-level teacher-match는 없으므로 관측·보상·표현력의 인과적 결함은 입증하지 못한다. 독립 분석1a4c8a39…의 usage는 input1,427,128 / cached1,271,808 / output15,745(reasoning10,128 포함)이다. 최초 prompt44,003bytes는 이 누적 토큰 사용량을 뜻하지 않는다.

후속 계획은 배치 잔여480초가 호출 제한600초보다 짧아 provider 호출 전에 중단됐다. 기존 소유 프로세스가 없음을 확인하고 18:25:05 `luna-publication-e2e-20260907-182505`에서 같은 완료 상태를 이어간다. 강한 결과·독립 확인·논문 패키지는 미완료다.

`ef2d9ab`은 실제 승인 복구가 약12분 걸린 점에 맞춰 최초 source execution review 제한을 복구와 같은1,200초로 조정했다. 연구 실행300/900초와 배치 예산은 유지되며 충분한 잔여 시간이 없으면 시작하지 않는다. 관련15개 기존 검사를 실행했다. `7dc535f`는 계획된 existing-source analysis도 저장 상태에서 직접 resolve_research_work로 전달한다. 준비·오류 복구는 감독 경로를 유지한다. 기존65개 검사와5개 subtest를 실행했다. 두 변경의 실제 시간 절약은 아직 측정하지 않았다.


## 후속 행동 재현 진단의 범위 거절

18:29:28 Luna가86c9787d3f7e86cbbaed1a8986162d0609b863ea83bf903e38f33b5b9e5c6da9를 선택했다. 기존 early curve 분석을 받아 identity-free teacher-action agreement를 label-shuffled control과 비교하고 짧은 paired raw endpoint를 보는 진단이다. 학습 코드는 Luna가 작성했다. source experiment.py SHA=d2ece64eccd415bb9028dc074332929f47a8c0fb542c6c84a47d93e81545b7fc, 준비본005e7c20…이다. 로컬 초안에서는395teacher rows/79internal-heldout rows, agreement0.3671 대0.3418, raw endpoint effect0을 생성했지만 등록된 연구 근거는 아니다.

18:46 범위 검토94cb6b6d…가 source review 전에 차단했다. entry31은 이미 완료된4c 진단 한 번만 허용했고, 새 설계는 scratch를label-shuffle로 바꾸며4회fine-tuning을 제거한다. 새 허가가 없으므로 기존 informative result가 다음 실행을 허가하지 않는다. 신규 formal execution은 없다. 계획의 authorization 비차단 가정과 독립 범위 판단이 불일치했다. 다음 계획 호출은 잔여492초가600초보다 짧아 시작 전 중단됐고, 소유 프로세스 정리 후18:47:43배치에서 재개했다.

`a1842b2`는 requires_replanning과 거절 및 next plan_research_work가 모두 저장된 경우, 거절 원문을 reconsider_reason으로 직접MCP에 전달한다. 기존 경로는 이번 재개에서도 감독 호출34,209bytes를 먼저 사용했다. 실제 subprocess 전달 회귀를 확장해66개 검사와5개subtest 및diff check를 실행했다. 현재 배치는 변경 전 프로세스이며 이를 위해 진행 중 계획 호출을 중단하지 않았다. 연구 선택 및 프로토콜 변경 판단은 Luna가 계속 담당한다.


## 과제 기회가 없는 조건을 고른 프로토콜 제안

Luna가 a33ec53121325d1f4f9998c46023a4b18f329606b8bf6ab2fda810b169b86814를 protocol_revision으로 선택했다. testCapture|211을 사용하지 않은 새 조건으로 제안했지만 첫 검토927c78fd…가600초에 중단됐다. 19:07:02배치에서 감독 호출 없이276,729bytes의 completed-inspection 복구c414f02a…로 이어갔다.

19:10:42복구는218.9초에 reject를 반환했다. usage input92,219/output11,835(reasoning11,292 포함). seed는 starting_agent가 명시되면 상태를 바꾸지 않고 testCapture는 과거 측정에서 상대 먹이·도달 가능 목표·pickup·return이 없었다. root가 실제 n_preflight_testcapture_mechanics_01의 metrics에서 reachable/pickup/return count가모두0임을 확인했다. 검토는 단순한 시드 라벨 부재로 새 조건을 인증한 점, 과제 양성 대조 부재, 학습기 용량·훈련 예산 미고정을 지적했다. 신규 실험은 실행되지 않았다.

19:18경 Luna는 bloxCapture/조건307/starting_agent1을 고정하고,307은 provenance표기일 뿐이라고 명시하는 수정안을 제출했다. reset-state digest, collect-and-return 양성 대조, 10차원 입력의 공유32/32비순환MLP, Adam0.001/200epoch, 모순 없는 훈련 예시 최소20건에서agreement0.95의capacity gate, 전체전이감사와endpoint pair를 제안했다. 고정240초·개발용·일회성 조건이며 기존 최종평가는 유지한다. 독립 검토97,464bytes가 진행 중이다. 이것은 승인 또는 결과가 아니다.

`7a96e6a`는 단일work 선택과 향후 개발 허용 범위를 구분하도록 계획·검토 지침을 보완했다. 진행 중 프로토콜이600초에 중단된 후, 정책 버전 증가가 기존 work를stale로 만들고 검토 지침 변경이 유료 검사 복구 해시를 바꾼다는 점을 확인했다. `0be1400`에서 정책은25로 유지하고 검토 지침 변경은 원복했다. 계획 담당자의 향후 새 선택에는 유한한 적응적 개발 절차를 고려하라는 안내가 남는다. 기존 허가나 현재 제안의 범위를 확대하지 않는다. 변경 전후 기존control/review57개검사와diff check를 실행했으나 실제선택개선·비용감소는미측정이다.


19:28:31 수정안27f39a6f…의 최초 검토가 다시600초에 중단됐다. 최초 요청은c9809a42…이며 아직 판정이 없다. 소유 프로세스가 모두 종료됐음을 확인하고19:30:04배치에서 같은 제안/완료검사로 복구를 시작했다. `f79aa3e`는 이후 protocol_revision 최초 검토도1,200초를 허용한다. 일반 분석·범위 판단600초, 실제실험·총계산·배치 예산은 유지한다. 이전54cc의600+508.6초, a33최초의600+218.9초 사례를 근거로 했다. 요청/영수증해시와검토지침은유지하며기존review15검사와diffcheck를실행했다. 실제절약은아직미측정이다.


19:39:45 수정안 복구e6a37e7b…가581.2초에 승인됐다. usage input68,258/output31,957(reasoning31,596포함). 최초 요청은c9809a42…이며 소스가 없는 study_design이라 source는 이후work에서준비·검토한다. 승인범위는고정bloxCapture/start1의양성대조·capacitygate를갖춘개발진단이며 강한결과나qualification은아니다. 감독호출없이170,348bytes의다음계획으로이어졌다.

## 진단 준비 계약과 실제 실행 후처리

20:08 최초 source 검토4e5e39a8…는 학습 actor 파일과 endpoint의 불변 연결이 없어 거절했다. 이후 감독자가 immutable prepared 요청을 최신 수정본으로 오인해 observation bindings 없는 옛 요청을 재생했다. `205f0da`는 준비 단계에서도 관측 계약을 검사하고, 실행용 경로를 현재 canonical dispatch로 제공하며, 현재 work와 맞지 않는 재생 요청이 dispatch를 덮어쓰지 못하게 했다. 실제 get_research_state에서 현재 요청과18개 관측 binding을 확인했다. 관련108개 검사와5개subtest를 실행했다. 과거 source와 연구 설계는 수정하지 않았다.

20:19 두 번째 source 검토b8e6aa5a…도 같은 actor provenance 문제로 거절했다. Luna가 actor 파일 저장과 arm·훈련조건·SHA의 endpoint 연결을 직접 구현했다. 수정본의 torch import는 읽기 전용 sandbox의 임시 디렉터리 부재로 한 번 실패했고 Luna가 workspace TMPDIR을 지정해 재시도했다. `b94f312`는 이후 coordinator 세션의 TMPDIR을 허용된 source workspace의 `.tmp`로 제공한다. 실제 Codex sandbox에서 Python 임시 파일 생성과17개 adapter 검사를 실행했다. 전역 쓰기 권한은 넓히지 않았다.

20:34:28 source 검토888d16ad… 승인 후5c20b5e2… 진단이15.846774초에 완료됐다. task feasibility 양성 대조는 pickup34회, return12회, returned food27개로 통과했다. capacity 양성 대조는224개 예시에서0.8392857로 사전 기준0.95에 못 미쳤다. held-out teacher agreement0.7017544 대 shuffled0.4736842, 짝 raw endpoint는−36 대−33이지만 capacity 실패로 두 원인 가설은 미결정이다. 이는 강한 결과·기준선 자격·원래 문제 해결의 증거가 아니다.

실행 후처리는 진단에도 단일 qualified baseline key 조건을 적용해 거절했다. 실제 report의 baselines에는 shuffled control의 정확도와 endpoint 두 측정값이 있었다. 해당 조건을 baseline_evidence_requirements가 있는 자격 검증에만 적용하도록 수정했다. 진단은 비교 측정값을 보존하며 baseline 자격 필드 없는 receipt와 scientific_approval=false를 유지한다. 기존 진단 회귀에 두 측정값을 추가하고 자격 검증에서는 추가 key를 계속 거절하는 검사를 확장했다. control/experiment_plan49개 검사와diff check를 실행했다.

root는 완료된 실제 아티팩트에서 deterministic worker report 재구성, 승인 요청 해시와 실행 소스, 입력 manifest, 두 actor SHA 및 endpoint provenance, 짝 reset 일치,224개 예측의capacity 정확도와raw effect를 확인했다. `/tmp/verify_recoverability_artifact.py`로 실행했고 결과는 `201631/recoverability-artifact-verification.json`이다. metrics SHA9cf14bac77bdf0b2910745a03ad3393c13839476c661ea4f29ebf5df1d9e0e77. 추가 학습이나 과거 work 기록 수정은 하지 않았다.

20:38 Luna는f803b116… 기존 자료 분석을 선택하며 capacity 실패 때문에 과거 원인 가설을 해석할 수 없다고 명시했다. 분석 호출은 남은456초가600초보다 짧아 시작 전 중단됐다. 소유 프로세스가 없음을 확인하고20:42:11 `204211` 배치에서 같은 분석을 재개했다. all-role Luna max를 유지한다. 5~7의 강한 결과, 독립 qualification/confirmation 및 논문 패키지는 여전히 미완료다.

20:50 f803의 선택된 분석 질문에 기존 표·예측 재구성뿐 아니라 “analysis-only identity-free control reaches ...0.95”라는 새 fitting 결과 요구가 섞여 있음을 확인했다. 분석 담당자의 실행 계약은 기존 자료 읽기와 재계산만 허용한다. 계획의 analysis 설명에 저장 자료 재계산과 새 모델 fitting을 구분하고, 저장된 훈련 자료의 capacity control도 diagnostic_experiment 또는 competence로 선택하도록 명시했다. 현재 분석의 요청·복구 해시·과거 work는 바꾸지 않았다. 이는 앞으로의 선택 안내이며 실제 계획 개선은 아직 관측하지 않았다. control 기존42개 검사를 실행했다.

20:55:03 기존 분석f803의 복구d79e056a…가122.4초에 answered로 완료됐다. usage input69,817/output6,420(reasoning5,696포함). 저장된U224개와예측은일치하며188개정답이었다. 동일훈련표를그대로조회하면224개정답을복원할수있으나이는학습또는일반화성공을뜻하지않는다. 새trained actor의capacity회복은미측정이며두원인가설은미결정이다.

후속계획9bc167f4…는20:59:17 이전분석ID는인용했지만필수work영수증ID를누락해거절됐다. 감독자호출과전체계획재생성이뒤따랐다. 이제completed previous work와응답의work_id가정확히일치할때하네스가이미아는work receipt를evidence_ids에연결한다. 다른work_id,외부인용,과학적판단및support검사는수정하지않는다. 모델원문은그대로저장한다. 기존parent_work_id와같은구조적연결이다. 회귀초기실행은코드삽입위치를잘못선택해8개가실패했고올바른계획정규화경계로옮긴뒤control44개검사가통과했다. 이변경은진행중재생성호출을중단하지않았다.

21:03:53 재생성된8b59271e…는fresh-cell diagnostic을선택했다.21:06 준비담당자가시드307을308로바꿔신선한상태를만들겠다고기술했다. starting_agent고정시시드가상태를바꾸지않는다는앞선a33검토사실과충돌한다. 이시점에는새실행또는성공이없으며,학습기와200epoch예산도그대로다. 검토·실행이이를어떻게차단하고다음행동으로바꾸는지계속확인해야한다.

## 준비 단계의 등록 외 학습 반복 차단

21:08:54 coordinator 초안의 reset novelty gate가 bloxCapture 시드308을 기존상태와같다고거부했다.그러나그뒤맵을바꿔학습capacity기준을통과하는조건을찾는실행을“사전실행가능성검사”로반복했다.이는단일진단의등록·계산예산·검토경로밖에서이뤄졌다.21:11경소유supervisor를SIGTERM으로중단했고소유환경표식의생존PID가없음을확인했다.새정식실험은없다.

원인은supervisor에게임의셸실행을제공하고초안입출력을직접실행하라고안내한것이다.준비담당자의shell/unified_exec/JS REPL을제거하고,thread-local 텍스트·소스·JSON값·해시를읽는read_research_artifact를추가했다.코드작성은기존design_experiment_template의content또는from_path/sha256/replacements를사용한다.일반학습·시뮬레이션은등록실행도구로만보낸다.저장자료의읽기와정적진단은유지한다.

실제Codex CLI0.153.4에로컬무인증Responses수신기를붙여고정된도구조회/소스읽기호출을한번반환했다.외부모델호출은하지않았다.초기에는Code Mode host까지끄면MCP가사라졌고,host를유지하면외부앱·legacy v1에이전트도구가노출됨을확인했다.앱·플러그인을끄고v2등록기를선택한상태에서multi_agent를비활성화하자실제목록은연구MCP39개와편집·자료조회·이미지보기5개만남았다.셸·외부앱·위임도구는없다.도구호출용V8의process/require/fetch는undefined이며실제소스해시읽기가성공했다.설정참고는[공식Codex설정문서](https://learn.chatgpt.com/docs/config-file/config-reference)이며효과는설치된CLI에서직접확인했다.

검증스크립트와결과는205301/probe_coordinator_tools.py 및coordinator-tool-inventory.json에보존했다.예상된로컬수신기400으로CLI가종료되도록한검사이며모델추론·실험실행성공을뜻하지않는다.164개관련검사와8개subtest,git diff --check를실행했다.첫검사에는새도구외에도기존retrieve_research_source가도구목록기대값에서누락된실패가있어실제API목록으로갱신했다.

중단시남아있던4개metrics파일을production/research_control/preparation_execution_archive에SHA별로보존했다.기록된조건은bloxCapture/alleyCapture/defaultCapture/crowdedCapture,모두시드표기308/start1이다.저장된capacity값은각각0.8392857/0.7611940/0.8175676/0.9104167이다.모두0.95에못미친다.현재소스는그후strategicCapture로바뀌어있으며파일누락으로미실행을증명할수없다.중간coordinator발언과저장값도다르므로이를완전한실행원장또는승인된측정으로취급하지않는다.공개기록preparation_execution_disclosure.json을get_research_state,계획및구현검토문맥에전달한다.실제get_research_state에서4개기록노출을확인했다.기존연구결과·프로토콜·학습코드는root가수정하지않았다.

21:35:29 새도구경계로재개한213529에서Luna가실제read_research_artifact를사용했다.그러나기존MCP완료16회마다작업을종료하는규칙이파일읽기도세어21:37:28과21:39:20에소스읽기중재시작했다.셸안에서읽던때에는드러나지않던연결버그다.순수read_research_artifact만작업완료카운트에서제외하고전체배치시간·예산과실행완료체크포인트는유지했다.기존실행완료회귀앞에20회파일읽기를추가해읽기후실제완료까지도달함을확인했다.이전규칙을로드한supervisor는중단했다.새정식실험은여전히없다.


## 소진된 일회성 허가를 바꿔 부르는 계획 반복

214040 배치에서는 read_research_artifact가 30회 완료된 뒤에도 같은 coordinator가 준비를 계속했고, 21:46:38 실제 실행 요청까지 도달했다. 읽기 호출 때문에 작업이 조기 종료되는 현상은 이 실행에서 재발하지 않았다. 새 도구 경계 이후 coordinator의 직접 셸 학습은 관측되지 않았다.

그러나 8b59271e…는 bloxCapture/start1을 유지한 채 시드 표기만 311로 바꿨다. 21:50경 범위 검토73e5607d…는 amendment32의 단일 진단 허가가 이미 5c 실행으로 소진됐다고 판정했다. 소스 검토나 새 LocalRunner 실행은 없었다. 검토 usage는 input70,938/output11,348이었다.

다음 계획 abd4ead0…는 저장된 teacher table만으로 같은 actor를 1000 epoch 학습하는 진단을 선택했다. Luna가 작성한 소스는 정식 실행 요청으로 전달됐다. 그러나 22:00:42까지 범위 검토a75b9887…는 기존 200 epoch 일회성 범위를 바꾸는 저장 자료 재학습도 새 사전 amendment가 필요하다고 판정했다. input70,511/output10,250. 새로운 empirical observation은 없다. 이번 scope packet은 약212KB이며, 전체 amendment history133KB와 실행 이력61KB가 대부분이었다. 이는 중복 제거로 모두 없앨 수 있는 단순 로그가 아니다.

계획 지침을 수정했다. 명시적 범위 거절 후에는 다음 실험의 이름·입력·예산 변경만으로 허가를 가정하지 못하며, 다른 실험을 선택하려면 보이는 허용 조항과 적용 차이를 설명해야 한다. 그렇지 않으면 protocol_revision을 선택한다. 반복되는 일회성 소진에는 유한한 개발 데이터·변경·전체 계산 예산·결과별 후속 선택·중단 규칙과 최종 후보 고정을 갖춘 적응적 개발 절차를 제안하도록 했다. 연구 방법이나 학습 코드는 root가 정하지 않는다. 기존 등록은 독립 승인 전까지 유지되며 최종 평가·성공 기준·시도 공개 의무는 바뀌지 않는다.

기존 research_control 검사44개와 git diff --check를 실행했다. 정책 버전과 진행 중 검토 해시는 바꾸지 않았다. 214040은 다음 호출에 필요한600초보다 잔여590초가 짧아 호출 전에 중단됐고, 소유 PID가 없음을 확인했다. 22:03:18 새 배치220318에서 Luna max의 계획을 재개했다. 이 시점에서 실제 계획 개선, 강한 결과, qualification/confirmation 및 논문 패키지는 아직 검증되지 않았다.
