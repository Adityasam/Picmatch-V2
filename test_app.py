# python test_app.py  — smoke check for event create, upload limit, paths API
import io, json, os, tempfile

os.environ['PICMATCH_DATA'] = tempfile.mkdtemp()
import app as picmatch

picmatch.MAX_IMAGES = 2
c = picmatch.app.test_client()

# admin users: created by CLI, separate login at /manage; they create general users
cli = picmatch.app.test_cli_runner()
assert cli.invoke(args=['create-admin', 'root'], input='rootpass1\nrootpass1\n').exit_code == 0
assert cli.invoke(args=['create-admin', 'ROOT'], input='rootpass1\nrootpass1\n').exit_code != 0   # taken
boss = picmatch.app.test_client()
assert boss.get('/manage').headers['Location'].startswith('/manage/login')
assert b'Wrong username' in boss.post('/manage/login', data={'username': 'root', 'password': 'nope'}).data
assert boss.post('/manage/login', data={'username': 'root', 'password': 'rootpass1'}).headers['Location'] == '/manage'
signup = lambda u, pw='secret123', confirm=None: boss.post('/manage', data={'username': u, 'password': pw, 'confirm': confirm or pw})
assert signup('x', 'short').status_code == 200                                   # validation error, no redirect
assert signup('alice').headers['Location'] == '/manage?created=alice'
assert boss.get('/admin').headers['Location'].startswith('/login')               # admin login ≠ user login
assert c.post('/login', data={'username': 'root', 'password': 'rootpass1'}).status_code == 200  # admins can't use user login

# general user login
assert c.get('/').headers['Location'] == '/login'
assert c.get('/admin').headers['Location'].startswith('/login')
assert c.get('/manage').headers['Location'].startswith('/manage/login')        # users can't reach admin area
assert c.post('/login', data={'username': 'alice', 'password': 'secret123'}).headers['Location'] == '/admin'
assert c.get('/admin').status_code == 200

r = c.post('/admin/new', data={'title': 'Party', 'date': '2026-09-26'})
slug = r.headers['Location'].rsplit('/', 1)[1]
assert c.get(f'/e/{slug}').status_code == 200
assert c.get('/e/nope').status_code == 404

up = lambda *names: c.post(f'/admin/e/{slug}/upload', content_type='multipart/form-data',
                           data={'images': [(io.BytesIO(b'x'), n) for n in names]})
assert up('a.jpg', 'b.txt').json == {'saved': 1}          # .txt skipped
assert up('c.png', 'd.png').status_code == 400            # over limit
name = picmatch.images(slug)[0]

assert c.post(f'/api/e/{slug}/paths', json={'names': [name, '../event.json', 'x.jpg']}).json == \
    {name: {'url': f'/i/{slug}/{name}', 'thumb': f'/i/{slug}/{name}'}}              # no thumb yet → original
assert c.get(f'/i/{slug}/{name}').data == b'x'
assert c.get(f'/admin/e/{slug}/qr.svg').status_code == 200
# edit: rename, set thumb, replace thumb (old file removed), remove thumb
edit = lambda **d: c.post(f'/admin/e/{slug}/edit', content_type='multipart/form-data', data={'title': 'Gala', 'date': '2026-10-01', **d})
edit(thumbnail=(io.BytesIO(b't1'), 'a.png'))
ev = picmatch.event(slug); t1 = ev['thumb']
assert ev['title'] == 'Gala' and ev['date'] == '2026-10-01' and c.get(f'/t/{slug}').data == b't1'
edit()                                                    # no file = keep
assert picmatch.event(slug)['thumb'] == t1
edit(thumbnail=(io.BytesIO(b't2'), 'b.jpg'))
assert c.get(f'/t/{slug}').data == b't2' and not os.path.exists(picmatch.path(slug, t1))
edit(remove_thumb='1')
assert picmatch.event(slug)['thumb'] is None and c.get(f'/t/{slug}').status_code == 404
assert c.get(f'/admin/e/{slug}').status_code == 200
assert edit(next='/admin').headers['Location'] == '/admin'                     # back to where dialog opened
assert edit(next='//evil.com').headers['Location'] == f'/admin/e/{slug}'      # no open redirect
for u in ['/admin', f'/admin/e/{slug}']:
    html = c.get(u).get_data(as_text=True)
    assert 'id="event-dialog"' in html and "data-event='{" in html
