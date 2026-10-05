import json, os, re, secrets, shutil, sqlite3, subprocess, threading, time, traceback
from contextlib import closing

import click
import qrcode, qrcode.image.svg
from dotenv import load_dotenv
from flask import Flask, Response, abort, g, jsonify, redirect, render_template, request, send_from_directory, session, url_for
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

ROOT = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(ROOT, '.env'))  # real environment variables win; works under gunicorn/systemd too
DATA = os.environ.get('PICMATCH_DATA', os.path.join(ROOT, 'data'))
MAX_IMAGES = 1000
EXTS = {'.jpg', '.jpeg', '.png', '.webp'}
DB = os.path.join(DATA, 'picmatch.db')
# full path, because services (systemd, supervisor) often run with a PATH that has no node on it
NODE = os.environ.get('PICMATCH_NODE') or shutil.which('node') or 'node'

app = Flask(__name__)
# Behind nginx: take the visitor's IP and https scheme from X-Forwarded-For/-Proto (one trusted proxy hop).
# Without this every scan would be logged from 127.0.0.1, and QR links would be http://
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)
app.config['MAX_CONTENT_LENGTH'] = 300 * 1024 * 1024  # per request; admin page uploads in batches
wake = threading.Event()  # nudges the worker when something is queued


def path(slug, *p):
    return os.path.join(DATA, slug, *p)


def q(sql, *args):
    with closing(sqlite3.connect(DB)) as c, c:  # second `c`: commit on success
        c.row_factory = sqlite3.Row
        return [dict(r) for r in c.execute(sql, args)]


os.makedirs(DATA, exist_ok=True)


def secret_key():
    # persisted so logins survive restarts; PICMATCH_SECRET env var overrides
    f = os.path.join(DATA, 'secret_key')
    if not os.path.exists(f):
        with open(f, 'w') as fh:
            fh.write(secrets.token_hex(32))
    with open(f) as fh:
        return fh.read().strip()


app.secret_key = os.environ.get('PICMATCH_SECRET') or secret_key()
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'  # blocks cross-site form posts riding the login cookie

# admin_users: staff who create/manage the general users below; separate login at /manage
q('''CREATE TABLE IF NOT EXISTS admin_users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)''')
# users: general users of the service; each owns events
# active = 0 blocks login; deleted_at set = soft deleted (hidden, can't log in, username stays reserved)
q('''CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    active INTEGER NOT NULL DEFAULT 1,
    deleted_at TEXT)''')
# status: idle | queued | processing | failed. Progress/ready/empty come from the files (images/ vs faces.json).
q('''CREATE TABLE IF NOT EXISTS events (
    slug TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    date TEXT NOT NULL,
    thumb TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    status TEXT NOT NULL DEFAULT 'idle',
    queued_at TEXT,
    user_id INTEGER REFERENCES users(id))''')
# scans: one row per attendee selfie scan, whatever the outcome (matching itself runs in the browser).
# result: matched | no_match | no_face | not_ready. No foreign key, so history survives event deletion.
q('''CREATE TABLE IF NOT EXISTS scans (
    id INTEGER PRIMARY KEY,
    slug TEXT NOT NULL,
    ts TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    ip TEXT,
    user_agent TEXT,
    result TEXT NOT NULL,
    matches INTEGER NOT NULL DEFAULT 0)''')
q('CREATE INDEX IF NOT EXISTS scans_ts ON scans (ts)')
# downloads: photo downloads from the results page, linked to the scan that found them
q('''CREATE TABLE IF NOT EXISTS downloads (
    id INTEGER PRIMARY KEY,
    scan_id INTEGER REFERENCES scans(id),
    slug TEXT NOT NULL,
    name TEXT NOT NULL,
    ts TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    ip TEXT)''')
q('CREATE INDEX IF NOT EXISTS downloads_scan ON downloads (scan_id)')
# a run cut off by a restart/crash goes back in the queue; encode.js resumes from faces.json
# ponytail: assumes one app process; with several, only reset from a single startup hook
q("UPDATE events SET status = 'queued' WHERE status = 'processing'")


