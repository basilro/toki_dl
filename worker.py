"""스케줄 1회 실행 — 만화/웹툰/소설 작품을 돌면서 무료 회차 다운로드.

폴더 구조:
  {root}/manhwa/{작품}/{NNNN_회차}/{001.jpg ...}
  {root}/webtoon/{작품}/{NNNN_회차}/{001.jpg ...}
  {root}/novel/{작품}/{NNNN_회차}.txt
"""
import os
import re
import threading
from datetime import datetime
from typing import List, Optional, Dict, Any, Tuple

from .client import (NewtokiClient, NewtokiError, NotReadableError,
                     BlockedError, KIND_LABEL, KIND_PATH)
from .model import ModelNewtokiItem
from .notify import send_webhook, build_download_summary
from .setup import *  # P, db, logger


_IMAGE_EXTS = ('.webp', '.jpg', '.jpeg', '.png', '.gif', '.bmp')

# 외부 의존
PIL_AVAILABLE = False
try:
    from PIL import Image  # noqa
    PIL_AVAILABLE = True
except Exception:
    pass


def _safe_filename(s: str) -> str:
    s = re.sub(r'[\\/*?:"<>|]', '_', s or '')
    return s.strip().strip('.')


# ─────────────────────────── ZIP 압축 ───────────────────────────


def compress_episode_folder(ep_folder: str) -> Optional[str]:
    """회차 폴더 → 같은 위치에 .zip 생성. 성공 시 원본 폴더 삭제. 멱등.

    안전장치: 폴더 안에 서브디렉토리가 있으면 회차 폴더가 아닌 작품 폴더로
    판단하여 압축 거부 (실수로 작품 전체를 날리는 사고 방지).
    """
    import shutil
    import zipfile
    if not os.path.isdir(ep_folder):
        return None

    try:
        entries = os.listdir(ep_folder)
    except Exception:
        return None
    for entry in entries:
        if os.path.isdir(os.path.join(ep_folder, entry)):
            P.logger.warning(
                '압축 거부 (서브디렉토리 존재 → 회차 폴더 아님): %s', ep_folder)
            return None

    parent = os.path.dirname(ep_folder)
    name = os.path.basename(ep_folder)
    zip_path = os.path.join(parent, name + '.zip')
    if os.path.exists(zip_path):
        try:
            shutil.rmtree(ep_folder)
        except Exception:
            pass
        return zip_path

    files_to_zip = []
    for f in sorted(entries):
        path = os.path.join(ep_folder, f)
        if os.path.isfile(path) and f.lower().endswith(_IMAGE_EXTS):
            files_to_zip.append((f, path))
    if not files_to_zip:
        return None

    tmp_zip = zip_path + '.tmp'
    try:
        with zipfile.ZipFile(tmp_zip, 'w', zipfile.ZIP_STORED) as zf:
            for arcname, path in files_to_zip:
                zf.write(path, arcname=arcname)
        os.replace(tmp_zip, zip_path)
    except Exception as e:
        if os.path.exists(tmp_zip):
            try:
                os.remove(tmp_zip)
            except Exception:
                pass
        P.logger.warning('압축 실패 %s: %s', ep_folder, e)
        return None

    try:
        shutil.rmtree(ep_folder)
    except Exception as e:
        P.logger.warning('압축 후 폴더 삭제 실패 %s: %s', ep_folder, e)
    return zip_path


# ─────────────────────────── ComicInfo XML ───────────────────────────


def _xml_escape(s) -> str:
    if s is None:
        return ''
    return (str(s).replace('&', '&amp;')
                  .replace('<', '&lt;').replace('>', '&gt;').strip())


_INFO_XML = '''<?xml version="1.0"?>
<ComicInfo xmlns:xsd="http://www.w3.org/2001/XMLSchema" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <Title>{title}</Title>
  <Series>{title}</Series>
  <Summary>{desc}</Summary>
  <Writer>{author}</Writer>
  <Publisher>{publisher}</Publisher>
  <Genre>{genre}</Genre>
  <Tags>{tags}</Tags>
  <LanguageISO>ko</LanguageISO>
  <Notes>{notes}</Notes>
</ComicInfo>'''


