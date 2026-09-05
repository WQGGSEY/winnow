# 메인 컨퍼런스 제출 준비 요구사항

확인일: 2026-09-05. 대상은 ICML, NeurIPS, ICLR, CVPR, COLT의 본 학회 연구 논문이다. 아래 수치는 **공식 2026년 최초 제출 규정의 스냅샷**이며 다음 회차의 규정이 아니다. 이번 검색에서 구체적으로 확인한 author guide는 2026판이다. ICLR 2027 author guide는 가져오지 못했고 ICML/CVPR의 2027 상세 author guide도 확인하지 못했다. 실제 투고 회차가 정해지면 템플릿·규정·마감·제출 폼을 다시 고정해야 한다.

## 1. 논문 준비 완료의 의미

이 프로젝트의 완료 기준은 **근거가 있는 연구 결과와 재현 자료를 바탕으로, 특정 학회·연도·트랙의 최초 제출용 원고를 완성하는 것**이다. 제출 가능한 파일, 학술적으로 설득력 있는 논문, 외부 학회 채택은 다른 판정이다. 내부 `accept`와 자동 검사는 외부 채택을 보장하지 않는다.

학술 검토는 기술적 타당성, 설명의 명료성, 중요성, 신규성을 각각 판단해야 한다. 좋은 점수 하나나 baseline 개선만으로 모두 충족되지 않는다. ICML은 이 네 항목을 구분하며, 이론의 가정·증명과 실험의 설계가 주장을 뒷받침하는지 본다. 가까운 연구와의 차이, 한계도 명시해야 한다. 새로운 방법만이 유일한 기여 형태는 아니다. [ICML 2026 Reviewer Instructions](https://icml.cc/Conferences/2026/ReviewerInstructions)

## 2. 학회별 최초 제출 패키지

| 회차 | 본문 한도 | PDF·부록·코드 | 추가 확인 사항 |
|---|---|---|---|
| ICML 2026 | 8쪽 | 해당 연도 LaTeX. 참고문헌·부록은 본문 뒤 동일 PDF, 분량 제한 없음. 제출 PDF 50MB 이하. 코드/데이터 보충 자료는 익명화. | 본문에 심사에 필수적인 근거를 포함. main track impact statement 필수. camera-ready의 9쪽 규정을 최초 제출에 적용하지 않음. |
| NeurIPS 2026 main track | 9쪽 | 해당 연도 LaTeX. 본문·참고문헌·부록·필수 checklist를 하나의 PDF로, 50MB 이하. 코드/데이터는 별도 익명 ZIP, 100MB 이하. | checklist를 실제 근거와 함께 완성. General/Theory/Use-Inspired/Concept & Feasibility/Negative Results 중 기여 유형에 맞춰 평가. |
| ICLR 2026 | 9쪽 | 해당 연도 LaTeX. 참고문헌·부록 별도 분량 제한 없음. 텍스트 부록은 참고문헌 뒤 단일 PDF 권장. 익명 코드 보충 자료 권장. | ethics/reproducibility statement는 권장. 연구 구상·작성에 기여자 수준의 LLM 사용이 있으면 별도 사용 설명 필수. 논의·camera-ready 단계의 10쪽과 구분. |
| CVPR 2026 | 8쪽 | CVPR 템플릿. 추가 쪽은 인용 참고문헌만 허용. 증명·추가 분석·영상 등은 보충 자료. 코드 제출은 권장. | 본문·보충 자료·영상·링크 익명성 확인. 외부 링크로 심사 대상 내용을 확장하거나 분량 제한을 우회하면 안 됨. |
| COLT 2026 | 12쪽 | PMLR 형식, 전체 원고 단일 PDF. 참고문헌·부록 분량 제한 없음. LaTeX `[anon]` 옵션. | 모든 필요한 증명·유도 포함. 핵심 신규성·중요성과 충분한 증명 설명은 본문에 포함. acknowledgments 제거. 제출 시스템은 CMT. |

행별 근거: [ICML Author Instructions](https://icml.cc/Conferences/2026/AuthorInstructions), [ICML CFP — impact statement](https://icml.cc/Conferences/2026/CallForPapers), [NeurIPS Main Track Handbook](https://neurips.cc/Conferences/2026/MainTrackHandbook), [ICLR Author Guide](https://iclr.cc/Conferences/2026/AuthorGuide), [CVPR Author Guidelines](https://cvpr.thecvf.com/Conferences/2026/AuthorGuidelines), [COLT CFP 및 Submission Instructions](https://learningtheory.org/colt2026/cfp.html).

ICLR 2026 안내의 일부 FAQ에는 submission과 rebuttal의 분량을 혼용한 표현이 있다. 위 표는 명시적인 `Paper length` 절의 최초 제출 9쪽을 따른다. 미래 회차로 이 값을 자동 복사하지 않는다. [ICLR Author Guide](https://iclr.cc/Conferences/2026/AuthorGuide)

## 3. 자동 검증과 학술 판단의 경계

아래는 공식 지침을 프로젝트 요구사항으로 옮긴 설계 제안이다. 학회가 동일한 자동 검사 도구나 산출물 이름을 요구한다는 뜻은 아니다.

| 검사 영역 | 자동으로 관찰 가능한 근거 | 사람이 판단해야 하는 부분 |
|---|---|---|
| 형식 | 고정한 공식 템플릿·버전, PDF 빌드 성공, 파일 크기, 용지·폰트·페이지 수, 누락된 인용/그림/참조, 제출 단계 표시 | 가독성, 본문과 부록 경계가 적절한지, 의미상 분량 우회 여부 |
| 익명성 | PDF metadata, 저자·이메일·기관 문자열, 코드·ZIP·영상 파일명, URL과 저장소 흔적 검사 | 문맥·감사문·자기 인용·데모를 통한 신원 노출. 문자열 검사 통과만으로 익명성을 증명하지 않음 |
| 서지 | 각 BibTeX 항목의 제목·저자·연도·DOI/출판 페이지 확인, cite key 연결 | 해당 논문이 실제 주장을 지지하는지, 가장 가까운 연구가 빠지지 않았는지 |
| 실험 | 입력/데이터 분할/코드/설정/seed/환경 고정, 실행 로그에서 표·그림 재생성, 보고 수치 대조 | 비교군의 적합성·예산 공정성, 표본 독립성, 통계 방법, 평가 누출, 주장의 일반화 범위 |
| 이론 | 정의·가정·정리·증명 문서 연결, 미해결 참조, 실제 사용한 formal checker 결과가 있으면 기록 | 증명의 정확성·빠진 경우·가정의 타당성·기존 정리와의 차이. 텍스트 존재나 LLM 투표는 증명 검증이 아님 |
| 정책/설명 | 요구 statement/checklist/저자 폼 필드의 존재·완성 여부 | 설명의 사실성, 이해충돌·연구 참여자 관련 판단, 책임 있는 저자 확인 |

서지 정확성은 ICML author instructions에도 명시돼 있다. 재현 가능한 코드·환경·데이터 정보와 저자의 내용 책임은 NeurIPS handbook에 제시돼 있다. [ICML Author Instructions](https://icml.cc/Conferences/2026/AuthorInstructions), [NeurIPS Main Track Handbook](https://neurips.cc/Conferences/2026/MainTrackHandbook)

## 4. 경험적 연구와 이론 연구를 별도 증거 경로로 지원

- **경험적 연구:** 주장에 맞는 강한 비교군과 단순 비교군, 실험 조건, 데이터 처리·분할, 모델 선택 절차, 변동성/불확실성, 주요 설명에 필요한 추가 실험을 준비한다. 데이터셋 수·seed 수·p-value 하나를 모든 논문에 공통 강제하는 것은 이번 공식 문헌에서 도출되지 않는다. 구체적 검증 방법은 주장과 실험 단위에 맞춰 정한다.
- **이론 연구:** 정의, 명시적 가정, 정확한 정리, 완전한 증명과 필요한 보조정리를 준비한다. COLT는 이론 중심이며 관련 실험은 분석을 보조할 수 있다. 따라서 모든 연구를 `real_holdout` 스칼라 개선으로만 통과시키는 모델은 대상 범위를 충족하지 않는다.
- **혼합 연구:** 정리가 보장하는 설정과 실험 설정의 관계를 설명하고 둘의 근거를 따로 추적한다. 실험이 증명의 빈틈을 대신하거나 정리가 측정하지 않은 실험 주장을 대신하지 않는다.

위 구분은 [ICML 심사 기준](https://icml.cc/Conferences/2026/ReviewerInstructions), [COLT CFP](https://learningtheory.org/colt2026/cfp.html), [ICLR reproducibility 안내](https://iclr.cc/Conferences/2026/AuthorGuide)를 프로젝트에 적용한 해석이다.

## 5. 프로젝트가 내보낼 제출 준비 산출물

이것은 구현 제안이며, 전부가 학회에 업로드되는 필수 파일이라는 의미는 아니다.

1. **원고:** `paper.pdf`, 편집 가능한 `.tex` 원본, `.bib`, 실제 표·그림 파일, 공식 스타일 파일의 출처·버전, 빌드 명령과 로그. HTML은 미리보기로 유지할 수 있지만 제출 PDF를 대신하지 않는다.
2. **심사용 보충 자료:** 회차에 맞게 배치한 부록·증명·추가 실험, 익명 코드/데이터 접근 안내, 환경과 재현 명령. 제한된 자원의 자료는 접근·재현 한계를 명시한다.
3. **내부 근거 묶음:** 논문의 각 핵심 주장과 표·그림이 어떤 실행 또는 증명에 연결되는지, 독립 재실행/검토 결과, 실패한 검증과 아직 미해결인 판단을 기록한다. 내부 경로·신원 정보가 심사 패키지에 그대로 섞이지 않도록 한다.
4. **회차별 준비 보고서:** 규정 확인일, 목표 연도·트랙·최초 제출 단계, 객관적 검사 결과, 과학적 검토 결과, 미해결 항목을 구분한다. 결과는 `형식 통과 / 근거 재현됨 / 학술 판단 미해결`처럼 사실별로 보여준다.
5. **사람이 확인할 제출 정보:** 제목·초록·저자와 계정·이해충돌·필수 선언·LLM 사용 설명·중복 제출 여부. 계정 입력과 제출은 패키지 생성과 별도 작업이며 자동 생성으로 저자의 내용 책임이 사라지지 않는다.

ICLR은 큰 LLM 기여 공개와 인간 저자의 책임을 명시한다. NeurIPS도 agent/LLM 저자 등재를 허용하지 않는다. COLT는 LLM 사용을 허용하지만 인용·분석 정확성의 책임을 저자에게 둔다. [ICLR Author Guide](https://iclr.cc/Conferences/2026/AuthorGuide), [NeurIPS Handbook](https://neurips.cc/Conferences/2026/MainTrackHandbook), [COLT CFP](https://learningtheory.org/colt2026/cfp.html)

## 조사 한계

공식 author/reviewer 페이지와 템플릿 안내를 확인했다. 실제 TeX archive 전체를 설치·컴파일하거나 예제 PDF를 검사하지 않았다. CVPR/COLT의 템플릿 다운로드 링크는 도구에서 오류가 발생했다. 학회별 제출 폼의 로그인 뒤 필드, 미래 회차의 규칙, 현 원고의 준수 여부는 아직 검증하지 않았다. 코드·설정·실험은 변경하거나 실행하지 않았다.