def event(slug):
    rows = q('SELECT * FROM events WHERE slug = ?', slug)
    if not rows:
        abort(404)
    return rows[0]


def my_event(slug):
    # admin routes: the event must belong to the logged-in user; others get 404, not a hint it exists
    ev = event(slug)
    if ev['user_id'] != g.user['id']:
        abort(404)
    return ev


def safe_next(nxt, default):
    # local paths only, no open redirect
    return nxt if nxt and nxt.startswith('/') and not nxt.startswith('//') else default


def images(slug):
    return sorted(os.listdir(path(slug, 'images')))


def faces(slug):
    try:
        with open(path(slug, 'faces.json')) as f:
            return json.load(f)
    except FileNotFoundError:
        return []


def pending(slug):
    return len(set(images(slug)) - {x['n'] for x in faces(slug)})


def encode(slug):
    with open(path(slug, 'encode.log'), 'w') as log:
        try:
            rc = subprocess.run([NODE, os.path.join(ROOT, 'encode.js'), path(slug)],
                                stdout=log, stderr=subprocess.STDOUT).returncode
        except OSError as e:  # e.g. node not found: record it instead of killing the worker
            log.write(f'could not start {NODE}: {e}\n')
            rc = 1
    # photos uploaded right as the run ended → run again
    after = 'failed' if rc else 'queued' if os.path.isdir(path(slug)) and pending(slug) else 'idle'
    q("UPDATE events SET status = ?, queued_at = CURRENT_TIMESTAMP WHERE slug = ? AND status = 'processing'",
      after, slug)


def worker():
    # one encode at a time; face detection is CPU heavy
    while True:
        try:
            # claim oldest queued event atomically, so a second worker can't grab the same one
            rows = q('''UPDATE events SET status = 'processing' WHERE slug =
                       (SELECT slug FROM events WHERE status = 'queued' ORDER BY queued_at LIMIT 1)
                       RETURNING slug''')
            if rows:
                encode(rows[0]['slug'])
                continue
        except Exception:  # never let one bad run stop all future encoding
            traceback.print_exc()
            time.sleep(5)
        wake.wait(5)
        wake.clear()


_worker = None


def ensure_worker():
    # started lazily in each serving process: a thread started at import doesn't survive
    # gunicorn --preload / uWSGI forking, and this also restarts it if it ever died
    global _worker
    if _worker is None or not _worker.is_alive():
        _worker = threading.Thread(target=worker, daemon=True)
        _worker.start()


def enqueue(slug):
    # while processing, encode.js picks up new files itself (and the worker re-queues leftovers)
    q("UPDATE events SET status = 'queued', queued_at = CURRENT_TIMESTAMP WHERE slug = ? AND status != 'processing'",
      slug)
    ensure_worker()
    wake.set()


def status(slug):
    st, imgs, left = event(slug)['status'], images(slug), pending(slug)
    state = st if st != 'idle' else 'pending' if left else 'ready' if imgs else 'empty'
    return {'state': state, 'total': len(imgs), 'pending': left}


ensure_worker()


# ---------- auth: general users (session 'uid') and admin users (session 'admin_uid') ----------

@app.before_request
def load_user():
    ensure_worker()
    uid, aid = session.get('uid'), session.get('admin_uid')
    g.user = (q('SELECT id, username, password_hash FROM users WHERE id = ? AND active = 1 AND deleted_at IS NULL', uid)
              or [None])[0] if uid else None  # checked every request: deactivating logs the user out at once
    if g.user and session.get('pwv') != pw_version(g.user['password_hash']):
        g.user = None  # password was reset since this login: log out everywhere
    g.admin = (q('SELECT id, username FROM admin_users WHERE id = ?', aid) or [None])[0] if aid else None
    p, nxt = request.path, request.full_path.rstrip('?') if request.method == 'GET' else None
    if p.startswith('/admin') and not g.user:
        return redirect(url_for('login', next=nxt))
    if p.startswith('/manage') and p != '/manage/login' and not g.admin:
        return redirect(url_for('admin_login', next=nxt))


