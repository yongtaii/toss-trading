# 현재 아키텍처

전략은 주간 SMA20/200 기반 다섯 가지 오프라인 연구 정책만 남겼다.

| 위치 | 역할 |
| --- | --- |
| `research/picks/` | 기본 전략·90·95·99·100% 다섯 명세 |
| `research/weekly_baseline.py` | 기본 전략의 신호·회계·주간 일정 |
| `research/compare_weekly_allocations.py` | 고비중 회계와 50회 비교 실행 |
| `research/daily_data.py` | 원본 일봉·메타데이터·거래일·자료 병합 검사 |
| `research/weekly_baseline_audit.py` | 기존형의 별도 원장 검산 |
| `research/audit_weekly_allocations.py` | 고비중형 검산·전체 평가 검사 |
| `research/results/weekly-comparison.json` | Git에 보존하는 비교 지표 |

연구 코드는 HTTP나 증권사 주문을 호출하지 않는다. 완료된 과거 일봉에서 신호를 계산하고,
다음 시가 체결·비용·정수 수량·현금·일별 평가액을 계산한다. 고비중형은 추가 매수 때
평균 취득가를 갱신하고 부분 매도 때 원가를 비례 배분한다. 원본 자료와 전체 원장은
Git에서 제외한 `data/`에 저장한다.

기존 `src/toss_trading/`의 API·조회 어댑터·시세 다운로드·캘린더·리스크·체결·원장·
계좌 대조·모니터링 기반 코드는 유지했다. `strategy.py`에는 공통 계약과 매매하지 않는
`research_only` 자리표시자만 있다. 삭제된 전략 이름은 팩토리에서 거부한다.
`backtest.py`에는 외부 주입 전략을 위한 공통 분봉 재생 도구가 남아 있으나 이전
전략 CLI·기준 전략·탐색 기능은 제거했다. 남긴 주간 전략의 실행 경로는 `research/`다.

`config/strategy.toml`은 기존 API·원장 스키마를 위한 비매매 설정이다.
`config/strategy-weekly-live.toml`은 별도 LIVE worker에서 주간 기본 전략을 선택한다.
`live/weekly.py`가 완료 일봉·미국 장 일정·호가를 읽어 주문 의도를 만들고,
`live/bridge.py`가 원장 기록, 실계좌 대조, 주문 전송과 체결 복구를 담당한다.
