import io
import os
import shutil
import subprocess
import tempfile
import time
import unittest

import jwt
from flask import Flask, url_for

from admin import create_admin_blueprint
from admin_auth import AdminAuth
from library import SoundLibrary
from sound_metadata import MetadataCache
import sound_search

EXTENSIONS = ('mp3', 'wav', 'ogg')
SECRET = 'a-shared-secret-of-sufficient-length-for-hs512-keys-0123456789-abcdef'
USER = {'X-Token-User-Email': 'dj@example.org'}
WRITE = {'X-Soundboard-Admin': '1'}
HAS_FFMPEG = shutil.which('ffmpeg') is not None


def token(email='dj@example.org', roles=('authp/admin',), secret=SECRET, exp_in=600, **extra):
    claims = dict(email=email, sub=email, roles=list(roles), exp=int(time.time()) + exp_in, **extra)
    return jwt.encode(claims, secret, algorithm='HS512')


def make_app(auth, lib=None):
    app = Flask(__name__, template_folder=os.path.join(os.path.dirname(__file__), 'templates'))
    app.testing = True
    cache = MetadataCache()

    @app.route('/')
    def index():
        return 'index'

    @app.route('/play/<path:filename>')
    def play(filename):
        return 'Playing'

    @app.route('/cover/<path:filename>')
    def cover(filename):
        return ''

    @app.route('/meta/<path:filename>')
    def sound_meta(filename):
        return ''

    def sound_info(name):
        path = lib.path(name)
        if not os.path.isfile(path):
            return None
        return dict(cache.metadata(path), name=name, count=0, loudness=lib.loudness_info(name),
                    play_url=url_for('play', filename=name), cover_url=url_for('cover', filename=name))

    if lib is None:
        tmp = tempfile.mkdtemp()
        lib = SoundLibrary(tmp, os.path.join(tmp, 'lib.db'), lambda: EXTENSIONS)
        app.cleanup_dir = tmp
    app.register_blueprint(create_admin_blueprint(lib, auth, sound_info, 5 * 1024 * 1024))
    return app


class FakeRequest:
    def __init__(self, headers=None, cookies=None, method='GET'):
        self.headers = headers or {}
        self.cookies = cookies or {}
        self.method = method