def check_login(table, where=''):
    # ponytail: no rate limiting on failed logins; add per-IP throttling before exposing publicly
    rows = q(f'SELECT * FROM {table} WHERE username = ? {where}', request.form.get('username', '').strip())
    ok = rows and check_password_hash(rows[0]['password_hash'], request.form.get('password', ''))
    return rows[0] if ok else None


def pw_version(password_hash):
    # tail of the salted hash changes on every password set; stored in the login session to spot resets
    return password_hash[-12:]


def log_in(key, uid):
    # separate keys, so one browser can be logged in as a user and as an admin at the same time
    session[key] = uid
    session.permanent = True


def account_error(table, name, pw, confirm):
    if not re.fullmatch(r'[\w.-]{3,32}', name):
        return 'Username: 3-32 letters, numbers, dot, dash or underscore.'
    if len(pw) < 8:
        return 'Password must be at least 8 characters.'
    if pw != confirm:
        return "Passwords don't match."
    if q(f'SELECT 1 FROM {table} WHERE username = ?', name):
        return 'That username is taken.'
    return None


@app.get('/')
def home():
    return redirect(url_for('login'))  # login forwards to /admin when already logged in


@app.route('/login', methods=['GET', 'POST'])
def login():
    if g.user:
        return redirect(url_for('admin'))
    error = None
    if request.method == 'POST':
        user = check_login('users', 'AND deleted_at IS NULL')  # deleted = as if it never existed
        if user and user['active']:
            log_in('uid', user['id'])
            session['pwv'] = pw_version(user['password_hash'])
            return redirect(safe_next(request.form.get('next'), url_for('admin')))
        # "disabled" only shown after a correct password, so it doesn't reveal which usernames exist
        error = 'This account is disabled. Contact the Picmatch team.' if user else 'Wrong username or password.'
    return render_template('login.html', error=error, next=request.values.get('next', ''),
                           heading='Log in', action=url_for('login'),
                           hint="Don't have an account? Ask the Picmatch team to create one.")


@app.post('/logout')
def logout():
    session.pop('uid', None)
    return redirect(url_for('login'))


# ---------- admin users: manage general users ----------

@app.route('/manage/login', methods=['GET', 'POST'])
def admin_login():
    if g.admin:
        return redirect(url_for('manage'))
    error = None
    if request.method == 'POST':
        admin_user = check_login('admin_users')
        if admin_user:
            log_in('admin_uid', admin_user['id'])
            return redirect(safe_next(request.form.get('next'), url_for('manage')))
        error = 'Wrong username or password.'
    return render_template('login.html', error=error, next=request.values.get('next', ''),
                           heading='Admin log in', action=url_for('admin_login'), admin_area=True)


@app.post('/manage/logout')
def admin_logout():
    session.pop('admin_uid', None)
    return redirect(url_for('admin_login'))


@app.route('/manage', methods=['GET', 'POST'])
def manage():
    error = None
    if request.method == 'POST':
        name, pw = request.form.get('username', '').strip(), request.form.get('password', '')
        error = account_error('users', name, pw, request.form.get('confirm', ''))
        if not error:
            first = not q('SELECT 1 FROM users LIMIT 1')
            uid = q('INSERT INTO users (username, password_hash) VALUES (?, ?) RETURNING id',
                    name, generate_password_hash(pw))[0]['id']
            if first:
                q('UPDATE events SET user_id = ? WHERE user_id IS NULL', uid)  # events made before users existed
            return redirect(url_for('manage', created=name))
    users = q('SELECT u.id, u.username, u.created_at, u.active, COUNT(e.slug) AS events FROM users u '
              'LEFT JOIN events e ON e.user_id = u.id WHERE u.deleted_at IS NULL GROUP BY u.id ORDER BY u.created_at')
    return render_template('users.html', error=error, created=request.args.get('created'), users=users,
                           reset=request.args.get('reset'), reset_error=request.args.get('reset_error'),
                           admin_area=True)


