import json
import os
import random

import spotipy
from dotenv import load_dotenv
from flask import Flask, render_template, request, redirect, url_for, session, jsonify
from spotipy.cache_handler import FlaskSessionCacheHandler
from spotipy.oauth2 import SpotifyOAuth

from elo import calculate_new_ratings

# --- CONFIGURATION ---
load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv('FLASK_SECRET_KEY', 'dev_key_change_in_prod')
app.config['SESSION_COOKIE_NAME'] = 'SpotifyCookie'

# Spotify Config
CLIENT_ID = os.getenv('SPOTIPY_CLIENT_ID')
CLIENT_SECRET = os.getenv('SPOTIPY_CLIENT_SECRET')
REDIRECT_URI = os.getenv('SPOTIPY_REDIRECT_URI', 'http://127.0.0.1:5000/callback')
SCOPE = "user-library-read playlist-read-private playlist-modify-private playlist-modify-public user-modify-playback-state user-read-playback-state"

# Data Storage Config
DATA_DIR = 'data'
MANIFEST_FILE = os.path.join(DATA_DIR, 'manifest.json')

# Ensure data directory exists
os.makedirs(DATA_DIR, exist_ok=True)


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


# --- DATABASE & MANIFEST HELPERS ---

def load_manifest():
    """Loads the registry of tracked playlists."""
    if not os.path.exists(MANIFEST_FILE):
        return {}
    try:
        with open(MANIFEST_FILE, 'r') as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {}


def save_manifest(data):
    """Saves the registry of tracked playlists."""
    with open(MANIFEST_FILE, 'w') as f:
        json.dump(data, f, indent=4)


def get_db_path(playlist_id):
    """Returns the filepath for a specific playlist ID."""
    # Sanitize ID to prevent directory traversal
    safe_id = "".join(x for x in playlist_id if x.isalnum())
    return os.path.join(DATA_DIR, f"{safe_id}.json")


def load_db(playlist_id):
    """Loads the specific JSON for the active playlist."""
    file_path = get_db_path(playlist_id)
    if not os.path.exists(file_path):
        return {}
    try:
        with open(file_path, 'r') as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {}


def save_db(playlist_id, data):
    """Atomic write for the specific playlist."""
    file_path = get_db_path(playlist_id)
    temp_file = f"{file_path}.tmp"
    with open(temp_file, 'w') as f:
        json.dump(data, f, indent=4)
    os.replace(temp_file, file_path)


# --- LOBBY & PLAYLIST MANAGEMENT ROUTES ---

@app.route('/')
def lobby():
    """The new Home Screen: List all tracked playlists."""
    manifest = load_manifest()
    return render_template('lobby.html', playlists=manifest)


@app.route('/add_playlist', methods=['POST'])
def add_playlist():
    """Fetches metadata for a new ID and creates the entry."""
    auth_manager = create_auth_manager()
    if not auth_manager.validate_token(auth_manager.cache_handler.get_cached_token()):
        return redirect(url_for('login'))

    sp = spotipy.Spotify(auth_manager=auth_manager)
    playlist_id = request.form.get('playlist_id')

    # Strip URL if pasted, keeping only the ID
    if 'spotify.com' in playlist_id:
        playlist_id = playlist_id.split('/')[-1].split('?')[0]

    try:
        # Fetch Metadata from Spotify
        pl_data = sp.playlist(playlist_id)
        name = pl_data['name']
        # Safely get image
        image = pl_data['images'][0]['url'] if pl_data['images'] else ""

        # Update Manifest
        manifest = load_manifest()
        manifest[playlist_id] = {
            'name': name,
            'image': image,
            'id': playlist_id
        }
        save_manifest(manifest)

        # Initialize empty DB file if not exists
        if not os.path.exists(get_db_path(playlist_id)):
            save_db(playlist_id, {})

        return redirect(url_for('lobby'))

    except Exception as e:
        return f"Error adding playlist. Is the ID correct? Spotify says: {e}"


@app.route('/delete_playlist/<playlist_id>', methods=['POST'])
def delete_playlist(playlist_id):
    """Removes a playlist from the manifest and deletes its data file."""
    # 1. Update Manifest
    manifest = load_manifest()
    if playlist_id in manifest:
        del manifest[playlist_id]
        save_manifest(manifest)

    # 2. Delete the physical JSON file
    file_path = get_db_path(playlist_id)
    if os.path.exists(file_path):
        try:
            os.remove(file_path)
        except OSError:
            pass  # File might already be gone, ignore error

    # 3. Clear session if this was the active playlist
    if session.get('active_playlist_id') == playlist_id:
        session.pop('active_playlist_id', None)
        session.pop('active_playlist_name', None)

    return redirect(url_for('lobby'))


@app.route('/select_playlist/<playlist_id>')
def select_playlist(playlist_id):
    """Sets the active session context."""
    manifest = load_manifest()
    if playlist_id not in manifest:
        return "Playlist not found in manifest", 404

    session['active_playlist_id'] = playlist_id
    session['active_playlist_name'] = manifest[playlist_id]['name']
    return redirect(url_for('dashboard'))


