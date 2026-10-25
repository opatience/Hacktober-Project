import os
import secrets
import re
import threading

from dotenv import load_dotenv
from flask import Flask, flash, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

load_dotenv()

import ai
import scraper
import storage
from feed import build_feed, preview_preferences
from cortex_agent import ask_agent

app = Flask(__name__)
app.secret_key = os.getenv('FLASK_SECRET_KEY') or secrets.token_hex(32)
app.config['MAX_CONTENT_LENGTH'] = 64 * 1024
_refresh_lock = threading.Lock()


EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')


class Unauthenticated(Exception):
    pass


def context():
    store = storage.get_store()
    user = store.user_by_id(session['user_id']) if session.get('user_id') else None
    if user is None:
        session.clear()
        raise Unauthenticated()
    return store, user


@app.errorhandler(Unauthenticated)
def unauthenticated(_):
    return jsonify(error={'code': 'unauthenticated', 'message': 'Log in first.'}), 401


@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        if not EMAIL_RE.match(email) or len(email) > 320:
            flash('Enter a valid email.')
        elif len(password) < 8:
            flash('Password must be at least 8 characters.')
        else:
            user = storage.get_store().create_account(email, generate_password_hash(password))
            if user:
                session.clear()
                session['user_id'] = user['user_id']
                return redirect(url_for('index'))
            flash('An account with that email already exists.')
    return render_template('auth.html', mode='signup')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        user = storage.get_store().user_by_email(request.form.get('email', '').strip().lower())
        if user and user.get('password_hash') and check_password_hash(user['password_hash'], request.form.get('password', '')):
            session.clear()
            session['user_id'] = user['user_id']
            return redirect(url_for('index'))
        flash('Wrong email or password.')
    return render_template('auth.html', mode='login')