def managed_user(uid):
    rows = q('SELECT * FROM users WHERE id = ? AND deleted_at IS NULL', uid)
    if not rows:
        abort(404)
    return rows[0]


@app.post('/manage/users/<int:uid>/active')
def set_active(uid):
    managed_user(uid)
    q('UPDATE users SET active = ? WHERE id = ?', 1 if request.form.get('active') == '1' else 0, uid)
    return redirect(url_for('manage'))


@app.post('/manage/users/<int:uid>/password')
def reset_password(uid):
    user = managed_user(uid)
    pw = request.form.get('password', '')
    if len(pw) < 8 or pw != request.form.get('confirm', ''):
        return redirect(url_for('manage', reset_error=user['username']))
    q('UPDATE users SET password_hash = ? WHERE id = ?', generate_password_hash(pw), uid)
    return redirect(url_for('manage', reset=user['username']))


@app.post('/manage/users/<int:uid>/delete')
def delete_user(uid):
    # soft delete: row, events and photos stay; only login and listing stop
    managed_user(uid)
    q('UPDATE users SET deleted_at = CURRENT_TIMESTAMP, active = 0 WHERE id = ?', uid)
    return redirect(url_for('manage'))


@app.cli.command('create-admin')
@click.argument('username')
def create_admin(username):
    """Create an admin user: flask --app app create-admin <username>"""
    pw = click.prompt('Password', hide_input=True, confirmation_prompt=True)
    error = account_error('admin_users', username, pw, pw)
    if error:
        raise click.ClickException(error)
    q('INSERT INTO admin_users (username, password_hash) VALUES (?, ?)', username, generate_password_hash(pw))
    click.echo(f'Admin {username} created. Log in at /manage/login')


@app.cli.command('reset-admin-password')
@click.argument('username')
def reset_admin_password(username):
    """Set a new password for an admin user: flask --app app reset-admin-password <username>"""
    if not q('SELECT 1 FROM admin_users WHERE username = ?', username):
        raise click.ClickException(f'No admin named {username}.')
    pw = click.prompt('New password', hide_input=True, confirmation_prompt=True)
    if len(pw) < 8:
        raise click.ClickException('Password must be at least 8 characters.')
    q('UPDATE admin_users SET password_hash = ? WHERE username = ?', generate_password_hash(pw), username)
    click.echo(f'Password for admin {username} changed.')


# ---------- admin users: scan dashboard ----------

SCAN_DAYS = (1, 7, 30, 90, 365)
RESULT_LABELS = {'matched': 'Photos found', 'no_match': 'No match', 'no_face': 'No face detected',
                 'not_ready': 'Photos not ready'}


@app.template_filter('device')
def device(ua):
    # short "Browser · OS" label for the table; the full user agent is in the CSV export
    ua = ua or ''
    os_ = next((n for k, n in (('iPhone', 'iPhone'), ('iPad', 'iPad'), ('Android', 'Android'), ('Windows', 'Windows'),
                               ('Mac OS', 'Mac'), ('CrOS', 'ChromeOS'), ('Linux', 'Linux')) if k in ua), 'Other')
    br = next((n for k, n in (('SamsungBrowser', 'Samsung'), ('Edg/', 'Edge'), ('OPR/', 'Opera'), ('Firefox', 'Firefox'),
                              ('FxiOS', 'Firefox'), ('CriOS', 'Chrome'), ('Chrome', 'Chrome'), ('Safari', 'Safari')) if k in ua),
              'Other')
    return f'{br} · {os_}'


def scan_filter():
    """WHERE clause + args for the dashboard's period and event filters (shared with the CSV export)."""
    days = request.args.get('days', 30, type=int)
    days = days if days in SCAN_DAYS else 30
    slug = request.args.get('event') or None
    where, args = "s.ts >= datetime('now', ?)", [f'-{days} days']
    if slug:
        where += ' AND s.slug = ?'
        args.append(slug)
    return days, slug, where, args