# --- CORE APP ROUTES ---

@app.route('/dashboard')
def dashboard():
    """Shows the leaderboard for the ACTIVE playlist."""
    if 'active_playlist_id' not in session:
        return redirect(url_for('lobby'))

    pid = session['active_playlist_id']
    pname = session.get('active_playlist_name', 'Unknown Playlist')

    db = load_db(pid)
    auth_manager = create_auth_manager()
    token_info = auth_manager.cache_handler.get_cached_token()
    is_logged_in = auth_manager.validate_token(token_info) if token_info else False

    sorted_songs = sorted(db.values(), key=lambda x: x['rating'], reverse=True) if db else []
    total_matches = sum(s['matches'] for s in db.values()) // 2 if db else 0

    return render_template('dashboard.html',
                           songs=sorted_songs,
                           first_run=(not db),
                           logged_in=is_logged_in,
                           total_matches=total_matches,
                           playlist_name=pname)


@app.route('/login')
def login():
    auth_manager = create_auth_manager()
    return redirect(auth_manager.get_authorize_url())


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('lobby'))


@app.route('/callback')
def callback():
    auth_manager = create_auth_manager()
    code = request.args.get("code")
    if code:
        auth_manager.get_access_token(code)
        # If we have an active playlist, go there, otherwise lobby
        if 'active_playlist_id' in session:
            return redirect(url_for('ingest_playlist'))
        return redirect(url_for('lobby'))
    return redirect(url_for('lobby'))


@app.route('/ingest')
def ingest_playlist():
    if 'active_playlist_id' not in session:
        return redirect(url_for('lobby'))

    auth_manager = create_auth_manager()
    if not auth_manager.validate_token(auth_manager.cache_handler.get_cached_token()):
        return redirect(url_for('login'))

    pid = session['active_playlist_id']
    sp = spotipy.Spotify(auth_manager=auth_manager)

    old_db = load_db(pid)
    new_db = {}

    try:
        results = sp.playlist_items(pid)
    except Exception:
        return "Error fetching playlist. It might be private or deleted."

    tracks = results['items']
    while results['next']:
        results = sp.next(results)
        tracks.extend(results['items'])

    for item in tracks:
        track = item.get('track')
        if not track or track.get('is_local'): continue

        uri = track['uri']
        img_url = track['album']['images'][0]['url'] if track['album']['images'] else ""

        if uri in old_db:
            new_db[uri] = old_db[uri]
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

    save_db(pid, new_db)
    return redirect(url_for('dashboard'))


@app.route('/rank', methods=['GET', 'POST'])
def rank():
    if 'active_playlist_id' not in session:
        return redirect(url_for('lobby'))

    pid = session['active_playlist_id']
    db = load_db(pid)
    uris = list(db.keys())

    if len(uris) < 2:
        return f"Not enough songs! <a href='/ingest'>Fetch Songs for {session['active_playlist_name']}</a> first."

    if request.method == 'POST':
        # Handle "next match without vote" (auto-advance or manual next)
        if 'next_match' in request.form:
            return redirect(url_for('rank'))

        # Handle actual vote
        winner = request.form.get('winner')
        loser = request.form.get('loser')

        if winner and loser and winner in db and loser in db:
            new_w, new_l = calculate_new_ratings(
                db[winner]['rating'], db[loser]['rating'], 1,
                db[winner]['matches'], db[loser]['matches']
            )
            db[winner]['rating'] = new_w
            db[winner]['matches'] += 1
            db[loser]['rating'] = new_l
            db[loser]['matches'] += 1
            save_db(pid, db)

            # Return JSON for AJAX vote confirmation
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return jsonify({'success': True, 'winner_uri': winner})

        return redirect(url_for('rank'))

    # --- MATCHMAKING (Unchanged Logic) ---
    zero_matches = [u for u in uris if db[u]['matches'] == 0]
    calibration = [u for u in uris if db[u]['matches'] < 5]

    if len(zero_matches) >= 2:
        id_a, id_b = random.sample(zero_matches, 2)
    elif len(zero_matches) == 1:
        id_a = zero_matches[0]
        id_b = random.choice([u for u in uris if u != id_a])
    elif len(calibration) >= 2:
        id_a, id_b = random.sample(calibration, 2)
    else:
        id_a = random.choice(uris)
        rating_a = db[id_a]['rating']
        candidates = [u for u in uris if u != id_a and abs(db[u]['rating'] - rating_a) < 100]
        if candidates:
            id_b = random.choice(candidates)
        else:
            id_b = random.choice([u for u in uris if u != id_a])

    return render_template('rank.html', song_a=db[id_a], song_b=db[id_b])