# photos list (50/page) + delete removes file and faces.json entry
picmatch.MAX_IMAGES = 1000
up(*[f'p{i}.jpg' for i in range(51)])
names = picmatch.images(slug)
json.dump([{'n': n, 'd': [[0.1]]} for n in names], open(picmatch.path(slug, 'faces.json'), 'w'))
assert c.get(f'/admin/e/{slug}/images').json['waiting'] == 52                   # no thumbnails yet → nothing listed
os.makedirs(picmatch.path(slug, 'thumbs'))
for n in names[1:]:
    open(picmatch.path(slug, 'thumbs', n + '.jpg'), 'wb').write(b'th')
r = c.get(f'/admin/e/{slug}/images?page=2').json
assert (r['total'], r['waiting'], r['pages'], r['page'], len(r['items'])) == (51, 1, 2, 2, 1) and r['items'][0]['faces'] == 1
open(picmatch.path(slug, 'thumbs', names[0] + '.jpg'), 'wb').write(b'th')
assert c.get(f'/admin/e/{slug}/images?page=2').json['total'] == 52
assert c.get(f'/admin/e/{slug}/images?page=99').json['page'] == 2
gone = names[0]
assert c.post(f'/api/e/{slug}/paths', json={'names': [gone]}).json[gone]['thumb'] == f'/th/{slug}/{gone}'
items = {i['name']: i for i in c.get(f'/admin/e/{slug}/images?page=1').json['items'] + c.get(f'/admin/e/{slug}/images?page=2').json['items']}
assert items[gone]['thumb'] == f'/th/{slug}/{gone}' and c.get(items[gone]['thumb']).data == b'th'
assert c.delete(f'/admin/e/{slug}/images/{gone}').json['total'] == 51
assert not os.path.exists(picmatch.path(slug, 'thumbs', gone + '.jpg'))
assert gone not in picmatch.images(slug) and gone not in {x['n'] for x in picmatch.faces(slug)}
assert len(picmatch.faces(slug)) == 51
assert c.delete(f'/admin/e/{slug}/images/{gone}').status_code == 404
assert c.delete(f'/admin/e/{slug}/images/..%2Ffaces.json').status_code == 404
# delete event: folder + db row gone, pages 404; blocked while encoding
assert 'id="del-event-dialog"' in c.get('/admin').get_data(as_text=True)
# status comes from db column + files
st = lambda: c.get(f'/admin/e/{slug}/status').json['state']
assert st() == 'ready'                                     # idle, everything in faces.json
json.dump([], open(picmatch.path(slug, 'faces.json'), 'w'))
assert st() == 'pending'
for s_ in ('queued', 'failed', 'processing'):
    picmatch.q('UPDATE events SET status = ? WHERE slug = ?', s_, slug)
    assert st() == s_
assert c.post(f'/admin/e/{slug}/delete').status_code == 409 and os.path.isdir(picmatch.path(slug))
picmatch.q("UPDATE events SET status = 'idle' WHERE slug = ?", slug)
assert c.post(f'/admin/e/{slug}/delete').headers['Location'] == '/admin'
assert not os.path.exists(picmatch.path(slug)) and not picmatch.q('SELECT 1 FROM events WHERE slug = ?', slug)
assert c.get(f'/admin/e/{slug}').status_code == 404 and c.get(f'/e/{slug}').status_code == 404
# second user can't see alice's events
anon = picmatch.app.test_client()
assert signup('bob').headers['Location'] == '/manage?created=bob'
assert signup('BOB').status_code == 200                                          # usernames are case-insensitive
assert b'bob' in boss.get('/manage').data
assert b'Wrong username' in anon.post('/login', data={'username': 'bob', 'password': 'nope'}).data
assert anon.post('/login', data={'username': 'bob', 'password': 'secret123', 'next': '//evil.com'}).headers['Location'] == '/admin'
s2 = c.post('/admin/new', data={'title': 'Alice party', 'date': '2026-10-02'}).headers['Location'].rsplit('/', 1)[1]
assert b'Alice party' in c.get('/admin').data and b'Alice party' not in anon.get('/admin').data
for u in [f'/admin/e/{s2}', f'/admin/e/{s2}/status', f'/admin/e/{s2}/images']:
    assert anon.get(u).status_code == 404