class AdminAuthTests(unittest.TestCase):
    def test_off_by_default_and_for_unknown_modes(self):
        for env in ({}, {'ADMIN_AUTH': 'yes-please'}, {'ADMIN_AUTH': 'off'}):
            result = AdminAuth(env).check(FakeRequest(USER))
            self.assertEqual(result.status, 404, env)

    def test_proxy_header(self):
        auth = AdminAuth({'ADMIN_AUTH': 'proxy'})
        self.assertEqual(auth.check(FakeRequest()).status, 401)
        self.assertEqual(auth.check(FakeRequest({'X-Token-User-Email': '  '})).status, 401)
        result = auth.check(FakeRequest(USER))
        self.assertTrue(result.ok)
        self.assertEqual(result.user, 'dj@example.org')

    def test_custom_header_name(self):
        auth = AdminAuth({'ADMIN_AUTH': 'proxy', 'ADMIN_USER_HEADER': 'X-Remote-User'})
        self.assertEqual(auth.check(FakeRequest(USER)).status, 401)
        self.assertTrue(auth.check(FakeRequest({'X-Remote-User': 'me'})).ok)

    def test_writes_need_the_csrf_header(self):
        auth = AdminAuth({'ADMIN_AUTH': 'proxy'})
        self.assertEqual(auth.check(FakeRequest(USER, method='POST')).status, 403)
        self.assertTrue(auth.check(FakeRequest(dict(USER, **WRITE), method='DELETE')).ok)

    def test_roles_from_header(self):
        auth = AdminAuth({'ADMIN_AUTH': 'proxy', 'ADMIN_ROLES': 'authp/admin, soundboard'})
        self.assertEqual(auth.check(FakeRequest(USER)).status, 403)
        self.assertEqual(auth.check(FakeRequest(dict(USER, **{'X-Token-User-Roles': 'authp/user'}))).status, 403)
        self.assertTrue(auth.check(FakeRequest(dict(USER, **{'X-Token-User-Roles': 'authp/user soundboard'}))).ok)

    def test_jwt_verification(self):
        auth = AdminAuth({'ADMIN_AUTH': 'proxy', 'ADMIN_JWT_KEY': SECRET})
        self.assertTrue(auth.verifies_jwt)
        # The header alone is not enough any more
        self.assertEqual(auth.check(FakeRequest(USER)).status, 401)
        ok = auth.check(FakeRequest(USER, cookies={'AUTHP_ACCESS_TOKEN': token()}))
        self.assertTrue(ok.ok)
        self.assertEqual(ok.roles, ['authp/admin'])
        # Bearer header and the other default cookie names work too
        self.assertTrue(auth.check(FakeRequest(dict(USER, Authorization='Bearer ' + token()))).ok)
        self.assertTrue(auth.check(FakeRequest(USER, cookies={'access_token': token()})).ok)

    def test_jwt_rejections(self):
        auth = AdminAuth({'ADMIN_AUTH': 'proxy', 'ADMIN_JWT_KEY': SECRET, 'ADMIN_ROLES': 'authp/admin'})
        cases = {
            'forged': token(secret='wrong-secret-wrong-secret-wrong-secret-wrong-secret-wrong-secret-0'),
            'expired': token(exp_in=-60),
            'other user': token(email='mallory@example.org'),
            'garbage': 'not.a.jwt',
        }
        for label, value in cases.items():
            result = auth.check(FakeRequest(USER, cookies={'AUTHP_ACCESS_TOKEN': value}))
            self.assertEqual(result.status, 401, label)
        no_role = auth.check(FakeRequest(USER, cookies={'AUTHP_ACCESS_TOKEN': token(roles=['authp/user'])}))
        self.assertEqual(no_role.status, 403)

    def test_jwt_roles_win_over_header_roles(self):
        auth = AdminAuth({'ADMIN_AUTH': 'proxy', 'ADMIN_JWT_KEY': SECRET, 'ADMIN_ROLES': 'authp/admin'})
        forged_header = dict(USER, **{'X-Token-User-Roles': 'authp/admin'})
        result = auth.check(FakeRequest(forged_header, cookies={'AUTHP_ACCESS_TOKEN': token(roles=[])}))
        self.assertEqual(result.status, 403)

    def test_jwt_audience_is_ignored_unless_configured(self):
        with_aud = token(aud='soundboard')
        self.assertTrue(AdminAuth({'ADMIN_AUTH': 'proxy', 'ADMIN_JWT_KEY': SECRET})
                        .check(FakeRequest(USER, cookies={'access_token': with_aud})).ok)
        strict = AdminAuth({'ADMIN_AUTH': 'proxy', 'ADMIN_JWT_KEY': SECRET, 'ADMIN_JWT_AUDIENCE': 'other'})
        self.assertEqual(strict.check(FakeRequest(USER, cookies={'access_token': with_aud})).status, 401)


class AdminGateTests(unittest.TestCase):
    def test_disabled_admin_is_404_everywhere(self):
        app = make_app(AdminAuth({}))
        self.addCleanup(shutil.rmtree, app.cleanup_dir)
        client = app.test_client()
        self.assertEqual(client.get('/edit', headers=USER).status_code, 404)
        response = client.get('/edit/api/trash', headers=USER)
        self.assertEqual(response.status_code, 404)
        self.assertIn('error', response.get_json())

    def test_signed_out_page_and_api(self):
        app = make_app(AdminAuth({'ADMIN_AUTH': 'proxy'}))
        self.addCleanup(shutil.rmtree, app.cleanup_dir)
        client = app.test_client()
        page = client.get('/edit/')
        self.assertEqual(page.status_code, 401)
        self.assertIn(b'PLEASE SIGN IN', page.data)
        self.assertEqual(client.delete('/edit/api/sounds/x.mp3').status_code, 401)

    def test_page_renders_for_admin(self):
        app = make_app(AdminAuth({'ADMIN_AUTH': 'proxy'}))
        self.addCleanup(shutil.rmtree, app.cleanup_dir)
        page = app.test_client().get('/edit', headers=USER)
        self.assertEqual(page.status_code, 200)
        self.assertIn(b'dj@example.org', page.data)
        self.assertIn(b'accept=".mp3,.wav,.ogg,audio/*"', page.data)


