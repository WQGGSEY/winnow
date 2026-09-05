# 연구 문제에서 제출 원고까지의 경로 감사

확인일: 2026-09-05. 기준 커밋: `4b4f21f`. 사용자는 연구 문제를 다른 세션에서 선정한다. 이 세션의 성공 조건은 **ICML, NeurIPS, ICLR, CVPR, COLT 중 선택한 학회에 제출 가능한 수준의 논문 생성**이다. 이 문서와 아래 수정만으로 그 조건을 달성했다고 판정하지 않는다.

이번 작업은 handoff의 결함 확인, 추가 결함 탐색, 주제 없이 재현 가능한 첫 수정이다. 연구 실험과 live LLM 호출은 실행하지 않았다. 기존 Fashion-MNIST 결과를 새 연구 성과로 재인증하지 않았다. 구체적인 회차별 요구사항은 [제출 요구사항 조사](submission-requirements.md)에 있다.

## 현재 판단

실험 증거 재구성과 종료 상태의 연결은 상당 부분 구현돼 있다. 그러나 연구 방향 생성, 선행연구의 적합성 판단, 독립 검증 실행, 논문의 주장과 증거 연결, 제출 패키지 빌드에는 미완성 경로가 남아 있다. `supported`, 내부 AC `accept`, HTML 생성은 각각 제한된 관찰이며 제출 준비 완료를 대신하지 않는다.

첫 재현에서 기존 `thread_gpu_strong_live`의 원고는 초록 포함 7개 절, 약 933단어였다. 참고문헌 절과 링크가 없고 이미지 3개에 모두 `src`가 없었다. 이 단어 수는 진단용 관찰이며 논문 합격 분량을 뜻하지 않는다. 동일한 결과표 그림이 초록·결과·결론에서 반복됐고, 방법 절은 주로 실행 절차를 설명했다. 문헌 대비 기여와 모델 방법의 설명이 부족한 상태였다.

## H: handoff의 문제 대조

`재현`은 해당 함수나 통합 경로를 실제 실행한 결과다. `코드 확인`은 제어 흐름과 입력·출력 계약을 읽은 결과이며 실제 연구 실행을 뜻하지 않는다.

| ID | 결함과 증거 | 상태 / 필요한 변경 |
|---|---|---|
| H1 | `goal_contract_input_from_artifacts`에 초기 방법을 포함한 operator problem을 넣으면 `GoalContract.question`에 그 방법이 그대로 남는다. `social sentiment as the initial idea`로 재현했다. [goal_contract.py](../../research_harness/orchestrator/goal_contract.py) | 미해결. 목표·불변 제약과 초기 제안 방법을 의미상 분리해야 한다. 방법명을 지우는 정규식으로 대체하면 안 된다. |
| H2 | `GenerationRequest`는 계약과 arXiv 분야 이름뿐이다. generator의 local tools도 꺼져 있어 스스로 문헌을 보충하지 못한다. connector의 실제 reading, correspondence, far-method papers는 production에 연결되지 않는다. [direction_generation.py](../../research_harness/orchestrator/direction_generation.py), [blind_mcp_adapter.py](../../research_harness/orchestrator/blind_mcp_adapter.py) | 미해결. P-blind `PerspectivePacket`의 보존·출처 결합·전달이 필요하다. P-aware claim을 그대로 연결하면 blindness를 깨뜨린다. |
| H3 | `firewall_clean=false`인데 field reading과 baseline research를 수행했다. 통합 테스트에서 field 1개가 실행된 것을 재현했다. [connector/orchestrator.py](../../research_harness/connector/orchestrator.py) | 수정·검증 완료. 잔여 용어가 있으면 `aborted`, fields=0, downstream calls=0. 이것은 현재 용어 스캐너의 경계 강제이며 의미적 무누출을 보장하지 않는다. |
| H4 | baseline이 통과한 report에서 `contradicted`는 `label_only_contradiction`, disproof 문자열은 `unsafe_positive`를 반환했다. [attempt_evidence.py](../../research_harness/orchestrator/attempt_evidence.py) | 미해결. 라벨만으로 닫지 않는 것은 타당하다. 대신 실행된 반증 predicate와 활성 attempt에 결합한 receipt가 없어 측정된 실패를 표현하는 경로가 좁다. |
| H5 | caller가 `needs[].candidates`를 제공하며 acquisition은 순서대로 소진한다. 소진 시 `AcquisitionBlocked`를 반환한다. [blind_mcp_adapter.py](../../research_harness/orchestrator/blind_mcp_adapter.py), [acquisition/service.py](../../research_harness/acquisition/service.py) | 코드 확인, 미해결. 자동 source discovery와 접근·라이선스 검토가 acquisition 앞에 필요하다. URL 목록 재시도는 새로운 출처 발견이 아니다. |
| H6 | 다음 관점은 분야 permutation과 draw index로 결정된다. 실패는 digest와 fingerprint로 기록되지만 유망성·데이터 가능성·비용·미탐색 가족을 함께 판단하는 controller가 없다. [blind_sequential_research.py](../../research_harness/orchestrator/blind_sequential_research.py) | 코드 확인, 미해결. 무기한 재개나 구조적 거리 증가가 발견 효율을 입증하지 않는다. 전용 평가가 필요하다. |
| H7 | positive direction은 prompt/regex, novelty는 LLM equivalence 판정이다. 필수 모듈과 데이터 사용은 source scan이다. success/disproof 문장은 고정하지만 모든 조건과 실행 predicate의 대응은 강제하지 않는다. holdout 계산기는 caller의 observed scalar를, construct 계산기는 caller가 나열한 worlds/budget를 판정한다. [mcp_server.py](../../research_harness/mcp_server.py), [experiment_plan.py](../../research_harness/orchestrator/experiment_plan.py), [falsifier.py](../../research_harness/falsifier.py), [construct_adversary.py](../../research_harness/construct_adversary.py) | 일부 직접 재현, 나머지 코드 확인. 독립 runner·evaluator·데이터 계보와 결과 receipt가 필요하다. 순수 계산기에 입력을 넣어 `passed`/`survived`가 나온 사실은 전체 terminal gate를 우회했다는 뜻은 아니다. |
| H8 | 실제 bootstrap이 `external_falsifier.kind=none`을 만들고 그 결과를 GoalContract compiler에 넣으면 `holdout kind must be real_holdout`으로 실패한다. [thread_supervisor.py](../../research_harness/thread_supervisor.py) | 재현, 미해결. 시작 전 holdout 등록 또는 해당 연구 유형의 증거 계약을 준비해야 한다. 임의 adapter·threshold·분할을 만들어 통과시키면 안 된다. |