assert anon.post(f'/admin/e/{s2}/delete').status_code == 404 and picmatch.event(s2)
assert anon.get(f'/e/{s2}').status_code == 200                                  # attendee page stays public
c.post('/logout')
assert c.get('/admin').headers['Location'].startswith('/login')
# manage: deactivate → logged out now + can't log in; activate → back; soft delete → hidden, can't log in, data kept
bob = picmatch.q("SELECT id FROM users WHERE username = 'bob'")[0]['id']
assert anon.get('/admin').status_code == 200
assert boss.post(f'/manage/users/{bob}/active', data={'active': '0'}).status_code == 302
assert anon.get('/admin').headers['Location'].startswith('/login')                # existing session cut off
assert b'disabled' in anon.post('/login', data={'username': 'bob', 'password': 'secret123'}).data
assert b'Wrong username' in anon.post('/login', data={'username': 'bob', 'password': 'bad'}).data  # no hint w/o password
assert b'Inactive' in boss.get('/manage').data
boss.post(f'/manage/users/{bob}/active', data={'active': '1'})
# reset password: bad input rejected; good one logs out old sessions, old password stops working
assert 'reset_error=bob' in boss.post(f'/manage/users/{bob}/password', data={'password': 'short', 'confirm': 'short'}).headers['Location']
assert 'reset_error=bob' in boss.post(f'/manage/users/{bob}/password', data={'password': 'newpass123', 'confirm': 'other1234'}).headers['Location']
assert anon.post('/login', data={'username': 'bob', 'password': 'secret123'}).headers['Location'] == '/admin'
assert 'reset=bob' in boss.post(f'/manage/users/{bob}/password', data={'password': 'newpass123', 'confirm': 'newpass123'}).headers['Location']
assert anon.get('/admin').headers['Location'].startswith('/login')                # old session cut off
assert b'Wrong username' in anon.post('/login', data={'username': 'bob', 'password': 'secret123'}).data
assert anon.post('/login', data={'username': 'bob', 'password': 'newpass123'}).headers['Location'] == '/admin'
assert b'changed' in boss.get('/manage?reset=bob').data
bob_event = anon.post('/admin/new', data={'title': 'Bob party', 'date': '2026-10-03'}).headers['Location'].rsplit('/', 1)[1]
assert boss.post(f'/manage/users/{bob}/delete').status_code == 302
assert anon.get('/admin').headers['Location'].startswith('/login')
assert b'Wrong username' in anon.post('/login', data={'username': 'bob', 'password': 'secret123'}).data
assert b'>bob<' not in boss.get('/manage').data and picmatch.q('SELECT deleted_at FROM users WHERE id = ?', bob)[0]['deleted_at']
assert os.path.isdir(picmatch.path(bob_event)) and anon.get(f'/e/{bob_event}').status_code == 200
assert boss.post(f'/manage/users/{bob}/delete').status_code == 404                 # already deleted
assert signup('bob').status_code == 200                                            # username stays reserved
assert anon.post(f'/manage/users/{bob}/active', data={'active': '1'}).headers['Location'].startswith('/manage/login')
boss.post('/manage/logout')
assert boss.get('/manage').headers['Location'].startswith('/manage/login')
print('ok')