@unittest.skipUnless(HAS_FFMPEG, 'ffmpeg is needed to create sample audio')
class AdminApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.sounds = os.path.join(self.tmp, 'sounds')
        os.makedirs(self.sounds)
        self.lib = SoundLibrary(self.sounds, os.path.join(self.tmp, 'lib.db'), lambda: EXTENSIONS)
        self.client = make_app(AdminAuth({'ADMIN_AUTH': 'proxy'}), self.lib).test_client()

    def audio(self, freq=440, ext='wav', seconds=0.5):
        path = os.path.join(self.tmp, f'{freq}.{ext}')
        subprocess.run(['ffmpeg', '-loglevel', 'error', '-y', '-f', 'lavfi', '-i', f'sine=f={freq}:d={seconds}', path],
                       check=True)
        with open(path, 'rb') as f:
            return f.read()

    def upload(self, data, name, headers=None):
        return self.client.post('/edit/api/upload', headers=headers or dict(USER, **WRITE),
                                data={'file': (io.BytesIO(data), name)}, content_type='multipart/form-data')

    def test_upload_flow_and_duplicates(self):
        horn = self.audio(440)
        response = self.upload(horn, 'Horn.wav')
        self.assertEqual(response.status_code, 201, response.get_json())
        self.assertEqual(response.get_json()['name'], 'Horn.wav')
        self.assertEqual(response.get_json()['sound']['name'], 'Horn.wav')

        duplicate = self.upload(horn, 'copy.wav')
        self.assertEqual(duplicate.status_code, 409)
        self.assertEqual(duplicate.get_json()['existing'], 'Horn.wav')

        self.assertEqual(self.upload(b'nope', 'x.exe').status_code, 415)
        self.assertEqual(self.upload(b'', 'empty.wav').status_code, 400)
        self.assertEqual(self.upload(horn, 'csrf.wav', headers=USER).status_code, 403)

    def test_oversized_upload(self):
        response = self.upload(b'\0' * (7 * 1024 * 1024), 'huge.wav')
        self.assertEqual(response.status_code, 413)
        self.assertIn('MB', response.get_json()['error'])

    def test_edit_tags_and_search(self):
        self.upload(self.audio(440), 'horn.wav')
        response = self.client.patch('/edit/api/sounds/horn.wav', headers=dict(USER, **WRITE),
                                     json={'title': 'Air Horn', 'year': '2014', 'tags': ['Loud', '#meme']})
        self.assertEqual(response.status_code, 200, response.get_json())
        sound = response.get_json()['sound']
        self.assertEqual((sound['title'], sound['year'], sound['tags']), ('Air Horn', '2014', ['loud', 'meme']))
        self.assertEqual([s['name'] for s in sound_search.search([sound], '#meme air')], ['horn.wav'])

        bad = self.client.patch('/edit/api/sounds/horn.wav', headers=dict(USER, **WRITE), json={'year': 'soon'})
        self.assertEqual(bad.status_code, 400)
        missing = self.client.patch('/edit/api/sounds/nope.wav', headers=dict(USER, **WRITE), json={'title': 'x'})
        self.assertEqual(missing.status_code, 404)
        hidden = self.client.patch('/edit/api/sounds/..%2Fx.wav', headers=dict(USER, **WRITE), json={'title': 'x'})
        self.assertEqual(hidden.status_code, 404)

    def test_cover_upload_and_removal(self):
        from PIL import Image
        self.upload(self.audio(440, 'mp3'), 'horn.mp3')
        buffer = io.BytesIO()
        Image.new('RGB', (50, 50), 'blue').save(buffer, 'PNG')
        response = self.client.put('/edit/api/sounds/horn.mp3/cover', headers=dict(USER, **WRITE),
                                   data={'cover': (io.BytesIO(buffer.getvalue()), 'c.png')},
                                   content_type='multipart/form-data')
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertTrue(response.get_json()['sound']['has_cover'])

        junk = self.client.put('/edit/api/sounds/horn.mp3/cover', headers=dict(USER, **WRITE),
                               data={'cover': (io.BytesIO(b'zzz'), 'c.png')}, content_type='multipart/form-data')
        self.assertEqual(junk.status_code, 400)

        removed = self.client.delete('/edit/api/sounds/horn.mp3/cover', headers=dict(USER, **WRITE))
        self.assertFalse(removed.get_json()['sound']['has_cover'])

    def test_cover_crop_and_preview(self):
        from PIL import Image
        self.upload(self.audio(440, 'mp3'), 'crop.mp3')
        image = Image.new('RGB', (400, 200), (255, 0, 0))
        image.paste((0, 0, 255), (200, 0, 400, 200))  # right half blue
        buffer = io.BytesIO()
        image.save(buffer, 'TIFF')  # not a browser format; converted on the server
        tiff = buffer.getvalue()

        preview = self.client.post('/edit/api/image-preview', headers=dict(USER, **WRITE),
                                   data={'image': (io.BytesIO(tiff), 'c.tif')}, content_type='multipart/form-data')
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.mimetype, 'image/jpeg')
        self.assertEqual((preview.headers['X-Image-Width'], preview.headers['X-Image-Height']), ('400', '200'))

        put = self.client.put('/edit/api/sounds/crop.mp3/cover', headers=dict(USER, **WRITE),
                              data={'cover': (io.BytesIO(tiff), 'c.tif'), 'crop': '0.5,0,0.5,1'},
                              content_type='multipart/form-data')
        self.assertEqual(put.status_code, 200, put.get_json())
        from sound_metadata import read_file
        _, (mime, data) = read_file(os.path.join(self.sounds, 'crop.mp3'))
        cover = Image.open(io.BytesIO(data))
        self.assertEqual((mime, cover.size), ('image/jpeg', (200, 200)))
        r, g, b = cover.convert('RGB').getpixel((100, 100))
        self.assertTrue(b > 200 and r < 60, (r, g, b))

        bad = self.client.put('/edit/api/sounds/crop.mp3/cover', headers=dict(USER, **WRITE),
                              data={'cover': (io.BytesIO(tiff), 'c.tif'), 'crop': '0.9,0,0.5,1'},
                              content_type='multipart/form-data')
        self.assertEqual(bad.status_code, 400)
        self.assertIn('inside the image', bad.get_json()['error'])
        junk = self.client.post('/edit/api/image-preview', headers=dict(USER, **WRITE),
                                data={'image': (io.BytesIO(b'zzz'), 'x.heic')}, content_type='multipart/form-data')
        self.assertEqual(junk.status_code, 400)
        self.assertEqual(self.client.post('/edit/api/image-preview', headers=dict(USER, **WRITE)).status_code, 400)

    def test_page_offers_many_image_types(self):
        page = self.client.get('/edit', headers=USER).get_data(as_text=True)
        self.assertIn('.heic', page)
        self.assertIn('id="crop-modal"', page)
        me = self.client.get('/edit/api/me', headers=USER).get_json()
        self.assertEqual(me['cover']['aspect'], 1.0)
        self.assertIn('AVIF', me['cover']['formats'])

    def test_listen_in_browser(self):
        self.upload(self.audio(440, seconds=1), 'horn.wav')
        calls = []
        real_filter = self.lib.playback_filter
        self.lib.playback_filter = lambda name: calls.append(name) or 'volume=-6dB'
        self.addCleanup(setattr, self.lib, 'playback_filter', real_filter)
        response = self.client.get('/edit/api/sounds/horn.wav/audio', headers=USER)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, 'audio/mpeg')
        self.assertTrue(response.data[:3] == b'ID3' or response.data[:2] in (b'\xff\xfb', b'\xff\xf3', b'\xff\xfa'))
        self.assertEqual(calls, ['horn.wav'])  # normalized like a channel play
        partial = self.client.get('/edit/api/sounds/horn.wav/audio', headers=dict(USER, Range='bytes=0-99'))
        self.assertEqual((partial.status_code, len(partial.data)), (206, 100))
        self.assertEqual(self.client.get('/edit/api/sounds/nope.wav/audio', headers=USER).status_code, 404)
        self.assertEqual(self.client.get('/edit/api/sounds/horn.wav/audio').status_code, 401)

    def test_trim_and_undo(self):
        self.upload(self.audio(440, seconds=2), 'horn.wav')
        wave = self.client.get('/edit/api/sounds/horn.wav/waveform?points=100', headers=USER).get_json()
        self.assertAlmostEqual(wave['duration'], 2.0, delta=0.01)
        self.assertEqual(len(wave['peaks']), 100)
        self.assertEqual(self.client.get('/edit/api/sounds/horn.wav/waveform?points=x', headers=USER).status_code, 400)

        body = {'start': 0.5, 'end': 1.5, 'fade_in': 0.05, 'fade_out': 0.05}
        self.assertEqual(self.client.post('/edit/api/sounds/horn.wav/trim', headers=USER, json=body).status_code, 403)
        trimmed = self.client.post('/edit/api/sounds/horn.wav/trim', headers=dict(USER, **WRITE), json=body)
        self.assertEqual(trimmed.status_code, 200, trimmed.get_json())
        self.assertAlmostEqual(trimmed.get_json()['sound']['duration'], 1.0, delta=0.05)
        self.assertEqual(trimmed.get_json()['sound']['trim_backups'], 1)
        self.assertEqual(self.client.get('/edit/api/trims', headers=USER).get_json(), {'horn.wav': 1})

        bad = self.client.post('/edit/api/sounds/horn.wav/trim', headers=dict(USER, **WRITE), json={'start': 1, 'end': 0.5})
        self.assertEqual(bad.status_code, 400)
        missing = self.client.post('/edit/api/sounds/nope.wav/trim', headers=dict(USER, **WRITE), json=body)
        self.assertEqual(missing.status_code, 404)

        undone = self.client.post('/edit/api/sounds/horn.wav/untrim', headers=dict(USER, **WRITE))
        self.assertEqual(undone.status_code, 200)
        self.assertAlmostEqual(undone.get_json()['sound']['duration'], 2.0, delta=0.05)
        self.assertEqual(undone.get_json()['sound']['trim_backups'], 0)
        self.assertIn(undone.get_json()['trimmed_id'], [i['id'] for i in self.client.get('/edit/api/trash', headers=USER).get_json()])
        self.assertEqual(self.client.post('/edit/api/sounds/horn.wav/untrim', headers=dict(USER, **WRITE)).status_code, 404)

    def test_delete_trash_restore(self):
        self.upload(self.audio(440), 'bye.wav')
        deleted = self.client.delete('/edit/api/sounds/bye.wav', headers=dict(USER, **WRITE))
        self.assertEqual(deleted.status_code, 200)
        masked = deleted.get_json()['id']
        self.assertTrue(os.path.isfile(os.path.join(self.sounds, masked)))

        trash = self.client.get('/edit/api/trash', headers=USER).get_json()
        self.assertEqual([item['id'] for item in trash], [masked])
        self.assertEqual(self.client.delete('/edit/api/sounds/bye.wav', headers=dict(USER, **WRITE)).status_code, 404)

        restored = self.client.post(f'/edit/api/trash/{masked}/restore', headers=dict(USER, **WRITE))
        self.assertEqual(restored.status_code, 200)
        self.assertEqual(restored.get_json()['name'], 'bye.wav')
        self.assertEqual(self.client.get('/edit/api/trash', headers=USER).get_json(), [])

    def test_me(self):
        me = self.client.get('/edit/api/me', headers=USER).get_json()
        self.assertEqual(me['user'], 'dj@example.org')
        self.assertFalse(me['jwt_verified'])
        self.assertEqual(me['extensions'], list(EXTENSIONS))