## P: 추가로 확인한 결함

| ID | 결함과 영향 | 증거 / 현재 상태 |
|---|---|---|
| P1 | 연구 완료 후 작성 경로가 끊긴다. `GoalAchieved`에는 active attempt가 없는데 `_resolve_promoted_node`가 이를 요구한다. | 실제 증거를 갖춘 테스트 fixture에서 원고 생성이 `the authoritative blind node is not promoted`로 실패. 수정했다. 원고 관련 reader만 현재 검증되는 완료 receipt의 node를 사용할 수 있으며 실험 node의 권한을 다시 열지 않는다. |
| P2 | `render_final_paper`가 selector의 강한 증거 검사에 의존하고 자체 진입점에서는 AC/attestation 라벨만 확인했다. | 코드 확인 후 직접 render 진입점에 검증과 writer lock을 추가했다. 유효한 증거로 렌더링한 뒤 report를 변조하면 거부하고 기존 파일을 보존하는 테스트 통과. |
| P3 | supervisor는 `rendered_artifacts` 목록이 있으면 실제 paper 파일이 없어도 완료한다. 파일·인용·그림·원고 digest는 실험 receipt에 결합돼 있지 않다. | 존재하지 않는 `missing.html`로 `(True, 'accept_with_goal_achieved')` 재현. **미해결.** Publication receipt가 실제 파일 hash와 strong-result digest, 작성 입력을 함께 묶고 terminal에서 이를 검증해야 한다. |
| P4 | sanitizer가 double-quoted attribute만 읽어 서버가 만든 single-quoted 이미지와 링크를 제거했다. 이미 escape된 URL도 다시 escape했다. | 기존 원고의 이미지 3개 `src=None` 및 query URL 변형 재현. 표준 HTMLParser로 수정. 실제 브라우저에서 이미지 3개 로드 확인. |
| P5 | 작성한 `supplementary`를 버리고 내부 state JSON으로 대체했다. 초록을 outline에 넣으면 Introduction이 2번부터 시작했다. | 둘 다 renderer 테스트로 재현·수정. 실제 작성 부록을 보존하고 본문 절만 번호를 매긴다. |
| P6 | 논문 본문에 내부 AC 점수·수정 지시·다음 작업·local state가 들어가며 자동 생성한 pipeline 소개가 impact statement를 대신했다. | 실제 원고 확인. 내부 자료를 companion report로 이동하고 자동 impact 문구를 제거했다. 본문에 작성자가 직접 적은 local path·식별 정보의 익명화는 미해결이다. |
| P7 | 작성 context가 계획·실행 로그·holdout·고정 문제·인용 가능한 논문 목록을 직접 제공하지 않았다. 절의 최소 조건은 200자이고 evidence anchor는 비어 있지 않은 문자열이면 된다. | context에 실제 artifact를 연결하고 작성 지침을 추가했다. final render에 초록·참고문헌 절을 요구한다. **인용·anchor 해석 및 본문 주장 검증은 아직 미해결.** 분량을 늘리거나 필드를 채우는 것만으로 품질을 판정하지 않는다. |
| P8 | `table_specs`와 `embedded_table_ids`를 선언할 수 있지만 자동 표 생성·데이터 대응을 강제하지 않는다. 이미지 registry 존재도 실제 본문 embed와 파일 존재를 입증하지 않는다. | schema·handler·renderer 코드 확인. 미해결. 표·그림은 실행 자료에서 생성하고 실제 원고의 참조와 파일을 검사해야 한다. |
| P9 | figure renderer는 일부 유형에 inline `drops`, `entries`, `rows`를 허용한다. 실행 report와 다른 숫자도 그림으로 그릴 수 있다. score radar는 연구 증거가 아닌 내부 심사 점수다. | [figures.py](../../research_harness/publishing/figures.py) 코드 확인. 미해결. 그림 데이터의 provenance와 실행 결과 대응 검사가 필요하다. |
| P10 | connector session은 far-method 문헌과 correspondence 원문을 보존하지 않고 claim과 `method_num_papers` 등을 저장한다. 문헌 검색이 0건이어도 reduced claim을 유지할 수 있다. | [connector/orchestrator.py](../../research_harness/connector/orchestrator.py), [far_method_market.py](../../research_harness/connector/far_method_market.py) 확인. 기존 happy-path 테스트도 빈 feed에서 claim을 만든다. 미해결. H2는 단순히 기존 JSON 한 필드를 연결해서 해결되지 않는다. |
| P11 | baseline dossier의 current-best는 첫 검색 결과로 정해진다. naive/null은 제목 heuristic 또는 placeholder다. 문헌 출처가 존재해도 적합한 강한 baseline임을 증명하지 않는다. | [market_research.py의 _assign_candidates](../../research_harness/agents/market_research.py) 확인. 미해결. 동일 task·data·budget에서 비교 가능한 baseline 선정과 재현 확인이 필요하다. |
| P12 | 정책상 성공할 때까지 계속 탐색하고 실패 holdout으로 방향을 교체한다. 평가를 반복 소비한 탐색 전체의 오류와 선택 편향을 제어하는 계약이 없다. | 실패 holdout → next direction 제어 흐름 확인. 통계적 실패율은 이번에 측정하지 않았다. 미해결. 탐색 평가와 최종 확증, 재사용 정책을 연구 시작 전에 정해야 한다. |
| P13 | 모든 결과가 real_holdout 스칼라 성공을 요구한다. COLT의 증명 중심 결과를 표현·검증할 경로가 없다. renderer와 outline도 figure를 필수로 요구했다. | real_holdout-only 타입과 [공식 학회 요구사항](submission-requirements.md) 대조. figure 강제만 제거했다. **이론 연구 지원 전체는 미해결.** |
| P14 | PDF/TeX/BibTeX·공식 스타일·학회/연도/트랙·분량·익명성·checklist 검증이 live publication 경로에 없다. `allow_tex=false` 설정 변경만으로 renderer가 생기지 않는다. | renderer와 호출 경로 확인. 로컬 `pdflatex`, `tectonic` 실행 파일은 존재하지만 실제 공식 템플릿 빌드는 미실행. 미해결. |
| P15 | 내부 성공 기준은 주로 고정 baseline과 scope를 다룬다. 학술적 중요성·가장 가까운 연구와의 차이·논문 전체의 논증 완결성이 종결의 별도 조건으로 입증되지 않는다. | 기존 원고 및 publication/ac 설정 대조. 미해결. 점수 threshold 추가만으로 해결하지 말고 문헌·실험·증명에 근거한 review와 해결 기록이 필요하다. |