def build_info_xml(meta: Dict[str, Any]) -> str:
    """뉴토끼 작품 메타 → ComicInfo XML.

    `meta`: NewtokiClient.get_work() 의 결과 (또는 부분).
    """
    title = meta.get('title') or ''
    desc = meta.get('description') or ''
    author = meta.get('author') or ''
    genres = meta.get('genres') or []
    kind = meta.get('kind') or ''
    publisher_label = '뉴토끼' + (
        f' ({KIND_LABEL.get(kind, kind)})' if kind else '')
    tags = ['뉴토끼']
    if kind:
        tags.append(KIND_LABEL.get(kind, kind))
    completed = meta.get('completed')
    if completed is True:
        notes = '완결'
    elif completed is False:
        notes = '연재중'
    else:
        notes = ''
    return _INFO_XML.format(
        title=_xml_escape(title),
        desc=_xml_escape(desc),
        author=_xml_escape(author),
        publisher=_xml_escape(publisher_label),
        genre=_xml_escape(', '.join(g for g in genres if g)),
        tags=_xml_escape(', '.join(t for t in tags if t)),
        notes=_xml_escape(notes),
    )


def title_dir_for(download_root: str, kind: str, title_name: str) -> str:
    """저장 폴더: {root}/{kind}/{safe_title}/"""
    return os.path.join(download_root, kind, _safe_filename(title_name))


def _download_cover_to_jpg(client: NewtokiClient, url: str,
                           dest_path: str, referer: str) -> bool:
    """cover URL → JPG 변환 저장. PIL 없으면 원본 그대로 저장."""
    if not url:
        return False
    try:
        data = client.download_image(url, referer=referer)
    except Exception as e:
        P.logger.warning('cover 다운 실패: %s', e)
        return False
    if not data:
        return False
    if data[:3] == b'\xff\xd8\xff':
        with open(dest_path, 'wb') as fp:
            fp.write(data)
        return True
    if not PIL_AVAILABLE:
        # PIL 없으면 원본 그대로 (.webp/.png 인 경우도 .jpg 확장자로 저장)
        with open(dest_path, 'wb') as fp:
            fp.write(data)
        return True
    try:
        import io
        img = Image.open(io.BytesIO(data))
        if img.mode in ('RGBA', 'LA'):
            bg = Image.new('RGB', img.size, (255, 255, 255))
            bg.paste(img, mask=img.split()[-1])
            img = bg
        elif img.mode == 'P':
            img = img.convert('RGBA')
            bg = Image.new('RGB', img.size, (255, 255, 255))
            bg.paste(img, mask=img.split()[-1])
            img = bg
        elif img.mode != 'RGB':
            img = img.convert('RGB')
        img.save(dest_path, format='JPEG', quality=92, optimize=False)
        return True
    except Exception as e:
        P.logger.warning('cover JPG 변환 실패 — 원본 저장: %s', e)
        with open(dest_path, 'wb') as fp:
            fp.write(data)
        return True


def ensure_title_metadata(client: NewtokiClient, download_root: str,
                          meta: Dict[str, Any]) -> Dict[str, Any]:
    """작품 폴더에 info.xml / cover.jpg 없으면 생성. 멱등."""
    result = {'info': False, 'cover': False, 'dir': ''}
    kind = meta.get('kind')
    title = meta.get('title') or ''
    if not kind or not title:
        return result
    folder = title_dir_for(download_root, kind, title)
    result['dir'] = folder
    try:
        os.makedirs(folder, exist_ok=True)
    except Exception as e:
        P.logger.warning('[%s] 작품 폴더 생성 실패: %s', title, e)
        return result

    info_path = os.path.join(folder, 'info.xml')
    if not os.path.exists(info_path):
        try:
            with open(info_path, 'w', encoding='utf-8') as fp:
                fp.write(build_info_xml(meta))
            P.logger.info('[%s] info.xml 생성', title)
            result['info'] = True
        except Exception as e:
            P.logger.warning('[%s] info.xml 생성 실패: %s', title, e)

    cover_path = os.path.join(folder, 'cover.jpg')
    if not os.path.exists(cover_path) and meta.get('thumb'):
        ref = f'{client.base_url}/{KIND_PATH[kind]}/{meta["work_id"]}'
        if _download_cover_to_jpg(client, meta['thumb'], cover_path, ref):
            P.logger.info('[%s] cover.jpg 생성', title)
            result['cover'] = True
    return result


# ─────────────────────────── 진행 상태 ───────────────────────────


_auto_state_lock = threading.Lock()
_auto_state: Dict[str, Any] = {
    'status': 'idle',
    'started_at': None,
    'finished_at': None,
    'message': '',
    'titles_total': 0,
    'titles_done': 0,
    'current_title': '',
    'current_phase': '',
    'current_episode': '',
    'current_pages_done': 0,
    'current_pages_total': 0,
    'summary': {'downloaded': 0, 'skipped': 0, 'failed': 0},
}


