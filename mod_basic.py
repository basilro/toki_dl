import threading
import traceback

from .model import ModelNewtokiItem
from .setup import *
from .worker import Worker


class ModuleBasic(PluginModuleBase):

    def __init__(self, P):
        super(ModuleBasic, self).__init__(
            P, name='basic', first_menu='setting',
            scheduler_desc='뉴토끼 자동 다운로드',
        )
        self.db_default = {
            f'db_version': '1',
            f'{self.name}_auto_start': 'False',
            # 6시간마다 — 뉴토끼는 회차 업데이트 빈도가 작품마다 달라 보수적
            f'{self.name}_interval': '0 */6 * * *',
            f'{self.name}_db_delete_day': '90',
            f'{self.name}_db_auto_delete': 'False',
            f'{P.package_name}_item_last_list_option': '',

            # 작품 목록 (3 종류)
            'titles': '',                # 만화 (/manhwa/N)
            'titles_webtoon': '',        # 웹툰 (/webtoon/N)
            'titles_novel': '',          # 소설 (/novel/N)

            # 사이트 설정
            'base_url': 'https://sbxh1.com',  # 뉴토끼 현재 도메인
            'download_path': '',
            'max_per_run': '5',           # 작품당 1회 실행 최대 다운 회차
            'use_compress': 'False',      # 회차 폴더 ZIP 압축 (만화/웹툰만)

            # 인증 (쿠키 + 프록시 + FlareSolverr + 도메인 자동 갱신)
            'cookies': '',                # 브라우저에서 복사한 쿠키 (k=v; k=v 형식)
            'use_proxy': 'False',
            'proxy_url': '',
            'flaresolverr_url': '',       # Cloudflare 우회용 — 예: http://flaresolverr:8191
            'auto_resolve_base_url': 'True',     # 스케줄 시작 시 도메인 죽었으면 자동 갱신
            'announcer_url': 'https://xn--h10b90bi5zuhh79k.net',  # 뉴토끼주소.net

            # 알림
            'notify_webhook_download': '',        # 만화/웹툰 다운 완료 알림
            'notify_webhook_download_novel': '',  # 소설 다운 완료 알림
            'notify_webhook_alert': '',           # 도메인/쿠키 만료 알림

            'auto_start': 'False',
        }
        self.web_list_model = ModelNewtokiItem

    def process_menu(self, sub, req):
        arg = P.ModelSetting.to_dict()
        if sub == 'setting':
            arg['is_include'] = F.scheduler.is_include(self.get_scheduler_name())
            arg['is_running'] = F.scheduler.is_running(self.get_scheduler_name())
        return render_template(
            f'{P.package_name}_{self.name}_{sub}.html', arg=arg)

    def process_command(self, command, arg1=None, arg2=None, arg3=None, req=None):
        try:
            P.logger.info('[basic.process_command] cmd=%r arg1=%r arg2=%r arg3=%r',
                          command, arg1, arg2, arg3)
        except Exception:
            pass
        ret = {'ret': 'success'}
        try:
            if command == 'run_now':
                ret = self.do_action()
            elif command == 'sync_metadata':
                ret = self.do_action_sync_metadata()
            elif command == 'compress_all':
                ret = self.do_action_compress_all()
            elif command == 'resolve_base':
                from .client import NewtokiClient
                proxy_url = NewtokiClient.resolve_proxy(
                    P.ModelSetting.get('use_proxy'),
                    P.ModelSetting.get('proxy_url'))
                cookies = (P.ModelSetting.get('cookies') or '').strip() or None
                fs_url = (P.ModelSetting.get('flaresolverr_url') or '').strip() or None
                cur = (P.ModelSetting.get('base_url') or '').strip()
                new_url = NewtokiClient.resolve_base_url(
                    current_base_url=cur, proxy_url=proxy_url,
                    cookies=cookies, flaresolverr_url=fs_url,
                    logger=P.logger)
                if new_url:
                    if cur != new_url:
                        P.ModelSetting.set('base_url', new_url)
                        ret = {'ret': 'success', 'base_url': new_url,
                               'msg': f'도메인 갱신됨: {cur} → {new_url}'}
                    else:
                        ret = {'ret': 'success', 'base_url': new_url,
                               'msg': f'현재 도메인 유효: {new_url}'}
                else:
                    announcer = (P.ModelSetting.get('announcer_url')
                                 or '').strip()
                    hint = (f' — 안내 사이트 확인 후 수동 갱신: {announcer}'
                            if announcer else '')
                    ret = {'ret': 'fail',
                           'msg': f'숫자 증가 후보 모두 실패{hint}'}
            elif command == 'ping_base':
                from .client import NewtokiClient
                proxy_url = NewtokiClient.resolve_proxy(
                    P.ModelSetting.get('use_proxy'),
                    P.ModelSetting.get('proxy_url'))
                base = (P.ModelSetting.get('base_url') or '').strip() or None
                cookies = (P.ModelSetting.get('cookies') or '').strip() or None
                fs_url = (P.ModelSetting.get('flaresolverr_url') or '').strip() or None
                try:
                    cli = NewtokiClient(base_url=base, logger=P.logger,
                                        proxy_url=proxy_url, cookies=cookies,
                                        flaresolverr_url=fs_url)
                    h = cli.check_health()
                    if h['domain_ok'] and h.get('cookies_ok') is not False:
                        ret = {'ret': 'success',
                               'msg': f'접속 OK — {cli.base_url}'}
                    elif h['domain_ok'] and h.get('cookies_ok') is False:
                        ret = {'ret': 'fail',
                               'msg': f'쿠키 만료 의심 — {h["reason"]}'}
                    else:
                        ret = {'ret': 'fail',
                               'msg': f'도메인 실패 — {h["reason"]} ({cli.base_url})'}
                except Exception as e:
                    ret = {'ret': 'fail', 'msg': str(e)}
            elif command == 'mrun':
                from . import manual_worker
                url = (arg1 or '').strip()
                if not url and req is not None:
                    try:
                        url = (req.form.get('url') or req.values.get('url')
                               or req.args.get('url') or '').strip()
                    except Exception:
                        pass
                ret = manual_worker.run_with_url(url)
            elif command == 'mcancel':
                from . import manual_worker
                manual_worker.cancel()
                ret = {'ret': 'success', 'msg': '취소 요청 보냄'}
            elif command == 'mprogress':
                from . import manual_worker
                ret = {'ret': 'success', 'state': manual_worker.get_state()}
            elif command == 'status_progress':
                from . import manual_worker, worker as auto_worker
                ret = {
                    'ret': 'success',
                    'auto': auto_worker.get_auto_state(),
                    'manual': manual_worker.get_state(),
                }
            elif command == 'notify_test':
                # arg1 = 'download' | 'download_novel' | 'alert'
                from .notify import send_webhook
                kind = (arg1 or 'download').strip().lower()
                if kind == 'download_novel':
                    url_key = 'notify_webhook_download_novel'
                    label = '소설'
                elif kind == 'alert':
                    url_key = 'notify_webhook_alert'
                    label = '도메인/쿠키 알림'
                else:
                    url_key = 'notify_webhook_download'
                    label = '만화/웹툰'
                url = (P.ModelSetting.get(url_key) or '').strip()
                if not url:
                    ret = {'ret': 'fail', 'msg': f'{kind} URL 미설정'}
                else:
                    msg = f'[뉴토끼 {label}] 테스트 알림 — 정상 수신 확인용'
                    ok = send_webhook(url, msg)
                    ret = {'ret': 'success' if ok else 'fail',
                           'msg': '발송 성공' if ok else '발송 실패 (URL/형식 확인)'}
            elif command == 'db_delete_items':
                ids = []
                for x in (arg1 or '').split(','):
                    x = x.strip()
                    if x.isdigit():
                        ids.append(int(x))
                if not ids:
                    ret = {'ret': 'fail', 'msg': '삭제할 ID 없음', 'count': 0}
                else:
                    cnt = (db.session.query(ModelNewtokiItem)
                           .filter(ModelNewtokiItem.id.in_(ids))
                           .delete(synchronize_session=False))
                    db.session.commit()
                    ret = {'ret': 'success', 'count': cnt}
        except Exception as e:
            P.logger.error('[basic.process_command] inner Exception: %s', e)
            P.logger.error(traceback.format_exc())
            ret = {'ret': 'fail', 'msg': str(e)}
        try:
            return jsonify(ret)
        except Exception as e:
            P.logger.error('[basic.process_command] jsonify 실패: %s ret=%r', e, ret)
            return jsonify({'ret': 'fail', 'msg': f'jsonify 실패: {e}'})

    def scheduler_function(self):
        P.logger.info('[basic] scheduler_function CALLED')
        try:
            ret = self.do_action()
            P.logger.info('[basic] scheduler 종료: %s', ret)
        except Exception as e:
            P.logger.error('[basic] scheduler Exception: %s', e)
            P.logger.error(traceback.format_exc())

    def do_action(self):
        P.logger.info('[basic] do_action BEGIN')
        try:
            with F.app.app_context():
                w = Worker()
                ret = w.run()
                P.logger.info('[basic] do_action END ret=%s', ret)
                return ret
        except Exception as e:
            P.logger.error('[basic] do_action Exception: %s', e)
            P.logger.error(traceback.format_exc())
            return {'ret': 'fail', 'msg': str(e)}

    def do_action_sync_metadata(self):
        from . import worker as auto_worker
        if auto_worker.get_auto_state().get('status') == 'running':
            return {'ret': 'fail', 'msg': '이미 자동 다운로드 실행 중'}

        def _bg():
            try:
                with F.app.app_context():
                    Worker().sync_metadata_all()
            except Exception as e:
                P.logger.error('[basic] sync_metadata Exception: %s', e)
                P.logger.error(traceback.format_exc())

        threading.Thread(target=_bg, daemon=True).start()
        return {'ret': 'success',
                'msg': '메타 동기화 시작됨 — "진행 상황" 메뉴에서 확인'}

    def do_action_compress_all(self):
        from . import worker as auto_worker
        if auto_worker.get_auto_state().get('status') == 'running':
            return {'ret': 'fail', 'msg': '이미 다른 작업 실행 중'}

        def _bg():
            try:
                with F.app.app_context():
                    Worker().compress_all()
            except Exception as e:
                P.logger.error('[basic] compress_all Exception: %s', e)
                P.logger.error(traceback.format_exc())

        threading.Thread(target=_bg, daemon=True).start()
        return {'ret': 'success',
                'msg': '압축 시작됨 — "진행 상황" 메뉴에서 확인'}
