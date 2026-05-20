"""뉴토끼 HTTP 클라이언트 — 만화/웹툰/소설 3종.

- 무로그인
- 핑거프린트 쿠키 자동 세팅
- 소설 본문은 `_novel_crypto` private 모듈로 위임

도메인은 로테이션되므로 `base_url` 외부에서 주입.
"""
import html as html_lib
import json
import os
import re
import secrets
import time
from typing import List, Dict, Any, Optional, Tuple
from urllib.parse import urlparse

import requests

# ── novel DRM 모듈 (private) ──
# 개발: 같은 디렉토리의 평문 `_novel_crypto.py` 직접 import.
# 운영(public 배포): `_novel_crypto.py` 가 없고 `_novel_crypto.pyf` (암호화) 만 존재 →
#                    flaskfarm 의 SupportSC 로 메모리 복호 후 import.
# 둘 다 실패하면 _NOVEL_CRYPTO=None → 소설 다운로드만 비활성, 나머지 정상 동작.
_NOVEL_CRYPTO = None
try:
    from . import _novel_crypto as _NOVEL_CRYPTO  # 개발: 평문
except Exception:
    try:
        from support import SupportSC
        _NOVEL_CRYPTO = SupportSC.load_module_f(__file__, '_novel_crypto')
    except Exception:
        _NOVEL_CRYPTO = None


DEFAULT_BASE = 'https://sbxh1.com'
IMAGE_CDN = 'https://i.toonflix.app'

DEFAULT_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36')

# kind ↔ URL prefix
KIND_PATH = {'manhwa': 'manhwa', 'webtoon': 'webtoon', 'novel': 'novel'}
KIND_LABEL = {'manhwa': '만화', 'webtoon': '웹툰', 'novel': '소설'}


class NewtokiError(Exception):
    pass


class NotReadableError(NewtokiError):
    """회차 잠금 (유료/연령제한/공개예정)."""


class BlockedError(NewtokiError):
    """서버 차단 (핑거프린트 누락, IP 블랙 등) — 재시도 권장."""


# ───────────────────────────── helpers ─────────────────────────────


# ───────────────────────────── client ──────────────────────────────