class SearchTests(unittest.TestCase):
    SOUNDS = [
        {'name': 'airhorn.mp3', 'title': 'Air Horn', 'artist': 'MLG', 'tags': ['loud', 'meme']},
        {'name': 'drumroll.wav', 'title': None, 'artist': None, 'tags': ['drums', 'suspense']},
        {'name': 'sad trombone.ogg', 'title': 'Wah wah', 'artist': None, 'tags': ['meme', 'fail']},
    ]

    def names(self, *args):
        return [s['name'] for s in sound_search.search(self.SOUNDS, *args)]

    def test_words_and_tags(self):
        self.assertEqual(self.names(''), ['airhorn.mp3', 'drumroll.wav', 'sad trombone.ogg'])
        self.assertEqual(self.names('#meme'), ['airhorn.mp3', 'sad trombone.ogg'])
        self.assertEqual(self.names('#MEME wah'), ['sad trombone.ogg'])
        self.assertEqual(self.names('susp'), ['drumroll.wav'])        # words also match tags
        self.assertEqual(self.names('#susp'), [])                     # #tag must match exactly
        self.assertEqual(self.names('', ['fail', 'meme']), ['sad trombone.ogg'])
        self.assertEqual(self.names('mlg air'), ['airhorn.mp3'])

    def test_tag_counts(self):
        self.assertEqual(sound_search.tag_counts(self.SOUNDS)[:2], [{'tag': 'meme', 'count': 2},
                                                                    {'tag': 'drums', 'count': 1}])


if __name__ == '__main__':
    unittest.main()