def get_auto_state() -> Dict[str, Any]:
    with _auto_state_lock:
        snap = dict(_auto_state)
        snap['summary'] = dict(_auto_state['summary'])
        return snap


def _auto_set(**kw):
    with _auto_state_lock:
        _auto_state.update(kw)


def _auto_reset():
    with _auto_state_lock:
        _auto_state.update({
            'status': 'idle', 'started_at': None, 'finished_at': None,
            'message': '', 'titles_total': 0, 'titles_done': 0,
            'current_title': '', 'current_phase': '',
            'current_episode': '', 'current_pages_done': 0,
            'current_pages_total': 0,
            'summary': {'downloaded': 0, 'skipped': 0, 'failed': 0},
        })


def _auto_summary_inc(key: str, delta: int = 1):
    with _auto_state_lock:
        _auto_state['summary'][key] = _auto_state['summary'].get(key, 0) + delta


# ─────────────────────────── Worker ───────────────────────────


def _split_items(raw: str) -> List[str]:
    """줄/| 구분 입력 → 토큰 리스트."""
    out = []
    for chunk in (raw or '').replace('\r', '').replace('|', '\n').split('\n'):
        s = chunk.strip()
        if s:
            out.append(s)
    return out


class Worker:

    def __init__(self):
        self.cfg = P.ModelSetting.to_dict()
        self.download_root = (self.cfg.get('download_path') or '').strip()
        self.base_url = (self.cfg.get('base_url') or '').strip() or None

        # 3개 textarea → [(kind, raw), ...]
        self.items: List[Tuple[str, str]] = []
        for raw in _split_items(self.cfg.get('titles') or ''):
            self.items.append(('manhwa', raw))
        for raw in _split_items(self.cfg.get('titles_webtoon') or ''):
            self.items.append(('webtoon', raw))
        for raw in _split_items(self.cfg.get('titles_novel') or ''):
            self.items.append(('novel', raw))

        try:
            self.max_per_run = int(self.cfg.get('max_per_run') or '5')
        except Exception:
            self.max_per_run = 5
        if self.max_per_run < 1:
            self.max_per_run = 1

        self.use_compress = (self.cfg.get('use_compress') or 'False') == 'True'
        self.proxy_url = NewtokiClient.resolve_proxy(
            self.cfg.get('use_proxy'), self.cfg.get('proxy_url'))
        self.cookies = (self.cfg.get('cookies') or '').strip() or None
        self.flaresolverr_url = (
            self.cfg.get('flaresolverr_url') or '').strip() or None
        self.auto_resolve = (
            (self.cfg.get('auto_resolve_base_url') or 'False') == 'True')
        self.announcer_url = (
            self.cfg.get('announcer_url') or '').strip()
        self.notify_download_url = (
            self.cfg.get('notify_webhook_download') or '').strip()
        self.notify_download_novel_url = (
            self.cfg.get('notify_webhook_download_novel') or '').strip()
        self.notify_alert_url = (
            self.cfg.get('notify_webhook_alert') or '').strip()

        self.client: Optional[NewtokiClient] = None
        # 완료 항목 — 이미지(만화/웹툰)와 소설을 분리해 별도 웹훅
        self.completed_image: List[Dict[str, Any]] = []
        self.completed_novel: List[Dict[str, Any]] = []
        # 한 실행 중 알림 중복 발송 방지
        self._alert_sent = False

    # ──────────────────────── public: run ────────────────────────

    def run(self) -> dict:
        P.logger.info('[basic] Worker.run BEGIN items=%d (max_per_run=%d)',
                      len(self.items), self.max_per_run)
        _auto_reset()
        _auto_set(status='running', started_at=datetime.now().isoformat(),
                  message='시작', titles_total=len(self.items))
        if not self.download_root:
            _auto_set(status='error', finished_at=datetime.now().isoformat(),
                      message='download_path 미설정')
            return {'ret': 'fail', 'reason': 'no_download_path'}
        if not self.items:
            _auto_set(status='error', finished_at=datetime.now().isoformat(),
                      message='체크할 작품 미설정')
            return {'ret': 'fail', 'reason': 'no_titles'}

        try:
            self.client = NewtokiClient(base_url=self.base_url,
                                        logger=P.logger,
                                        proxy_url=self.proxy_url,
                                        cookies=self.cookies,
                                        flaresolverr_url=self.flaresolverr_url)
        except Exception as e:
            _auto_set(status='error', finished_at=datetime.now().isoformat(),
                      message=f'클라이언트 초기화 실패: {e}')
            return {'ret': 'fail', 'reason': 'client_init', 'msg': str(e)}

        health = self.client.check_health()
        if not health['domain_ok']:
            old_url = self.client.base_url
            # 자동 갱신 시도 — 호스트 끝 숫자 증가 방식
            new_url = None
            if self.auto_resolve:
                _auto_set(current_phase='resolve_base_url')
                P.logger.info('[basic] 도메인 자동 갱신 시도: %s → +N', old_url)
                try:
                    new_url = NewtokiClient.resolve_base_url(
                        current_base_url=old_url,
                        proxy_url=self.proxy_url,
                        cookies=self.cookies,
                        flaresolverr_url=self.flaresolverr_url,
                        logger=P.logger)
                except Exception as e:
                    P.logger.warning('도메인 자동 갱신 예외: %s', e)
            if new_url:
                P.ModelSetting.set('base_url', new_url)
                self.base_url = new_url
                # 새 클라이언트로 재시작
                self.client = NewtokiClient(base_url=new_url,
                                            logger=P.logger,
                                            proxy_url=self.proxy_url,
                                            cookies=self.cookies,
                                            flaresolverr_url=self.flaresolverr_url)
                health = self.client.check_health()
                self._send_alert(
                    f'[뉴토끼] 도메인 자동 갱신 (숫자 증가) — '
                    f'{old_url} → {new_url}')
                if not health['domain_ok']:
                    self._send_alert(
                        f'[뉴토끼] 자동 갱신 후에도 접속 실패 — {new_url}\n'
                        f'사유: {health["reason"]}')
                    _auto_set(status='error',
                              finished_at=datetime.now().isoformat(),
                              message=f'갱신 도메인도 접속 실패: {new_url}')
                    return {'ret': 'fail', 'reason': 'unreachable_after_resolve'}
            else:
                hint = (f'\n안내 사이트 확인: {self.announcer_url}'
                        if self.announcer_url else '')
                msg = (f'[뉴토끼] 도메인 접속 실패 — {old_url}\n'
                       f'사유: {health["reason"]}\n'
                       + ('→ 숫자 증가 후보 모두 실패, 수동 갱신 필요'
                          if self.auto_resolve
                          else '→ 설정 → 인증 탭에서 도메인 갱신 필요')
                       + hint)
                self._send_alert(msg)
                _auto_set(status='error',
                          finished_at=datetime.now().isoformat(),
                          message=f'base_url 접속 실패: {old_url}')
                return {'ret': 'fail', 'reason': 'unreachable'}
        if health.get('cookies_ok') is False:
            msg = (f'[뉴토끼] 쿠키/핑거프린트 만료 의심 — {self.client.base_url}\n'
                   f'사유: {health["reason"]}\n'
                   f'→ 설정 → 인증 탭에서 쿠키 갱신 필요')
            self._send_alert(msg)
            # 쿠키 만료지만 일단 실행은 계속 (일부 페이지는 받아질 수 있음)

        summary = {'titles': len(self.items),
                   'downloaded': 0, 'skipped': 0, 'failed': 0}
        for kind, raw in self.items:
            _auto_set(current_title=f'[{KIND_LABEL.get(kind, kind)}] {raw}',
                      current_phase='resolve',
                      current_episode='', current_pages_done=0,
                      current_pages_total=0)
            try:
                got = self._process_item(kind, raw)
                if got == 'downloaded':
                    summary['downloaded'] += 1; _auto_summary_inc('downloaded')
                elif got == 'skipped':
                    summary['skipped'] += 1; _auto_summary_inc('skipped')
                else:
                    summary['failed'] += 1; _auto_summary_inc('failed')
            except Exception as e:
                import traceback
                P.logger.error('[%s] item %r 예외: %s', kind, raw, e)
                P.logger.error(traceback.format_exc())
                summary['failed'] += 1; _auto_summary_inc('failed')
            _auto_set(titles_done=summary['downloaded'] + summary['skipped']
                      + summary['failed'])

        # ---- 완료 요약 알림 (받은 게 있을 때만, 이미지/소설 분리) ----
        if self.completed_image and self.notify_download_url:
            try:
                msg = build_download_summary(self.completed_image, bucket='image')
                if msg:
                    ok = send_webhook(self.notify_download_url, msg)
                    P.logger.info('만화/웹툰 다운 요약 알림: %s (%d건)',
                                  'OK' if ok else 'FAIL',
                                  len(self.completed_image))
            except Exception as e:
                P.logger.warning('만화/웹툰 요약 알림 예외: %s', e)
        if self.completed_novel and self.notify_download_novel_url:
            try:
                msg = build_download_summary(self.completed_novel, bucket='novel')
                if msg:
                    ok = send_webhook(self.notify_download_novel_url, msg)
                    P.logger.info('소설 다운 요약 알림: %s (%d건)',
                                  'OK' if ok else 'FAIL',
                                  len(self.completed_novel))
            except Exception as e:
                P.logger.warning('소설 요약 알림 예외: %s', e)

        _auto_set(status='done', finished_at=datetime.now().isoformat(),
                  current_title='', current_phase='', current_episode='',
                  message=(f"완료 — 다운 {summary['downloaded']}, "
                           f"스킵 {summary['skipped']}, "
                           f"실패 {summary['failed']}"))
        return {'ret': 'success', **summary}

    # ──────────────────────── per item ────────────────────────

    def _process_item(self, default_kind: str, raw: str) -> str:
        """item raw → 작품 1개 처리."""
        parsed = NewtokiClient.extract_work_id(raw, default_kind)
        if not parsed:
            P.logger.warning('[%s] %r workId 추출 실패', default_kind, raw)
            return 'failed'
        kind, work_id = parsed
        P.logger.info('[%s] %r → kind=%s work_id=%s',
                      default_kind, raw, kind, work_id)

        _auto_set(current_phase='fetch_episodes')
        try:
            work = self.client.get_work(kind, work_id)
        except NewtokiError as e:
            P.logger.warning('[%s/%d] get_work 실패: %s', kind, work_id, e)
            return 'failed'

        title = work.get('title') or f'{KIND_LABEL.get(kind, kind)}_{work_id}'
        _auto_set(current_title=f'[{KIND_LABEL.get(kind, kind)}] {title}')

        # info.xml / cover.jpg 자동 생성 (다운로드 여부 무관)
        try:
            self._ensure_title_metadata(work)
        except Exception as e:
            P.logger.warning('[%s] 메타 자동 생성 예외 (계속): %s', title, e)

        episodes = work.get('episodes') or []
        if not episodes:
            P.logger.warning('[%s] 회차 없음', title)
            return 'failed'

        # 분류: 무료 & 미수신 → 다운 대상
        free_pending: List[Dict[str, Any]] = []
        paid_cnt = 0
        for ep in episodes:
            if ep.get('paid'):
                paid_cnt += 1
                continue
            rec = (db.session.query(ModelNewtokiItem)
                   .filter_by(work_kind=kind, work_id=work_id,
                              ep_url_id=ep['ep_url_id']).first())
            if rec and rec.status == 'completed':
                continue
            free_pending.append(ep)
        P.logger.info('[%s] 미수신 무료 %d, 유료 %d (max_per_run=%d)',
                      title, len(free_pending), paid_cnt, self.max_per_run)

        if not free_pending:
            return 'skipped'

        # 최신 회차 우선 다운 (높은 번호부터 limit 만큼) — 다 받으면 그 다음 회차도
        free_pending.sort(key=lambda e: e['no'], reverse=True)
        targets = free_pending[:self.max_per_run]
        # 내부에서는 회차 번호 오름차순으로 받아야 사용자 입장에서 자연스러움
        targets.sort(key=lambda e: e['no'])

        downloaded = 0
        _auto_set(current_phase='downloading')
        for ep in targets:
            _auto_set(current_episode=ep.get('title', ''),
                      current_pages_done=0, current_pages_total=0)
            if self._download_one(kind, work_id, title, ep) == 'downloaded':
                downloaded += 1
        return 'downloaded' if downloaded else 'skipped'

    # ──────────────────────── one episode ────────────────────────

    def _download_one(self, kind: str, work_id: str, title: str,
                      ep: Dict[str, Any]) -> str:
        ep_url_id = ep['ep_url_id']
        no = int(ep.get('no') or 0)
        ep_title = ep.get('title') or f'{no}화'

        rec = (db.session.query(ModelNewtokiItem)
               .filter_by(work_kind=kind, work_id=work_id,
                          ep_url_id=ep_url_id).first())
        if rec and rec.status == 'completed':
            return 'skipped'
        if rec is None:
            rec = ModelNewtokiItem()
            rec.work_kind = kind
            rec.work_id = work_id
            rec.work_title = title
            rec.ep_url_id = ep_url_id
            rec.episode_no = no
            rec.episode_title = ep_title
            db.session.add(rec)
            db.session.commit()
        rec.updated_time = datetime.now()
        rec.status = 'downloading'
        db.session.commit()

        if kind == 'novel':
            return self._download_novel(rec, kind, work_id, title, ep)
        return self._download_images(rec, kind, work_id, title, ep)

    def _download_images(self, rec: 'ModelNewtokiItem', kind: str,
                         work_id: str, title: str,
                         ep: Dict[str, Any]) -> str:
        ep_url_id = ep['ep_url_id']
        no = int(ep.get('no') or 0)
        ep_title = ep.get('title') or f'{no}화'

        try:
            urls, subtitle = self.client.get_episode_images(
                kind, work_id, ep_url_id)
        except NotReadableError as e:
            rec.status = 'skipped_paid'; rec.error_msg = str(e)
            db.session.commit()
            return 'skipped'
        except BlockedError as e:
            rec.status = 'failed'; rec.error_msg = f'blocked: {e}'
            db.session.commit()
            self._send_alert(
                f'[뉴토끼] 다운로드 중 차단 발생 (만화/웹툰) — {kind}/{work_id}\n'
                f'사유: {e}\n→ 쿠키/IP 만료 의심, 설정 → 인증 탭 확인')
            return 'failed'
        except NewtokiError as e:
            rec.status = 'failed'; rec.error_msg = f'images: {e}'
            db.session.commit()
            return 'failed'

        # ep_title 보강 (회차 목록의 title 우선, 비어있으면 뷰어 title)
        if not ep_title.strip() and subtitle:
            ep_title = subtitle

        save_dir = os.path.join(
            title_dir_for(self.download_root, kind, title),
            f'{no:04d}_{_safe_filename(ep_title)}')
        os.makedirs(save_dir, exist_ok=True)
        rec.save_dir = save_dir
        rec.page_count = len(urls)
        db.session.commit()
        _auto_set(current_pages_total=len(urls), current_pages_done=0)

        referer = f'{self.client.base_url}/{KIND_PATH[kind]}/{work_id}/{ep_url_id}'
        downloaded = 0
        total_bytes = 0
        failed: List[Tuple[int, str]] = []
        for i, url in enumerate(urls, start=1):
            try:
                data = self.client.download_image(url, referer=referer)
                ext = NewtokiClient.url_ext(url)
                local = os.path.join(save_dir, f'{i:03d}{ext}')
                with open(local, 'wb') as fp:
                    fp.write(data)
                total_bytes += len(data)
                downloaded += 1
                _auto_set(current_pages_done=downloaded)
            except Exception as e:
                failed.append((i, str(e)))
                P.logger.warning('[%s] %s page %d 실패: %s',
                                 title, ep_title, i, e)

        rec.downloaded_count = downloaded
        rec.total_bytes = total_bytes
        rec.downloaded_at = datetime.now()
        rec.updated_time = rec.downloaded_at
        if downloaded == len(urls):
            rec.status = 'completed'
            P.logger.info('[%s] %s 다운로드 완료 (%d개, %.1fKB)',
                          title, ep_title, downloaded, total_bytes / 1024)
            self.completed_image.append({
                'work_kind': kind, 'series_title': title,
                'episode_title': ep_title, 'episode_no': no,
            })
        elif downloaded > 0:
            rec.status = 'partial'
            rec.error_msg = f'failed {len(failed)}/{len(urls)}'
        else:
            rec.status = 'failed'
            rec.error_msg = f'all failed ({len(urls)})'
        db.session.commit()

        # 압축 옵션 — 정상 완료 + 이미지 종류만 (소설은 위에서 분기)
        if self.use_compress and rec.status == 'completed':
            zip_path = compress_episode_folder(save_dir)
            if zip_path:
                rec.save_dir = zip_path
                db.session.commit()
                P.logger.info('[%s] %s 압축 완료 → %s',
                              title, ep_title, zip_path)

        return 'downloaded' if rec.status in ('completed', 'partial') else 'failed'

    def _download_novel(self, rec: 'ModelNewtokiItem', kind: str,
                        work_id: str, title: str,
                        ep: Dict[str, Any]) -> str:
        ep_url_id = ep['ep_url_id']
        no = int(ep.get('no') or 0)
        ep_title = ep.get('title') or f'{no}화'

        try:
            data = self.client.get_novel_episode(work_id, ep_url_id)
        except NotReadableError as e:
            rec.status = 'skipped_paid'; rec.error_msg = str(e)
            db.session.commit()
            return 'skipped'
        except BlockedError as e:
            rec.status = 'failed'; rec.error_msg = f'blocked: {e}'
            db.session.commit()
            self._send_alert(
                f'[뉴토끼] 다운로드 중 차단 발생 (소설) — {work_id}\n'
                f'사유: {e}\n→ 쿠키/IP 만료 의심, 설정 → 인증 탭 확인')
            return 'failed'
        except NewtokiError as e:
            rec.status = 'failed'; rec.error_msg = f'novel: {e}'
            db.session.commit()
            return 'failed'

        paragraphs = data.get('paragraphs') or []
        if not paragraphs:
            rec.status = 'failed'
            rec.error_msg = 'no paragraphs'
            db.session.commit()
            return 'failed'

        series_dir = title_dir_for(self.download_root, kind, title)
        os.makedirs(series_dir, exist_ok=True)
        fname = f'{no:04d}_{_safe_filename(ep_title)}.txt'
        save_path = os.path.join(series_dir, fname)

        content = '\n\n'.join(paragraphs)
        with open(save_path, 'w', encoding='utf-8') as fp:
            fp.write(content)
        total_bytes = os.path.getsize(save_path)

        rec.save_dir = save_path
        rec.page_count = len(paragraphs)
        rec.downloaded_count = len(paragraphs)
        rec.total_bytes = total_bytes
        rec.downloaded_at = datetime.now()
        rec.updated_time = rec.downloaded_at
        rec.status = 'completed'
        db.session.commit()
        P.logger.info('[%s] %s 다운로드 완료 (%d 단락, %.1fKB)',
                      title, ep_title, len(paragraphs), total_bytes / 1024)
        self.completed_novel.append({
            'work_kind': kind, 'series_title': title,
            'episode_title': ep_title, 'episode_no': no,
        })
        return 'downloaded'

    # ──────────────────────── 메타 동기화 (UI 버튼) ────────────────────────

    def _ensure_title_metadata(self, meta: Dict[str, Any]) -> Dict[str, Any]:
        return ensure_title_metadata(self.client, self.download_root, meta)

    # ──────────────────────── 알림 발송 ────────────────────────

    def _send_alert(self, message: str):
        """도메인/쿠키 만료 알림 — 한 실행당 최대 1회."""
        if self._alert_sent or not self.notify_alert_url:
            return
        try:
            ok = send_webhook(self.notify_alert_url, message)
            P.logger.info('알림 발송: %s', 'OK' if ok else 'FAIL')
        except Exception as e:
            P.logger.warning('알림 발송 예외: %s', e)
        self._alert_sent = True

    def sync_metadata_all(self) -> dict:
        """체크할 작품 폴더에 info.xml/cover.jpg 누락분 생성.

        다운로드 폴더에 작품 폴더가 이미 있는 항목만 처리.
        """
        P.logger.info('[basic] sync_metadata_all BEGIN items=%d',
                      len(self.items))
        _auto_reset()
        _auto_set(status='running', started_at=datetime.now().isoformat(),
                  message='메타 동기화 시작', titles_total=len(self.items))
        if not self.download_root:
            _auto_set(status='error', finished_at=datetime.now().isoformat(),
                      message='download_path 미설정')
            return {'ret': 'fail', 'reason': 'no_download_path'}
        if not self.items:
            _auto_set(status='error', finished_at=datetime.now().isoformat(),
                      message='체크할 작품 미설정')
            return {'ret': 'fail', 'reason': 'no_titles'}

        try:
            self.client = NewtokiClient(base_url=self.base_url,
                                        logger=P.logger,
                                        proxy_url=self.proxy_url,
                                        cookies=self.cookies,
                                        flaresolverr_url=self.flaresolverr_url)
        except Exception as e:
            _auto_set(status='error', finished_at=datetime.now().isoformat(),
                      message=f'클라이언트 초기화 실패: {e}')
            return {'ret': 'fail', 'reason': 'client_init', 'msg': str(e)}

        summary = {'titles': len(self.items), 'info': 0, 'cover': 0,
                   'skipped_no_folder': 0, 'failed': 0}
        for default_kind, raw in self.items:
            _auto_set(current_title=f'[{KIND_LABEL.get(default_kind, default_kind)}] {raw}',
                      current_phase='sync_metadata',
                      current_episode='', current_pages_done=0,
                      current_pages_total=0)
            try:
                parsed = NewtokiClient.extract_work_id(raw, default_kind)
                if not parsed:
                    summary['failed'] += 1
                    continue
                kind, work_id = parsed
                # 메타 조회 — 폴더 존재 여부 먼저 봐서 없으면 API 호출 안 함
                title_guess = raw
                folder = title_dir_for(self.download_root, kind, title_guess)
                meta: Dict[str, Any] = {}
                if not os.path.isdir(folder):
                    # 작품명 미상 — API로 메타 받아서 실제 폴더 확인
                    try:
                        meta = self.client.get_work(kind, work_id)
                    except NewtokiError as e:
                        P.logger.warning('[%s] meta 실패: %s', raw, e)
                        summary['failed'] += 1
                        continue
                    real_title = meta.get('title') or ''
                    if real_title:
                        folder = title_dir_for(self.download_root, kind,
                                               real_title)
                    if not os.path.isdir(folder):
                        summary['skipped_no_folder'] += 1
                        continue
                    title_guess = real_title
                _auto_set(current_title=f'[{KIND_LABEL.get(kind, kind)}] {title_guess}')

                info_p = os.path.join(folder, 'info.xml')
                cover_p = os.path.join(folder, 'cover.jpg')
                if os.path.isfile(info_p) and os.path.isfile(cover_p):
                    continue

                if not meta:
                    try:
                        meta = self.client.get_work(kind, work_id)
                    except NewtokiError as e:
                        P.logger.warning('[%s] meta 실패: %s', raw, e)
                        summary['failed'] += 1
                        continue
                r = self._ensure_title_metadata(meta)
                if r.get('info'):
                    summary['info'] += 1
                if r.get('cover'):
                    summary['cover'] += 1
            except Exception as e:
                import traceback
                P.logger.error('[sync_metadata] %r 예외: %s', raw, e)
                P.logger.error(traceback.format_exc())
                summary['failed'] += 1
            _auto_set(titles_done=(summary['info'] + summary['cover']
                                   + summary['skipped_no_folder']
                                   + summary['failed']))

        _auto_set(status='done', finished_at=datetime.now().isoformat(),
                  current_title='', current_phase='', current_episode='',
                  message=(f"메타 동기화 완료 — info {summary['info']}, "
                           f"cover {summary['cover']}, "
                           f"폴더없음 {summary['skipped_no_folder']}, "
                           f"실패 {summary['failed']}"))
        return {'ret': 'success', **summary}

    # ──────────────────────── 회차 폴더 일괄 압축 ────────────────────────

    def compress_all(self) -> dict:
        """download_path 아래 모든 회차 폴더 ZIP 압축.

        '회차 폴더' = 서브디렉토리 없는 leaf + 이미지 보유 폴더.
        소설(.txt) / 작품 메타만 있는 폴더는 자동 제외.
        """
        P.logger.info('[basic] compress_all BEGIN root=%s', self.download_root)
        _auto_reset()
        _auto_set(status='running', started_at=datetime.now().isoformat(),
                  message='압축 시작')
        if not self.download_root or not os.path.isdir(self.download_root):
            _auto_set(status='error', finished_at=datetime.now().isoformat(),
                      message='download_path 미설정/없음')
            return {'ret': 'fail', 'reason': 'no_download_path'}

        candidates: List[str] = []
        for root, dirs, files in os.walk(self.download_root):
            if dirs:
                continue
            if any(f.lower().endswith(_IMAGE_EXTS) for f in files):
                candidates.append(root)

        _auto_set(titles_total=len(candidates))
        compressed = 0; skipped = 0; failed = 0
        for idx, ep in enumerate(candidates, start=1):
            rel = os.path.relpath(ep, self.download_root)
            _auto_set(current_title=rel, current_phase='compressing',
                      titles_done=idx - 1)
            try:
                zip_path = compress_episode_folder(ep)
                if zip_path:
                    compressed += 1
                else:
                    skipped += 1
            except Exception as e:
                P.logger.warning('압축 예외 %s: %s', ep, e)
                failed += 1
            _auto_set(titles_done=idx)

        _auto_set(status='done', finished_at=datetime.now().isoformat(),
                  current_title='', current_phase='',
                  message=(f'압축 완료 — 처리 {compressed}개, '
                           f'스킵 {skipped}개, 실패 {failed}개'))
        P.logger.info('[basic] compress_all END processed=%d skipped=%d failed=%d',
                      compressed, skipped, failed)
        return {'ret': 'success', 'processed': compressed,
                'skipped': skipped, 'failed': failed}