## 구현 순서와 검증 단위

다음은 아직 구현되지 않은 작업 순서다. 하나의 연구 문제를 위한 실행 가능한 경로를 먼저 만들고, 모든 학회·연구 유형을 동시에 일반화하지 않는다.

1. **연구 목표와 제출 목표를 분리해 고정한다.** ProblemGoal과 초기 아이디어를 분리한다. 제출 목표에는 venue/year/track과 경험적·이론적·혼합 기여 형태를 둔다. 초기 성공 기준이 쉬운 toy baseline으로 변하지 않는지 확인한다. H1/H8/P13/P15.
2. **문헌에서 다음 방향까지 연결한다.** clean abstraction에서 P-blind reading, 논문·방법·correspondence를 보존하고 검증 가능한 PerspectivePacket으로 전달한다. 실제 baseline의 적합성은 P-aware 경로에서 별도로 확인한다. H2/P10/P11.
3. **실행된 증거로 연구를 계속하거나 닫는다.** 데이터 발견, source/evaluator 계보, success/disproof predicate, 독립 holdout·adversary 실행을 연결한다. 실패 receipt와 평가 재사용 정책을 검증한다. H4/H5/H7/P12.
4. **논문 전체의 증거를 구성한다.** 한 promoted node의 요약을 넘어 주요 주장, 비교 실험, 불확실성, ablation 또는 증명, 문헌 차이를 묶는다. 표·그림·인용·evidence anchor를 실제 자료에 대응시킨다. H6/P7/P8/P9/P15.
5. **제출 패키지를 빌드하고 완료 조건에 결합한다.** 공식 스타일의 PDF/TeX/BibTeX와 회차에 맞는 부록·익명 재현 패키지를 만든다. 원고 및 입력 hash를 strong receipt에 연결한다. 파일 삭제·수치 변조·누락 인용·잘못된 회차에서 완료가 거부되는지 검사한다. P3/P14.
6. **선정한 실제 문제를 끝까지 실행한다.** 문헌·실험·논문을 사람의 수동 보충 없이 생성할 수 있었는지와 실제 개입을 기록한다. 핵심 표·그림 또는 증명을 독립적으로 검토하고, 전체 제출물을 외부 리뷰어 관점으로 판단한다. 이번 수정의 단위 테스트는 이 검증을 대신하지 않는다.

