# 첫 논문 연구 문제 후보

조사일: 2026-09-05. 사용자는 아직 문제를 선택하지 않았다. 아래는 선택 전 검토안이며 신규성, 구현 가능성, 학회 채택 가능성을 입증한 결과가 아니다. RTX 3060 12GB를 파일럿 설계의 자원 가정으로 사용했다. 실행 시간과 메모리는 아직 측정하지 않았다.

`research_profile.md`의 capability-first, current-best/naive/random 비교 원칙을 따른다. 이 프로젝트 자체를 논문 주제로 삼는 것과 이 프로젝트로 외부 ML 문제의 논문을 만드는 것은 다른 목표다. 후자를 검증하려면 후보 1 또는 2가 더 직접적이다. 학회 적합성은 아래 선행연구와 문제 성격을 바탕으로 한 판단이다.

## 후보 1: 제한된 라벨 예산으로 변화하는 영상 분포에서 적응 여부 결정

**질문:** 클래스 비율과 이미지 손상이 함께 변하는 스트림에서, 아주 적은 지연 라벨을 언제 요청하고 언제 모델을 갱신·복원할지 공동으로 결정하면 동일한 라벨·연산 예산의 주기적 적응과 무작위 라벨 요청보다 누적 오류를 줄일 수 있는가?

