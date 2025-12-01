# app.py

# =================================================================
# 🛑 CRITICAL FIX: EVENTLET MONKEY-PATCHING MUST BE FIRST
# =================================================================
import eventlet
eventlet.monkey_patch()
# =================================================================

from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit
# We will still use threading.Lock, but not threading.Thread for the main loop
import threading 
import time
import re 
import os
from datetime import datetime, timedelta 

# --- Flask & Socket.IO Setup ---
app = Flask(__name__)
# WARNING: Change 'your_secret_key' to a long, random value in production
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'a-secure-random-string') 
# Explicitly use async_mode='eventlet'
socketio = SocketIO(app, async_mode='eventlet') 

# --- Global Configuration & Data Structures ---
HISTORY_RETENTION_SECONDS = 2 * 60 * 60 
PURGE_INTERVAL_SECONDS = 5 * 60 
PERSISTENCE_TIMEOUT_SECONDS = 60 

user_aliases = {} 
current_anon_id = 0
alias_lock = threading.Lock()
temporary_sessions = {}
typing_users = set()
typing_lock = threading.Lock()
chat_history = []
history_lock = threading.Lock()

# --- CENSORSHIP LOGIC ---
BAD_WORDS = ['hate', 'harass', 'slur', 'offensive', 'fuck', 'shit', 'ass', 'bastard', 'bitch', 'cock', 'dick', 'whore', 'sex'] 

def censor_message(msg):
    """Censors bad words in the message using regular expressions for word boundaries."""
    censored_msg = msg
    for word in BAD_WORDS:
        pattern = r'\b' + re.escape(word) + r'\b'
        replacement = '*' * len(word)
        censored_msg = re.sub(pattern, replacement, censored_msg, flags=re.IGNORECASE)
    return censored_msg

# --- Broadcast Functions ---

def broadcast_user_count():
    with alias_lock:
        count = len(user_aliases)
    socketio.emit('user_count', {'count': count})
    print(f"[STATUS] Broadcasting user count: {count}")

def broadcast_typists():
    with typing_lock:
        typists_list = list(typing_users)
    socketio.emit('typists', {'typists': typists_list})

# --- Identity Persistence Helper ---

def get_alias_or_reconnect(sid, provided_alias=None):
    global current_anon_id
    if provided_alias and provided_alias in temporary_sessions:
        expiry_time = temporary_sessions[provided_alias]
        if datetime.now() < expiry_time:
            print(f"[RECONNECT] {provided_alias} reused alias (SID: {sid}).")
            del temporary_sessions[provided_alias]
            user_aliases[sid] = provided_alias
            return provided_alias, True

    current_anon_id += 1
    new_alias = f"Anon-User-{current_anon_id}"
    user_aliases[sid] = new_alias
    print(f"[NEW CONNECTION] {new_alias} assigned (SID: {sid}).")
    return new_alias, False

# --- Background Purge Logic ---
def purge_messages_loop():
    """Runs a continuous loop as a background task to purge old messages."""
    global chat_history

    while True:
        try:
            print(f"[PURGE] Starting message purge check at {datetime.now().strftime('%H:%M:%S')}")

            cutoff_time = time.time() - HISTORY_RETENTION_SECONDS

            with history_lock:
                new_history = [
                    msg for msg in chat_history if msg['timestamp'] > cutoff_time
                ] 
                removed_count = len(chat_history) - len(new_history)
                if removed_count > 0:
                    chat_history = new_history
                    print(f"[PURGE] Removed {removed_count} old messages. History size: {len(chat_history)}")
                else:
                    print(f"[PURGE] No messages removed. History size: {len(chat_history)}")
            
            # CRITICAL: Sleep using eventlet.sleep to yield control
            eventlet.sleep(PURGE_INTERVAL_SECONDS) # Use eventlet.sleep instead of time.sleep

        except Exception as e:
            print(f"[PURGE ERROR] An exception occurred in the purge loop: {e}")
            eventlet.sleep(PURGE_INTERVAL_SECONDS) 