@app.post('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


def payload():
    value = request.get_json(silent=True)
    if not isinstance(value, dict):
        raise ValueError('Send a JSON object.')
    return value


def selected_feed(store, user, query=None, overrides=None):
    prefs = store.preferences(user['user_id'])
    follows = store.follows(user['user_id'])
    query = request.args.get('q', '') if query is None else query
    if not isinstance(query, str) or len(query) > 500:
        raise ValueError('Keep searches under 500 characters.')
    filters = dict(request.args) if overrides is None else dict(overrides)
    search_text = query or prefs.get('preference_text', '')
    semantic_ids, warnings = [], []
    if search_text and os.getenv('SNOWFLAKE_SEARCH_SERVICE'):
        try:
            semantic_ids = store.semantic_search(search_text, limit=100)
        except Exception:
            warnings.append('Semantic search is unavailable; results use your follows, interests, and event text.')
    result = build_feed(store.events(), prefs, follows, query, filters, semantic_ids)
    result['meta'].update(store.status())
    result['meta']['search_degraded'] = bool(warnings)
    result['warnings'] = warnings
    return result


@app.errorhandler(ValueError)
def invalid_input(error):
    conflict = isinstance(error, storage.VersionConflict)
    return jsonify(error={'code': 'version_conflict' if conflict else 'invalid_input', 'message': str(error)}), 409 if conflict else 400


@app.errorhandler(RuntimeError)
def dependency_error(error):
    app.logger.warning('Backend dependency unavailable: %s', type(error).__name__)
    return jsonify(error={'code': 'dependency_unavailable', 'message': 'The data service is unavailable. Check the Snowflake connection and setup, then retry.'}), 503


@app.get('/')
def index():
    try:
        _, user = context()
    except Unauthenticated:
        return redirect(url_for('login'))
    return render_template('index.html', user=user)


@app.get('/v1/status')
def status():
    return jsonify(meta=storage.get_store().status())


@app.get('/v1/me')
def me():
    store, user = context()
    return jsonify(user=user, meta=store.status())


@app.get('/v1/tags')
def tags():
    rows = storage.get_store().tags()
    return jsonify(tags=rows, items=rows)


@app.get('/v1/clubs')
def clubs():
    rows = storage.get_store().clubs()
    query = request.args.get('q', '').strip().lower()
    if query:
        rows = [row for row in rows if query in (row.get('name', '') + ' ' + (row.get('description') or '')).lower()]
    return jsonify(clubs=rows, items=rows)


@app.route('/v1/me/preferences', methods=['GET', 'PUT'])
def preferences():
    store, user = context()
    value = store.save_preferences(user['user_id'], payload()) if request.method == 'PUT' else store.preferences(user['user_id'])
    return jsonify(preferences=value)


@app.post('/v1/preferences/preview')
def preference_preview():
    text = payload().get('preference_text', '')
    if not isinstance(text, str) or len(text) > 2000:
        raise ValueError('Preferences must be text under 2,000 characters.')
    return jsonify(preview_preferences(text, storage.get_store().tags()))


@app.route('/v1/me/follows', methods=['GET', 'POST'])
def follows():
    store, user = context()
    if request.method == 'POST':
        account = store.follow(user['user_id'], payload().get('username', ''))
        return jsonify(follow=account), 201 if account.get('created') else 200
    rows = store.follows(user['user_id'])
    return jsonify(follows=rows, items=rows)


@app.put('/v1/me/follows/<username>')
def follow_username(username):
    store, user = context()
    account = store.follow(user['user_id'], username)
    return jsonify(follow=account), 201 if account.get('created') else 200


@app.delete('/v1/me/follows/<account_id>')
def unfollow(account_id):
    store, user = context()
    store.unfollow(user['user_id'], account_id)
    return '', 204


@app.get('/v1/events')
def events():
    store, user = context()
    return jsonify(selected_feed(store, user))


@app.get('/v1/events/<event_id>')
def event_detail(event_id):
    store, user = context()
    rows = selected_feed(store, user, overrides={'horizon_days': '90', 'limit': '50'})['events']
    row = next((row for row in rows if row['event_id'] == event_id), None)
    if row is None:
        return jsonify(error={'code': 'not_found', 'message': 'This event is not available.'}), 404
    return jsonify(event=row)


@app.post('/v1/events/ask')
def ask():
    store, user = context()
    query = payload().get('q', '')
    if not isinstance(query, str) or not query.strip():
        raise ValueError('Enter a question about upcoming events.')
    result = selected_feed(store, user, query=query)
    agent = ask_agent(query) if os.getenv('SNOWFLAKE_AGENT') else {'answer': None, 'cited_event_ids': [], 'warnings': []}
    cited = set(agent.get('cited_event_ids') or [])
    eligible = result['events']
    top = ([event for event in eligible if event['event_id'] in cited] + [event for event in eligible if event['event_id'] not in cited])[:5]
    answer = '\n'.join(f"{event['title']} — {event['local_start_date']}, {event.get('location_name') or 'location not announced'}" for event in top)
    return jsonify(answer=answer or 'No matching upcoming events were found with your current filters.', citations=[{'event_id': e['event_id'], 'source_url': e.get('url')} for e in top if e.get('url')], warnings=result['warnings'] + agent.get('warnings', []), meta={'agent_used': bool(agent.get('answer')), 'backend': store.status()['backend']})


@app.get('/v1/newsletter')
def newsletter_feed():
    store, user = context()
    days = int(request.args.get('days', 30))
    if not 1 <= days <= 30:
        raise ValueError('Choose a newsletter range between 1 and 30 days.')
    rows = store.posts(user['user_id'], days=days)
    return jsonify(posts=rows, items=rows, meta=store.status(), next_cursor=None)


def refresh_accounts(store, user, usernames=None):
    if usernames:
        for username in usernames:
            store.follow(user['user_id'], username)
    handles = [row['username'] for row in store.follows(user['user_id'])]
    if not handles:
        raise ValueError('Follow an Instagram account first.')
    if not _refresh_lock.acquire(blocking=False):
        return jsonify(error={'code': 'refresh_in_progress', 'message': 'A refresh is already running. Try again in a moment.'}), 409
    try:
        posts = ai.analyze(scraper.scrape(handles))
        modes = sorted({p.get('source_mode', 'live') for p in posts})
        report = store.ingest_posts(posts, source_mode=modes[0] if len(modes) == 1 else 'mixed')
        result = selected_feed(store, user, query='', overrides={})
        warnings = list(result.get('warnings', []))
        if any(p.get('source_mode') == 'cached' for p in posts):
            warnings.append('Some posts came from the most recent saved Instagram capture.')
        for post in posts:
            for warning in post.get('extraction_warnings', []):
                if warning not in warnings:
                    warnings.append(warning)
        return jsonify(posts=store.posts(user['user_id'], days=30), events=result['events'], meta=result['meta'], warnings=warnings, ingestion=report)
    finally:
        _refresh_lock.release()


@app.post('/v1/refresh')
def refresh():
    store, user = context()
    return refresh_accounts(store, user)


@app.post('/api/newsletter')
def legacy_newsletter():
    raw = payload().get('usernames', '')
    if not isinstance(raw, str):
        raise ValueError('Enter Instagram usernames separated by commas.')
    handles = [u.strip().lstrip('@') for u in raw.replace('\n', ',').split(',') if u.strip()]
    if not handles:
        raise ValueError('Enter at least one username.')
    store, user = context()
    return refresh_accounts(store, user, handles)


if __name__ == '__main__':
    app.run(host='127.0.0.1', port=int(os.getenv('PORT', '5000')), debug=False)