## 이번 수정의 검증

- 변경 전 관련 테스트: `venv/bin/python -m pytest -q tests/test_sakana_paper.py tests/test_figure_renderer.py tests/test_connector_orchestrator.py tests/test_blind_sequential_research.py tests/test_blind_mcp_adapter.py tests/test_falsifier_gate.py tests/test_construct_adversary.py` → 139 passed.
- 네 회귀 재현은 최초 실행에서 모두 실패했다. publication 통합 재현은 먼저 완료 node 해석 오류를 드러냈고, 이를 고친 뒤 증거 변조 시 진입점의 거부 응답까지 확인했다.
- 변경 후 관련 테스트: `venv/bin/python -m pytest -q tests/test_sakana_paper.py tests/test_mcp_server.py tests/test_connector_orchestrator.py tests/test_thread_supervisor.py tests/test_blind_mcp_adapter.py tests/test_blind_sequential_research.py` → 127 passed, 3 subtests passed.
- 변경 후 전체 테스트: `venv/bin/python -m pytest -q` → 954 passed, 20 subtests passed.
- Playwright와 기존 로컬 Chromium으로 기존 원고를 새 디렉터리에 렌더링했다. 1280px와 390px viewport 모두 이미지 3/3 로드, document scrollWidth=viewport width. 첫 모바일 점검에서 긴 hash 문자열의 overflow를 발견해 수정한 뒤 다시 확인했다. internal report의 revision directives도 확인했다.
- `git diff --check` 통과. 새 dependency는 추가하지 않았다. 기존 연구 원고·실험 산출물은 덮어쓰지 않았다. 커밋은 생성하지 않았다.

## 검토용 산출물과 한계

새 레이아웃의 [원고 미리보기](../../runs/audits/paper-preview-20260905/paper.html)와 [내부 보고서](../../runs/audits/paper-preview-20260905/interactive_summary.html)를 만들었다. screenshots와 `preview_provenance.json`도 같은 디렉터리에 있다. 기존 원고의 문장·실험·숫자는 그대로 사용했으므로 내용의 학술적 부족은 남아 있다. 이 산출물은 수정된 renderer를 검토하기 위한 것이며 논문 제출물이 아니다.

실제 신규 연구, 공식 템플릿 컴파일, 인용 원문 대조, 독립 실험 재실행, 외부 학술 심사는 수행하지 않았다. H/P의 미해결 항목이 남아 있는 동안 이 세션의 논문 생성 성공 조건은 충족되지 않았다.
