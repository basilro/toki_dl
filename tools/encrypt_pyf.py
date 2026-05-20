"""평문 .py → flaskfarm .pyf (암호화) 변환.

사용:
    # SJVA Linux 컨테이너 안에서 실행 (Python 3.10 또는 3.11)
    cd /volume1/docker/ff/plugins/toki_dl
    python3 tools/encrypt_pyf.py _novel_crypto.py

flaskfarm 의 native `sc` 모듈을 사용. 결과:
    _novel_crypto.py   →  _novel_crypto.pyf  (같은 디렉토리)

평문 .py 는 손대지 않음. .pyf 만 commit, .py 는 .gitignore 로 비공개.
"""
import os
import sys


def _find_libsc() -> str:
    """flaskfarm libsc 디렉토리 자동 탐색.

    우선순위:
      1) 환경변수 FLASKFARM_LIBSC
      2) 일반 SJVA 경로 후보들
    """
    env = os.environ.get('FLASKFARM_LIBSC')
    if env and os.path.isdir(env):
        return env
    candidates = [
        '/app/flaskfarm/lib/support/libsc',
        '/opt/flaskfarm/lib/support/libsc',
        '/sjva/flaskfarm/lib/support/libsc',
        '/volume1/docker/ff/flaskfarm/lib/support/libsc',
        '/data/flaskfarm/lib/support/libsc',
    ]
    for c in candidates:
        if os.path.isdir(c):
            return c
    raise RuntimeError(
        'flaskfarm libsc 디렉토리 못 찾음. '
        'FLASKFARM_LIBSC=/path/to/flaskfarm/lib/support/libsc 로 지정')


def encode_file(src_path: str) -> str:
    if not os.path.isfile(src_path):
        raise FileNotFoundError(src_path)
    libsc = _find_libsc()
    sys.path.insert(0, libsc)
    import sc  # noqa
    with open(src_path, 'r', encoding='utf-8') as fp:
        src = fp.read()
    encoded = sc.encode(src, 0)
    if not encoded:
        raise RuntimeError('sc.encode() returned empty')
    dst_path = src_path[:-3] + '.pyf' if src_path.endswith('.py') \
        else src_path + '.pyf'
    with open(dst_path, 'w', encoding='utf-8') as fp:
        fp.write(encoded if isinstance(encoded, str)
                 else encoded.decode('utf-8'))
    return dst_path


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 1
    for src in argv[1:]:
        try:
            dst = encode_file(src)
            print(f'OK: {src} → {dst}')
        except Exception as e:
            print(f'FAIL: {src} — {type(e).__name__}: {e}')
            return 2
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