@app.get('/manage/scans')
def scans_dashboard():
    days, slug, where, args = scan_filter()
    one = lambda sql: q(sql, *args)[0]
    totals = one(f'''SELECT COUNT(*) AS scans, COUNT(DISTINCT s.ip) AS visitors,
                            COALESCE(SUM(s.result = 'matched'), 0) AS matched, COALESCE(SUM(s.matches), 0) AS photos
                     FROM scans s WHERE {where}''')
    totals['downloads'] = one(f'SELECT COUNT(*) AS n FROM downloads d JOIN scans s ON s.id = d.scan_id WHERE {where}')['n']
    results = {r['result']: r['n'] for r in q(f'SELECT s.result, COUNT(*) AS n FROM scans s WHERE {where} GROUP BY 1', *args)}
    per_day = {r['day']: r for r in q(f'''SELECT date(s.ts) AS day, COUNT(*) AS scans, COUNT(DISTINCT s.ip) AS visitors
                                          FROM scans s WHERE {where} GROUP BY 1''', *args)}
    today = q("SELECT date('now') AS d")[0]['d']
    daily = [per_day.get(d, {'day': d, 'scans': 0, 'visitors': 0}) for d in
             (r['d'] for r in q("WITH RECURSIVE n(i) AS (SELECT 0 UNION ALL SELECT i + 1 FROM n WHERE i < ?) "
                                "SELECT date(?, '-' || i || ' days') AS d FROM n ORDER BY d", min(days, 90) - 1, today))]
    events = q(f'''SELECT s.slug, e.title, u.username AS owner, COUNT(*) AS scans, COUNT(DISTINCT s.ip) AS visitors,
                          SUM(s.result = 'matched') AS matched, SUM(s.matches) AS photos, MAX(s.ts) AS last,
                          (SELECT COUNT(*) FROM downloads d WHERE d.slug = s.slug AND d.scan_id IN
                             (SELECT s2.id FROM scans s2 WHERE s2.slug = s.slug AND s2.ts >= datetime('now', ?))) AS downloads
                   FROM scans s LEFT JOIN events e ON e.slug = s.slug LEFT JOIN users u ON u.id = e.user_id
                   WHERE {where} GROUP BY s.slug ORDER BY scans DESC''', args[0], *args)
    recent = q(f'''SELECT s.*, e.title, (SELECT COUNT(*) FROM downloads d WHERE d.scan_id = s.id) AS downloads
                   FROM scans s LEFT JOIN events e ON e.slug = s.slug
                   WHERE {where} ORDER BY s.id DESC LIMIT 50''', *args)
    all_events = q('SELECT DISTINCT s.slug, COALESCE(e.title, s.slug) AS title FROM scans s '
                   'LEFT JOIN events e ON e.slug = s.slug ORDER BY title')
    return render_template('scans.html', admin_area=True, days=days, slug=slug, day_options=SCAN_DAYS,
                           totals=totals, results=results, labels=RESULT_LABELS, daily=daily, events=events,
                           recent=recent, all_events=all_events, peak=max([d['scans'] for d in daily] + [1]))


@app.get('/manage/scans.csv')
def scans_csv():
    import csv, io
    days, slug, where, args = scan_filter()
    rows = q(f'''SELECT s.id, s.ts, s.slug, e.title, s.ip, s.result, s.matches,
                        (SELECT COUNT(*) FROM downloads d WHERE d.scan_id = s.id) AS downloads, s.user_agent
                 FROM scans s LEFT JOIN events e ON e.slug = s.slug WHERE {where} ORDER BY s.id''', *args)
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(['scan_id', 'time_utc', 'event_slug', 'event_title', 'ip', 'result', 'photos_found', 'downloads', 'user_agent'])
    w.writerows([r['id'], r['ts'], r['slug'], r['title'] or '(deleted)', r['ip'], r['result'], r['matches'],
                 r['downloads'], r['user_agent']] for r in rows)
    name = f"picmatch-scans-{slug or 'all'}-{days}d.csv"
    return Response(out.getvalue(), mimetype='text/csv', headers={'Content-Disposition': f'attachment; filename={name}'})


# ---------- admin: every route below needs login (load_user) and ownership (my_event) ----------

