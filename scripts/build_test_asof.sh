#!/usr/bin/env bash
# test 과제의 기준 시각 스냅샷 생성과 검증. 별도 창(Git Bash)에서 실행한다. 비용 없음. 중단되면 같은 명령으로 이어 실행된다.
#   상태   data/derived/db/asof/status.txt, runs/testtasks_status.txt(시작과 끝)      출력   runs/testgen_logs/asof_test.txt
# 러너(agent_run.py)와 동시에 실행하지 않는다(메모리).
cd "$(dirname "$0")/.." || exit 1
export PYTHONIOENCODING=utf-8
ST=runs/testtasks_status.txt
echo "$(date '+%F %T') asof start" >> "$ST"
.venv-agent/Scripts/python -u scripts/agent_prepare_asof.py --set test >> runs/testgen_logs/asof_test.txt 2>&1
echo "$(date '+%F %T') asof rc=$? ASOF DONE" >> "$ST"