# 🛑 REMOVE: The thread creation/start is now moved to the setup function/block.
# purge_thread = threading.Thread(target=purge_messages_loop, daemon=True)
# purge_thread.start()


# --- ROUTES (Serving the UI) ---
@app.route('/')
def index():
    """Renders the main chatroom HTML page."""
    return render_template('index.html')

# --- SOCKET.IO EVENT HANDLERS (Real-time Communication) ---

# ⭐️ NEW: Use the 'before_first_request' hook to start the background task ONLY once 
# after the app context is ready and before the first request is processed.
@app.before_first_request
def start_background_tasks():
    print("[SETUP] Starting background message purge task.")
    socketio.start_background_task(purge_messages_loop)

@socketio.on('connect')
def handle_connect(auth):
    sid = request.sid
    provided_alias = auth.get('alias') if auth else None
    
    with alias_lock:
        alias, is_reconnect = get_alias_or_reconnect(sid, provided_alias)
    
    broadcast_user_count()
    broadcast_typists() 
    emit('set_alias', {'alias': alias})

    with history_lock:
        history_to_send = [
            {'alias': msg['alias'], 'msg': msg['msg']} 
            for msg in chat_history
        ]
        emit('history', {'messages': history_to_send})

    if is_reconnect:
        join_msg = f'{alias} reconnected.'
    else:
        join_msg = f'{alias} has joined the chat.'
        
    emit('message', {'alias': 'SERVER', 'msg': join_msg}, 
              broadcast=True, include_self=False)

@socketio.on('disconnect')
def handle_disconnect():
    sid = request.sid
    with alias_lock:
        alias = user_aliases.pop(sid, 'Unknown User')
        if alias != 'Unknown User':
            expiry_time = datetime.now() + timedelta(seconds=PERSISTENCE_TIMEOUT_SECONDS)
            temporary_sessions[alias] = expiry_time
            print(f"[DISCONNECT] {alias} reserved until {expiry_time.strftime('%H:%M:%S')}")

    with typing_lock:
        if alias in typing_users:
            typing_users.remove(alias)
            broadcast_typists() 

    broadcast_user_count()
    emit('message', {'alias': 'SERVER', 'msg': f'{alias} has temporarily disconnected (timeout: {PERSISTENCE_TIMEOUT_SECONDS}s).'}, 
              broadcast=True, include_self=False)

# --- SOCKET.IO EVENT HANDLERS (Real-time Communication) ---
@socketio.on('send_message')
def handle_send_message(data):
    sid = request.sid
    alias = user_aliases.get(sid, 'Unknown Anon')
    msg = data.get('msg')
    
    with typing_lock:
        if alias in typing_users:
            typing_users.remove(alias)
            broadcast_typists()

    if msg and msg.strip():
        censored_msg = censor_message(msg)
        message_data = {
            'alias': alias, 
            'msg': censored_msg, 
            'timestamp': time.time() 
        }
        with history_lock:
            chat_history.append(message_data)
        emit('message', {'alias': alias, 'msg': censored_msg}, broadcast=True)
    else:
        print(f"[DEBUG] Message was empty or whitespace from {alias}.")

# --- Typing Status Events ---
@socketio.on('is_typing')
def handle_is_typing():
    sid = request.sid
    alias = user_aliases.get(sid, 'Unknown Anon')
    with typing_lock:
        if alias not in typing_users:
            typing_users.add(alias)
            broadcast_typists()

@socketio.on('not_typing')
def handle_not_typing():
    sid = request.sid
    alias = user_aliases.get(sid, 'Unknown Anon')
    with typing_lock:
        if alias in typing_users:
            typing_users.remove(alias)
            broadcast_typists()


if __name__ == '__main__':
    # When running locally via 'python app.py', use the internal runner
    socketio.run(app, debug=True)
    # When running via Gunicorn (deployment):
    # The Gunicorn worker process will import the module, and the 
    # @app.before_first_request hook will handle starting the background task 
    # inside each worker process.