@app.get('/admin')
def admin():
    return render_template('admin.html', events=q(
        'SELECT * FROM events WHERE user_id = ? ORDER BY date DESC, created_at DESC', g.user['id']))


def save_event(slug, thumb=None):
    """Save the posted form to the events table; shared by create and edit."""
    title, date = request.form['title'].strip(), request.form['date']
    if not title or not date:
        abort(400)
    f = request.files.get('thumbnail')
    ext = os.path.splitext(f.filename)[1].lower() if f and f.filename else ''
    if ext in EXTS or (thumb and request.form.get('remove_thumb')):
        if thumb:
            os.remove(path(slug, thumb))
        thumb = None
        if ext in EXTS:
            thumb = f'thumb-{secrets.token_hex(4)}{ext}'  # new name each time, so cached old cover isn't shown
            f.save(path(slug, thumb))
    q('''INSERT INTO events (slug, title, date, thumb, user_id) VALUES (?, ?, ?, ?, ?)
         ON CONFLICT(slug) DO UPDATE SET title = excluded.title, date = excluded.date, thumb = excluded.thumb''',
      slug, title, date, thumb, g.user['id'])


@app.post('/admin/new')
def new_event():
    slug = secrets.token_urlsafe(6)  # unguessable public link
    os.makedirs(path(slug, 'images'))
    save_event(slug)
    return redirect(url_for('admin_event', slug=slug))


@app.post('/admin/e/<slug>/edit')
def edit_event(slug):
    save_event(slug, my_event(slug)['thumb'])
    return redirect(safe_next(request.form.get('next'), url_for('admin_event', slug=slug)))


@app.post('/admin/e/<slug>/delete')
def delete_event(slug):
    if my_event(slug)['status'] == 'processing':
        abort(409, 'Face processing is running for this event. Delete it after processing finishes.')
    shutil.rmtree(path(slug))  # folder first: if this fails the event stays listed instead of orphaning files
    q('DELETE FROM events WHERE slug = ?', slug)
    return redirect(url_for('admin'))


@app.get('/admin/e/<slug>')
def admin_event(slug):
    return render_template('event.html', slug=slug, ev=my_event(slug), status=status(slug),
                           max_images=MAX_IMAGES, public_url=url_for('attend', slug=slug, _external=True))


@app.post('/admin/e/<slug>/upload')
def upload(slug):
    my_event(slug)
    files = [f for f in request.files.getlist('images') if os.path.splitext(f.filename)[1].lower() in EXTS]
    if len(images(slug)) + len(files) > MAX_IMAGES:
        return jsonify(error=f'limit is {MAX_IMAGES} images per event'), 400
    for f in files:
        f.save(path(slug, 'images', secrets.token_hex(8) + os.path.splitext(f.filename)[1].lower()))
    return jsonify(saved=len(files))


