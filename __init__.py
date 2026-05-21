import os

try:
    import requests  # noqa
except Exception:
    os.system("pip install requests")

# curl_cffi — Cloudflare TLS 핑거프린트 우회 (필수, 미설치 시 403 가능성 큼)
try:
    import curl_cffi  # noqa
except Exception:
    try:
        os.system("pip install curl_cffi")
    except Exception:
        pass
