import json
import os
import random

import spotipy
from dotenv import load_dotenv
from flask import Flask, render_template, request, redirect, url_for, session
from spotipy.cache_handler import FlaskSessionCacheHandler
from spotipy.oauth2 import SpotifyOAuth

from elo import calculate_new_ratings

# --- CONFIGURATION ---
load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv('FLASK_SECRET_KEY', 'dev_key_change_in_prod')
app.config['SESSION_COOKIE_NAME'] = 'Spotify Cookie'

# Spotify Config
CLIENT_ID = os.getenv('SPOTIPY_CLIENT_ID')
CLIENT_SECRET = os.getenv('SPOTIPY_CLIENT_SECRET')
REDIRECT_URI = os.getenv('SPOTIPY_REDIRECT_URI', 'http://127.0.0.1:5000/callback')

# App Config
TARGET_PLAYLIST_ID = '4lJhU5XSMgOpHyuZxbyP0Z'
DB_FILE = 'songs.json'
SCOPE = "user-library-read playlist-read-private playlist-modify-private playlist-modify-public user-modify-playback-state"

# --- AUTH MANAGER ---
def create_auth_manager():
    return SpotifyOAuth(
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        redirect_uri=REDIRECT_URI,
        scope=SCOPE,
        cache_handler=FlaskSessionCacheHandler(session),
        show_dialog=True
    )


# --- DATABASE HELPERS ---
def load_db():
    if not os.path.exists(DB_FILE):
        return {}
    try:
        with open(DB_FILE, 'r') as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {}


def save_db(data):
    # Atomic write pattern: write to temp file then rename.
    # Prevents corruption if the script crashes while writing.
    temp_file = f"{DB_FILE}.tmp"
    with open(temp_file, 'w') as f:
        json.dump(data, f, indent=4)
    os.replace(temp_file, DB_FILE)


# --- ROUTES ---

@app.route('/')
def dashboard():
    """Shows the leaderboard."""
    db = load_db()
    auth_manager = create_auth_manager()

    # Check token validity safely
    token_info = auth_manager.cache_handler.get_cached_token()
    is_logged_in = auth_manager.validate_token(token_info) if token_info else False

    # Sort songs by rating (descending)
    sorted_songs = sorted(db.values(), key=lambda x: x['rating'], reverse=True) if db else []

    return render_template('dashboard.html', songs=sorted_songs, first_run=(not db), logged_in=is_logged_in)


@app.route('/login')
def login():
    auth_manager = create_auth_manager()
    return redirect(auth_manager.get_authorize_url())


@app.route('/callback')
def callback():
    auth_manager = create_auth_manager()
    code = request.args.get("code")
    if code:
        auth_manager.get_access_token(code)
        return redirect(url_for('ingest_playlist'))
    return redirect(url_for('login'))


@app.route('/ingest')
def ingest_playlist():
    """Pulls songs from Spotify, preserving ratings."""
    auth_manager = create_auth_manager()
    if not auth_manager.validate_token(auth_manager.cache_handler.get_cached_token()):
        return redirect(url_for('login'))

    sp = spotipy.Spotify(auth_manager=auth_manager)
    old_db = load_db()
    new_db = {}

    results = sp.playlist_items(TARGET_PLAYLIST_ID)
    tracks = results['items']

    while results['next']:
        results = sp.next(results)
        tracks.extend(results['items'])

    for item in tracks:
        track = item.get('track')
        if not track or track.get('is_local'):
            continue  # Skip local files or empty tracks

        uri = track['uri']

        # Determine image safely
        img_url = track['album']['images'][0]['url'] if track['album']['images'] else ""

        if uri in old_db:
            new_db[uri] = old_db[uri]
            # Update metadata only
            new_db[uri]['name'] = track['name']
            new_db[uri]['image'] = img_url
        else:
            new_db[uri] = {
                'name': track['name'],
                'artist': track['artists'][0]['name'],
                'image': img_url,
                'uri': uri,
                'rating': 1000.0,
                'matches': 0
            }

    save_db(new_db)
    return redirect(url_for('dashboard'))


@app.route('/rank', methods=['GET', 'POST'])
def rank():
    db = load_db()
    uris = list(db.keys())

    # Safety check: Need at least 2 songs to rank
    if len(uris) < 2:
        return "Not enough songs to rank! Please <a href='/ingest'>Sync Playlist</a> first."

    if request.method == 'POST':
        winner = request.form.get('winner')
        loser = request.form.get('loser')

        if winner in db and loser in db:
            new_w, new_l = calculate_new_ratings(db[winner]['rating'], db[loser]['rating'], 1)

            db[winner]['rating'] = new_w
            db[winner]['matches'] += 1
            db[loser]['rating'] = new_l
            db[loser]['matches'] += 1

            save_db(db)

        return redirect(url_for('rank'))

    # --- MATCHMAKING LOGIC ---
    # 1. Calibration: Prioritize songs with few matches
    unranked = [u for u in uris if db[u]['matches'] < 5]

    if len(unranked) >= 2:
        id_a, id_b = random.sample(unranked, 2)
    elif len(unranked) == 1:
        id_a = unranked[0]
        id_b = random.choice([u for u in uris if u != id_a])
    else:
        # 2. Standard Matchmaking: Find close games
        id_a = random.choice(uris)
        rating_a = db[id_a]['rating']

        # Look for opponents within 100 ELO points
        candidates = [u for u in uris if u != id_a and abs(db[u]['rating'] - rating_a) < 100]

        if candidates:
            id_b = random.choice(candidates)
        else:
            # Fallback: Pick any random opponent
            id_b = random.choice([u for u in uris if u != id_a])

    return render_template('rank.html', song_a=db[id_a], song_b=db[id_b])


@app.route('/sync')
def sync_playlist():
    """Updates Spotify playlist order based on local ratings."""
    auth_manager = create_auth_manager()
    if not auth_manager.validate_token(auth_manager.cache_handler.get_cached_token()):
        return redirect(url_for('login'))

    sp = spotipy.Spotify(auth_manager=auth_manager)
    db = load_db()

    if not db:
        return redirect(url_for('dashboard'))

    sorted_songs = sorted(db.values(), key=lambda x: x['rating'], reverse=True)
    sorted_uris = [song['uri'] for song in sorted_songs]

    # Batch processing for Spotify API (limit 100)
    for i in range(0, len(sorted_uris), 100):
        chunk = sorted_uris[i: i + 100]
        if i == 0:
            sp.playlist_replace_items(TARGET_PLAYLIST_ID, chunk)
        else:
            sp.playlist_add_items(TARGET_PLAYLIST_ID, chunk)

    return redirect(url_for('dashboard'))


@app.route('/reset')
def reset_elos():
    db = load_db()
    for uri in db:
        db[uri]['rating'] = 1000.0
        db[uri]['matches'] = 0
    save_db(db)
    return redirect(url_for('dashboard'))


@app.route('/play/<path:uri>')
def play_track(uri):
    """Remote controls Spotify to play the specific track."""
    auth_manager = create_auth_manager()
    if not auth_manager.validate_token(auth_manager.cache_handler.get_cached_token()):
        return "Unauthorized", 401

    sp = spotipy.Spotify(auth_manager=auth_manager)

    try:
        # This tells the active Spotify device to play this URI
        sp.start_playback(uris=[uri])
        return "Playing", 200
    except spotipy.exceptions.SpotifyException as e:
        # This usually happens if no Spotify device is active
        return "No active device found. Open Spotify on your computer or phone.", 404


if __name__ == '__main__':
    app.run(port=5000, debug=True)