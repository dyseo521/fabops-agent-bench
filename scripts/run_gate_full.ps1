# 게이트-오라클 전수 점검을 시나리오 묶음 단위로 끝까지 실행한다. 별도 터미널에서 직접 실행하는 용도.
# check_gate_oracle.py는 --max-new 건을 처리하고 남은 것이 있으면 종료 코드 3으로 끝난다. 0 또는 1이 나올 때까지 반복한다.
# 중단돼도 체크포인트(data/derived/scenarios/<이름>_gate_check_parts_<서명>.jsonl)에서 이어서 실행된다.
#
# 예) powershell -File scripts\run_gate_full.ps1
#     powershell -File scripts\run_gate_full.ps1 -Names lvhm_e_cr_s0 -Chunk 100
param(
    [string[]]$Names = @('lvhm_e_cr_s1', 'lvhm_e_cr_s0', 'lvhm_e_cr_s0_w75'),
    [int]$Chunk = 150
)
Set-Location (Split-Path $PSScriptRoot -Parent)
foreach ($n in $Names) {
    do {
        python -u scripts/check_gate_oracle.py "data/derived/db/$n.sqlite" --per-template 100000 --max-new $Chunk
        $rc = $LASTEXITCODE
    } while ($rc -eq 3)
    Write-Host ("gate {0} rc={1} {2}" -f $n, $rc, (Get-Date -Format 'HH:mm'))
}