@app.route('/push')
def push_playlist():
    if 'active_playlist_id' not in session: return redirect(url_for('lobby'))

    auth_manager = create_auth_manager()
    if not auth_manager.validate_token(auth_manager.cache_handler.get_cached_token()):
        return redirect(url_for('login'))

    pid = session['active_playlist_id']
    sp = spotipy.Spotify(auth_manager=auth_manager)
    db = load_db(pid)

    if not db: return redirect(url_for('dashboard'))

    sorted_songs = sorted(db.values(), key=lambda x: x['rating'], reverse=True)
    sorted_uris = [song['uri'] for song in sorted_songs]

    # Batch processing
    for i in range(0, len(sorted_uris), 100):
        chunk = sorted_uris[i: i + 100]
        if i == 0:
            sp.playlist_replace_items(pid, chunk)
        else:
            sp.playlist_add_items(pid, chunk)

    return redirect(url_for('dashboard'))


@app.route('/reset')
def reset_elos():
    if 'active_playlist_id' not in session: return redirect(url_for('lobby'))

    pid = session['active_playlist_id']
    db = load_db(pid)
    songs_list = list(db.values())

    for song in songs_list:
        song['rating'] = 1000.0
        song['matches'] = 0

    random.shuffle(songs_list)
    new_db = {song['uri']: song for song in songs_list}
    save_db(pid, new_db)
    return redirect(url_for('dashboard'))


# --- NEW API ENDPOINTS ---

@app.route('/api/playback_status')
def playback_status():
    """Returns current playback state for polling."""
    auth_manager = create_auth_manager()
    if not auth_manager.validate_token(auth_manager.cache_handler.get_cached_token()):
        return jsonify({'error': 'Unauthorized'}), 401

    sp = spotipy.Spotify(auth_manager=auth_manager)
    try:
        playback = sp.current_playback()
        if not playback or not playback.get('item'):
            return jsonify({
                'is_playing': False,
                'current_uri': None,
                'progress_ms': 0,
                'duration_ms': 0
            })

        return jsonify({
            'is_playing': playback.get('is_playing', False),
            'current_uri': playback['item']['uri'],
            'progress_ms': playback.get('progress_ms', 0),
            'duration_ms': playback['item']['duration_ms']
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500



@app.route('/seek')
def seek_track():
    auth_manager = create_auth_manager()
    if not auth_manager.validate_token(auth_manager.cache_handler.get_cached_token()):
        return "Unauthorized", 401

    sp = spotipy.Spotify(auth_manager=auth_manager)
    position_ms = request.args.get('position_ms')

    try:
        if position_ms:
            sp.seek_track(int(position_ms))
            return "Seeked", 200
        return "Missing position", 400
    except Exception as e:
        return str(e), 500


@app.route('/toggle_playback', methods=['POST'])
def toggle_playback():
    """Toggles between pause and play."""
    auth_manager = create_auth_manager()
    if not auth_manager.validate_token(auth_manager.cache_handler.get_cached_token()):
        return jsonify({'error': 'Unauthorized'}), 401

    sp = spotipy.Spotify(auth_manager=auth_manager)
    try:
        playback = sp.current_playback()
        if not playback:
            return jsonify({'error': 'No active device'}), 404

        if playback.get('is_playing'):
            sp.pause_playback()
            return jsonify({'action': 'paused', 'is_playing': False})
        else:
            sp.start_playback()
            return jsonify({'action': 'resumed', 'is_playing': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# --- PLAYER CONTROLS ---

@app.route('/skip_forward')
def skip_forward():
    auth_manager = create_auth_manager()
    if not auth_manager.validate_token(auth_manager.cache_handler.get_cached_token()):
        return "Unauthorized", 401
    sp = spotipy.Spotify(auth_manager=auth_manager)
    try:
        playback = sp.current_playback()
        if not playback or not playback.get('is_playing'):
            return "Paused", 400
        new_pos = playback['progress_ms'] + 10000
        if new_pos > playback['item']['duration_ms']:
            new_pos = playback['item']['duration_ms'] - 1000
        sp.seek_track(new_pos)
        return "Skipped", 200
    except Exception as e:
        return str(e), 500


@app.route('/play_match')
def play_match_pair():
    auth_manager = create_auth_manager()
    if not auth_manager.validate_token(auth_manager.cache_handler.get_cached_token()):
        return "Unauthorized", 401
    sp = spotipy.Spotify(auth_manager=auth_manager)
    uri_a = request.args.get('uri_a')
    uri_b = request.args.get('uri_b')
    if not uri_a or not uri_b:
        return "Missing URIs", 400
    try:
        sp.start_playback(uris=[uri_a, uri_b])
        return "Playing Match", 200
    except Exception:
        return "No active device", 404


@app.route('/play/<path:uri>')
def play_track(uri):
    auth_manager = create_auth_manager()
    if not auth_manager.validate_token(auth_manager.cache_handler.get_cached_token()):
        return "Unauthorized", 401
    sp = spotipy.Spotify(auth_manager=auth_manager)
    try:
        sp.start_playback(uris=[uri])
        return "Playing", 200
    except Exception:
        return "No active device", 404


if __name__ == '__main__':
    app.run(port=5000, debug=True)