@app.get('/admin/e/<slug>/images')
def list_images(slug):
    my_event(slug)
    per = 50
    all_names = images(slug)
    # only photos encode.js has made a thumbnail for; the rest show up once processed
    names = sorted((n for n in all_names if os.path.isfile(path(slug, 'thumbs', n + '.jpg'))),
                   key=lambda n: os.path.getmtime(path(slug, 'images', n)), reverse=True)
    pages = max(1, -(-len(names) // per))
    page = min(max(request.args.get('page', 1, type=int), 1), pages)
    counts = {x['n']: len(x['d']) for x in faces(slug)}
    return jsonify(page=page, pages=pages, total=len(names), waiting=len(all_names) - len(names), items=[
        {'name': n, 'url': url_for('image', slug=slug, name=n), 'thumb': url_for('thumb_image', slug=slug, name=n),
         'faces': counts.get(n)}
        for n in names[(page - 1) * per:page * per]])


@app.delete('/admin/e/<slug>/images/<name>')
def delete_image(slug, name):
    ev = my_event(slug)
    if name not in images(slug):  # also blocks path tricks in name
        abort(404)
    os.remove(path(slug, 'images', name))
    if os.path.isfile(path(slug, 'thumbs', name + '.jpg')):
        os.remove(path(slug, 'thumbs', name + '.jpg'))
    if ev['status'] != 'processing':  # while encoding, encode.js drops it on its next save (it skips missing files)
        tmp = path(slug, 'faces.json.tmp')
        with open(tmp, 'w') as f:
            json.dump([x for x in faces(slug) if x['n'] != name], f)
        os.replace(tmp, path(slug, 'faces.json'))
    return jsonify(status(slug))


@app.get('/admin/e/<slug>/status')
def admin_status(slug):
    my_event(slug)
    return jsonify(status(slug))


@app.post('/admin/e/<slug>/process')
def process(slug):
    my_event(slug)
    enqueue(slug)
    return jsonify(status(slug))


@app.get('/admin/e/<slug>/qr.svg')
def qr(slug):
    my_event(slug)
    img = qrcode.make(url_for('attend', slug=slug, _external=True), image_factory=qrcode.image.svg.SvgPathImage)
    return img.to_string(), 200, {'Content-Type': 'image/svg+xml'}


# ---------- attendee ----------

@app.get('/e/<slug>')
def attend(slug):
    return render_template('attend.html', slug=slug, ev=event(slug))


@app.get('/e/<slug>/faces.json')
def faces_json(slug):
    event(slug)
    return send_from_directory(path(slug), 'faces.json', max_age=60)


def record_scan(slug, result, matches=0):
    return q('INSERT INTO scans (slug, ip, user_agent, result, matches) VALUES (?, ?, ?, ?, ?) RETURNING id',
             slug, request.remote_addr, request.user_agent.string[:400], result, matches)[0]['id']


@app.post('/api/e/<slug>/paths')
def paths(slug):
    # called once per scan that matched faces in the browser, so this is where matched scans are recorded
    event(slug)
    have = set(images(slug))
    names = (request.get_json(silent=True) or {}).get('names', [])
    def urls(n):
        url = url_for('image', slug=slug, name=n)
        has_thumb = os.path.isfile(path(slug, 'thumbs', n + '.jpg'))
        return {'url': url, 'thumb': url_for('thumb_image', slug=slug, name=n) if has_thumb else url}
    found = {n: urls(n) for n in names[:MAX_IMAGES] if isinstance(n, str) and n in have}
    scan_id = record_scan(slug, 'matched' if found else 'no_match', len(found))
    resp = jsonify(found)
    resp.headers['X-Scan-Id'] = str(scan_id)  # the page tags its download links with it
    return resp


@app.post('/api/e/<slug>/scan')
def report_scan(slug):
    # scans that end in the browser without a match (no face / no match / photos not processed yet)
    event(slug)
    result = (request.get_json(silent=True) or {}).get('result')
    if result not in ('no_match', 'no_face', 'not_ready'):
        abort(400)
    return jsonify(id=record_scan(slug, result)), 201


@app.get('/i/<slug>/<name>')
def image(slug, name):
    event(slug)
    if request.args.get('dl') and name in images(slug):  # download button on the results page
        scan = request.args.get('s', type=int)
        if scan and not q('SELECT 1 FROM scans WHERE id = ? AND slug = ?', scan, slug):
            scan = None
        q('INSERT INTO downloads (scan_id, slug, name, ip) VALUES (?, ?, ?, ?)', scan, slug, name, request.remote_addr)
    return send_from_directory(path(slug, 'images'), name, max_age=86400)


@app.get('/th/<slug>/<name>')
def thumb_image(slug, name):
    event(slug)
    return send_from_directory(path(slug, 'thumbs'), name + '.jpg', max_age=86400)


@app.get('/t/<slug>')
def thumb(slug):
    ev = event(slug)
    if not ev['thumb']:
        abort(404)
    return send_from_directory(path(slug), ev['thumb'], max_age=86400)


@app.get('/models/<path:p>')
def models(p):
    # same weights encode.js loads
    return send_from_directory(os.path.join(ROOT, 'models'), p, max_age=86400)


if __name__ == '__main__':
    app.run(debug=True)
