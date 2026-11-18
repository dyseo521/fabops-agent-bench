"""SMT2020 공식 AutoSched 데이터셋(.asd, UTF-16)을 PySCFabSim 입력 형식(UTF-8)으로 변환한다.

- 모든 *.txt를 UTF-8로 재인코딩
- tool.txt의 연속 행(STNFAM이 빈 행)을 앞 행에 ';'로 병합해 tool.txt.1l 생성
- --engineering drop : engineering lot(WIP의 part_E*, E_order.txt, route_E*)을 제외
- --engineering keep : E_order.txt를 그대로 복사(시뮬레이터 패치 필요)
"""
import argparse, io, os, sys

KEEP = {'attach.txt', 'downcal.txt', 'fromto.txt', 'order.txt', 'part.txt', 'pmcal.txt',
        'setup.txt', 'setupgrp.txt', 'WIP.txt', 'tool.txt', 'E_order.txt'}


def read(path):
    raw = open(path, 'rb').read()
    enc = 'utf-16' if raw[:2] in (b'\xff\xfe', b'\xfe\xff') else 'utf-8'
    return [l.rstrip('\r') for l in raw.decode(enc).split('\n')]


def merge_tool(lines):
    out, hdr = [lines[0]], lines[0].split('\t')
    cur = None
    for l in lines[1:]:
        if not l.strip():
            continue
        c = l.split('\t') + [''] * (len(hdr) - len(l.split('\t')))
        if c[0]:
            if cur: out.append('\t'.join(cur))
            cur = c
        else:
            for i, v in enumerate(c):
                if v: cur[i] = cur[i] + ';' + v if cur[i] else v
    if cur: out.append('\t'.join(cur))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('src'); ap.add_argument('dst')
    ap.add_argument('--engineering', choices=['drop', 'keep'], default='drop')
    a = ap.parse_args()
    os.makedirs(a.dst, exist_ok=True)
    for f in sorted(os.listdir(a.src)):
        is_route = f.startswith('route_') and f.endswith('.txt')
        if not (f in KEEP or is_route):
            continue
        eng = f == 'E_order.txt' or f.startswith('route_E')
        if eng and a.engineering == 'drop':
            continue
        lines = [l for l in read(os.path.join(a.src, f)) if l.strip()]
        if f in ('WIP.txt', 'part.txt') and a.engineering == 'drop':
            col = lines[0].split('\t').index('PART')
            lines = [lines[0]] + [l for l in lines[1:] if not l.split('\t')[col].startswith('part_E')]
        name = f
        if f == 'tool.txt':
            lines, name = merge_tool(lines), 'tool.txt.1l'
        with io.open(os.path.join(a.dst, name), 'w', encoding='utf-8', newline='\n') as o:
            o.write('\n'.join(lines) + '\n')
        print(f'{name}: {len(lines) - 1} rows')


if __name__ == '__main__':
    sys.exit(main())
