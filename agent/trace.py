"""시행 기록. 시행 1건 = JSONL 1개. 끝난 시행만 최종 이름으로 남긴다(중단된 시행은 .part로 남아 재개 때 다시 실행된다)."""
import hashlib
import json
import os
import time


def sha256(data):
    if isinstance(data, str):
        data = data.encode('utf-8')
    return hashlib.sha256(data).hexdigest()


def sha256_file(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for b in iter(lambda: f.read(chunk), b''):
            h.update(b)
    return h.hexdigest()


class Tracer:
    def __init__(self, path=None):
        """path가 없으면 메모리에만 둔다(시험용)."""
        self.path, self.events, self._t0, self._f = path, [], time.time(), None
        if path:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            self._f = open(path + '.part', 'w', encoding='utf-8')

    def event(self, kind, **fields):
        e = dict(seq=len(self.events), t=round(time.time() - self._t0, 3), event=kind, **fields)
        self.events.append(e)
        if self._f:
            self._f.write(json.dumps(e, ensure_ascii=False, default=str) + '\n')
            self._f.flush()
        return e

    def close(self, complete=True):
        if self._f:
            self._f.close()
            self._f = None
            if complete:
                os.replace(self.path + '.part', self.path)

    def of(self, kind):
        return [e for e in self.events if e['event'] == kind]
