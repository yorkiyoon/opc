# OPC conditional retarget (Python / KLayout)

SVRF 스타일의 **조건부 리타겟**을 Python(KLayout API)으로 구현한 도구입니다.
입력은 GDS, 룰은 JSON(µm 단위)로 정의합니다.

## 설치

```bash
pip install -r requirements.txt
```

## 실행

```bash
python tools/make_test_gds.py test_m1.gds                       # 테스트 패턴 생성 (선택)
python opc_retarget.py test_m1.gds rules/example_m1.json -o out.gds --report report.txt
```

- 출력 GDS = 원본 + 리타겟 결과 레이어(+ MRC marker 레이어)
- MRC 위반이 있으면 exit code 1

## 동작 방식

1. **Stage 1 – body bias**: 각 edge를 마주보는 width/space(projection 기준,
   SVRF `INT/EXT ... PROJ`와 동일)로 fragment 하고, width × space bin에 따라 bias.
   line-end edge는 이 단계에서 고정.
2. **Stage 2 – line-end bias**: Stage 1 결과에서 line-end를 다시 찾아
   tip-to-tip space bin에 따라 bias. 측면 bias가 반영된 폭으로 연장되므로 jog 없음.
3. **MRC**: 결과에 min width / min space 체크, 위반을 marker 레이어로 출력.

## 룰 파일 형식 (`rules/example_m1.json`)

| 키 | 의미 |
|---|---|
| `input` / `output` | `"layer/datatype"` |
| `bias_table.width_bins` | width 상한 경계(오름차순, 미만 기준). N개 → N+1 bin |
| `bias_table.space_bins` | space 상한 경계. N개 → N+1 bin (마지막 = isolated) |
| `bias_table.bias` | `[width bin][space bin]` 2D 표. 1D 리스트면 space 전용 |
| `line_end.max_width` | 이 길이 이하 + 양끝 convex corner인 edge = line-end |
| `line_end.space_bins` / `bias` | tip-to-tip space 별 bias |
| `mrc.min_width` / `min_space` / `marker_layer` | 결과 검증 |

bias 부호: `+` = 바깥으로(성장), `-` = 안쪽으로(축소).

SVRF 대응 예:

```
// SVRF                                     // JSON
s1 = EXT M1 < 0.06 PROJ  → edge -4nm        "space_bins": [0.06, ...], "bias": [[-0.004, ...]]
```

## 알려진 한계

- 계층을 flatten 해서 처리 (대용량 칩은 deep mode / tiling 도입 필요)
- 서로 다른 bin의 edge가 만나는 convex corner는 두 bias 중 작은 쪽 기준으로 모서리가 생김
  (fragment 경계에서의 계단 형태는 정상 동작)
- Manhattan 형상 기준으로 검증됨

## 테스트

```bash
python -m pytest -q tests
```