가장 가까운 연구는 이미 강하다. [TTAB / On Pitfalls of Test-Time Adaptation](https://arxiv.org/abs/2306.03536)은 모델 선택과 다양한 shift의 평가 문제를 지적했다. [Realistic Evaluation](https://arxiv.org/abs/2407.14231)은 비지도 하이퍼파라미터 선택과 제한된 supervision을 비교한다. [Agreement-on-the-Line](https://arxiv.org/abs/2310.04941)은 타깃 라벨 없는 적응 전략 선택을 제시한다. [Monitoring Risks in TTA](https://arxiv.org/abs/2507.08721)는 갱신 중인 모델의 위험을 confidence sequence로 감시한다. 따라서 단순히 “불확실성이 높으면 적응 중단”하는 방법은 차별화 근거가 부족하다.

추가로 [Exploring Human-in-the-Loop Test-Time Adaptation by Synergizing Active Learning and Model Selection](https://arxiv.org/abs/2405.18911)은 라벨 요청과 모델 선택을 이미 함께 다룬다. 따라서 공동 결정 자체도 신규성 근거가 아니다. 가능한 차별점은 지연 피드백과 비정상 스트림에서 요청·갱신·복원 정책이 보이는 구체적인 한계와 그 해결에 있다. active TTA, label-efficient TTA, delayed-feedback 문헌을 더 조사해야 하며 아직 빈틈으로 확정할 수 없다. 완전 비지도 설정과 라벨 허용 설정을 섞어 비교해서는 안 된다.

파일럿 제안:

- CIFAR-10-C의 작은 pretrained CNN으로 시작한다. 연속 손상 전환과 클래스 불균형을 조합하고, 개발용과 최종 평가용 스트림·손상 조합을 분리한다.
- 비적응, 주기적/항상 적응, 기존 label-free 선택법, 동일 예산 무작위/주기적 라벨 요청을 비교한다. 라벨을 받는 모든 방법에 같은 지연을 적용한다.
- 누적 오류, 최악 구간 오류, 요청 라벨 수, 총 실행 시간을 함께 측정한다. 감독 가능한 개발 스트림에서만 임계값을 정한다.
- 여러 스트림 순서에서 이점이 유지되고 그 원인이 확인되면 두 번째 데이터셋과 backbone으로 확장한다. CIFAR 파일럿 자체는 최종 논문 증거가 아니다.

**중단 기준:** 무작위 라벨 요청과 단순 주기적 복원으로 이점이 사라지거나, 평가 스트림별 oracle tuning이 필요하거나, 이미 같은 공동 의사결정을 해결하는 선행연구가 있으면 현재 가설을 폐기한다. 후보 수준의 적합성: CVPR/ICLR/NeurIPS/ICML. 세 후보 중 외부 ML 문제의 첫 실험으로 우선 검토할 만하다.

## 후보 2: 학습 초반 순위가 뒤집히는 상황에서 실험 예산 배분

**질문:** 초기 학습 곡선에서 뒤처진 후보를 성급하게 제거하지 않으면서, 한정된 GPU 예산으로 최종 성능이 좋은 설정을 찾는 확률을 높일 수 있는가? 특히 새 모델 계열이나 스케줄로 학습 곡선 분포가 바뀌는 경우를 다룬다.

[In-Context Freeze-Thaw BO](https://proceedings.mlr.press/v235/rakotoarison24a.html)는 학습 곡선을 예측해 실행을 중단·재개한다. [Cost-Sensitive Freeze-thaw BO](https://arxiv.org/abs/2510.21379)는 비용을 고려한 후보 선택과 transfer를 이미 다룬다. [Difficulty-Aware Learning Curve Extrapolation](https://ojs.aaai.org/index.php/AAAI/article/view/39467)은 2026년의 추가 경쟁작이다. “좋아 보이는 실험에 예산을 더 준다”는 것은 신규 기여가 아니다.

파일럿은 공개 학습 곡선을 재생하여 CPU에서 정책을 비교하고, 하나의 작은 CNN 실험으로 실제 실행 시간과 선택 결과가 맞는지 확인하는 식으로 제한한다. task/family 단위로 곡선 학습과 평가를 나누고, random search, successive halving/Hyperband, ifBO, cost-sensitive freeze-thaw와 같은 예산에서 비교한다. 목표는 최종 regret와 좋은 후보의 오제거율이다. 후보 메커니즘은 곡선 외삽의 불확실성이 큰 계열에 최소 탐색 예산을 남기는 것이다.

**중단 기준:** 단순히 최소 epoch를 늘리는 baseline이 같은 비용으로 이기거나, 특정 수작업 crossing curve에서만 개선되거나, 곡선 예측기의 재학습만으로 해결되면 중단한다. 구현용 checkpoint·benchmark의 접근 조건을 추가 확인해야 한다. 적합성 추론: ICML/NeurIPS/ICLR. 새로운 보장이나 하한 없이 COLT를 우선 목표로 삼기는 어렵다.

## 후보 3: 연구 방향을 반복 교체하는 탐색 전체의 오류 통제

**질문:** 같은 목표 아래 가설을 버리고 새 가설로 이동하는 탐색에서, 데이터와 비용을 제한하면서 최종적으로 보고하는 잘못된 발견의 확률을 통제하고 참인 개선을 발견하는 확률을 높일 수 있는가?

직접 경쟁작 [POPPER, ICML 2025](https://proceedings.mlr.press/v267/huang25n.html)는 agent가 반증 실험을 설계·실행하고 sequential testing으로 Type-I error를 통제한다. [Online multiple testing with e-values](https://proceedings.mlr.press/v238/xu24a.html)는 계속 들어오는 가설의 다중 검정을 다룬다. [Generalization in Adaptive Data Analysis and Holdout Reuse](https://arxiv.org/abs/1506.02629)와 [Ladder](https://proceedings.mlr.press/v37/blum15.html)는 adaptive holdout reuse의 기존 기반이다. [E-valuator](https://arxiv.org/abs/2512.03109)는 agent trajectory 검증 점수를 순차 통계 판단으로 바꾼다. 각 논문은 서로 다른 통계 목표·가정을 가지므로 baseline을 동일 기능처럼 치환하지 않는다.

후보 차별점은 개별 가설의 검증을 넘어 **가설 생성·교체·중단을 포함한 목표 전체의 오류와 발견 효율**을 다루는 것이다. 다만 기존 결과를 연결하는 것만으로 새 논문이 되는지는 미확인이다. e-value를 붙였다고 adaptive data reuse 문제가 사라지지 않으며, 데이터·검정 선택 이후에도 필요한 유효성 조건이 성립하는지 증명해야 한다.

파일럿은 정답을 아는 null/작은 효과의 합성 세계에서 시작한다. 전체 시뮬레이션 반복은 CPU로 하고 일부만 실제 LLM 방향 생성으로 반복한다. 순진한 반복 검정, 한 번만 사용하는 최종 holdout, 유효한 alpha spending, 적용 조건을 맞춘 온라인 검정/holdout 방법을 비교한다. 전체 실행 중 하나라도 거짓 결론을 내는 확률, 참 발견 확률, 데이터 소비, 총 비용을 측정한다. 1,000회 null 반복에서 nominal 5% 주변의 Monte Carlo 오차도 보고한다.

**중단 기준:** 기존 유효한 방법의 단순 적용과 차이가 없거나, FDR와 FWER를 혼동해야 우월성이 나오거나, 검정 유효성을 LLM의 자기 보고에 의존하면 신규 연구로 밀지 않는다. 이 경우는 필요한 시스템 수정으로 남긴다. 적합성 추론: ICML/NeurIPS, 이론 기여가 확실해질 경우 COLT. 이 후보는 harness를 연구 대상으로 삼기 때문에 외부 문제에서 한 편을 완성하는 능력을 단독으로 입증하지 못한다.

## 선택 전 필요한 판단

추천은 후보 1과 2의 가장 가까운 논문·공식 구현을 먼저 재현 가능성까지 확인하고, 차별화 가능한 하나만 짧은 파일럿으로 진행하는 것이다. 후보 3은 시스템 신뢰성을 위해 필요하지만 첫 외부 논문 목표를 대체하지 않는다. 범용 autonomous scientist 제작 자체도 [AI Scientist-v2](https://arxiv.org/abs/2504.08066), [AutoResearchClaw](https://arxiv.org/abs/2605.20025), [SAGE](https://arxiv.org/abs/2606.31478)와 겹치므로, 자동 원고 작성이나 실패 회복 기능만으로 학술적 신규성을 주장해서는 안 된다.

이 문서는 1차 문헌 검색과 초록·공식 출판 페이지를 중심으로 한 예비 조사다. full-text 방법·가정 대조, 전체 인용망 탐색, 구현 실행, 신규성 판정은 아직 수행하지 않았다. 코드·테스트는 변경하거나 실행하지 않았다.