class NewtokiClient:

    def __init__(self, base_url: Optional[str] = None,
                 logger=None, proxy_url: Optional[str] = None,
                 cookies: Optional[str] = None):
        self.base_url = (base_url or DEFAULT_BASE).rstrip('/')
        self.logger = logger
        self._proxy_url = (proxy_url or '').strip() or None
        self._user_cookies = (cookies or '').strip() or None
        # 세션 단위로 고정해두는 핑거프린트 (브라우저 client-side 쿠키 모방)
        self._ntk_pid = secrets.token_hex(16)
        self._ntk_fp = secrets.token_hex(16)
        # nv 쿠키는 소설 다운로드 시 최초 1회 발급 후 세션 재사용
        self._nv: Optional[str] = None
        # 모든 호출 공유하는 세션
        self._sess = self._build_session()

    # ---- 로깅 ----
    def _log(self, level: str, msg: str, *args):
        if self.logger:
            getattr(self.logger, level, self.logger.info)(msg, *args)
        else:
            print(f'[{level.upper()}] ' + (msg % args if args else msg))

    # ---- 세션 / 헤더 ----
    def _build_session(self) -> requests.Session:
        s = requests.Session()
        s.headers.update({
            'User-Agent': DEFAULT_UA,
            'Accept-Language': 'ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7',
            # br 빼야 함 — brotli 미설치 환경 대응
            'Accept-Encoding': 'gzip, deflate',
        })
        host = urlparse(self.base_url).netloc
        # 핑거프린트 쿠키 — 서버가 이게 없으면 /api/novel-content 등에서 "blocked"
        for name, val in (('ntk_pid', self._ntk_pid), ('ntk_fp', self._ntk_fp)):
            try:
                s.cookies.set(name, val, domain=host, path='/')
            except Exception:
                pass
        # 사용자 입력 쿠키 — `k1=v1; k2=v2` 또는 줄바꿈 구분
        if self._user_cookies:
            for name, val in self._parse_cookie_string(self._user_cookies):
                try:
                    s.cookies.set(name, val, domain=host, path='/')
                except Exception:
                    pass
        if self._proxy_url:
            s.proxies = {'http': self._proxy_url, 'https': self._proxy_url}
        return s

    @staticmethod
    def _parse_cookie_string(raw: str) -> List[Tuple[str, str]]:
        """`k1=v1; k2=v2` / 줄바꿈 / 콤마 구분 → [(name, value), ...]"""
        out: List[Tuple[str, str]] = []
        if not raw:
            return out
        chunks = re.split(r'[;\n]+', raw.strip())
        for c in chunks:
            c = c.strip()
            if not c or '=' not in c:
                continue
            name, _, val = c.partition('=')
            name = name.strip()
            val = val.strip().strip('"')
            if name:
                out.append((name, val))
        return out

    def _html_headers(self, referer: Optional[str] = None) -> Dict[str, str]:
        h = {
            'Accept': ('text/html,application/xhtml+xml,application/xml;'
                       'q=0.9,image/avif,image/webp,*/*;q=0.8'),
            'sec-ch-ua': ('"Chromium";v="148", "Google Chrome";v="148", '
                          '"Not/A)Brand";v="99"'),
            'sec-ch-ua-mobile': '?0',
            'sec-ch-ua-platform': '"Windows"',
            'sec-fetch-dest': 'document',
            'sec-fetch-mode': 'navigate',
            'sec-fetch-site': 'same-origin' if referer else 'none',
            'sec-fetch-user': '?1',
            'upgrade-insecure-requests': '1',
        }
        if referer:
            h['Referer'] = referer
        return h

    def _api_headers(self, referer: str,
                     extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        h = {
            'Accept': '*/*',
            'Origin': self.base_url,
            'Referer': referer,
            'sec-ch-ua': ('"Chromium";v="148", "Google Chrome";v="148", '
                          '"Not/A)Brand";v="99"'),
            'sec-ch-ua-mobile': '?0',
            'sec-ch-ua-platform': '"Windows"',
            'sec-fetch-dest': 'empty',
            'sec-fetch-mode': 'cors',
            'sec-fetch-site': 'same-origin',
        }
        if extra:
            h.update(extra)
        return h

    # ───────────────────── URL / ID 추출 ─────────────────────

    # workId: 숫자(예: '26014') 또는 newtoki 슬러그(예: 'u-mp6o4krq-kn5n') 둘 다 허용
    _WORK_ID_RE = r'(\d+|u-[A-Za-z0-9-]+)'

    @staticmethod
    def parse_input(token: str) -> Optional[Tuple[str, str]]:
        """입력 토큰을 `(kind, work_id)` 로 정규화. work_id 는 항상 str.

        지원:
          - 'https://sbxh1.com/manhwa/26014' → ('manhwa', '26014')
          - 'https://sbxh1.com/manhwa/u-mp6o4krq-kn5n' → ('manhwa', 'u-mp6o4krq-kn5n')
          - 'https://sbxh1.com/webtoon/9291/u-xxx' → ('webtoon', '9291')
                (회차 viewer URL 도 작품 단위로 인식)
          - '/novel/58043' → ('novel', '58043')
          - 'manhwa/26014' → ('manhwa', '26014')
          - '26014' → None (kind 불명, 호출자가 default_kind 지정)
        """
        s = (token or '').strip()
        if not s:
            return None
        m = re.search(r'/(manhwa|webtoon|novel)/' + NewtokiClient._WORK_ID_RE, s)
        if m:
            return (m.group(1), m.group(2))
        m = re.match(r'(manhwa|webtoon|novel)/' + NewtokiClient._WORK_ID_RE
                     + r'$', s)
        if m:
            return (m.group(1), m.group(2))
        return None

    @staticmethod
    def extract_work_id(token: str,
                        default_kind: str) -> Optional[Tuple[str, str]]:
        """`parse_input` 실패 시 default_kind 로 보완 — 순수 숫자 입력 등.

        반환 None 이면 매칭 실패.
        """
        r = NewtokiClient.parse_input(token)
        if r:
            return r
        s = (token or '').strip()
        if default_kind in KIND_PATH and re.fullmatch(
                NewtokiClient._WORK_ID_RE, s):
            return (default_kind, s)
        return None

    @staticmethod
    def extract_episode_url_id(token: str
                               ) -> Optional[Tuple[str, str, str]]:
        """뷰어 URL → (kind, work_id, ep_url_id). 수동 다운에서 쓰임.

        epUrlId 도 숫자/슬러그 둘 다 허용.
        """
        s = (token or '').strip()
        m = re.search(r'/(manhwa|webtoon|novel)/'
                      + NewtokiClient._WORK_ID_RE + r'/([\w-]+)', s)
        if m:
            return (m.group(1), m.group(2), m.group(3))
        return None

    # ───────────────────── 작품 / 회차 목록 ─────────────────────

    def get_work(self, kind: str, work_id: str) -> Dict[str, Any]:
        """작품 페이지 → 메타 + 회차 목록 통합 반환.

        `work_id` 는 숫자 문자열 또는 'u-xxx-xxxx' 슬러그.

        반환:
          {
            'kind': 'manhwa'|'webtoon'|'novel',
            'work_id': str,
            'title': str,
            'thumb': str,            # cover 이미지 URL (있을 경우)
            'description': str,
            'author': str,
            'genres': [str, ...],
            'completed': bool|None,  # 완결 여부 (불확실하면 None)
            'episodes': [
              {'ep_url_id': str, 'no': int, 'title': str, 'paid': bool},
              ...
            ],  # 정렬: 회차 번호 오름차순 (1, 2, 3, ...)
          }
        """
        if kind not in KIND_PATH:
            raise NewtokiError(f'unknown kind: {kind}')
        url = f'{self.base_url}/{KIND_PATH[kind]}/{work_id}'
        r = self._sess.get(url, timeout=20,
                           headers=self._html_headers(
                               referer=f'{self.base_url}/{KIND_PATH[kind]}'))
        if r.status_code == 404:
            raise NewtokiError(f'work not found: {kind}/{work_id}')
        if r.status_code != 200:
            raise NewtokiError(f'work HTTP {r.status_code}: {kind}/{work_id}')
        html = r.text or ''
        body_lower = html[:5000].lower()
        if 'just a moment' in body_lower or 'cdn-cgi/challenge' in body_lower:
            raise BlockedError(f'cf challenge on work page: {kind}/{work_id}')

        meta = self._parse_work_meta(html, kind, work_id)
        if kind == 'novel':
            eps = self._parse_novel_episodes(html, work_id)
        else:
            eps = self._parse_v2_episodes(html, kind, work_id)
        # 회차 번호 오름차순
        eps.sort(key=lambda e: e['no'])
        meta['episodes'] = eps
        return meta

    def _parse_work_meta(self, html: str, kind: str,
                         work_id: str) -> Dict[str, Any]:
        """RSC payload + meta 태그에서 작품 정보 추출."""
        # workTitle은 RSC payload(__next_f.push)에 \"workTitle\":\"...\" 형태
        title = ''
        m = re.search(r'\\"workTitle\\":\\"([^"\\]+)\\"', html)
        if m:
            title = self._json_unescape(m.group(1))
        if not title:
            # og:title 폴백
            m = re.search(
                r'<meta property="og:title"[^>]*content="([^"]+)"', html)
            if m:
                title = m.group(1).split(' | ')[0].strip()
        # description
        desc = ''
        m = re.search(r'<meta name="description" content="([^"]+)"', html)
        if m:
            desc = m.group(1).strip()
        # cover 썸네일 — 다중 폴백 (페이지마다 위치/속성 순서가 다름)
        # 1) RSC payload 의 \"thumb\" 안 <img src="..."> — 가장 신뢰도 높음 (소설)
        # 2) <link rel="preload" ... as="image" href="..."> — 만화 (속성 순서: as,href)
        # 3) <link rel="preload" ... href="..." as="image"> — 소설 (속성 순서: href,as)
        # 4) <meta property="og:image"> — 마지막 폴백 (og-default.png placeholder 제외)
        thumb = self._extract_cover_url(html)
        # 장르 (작품 페이지에 .meta-genres 또는 RSC genres 필드)
        genres: List[str] = []
        m = re.search(r'\\"genres\\":\[(.*?)\]', html)
        if m:
            for gm in re.finditer(r'\\"([^"\\]+)\\"', m.group(1)):
                g = self._json_unescape(gm.group(1)).strip()
                if g:
                    genres.append(g)
        # 작가
        author = ''
        m = re.search(r'\\"author\\":\\"([^"\\]+)\\"', html)
        if m:
            author = self._json_unescape(m.group(1))
        # 완결 여부 (불확실 — RSC에 있을 때만)
        completed: Optional[bool] = None
        m = re.search(r'\\"completed\\":(true|false)', html)
        if m:
            completed = (m.group(1) == 'true')
        return {
            'kind': kind, 'work_id': work_id,
            'title': title or f'{KIND_LABEL.get(kind, kind)}_{work_id}',
            'thumb': thumb, 'description': desc, 'author': author,
            'genres': genres, 'completed': completed,
        }

    # 사이트의 placeholder cover URL 패턴 (실제로 404 또는 의미없는 기본 이미지)
    _COVER_PLACEHOLDER_RE = re.compile(
        r'/og-default\.\w+$|/brand/logo|/default[-_]thumb', re.IGNORECASE)

    @classmethod
    def _extract_cover_url(cls, html: str) -> str:
        """작품 페이지 HTML 에서 cover URL 추출. 못 찾으면 ''."""
        # 1) RSC payload — <div class="thumb"><img src="..."> 형태가 JSON 안에 임베드
        #    패턴: \"thumb\",\"children\":[[\"$\",\"img\",null,{\"src\":\"<url>\"
        m = re.search(
            r'\\"thumb\\"[^{]*?\\"img\\"[^{]*?\\"src\\":\\"([^"\\]+)\\"',
            html)
        if m:
            url = m.group(1).replace(r'&', '&')
            if not cls._COVER_PLACEHOLDER_RE.search(url):
                return url
        # 2) 렌더된 HTML — <div class="thumb"><img src="<url>"
        m = re.search(
            r'<div[^>]*class="[^"]*thumb[^"]*"[^>]*>\s*<img[^>]+src="([^"]+)"',
            html)
        if m:
            url = m.group(1).replace('&amp;', '&')
            if not cls._COVER_PLACEHOLDER_RE.search(url):
                return url
        # 3) <link rel="preload" as="image" href="..."> — 속성 순서 무관
        for m in re.finditer(
                r'<link\s+([^>]*?\brel="preload"[^>]*)>', html):
            attrs = m.group(1)
            if 'as="image"' not in attrs:
                continue
            mh = re.search(r'href="([^"]+)"', attrs)
            if not mh:
                continue
            url = mh.group(1).replace('&amp;', '&')
            if not cls._COVER_PLACEHOLDER_RE.search(url):
                return url
        # 4) og:image — 마지막 폴백
        m = re.search(
            r'<meta property="og:image"[^>]*content="([^"]+)"', html)
        if m:
            url = m.group(1).replace('&amp;', '&')
            if not cls._COVER_PLACEHOLDER_RE.search(url):
                return url
        return ''

    @staticmethod
    def _json_unescape(s: str) -> str:
        """RSC payload 의 \" \\n \\u0041 등을 풀어줌."""
        try:
            return json.loads('"' + s + '"')
        except Exception:
            return s.replace('\\"', '"').replace('\\n', '\n')

    @staticmethod
    def _parse_v2_episodes(html: str, kind: str,
                           work_id: str) -> List[Dict[str, Any]]:
        """만화/웹툰 회차 목록 (`<a class="ep-row-v2-link">` 앵커).

        epUrlId/workId 둘 다 숫자 또는 `u-xxx-xxxx` 슬러그 가능.
        """
        # <li class="ep-row-v2 ep-row-v2--ready|--locked|..."> ... <a class="ep-row-v2-link" href="/<kind>/<wid>/<eid>"> ... <span class="ep-row-v2-no">N</span> ... <strong>title</strong>
        out: List[Dict[str, Any]] = []
        pattern = re.compile(
            r'<li[^>]*class="ep-row-v2([^"]*)"[^>]*>.*?'
            r'<a class="ep-row-v2-link" '
            r'href="/' + re.escape(KIND_PATH[kind]) + r'/'
            + re.escape(work_id) + r'/([\w-]+)">.*?'
            r'<span class="ep-row-v2-no">(\d+)</span>.*?'
            r'<strong>([^<]+)</strong>',
            re.DOTALL)
        for m in pattern.finditer(html):
            cls_extra = m.group(1) or ''
            ep_url_id = m.group(2)
            no = int(m.group(3))
            title = html_lib.unescape(m.group(4)).strip()
            # 유료 회차 표식 (예상): ep-row-v2--locked, ep-row-v2--paid
            paid = ('--locked' in cls_extra or '--paid' in cls_extra)
            out.append({'ep_url_id': ep_url_id, 'no': no,
                        'title': title, 'paid': paid})
        # 같은 url_id 중복 (cta-primary 등 다른 anchor도 매칭될 수 있음) 제거
        seen = set()
        uniq: List[Dict[str, Any]] = []
        for it in out:
            if it['ep_url_id'] in seen:
                continue
            seen.add(it['ep_url_id'])
            uniq.append(it)
        return uniq

    @staticmethod
    def _parse_novel_episodes(html: str,
                              work_id: str) -> List[Dict[str, Any]]:
        """소설 회차 목록 (`<a class="novel-ep-link">` 앵커)."""
        # <li data-ep="N" class="novel-ep-row[--locked]?">
        #   <a href="/novel/<wid>/<eid>" class="novel-ep-link">
        #     <span class="ne-num">N화</span>
        #     <span class="ne-title-wrap"><span class="ne-title">제목</span>
        out: List[Dict[str, Any]] = []
        pattern = re.compile(
            r'<li[^>]*data-ep="(\d+)"[^>]*class="novel-ep-row([^"]*)"[^>]*>'
            r'\s*<a href="/novel/' + re.escape(work_id) + r'/([\w-]+)" '
            r'class="novel-ep-link">'
            r'\s*<span class="ne-num">[^<]*</span>'
            r'\s*<span class="ne-title-wrap">'
            r'\s*<span class="ne-title">([^<]+)</span>',
            re.DOTALL)
        for m in pattern.finditer(html):
            no = int(m.group(1))
            cls_extra = m.group(2) or ''
            ep_url_id = m.group(3)
            title = html_lib.unescape(m.group(4)).strip()
            paid = ('--locked' in cls_extra or '--paid' in cls_extra)
            out.append({'ep_url_id': ep_url_id, 'no': no,
                        'title': title, 'paid': paid})
        return out

    # ───────────────────── 회차 뷰어 ─────────────────────

    @staticmethod
    def _parse_images_rsc(html: str) -> List[str]:
        """RSC payload 의 `"images":[{"page":N,"src":"<url>",...},...]` 에서
        page 순서대로 src URL 리스트 반환.

        `shuffledSrc`/`shuffleSeed` 는 현재 활성화 안 됨 (HAR 분석 + 뷰어.html 확인).
        활성화 되면 후속 패치 필요.
        """
        # find first images array (toonflix 도메인 포함된 것)
        m = re.search(
            r'\\"images\\":\[(.*?)\](?:,\\"|\}|\],\\")', html, re.DOTALL)
        if not m:
            return []
        block = m.group(1)
        # 각 원소: {"page":N,"src":"<url>",...}
        items = []
        for em in re.finditer(
                r'\\"page\\":(\d+),\\"src\\":\\"([^"\\]+)\\"', block):
            page = int(em.group(1))
            src = em.group(2)
            items.append((page, src))
        items.sort(key=lambda x: x[0])
        return [src for _, src in items]

    def get_episode_images(self, kind: str, work_id: str,
                           ep_url_id: str) -> Tuple[List[str], str]:
        """만화/웹툰 회차 → (이미지 URL 리스트, 회차 제목).

        본문 이미지가 없으면 NotReadableError (잠금/공개예정/연령제한 등).
        """
        if kind not in ('manhwa', 'webtoon'):
            raise NewtokiError(f'image flow not for kind={kind}')
        url = f'{self.base_url}/{KIND_PATH[kind]}/{work_id}/{ep_url_id}'
        r = self._sess.get(url, timeout=20,
                           headers=self._html_headers(
                               referer=f'{self.base_url}/{KIND_PATH[kind]}'
                                       f'/{work_id}'))
        if r.status_code == 404:
            raise NotReadableError(f'episode 404: {kind}/{work_id}/{ep_url_id}')
        if r.status_code != 200:
            raise NewtokiError(f'viewer HTTP {r.status_code}: '
                               f'{kind}/{work_id}/{ep_url_id}')
        html = r.text or ''
        # RSC payload 의 "images":[{"page":N,"src":"<url>","shuffledSrc":...}]
        # 만화는 /manhwa/{wid}/{eid}/pNNN.ext, 웹툰은 /webtoon_uploads/<hash>.jpg
        # 양쪽 다 안정적 추출 — RSC 구조 우선, 폴백으로 URL 정규식
        urls = self._parse_images_rsc(html)
        if not urls:
            # 폴백: 만화 패턴 URL 직접 추출 (구버전 회차)
            pattern = (r'https://i\.toonflix\.app/' + re.escape(KIND_PATH[kind])
                       + r'/' + re.escape(work_id) + r'/'
                       + re.escape(ep_url_id) + r'/p\d+\.[a-zA-Z]+')
            urls = re.findall(pattern, html)
            urls = list(dict.fromkeys(urls))
        if not urls:
            # 결제 필요/잠금 회차 — 본문 영역에 NovelPaidGate 같은 안내가 뜸
            if 'novel-paid-gate' in html or 'paid-gate' in html.lower():
                raise NotReadableError(f'paid episode: '
                                       f'{kind}/{work_id}/{ep_url_id}')
            raise NotReadableError(f'no images in viewer: '
                                   f'{kind}/{work_id}/{ep_url_id}')
        # 회차 제목
        subtitle = ''
        m = re.search(r'<title>([^<|]+?)\s*\|', html)
        if m:
            subtitle = m.group(1).strip()
        return urls, subtitle

    def get_novel_episode(self, work_id: str,
                          ep_url_id: str) -> Dict[str, Any]:
        """소설 회차 → 본문 dict (private 모듈 위임)."""
        if _NOVEL_CRYPTO is None:
            raise NewtokiError(
                '소설 다운로드 모듈(_novel_crypto) 미설치 — '
                '이 빌드는 만화/웹툰만 지원합니다.')
        ua = self._sess.headers.get('User-Agent', DEFAULT_UA)
        nv_holder = {'nv': self._nv}
        try:
            result = _NOVEL_CRYPTO.fetch_novel_episode(
                self._sess, self.base_url, work_id, ep_url_id, ua,
                self._html_headers, self._api_headers, nv_holder)
        except _NOVEL_CRYPTO.NovelPaid as e:
            raise NotReadableError(str(e))
        except _NOVEL_CRYPTO.NovelBlocked as e:
            raise BlockedError(str(e))
        except _NOVEL_CRYPTO.NovelFailed as e:
            raise NewtokiError(str(e))
        # nv 캐시 갱신
        if nv_holder.get('nv'):
            self._nv = nv_holder['nv']
        return result

    # ───────────────────── 이미지 다운로드 ─────────────────────

    def download_image(self, url: str, referer: str,
                       max_retries: int = 2) -> bytes:
        """이미지 1장 다운로드 (요청별 Referer 필수)."""
        last_err = None
        for attempt in range(max_retries + 1):
            try:
                r = self._sess.get(url, timeout=30, headers={
                    'Accept': ('image/avif,image/webp,image/apng,'
                               'image/svg+xml,image/*,*/*;q=0.8'),
                    'Referer': referer,
                    'sec-fetch-dest': 'image',
                    'sec-fetch-mode': 'no-cors',
                    'sec-fetch-site': 'cross-site',
                })
                if r.status_code == 200:
                    return r.content
                last_err = NewtokiError(
                    f'image HTTP {r.status_code}: {url[:120]}')
            except Exception as e:
                last_err = NewtokiError(f'image fetch fail: {e}')
            if attempt < max_retries:
                time.sleep(0.5 * (attempt + 1))
        raise last_err or NewtokiError(f'image fetch fail: {url[:120]}')

    @staticmethod
    def url_ext(url: str) -> str:
        """이미지 URL → 확장자 (`.jpg`/`.png`/...)."""
        m = re.search(r'\.([a-zA-Z0-9]{2,5})(?:\?|$)', url or '')
        if not m:
            return '.jpg'
        ext = '.' + m.group(1).lower()
        if ext in ('.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp'):
            return ext
        return '.jpg'

    # ───────────────────── 디버그 / 진단 ─────────────────────

    def ping(self) -> bool:
        """base_url 이 살아있는지 가벼운 GET 으로 확인."""
        try:
            r = self._sess.get(self.base_url + '/', timeout=10,
                               headers=self._html_headers())
            return r.status_code == 200
        except Exception as e:
            self._log('info', 'ping 실패: %s', e)
            return False

    def check_health(self) -> Dict[str, Any]:
        """도메인 + 쿠키 헬스 체크.

        반환:
          {
            'domain_ok': bool,       # base_url GET 200
            'cookies_ok': bool|None, # None = 쿠키 검증 불가 (도메인 죽음 등)
            'reason': str,           # 실패 원인 (사용자 알림 메시지용)
            'status_code': int|None, # 마지막 응답 코드
          }

        판정:
          - 도메인 GET 200 이고 본문에 cf challenge 표식 없음 → domain_ok=True
          - cookies_ok: 본문에 'blocked'/'just a moment' 발견 시 False.
            확정 불가 (정상 응답) 시 True.
        """
        result: Dict[str, Any] = {
            'domain_ok': False, 'cookies_ok': None,
            'reason': '', 'status_code': None,
        }
        try:
            r = self._sess.get(self.base_url + '/', timeout=10,
                               headers=self._html_headers())
        except Exception as e:
            result['reason'] = f'접속 불가: {e}'
            return result
        result['status_code'] = r.status_code
        if r.status_code != 200:
            result['reason'] = f'HTTP {r.status_code} — 도메인 변경/만료 의심'
            return result
        body_head = (r.text or '')[:8000].lower()
        if ('just a moment' in body_head
                or 'cdn-cgi/challenge' in body_head
                or 'cloudflare' in body_head and 'challenge' in body_head):
            result['domain_ok'] = True
            result['cookies_ok'] = False
            result['reason'] = 'Cloudflare 챌린지 — 쿠키/핑거프린트 만료 의심'
            return result
        result['domain_ok'] = True
        result['cookies_ok'] = True
        return result

    def looks_like_newtoki_home(self) -> bool:
        """homepage 본문에서 뉴토끼 시그니처를 가볍게 검사.

        / 페이지에 '/manhwa/' 또는 '/webtoon/' 또는 '/novel/' 링크가 있고,
        Cloudflare 챌린지 화면이 아니면 True.
        """
        try:
            r = self._sess.get(self.base_url + '/', timeout=15,
                               headers=self._html_headers())
        except Exception:
            return False
        if r.status_code != 200:
            return False
        body = (r.text or '')[:50000].lower()
        if 'just a moment' in body or 'cdn-cgi/challenge' in body:
            return False
        hits = 0
        for path in ('/manhwa/', '/webtoon/', '/novel/'):
            if path in body:
                hits += 1
        # 두 개 이상 카테고리 링크가 보이면 뉴토끼로 인정
        return hits >= 2

    @staticmethod
    def increment_base_url_candidates(current_base_url: str,
                                      max_try: int = 3) -> List[str]:
        """현재 base_url 의 호스트 끝 숫자(첫 TLD 직전) 를 +1 ~ +max_try 증가시킨 후보 반환.

        예) https://sbxh1.com   → [sbxh2.com, sbxh3.com, sbxh4.com]
            https://newtoki001.com → [newtoki002.com, newtoki003.com, newtoki004.com]
            https://m.sbxh3.co.kr → [m.sbxh4.co.kr, m.sbxh5.co.kr, m.sbxh6.co.kr]
            https://newtoki.com   → []   (끝에 숫자 없음 — 증가 불가)
        """
        if not current_base_url:
            return []
        parsed = urlparse(current_base_url)
        host = parsed.netloc
        scheme = parsed.scheme or 'https'
        # 호스트 끝쪽: (\d+)(.tld[.tld]...) 형태
        m = re.search(r'(\d+)((?:\.[a-z]+)+)$', host, re.IGNORECASE)
        if not m:
            return []
        num_str = m.group(1)
        suffix = m.group(2)
        prefix = host[:m.start(1)]
        cur = int(num_str)
        width = len(num_str)
        out: List[str] = []
        for delta in range(1, max_try + 1):
            n = cur + delta
            new_num = str(n).zfill(width)
            out.append(f'{scheme}://{prefix}{new_num}{suffix}')
        return out

    @classmethod
    def resolve_base_url(cls, current_base_url: str,
                         proxy_url: Optional[str] = None,
                         cookies: Optional[str] = None,
                         max_try: int = 3,
                         logger=None) -> Optional[str]:
        """호스트 끝 숫자를 +1 ~ +max_try 까지 증가시키며 최초로 살아있는 도메인 반환.

        각 후보에 대해 `check_health()` + `looks_like_newtoki_home()` 검증.
        모두 실패 시 None.
        """
        cands = cls.increment_base_url_candidates(current_base_url,
                                                  max_try=max_try)
        if not cands:
            if logger:
                logger.warning('도메인 자동 갱신 불가 — 호스트 끝 숫자 없음: %s',
                               current_base_url)
            return None
        if logger:
            logger.info('도메인 증가 후보 %d개: %s ...', len(cands),
                        ', '.join(cands[:3]))
        for url in cands:
            try:
                cli = cls(base_url=url, logger=logger,
                          proxy_url=proxy_url, cookies=cookies)
                h = cli.check_health()
                if not h['domain_ok']:
                    if logger:
                        logger.info('도메인 %s — %s', url, h['reason'])
                    continue
                if not cli.looks_like_newtoki_home():
                    if logger:
                        logger.info('도메인 %s — 시그니처 미일치', url)
                    continue
                if logger:
                    logger.info('도메인 자동 갱신 성공: %s', url)
                return cli.base_url
            except Exception as e:
                if logger:
                    logger.warning('도메인 %s 검증 예외: %s', url, e)
                continue
        return None

    # ---- 설정값 정규화 ----
    @staticmethod
    def resolve_proxy(use_proxy, proxy_url) -> str:
        """설정값 → 실제 프록시 URL. use_proxy=True 이고 URL 있을 때만."""
        try:
            enabled = (str(use_proxy or 'False').strip() == 'True')
        except Exception:
            enabled = False
        if not enabled:
            return ''
        return (proxy_url or '').strip()
