# python test_app.py  — smoke check for event create, upload limit, paths API
import io, json, os, sys, tempfile

os.environ['PICMATCH_DATA'] = tempfile.mkdtemp()
import app as picmatch

picmatch.MAX_IMAGES = 2
c = picmatch.app.test_client()
queued = []
picmatch.enqueue = queued.append   # record instead of running the real encoder in tests

# admin users: created by CLI, separate login at /manage; they create general users
cli = picmatch.app.test_cli_runner()
assert cli.invoke(args=['create-admin', 'root'], input='rootpass1\nrootpass1\n').exit_code == 0
assert cli.invoke(args=['create-admin', 'ROOT'], input='rootpass1\nrootpass1\n').exit_code != 0   # taken
assert cli.invoke(args=['reset-admin-password', 'nobody'], input='x\n').exit_code != 0         # unknown admin
assert cli.invoke(args=['reset-admin-password', 'root'], input='short\nshort\n').exit_code != 0  # too short
assert cli.invoke(args=['reset-admin-password', 'root'], input='newroot12\nnewroot12\n').exit_code == 0
assert picmatch.check_password_hash(picmatch.q("SELECT password_hash FROM admin_users WHERE username = 'root'")[0]['password_hash'], 'newroot12')
assert cli.invoke(args=['reset-admin-password', 'root'], input='rootpass1\nrootpass1\n').exit_code == 0
boss = picmatch.app.test_client()
assert boss.get('/manage').headers['Location'].startswith('/manage/login')
assert b'Wrong username' in boss.post('/manage/login', data={'username': 'root', 'password': 'nope'}).data
assert boss.post('/manage/login', data={'username': 'root', 'password': 'rootpass1'}).headers['Location'] == '/manage'
signup = lambda u, pw='secret123', confirm=None, credits='0': boss.post(
    '/manage', data={'username': u, 'password': pw, 'confirm': confirm or pw, 'credits': credits})
assert signup('x', 'short').status_code == 200                                   # validation error, no redirect
assert b'Starting credits' in signup('alice', credits='-5').data                  # bad starting credits
assert signup('alice', credits='100').headers['Location'] == '/manage?created=alice'
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
                           data={'images': [(io.BytesIO(n.encode()), n) for n in names]})   # content = name
assert up('a.jpg', 'b.txt').json == {'saved': 1, 'skipped': 0, 'available': 99}   # .txt skipped; 1 credit held, none spent
assert up('c.png', 'd.png').status_code == 400                        # over image limit
assert queued == [slug]                                               # processing queued by the server itself
# same photo again (e.g. re-picked after leaving mid-upload): skipped, no hold, not re-queued
assert up('a.jpg').json == {'saved': 0, 'skipped': 1, 'available': 99} and len(picmatch.images(slug)) == 1
assert queued == [slug]
assert b'99' in c.get(f'/admin/e/{slug}').data

# credits: held at upload, charged once per photo as it finishes processing; failed photos are free
alice = picmatch.q("SELECT id FROM users WHERE username = 'alice'")[0]['id']
credits = lambda: picmatch.q('SELECT credits FROM users WHERE id = ?', alice)[0]['credits']
first = picmatch.images(slug)[0]
assert credits() == 100 and picmatch.available(alice) == 99                         # nothing charged at upload
picmatch.q('UPDATE users SET credits = 2 WHERE id = ?', alice)                       # 1 held → 1 available
picmatch.MAX_IMAGES = 1000
r = up('e.jpg', 'f.jpg')
assert r.status_code == 402 and r.json['available'] == 1 and len(picmatch.images(slug)) == 1   # nothing saved
assert up('e.jpg').json == {'saved': 1, 'skipped': 0, 'available': 0}
assert up('g.jpg').status_code == 402                                               # all credits held
second = [n for n in picmatch.images(slug) if n != first][0]
# encoder finishes `first` fine, can't read `second` (err)
json.dump([{'n': first, 'd': [[0.1]]}, {'n': second, 'd': [], 'err': 1}], open(picmatch.path(slug, 'faces.json'), 'w'))
picmatch.bill(slug)
assert credits() == 1 and picmatch.available(alice) == 1                            # 1 charged; failed one free + released
picmatch.bill(slug)
assert credits() == 1                                                                # never charged twice
# deleting a photo that wasn't processed yet releases its hold, no charge
assert up('h.jpg').json['available'] == 0
third = [n for n in picmatch.images(slug) if n not in (first, second)][0]
c.delete(f'/admin/e/{slug}/images/{third}')
assert picmatch.available(alice) == 1 and credits() == 1
# admins add credits; every change is logged
assert 'credit_error=alice' in boss.post(f'/manage/users/{alice}/credits', data={'amount': '0'}).headers['Location']
assert 'credit_error=alice' in boss.post(f'/manage/users/{alice}/credits', data={'amount': '-3'}).headers['Location']
assert 'credit_error=alice' in boss.post(f'/manage/users/{alice}/credits', data={'amount': '999999'}).headers['Location']
assert 'credited=' in boss.post(f'/manage/users/{alice}/credits', data={'amount': '97', 'note': 'INV-7'}).headers['Location']
assert credits() == 98
log = picmatch.q('SELECT delta, balance, reason, slug, admin_id FROM credit_log WHERE user_id = ? ORDER BY id', alice)
assert [(l['delta'], l['reason']) for l in log] == [(100, 'starting credits'), (-1, 'processed'), (97, 'admin grant: INV-7')]
assert log[1]['slug'] == slug and log[2]['balance'] == 98 and log[2]['admin_id'] is not None
# worker bills while encode.js is still running (fake encoder: writes one finished photo, then keeps running)
fake = os.path.join(os.environ['PICMATCH_DATA'], 'fake_encode.py')
open(fake, 'w').write('import json, sys, time\n'
                      'json.dump([{"n": "x1.jpg", "d": [[0.1]]}], open(sys.argv[1] + "/faces.json", "w"))\n'
                      'time.sleep(7)\n')
s3 = c.post('/admin/new', data={'title': 'Bill test', 'date': '2026-10-06'}).headers['Location'].rsplit('/', 1)[1]
open(picmatch.path(s3, 'images', 'x1.jpg'), 'wb').write(b'x')
real_node, real_root = picmatch.NODE, picmatch.ROOT
picmatch.NODE, picmatch.ROOT = sys.executable, os.environ['PICMATCH_DATA']
os.rename(fake, os.path.join(picmatch.ROOT, 'encode.js'))
import threading, time
t = threading.Thread(target=picmatch.encode, args=(s3,)); t.start()
time.sleep(6.5)
assert credits() == 97 and t.is_alive()                                             # charged mid-run
t.join()
picmatch.NODE, picmatch.ROOT = real_node, real_root
assert credits() == 97                                                               # final bill: no double charge
# tidy: back to one photo in the main event, enough credits for later tests
for n in picmatch.images(slug):
    if n != first:
        os.remove(picmatch.path(slug, 'images', n))
os.remove(picmatch.path(slug, 'faces.json'))
boss.post(f'/manage/users/{alice}/credits', data={'amount': '200'})
picmatch.MAX_IMAGES = 2
assert b'297</strong> credits' in boss.get('/manage').data
name = picmatch.images(slug)[0]

assert c.post(f'/api/e/{slug}/paths', json={'names': [name, '../event.json', 'x.jpg']}).json == \
    {name: {'url': f'/i/{slug}/{name}', 'thumb': f'/i/{slug}/{name}'}}              # no thumb yet → original
assert c.get(f'/i/{slug}/{name}').data == b'a.jpg'
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

# same photo twice in one batch: saved once
c.post('/login', data={'username': 'alice', 'password': 'secret123'})
s4 = c.post('/admin/new', data={'title': 'Dupes', 'date': '2026-10-07'}).headers['Location'].rsplit('/', 1)[1]
r = c.post(f'/admin/e/{s4}/upload', content_type='multipart/form-data',
           data={'images': [(io.BytesIO(b'same'), 'one.jpg'), (io.BytesIO(b'same'), 'two.jpg'), (io.BytesIO(b'other'), 'three.jpg')]})
assert (r.json['saved'], r.json['skipped']) == (2, 1) and len(picmatch.images(s4)) == 2
# deleting a photo before the first encoding run must not create an empty faces.json
c.delete(f'/admin/e/{s4}/images/{picmatch.images(s4)[0]}')
assert not os.path.exists(picmatch.path(s4, 'faces.json')) and c.get(f'/e/{s4}/faces.json').status_code == 404

# page sends files named by the hash of the original photo: kept as-is; anything else gets hashed server-side
key = '0123456789abcdef'
r = c.post(f'/admin/e/{s4}/upload', content_type='multipart/form-data',
           data={'images': [(io.BytesIO(b'compressed-1'), key + '.jpg'), (io.BytesIO(b'compressed-2'), '../evil.jpg')]})
assert r.json['saved'] == 2 and key + '.jpg' in picmatch.images(s4) and '../evil.jpg' not in picmatch.images(s4)
assert all(picmatch.HASH_NAME.fullmatch(os.path.splitext(n)[0]) for n in picmatch.images(s4))
# same original from another browser compresses differently, but has the same hash name: skipped
r = c.post(f'/admin/e/{s4}/upload', content_type='multipart/form-data',
           data={'images': [(io.BytesIO(b'compressed-differently'), key + '.jpg')]})
assert (r.json['saved'], r.json['skipped']) == (0, 1)
r = c.post(f'/admin/e/{s4}/known', json={'keys': [key, 'ffffffffffffffff', 7]}).json
assert r['known'] == [key] and r['available'] == picmatch.available(alice)
assert anon.post(f'/admin/e/{s4}/known', json={'keys': [key]}).status_code in (302, 404)  # not the owner
print('ok')
