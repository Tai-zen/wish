# app.py

# =================================================================
# 🛑 CRITICAL FIX: EVENTLET MONKEY-PATCHING MUST BE FIRST
# =================================================================
import eventlet
eventlet.monkey_patch()
# =================================================================

from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit
import threading
import time
import re 
import os
from datetime import datetime, timedelta 

# --- Flask & Socket.IO Setup ---
app = Flask(__name__)
# WARNING: Change 'your_secret_key' to a long, random value in production
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'a-secure-random-string') 
# Use the eventlet message queue and server for compatibility
socketio = SocketIO(app, async_mode='eventlet') 

# --- Global Configuration & Data Structures ---
# Retention time: 2 hours (7200 seconds)
HISTORY_RETENTION_SECONDS = 2 * 60 * 60 
# Purge check interval: Check every 5 minutes (300 seconds)
PURGE_INTERVAL_SECONDS = 5 * 60 

# Time to keep an alias reserved after disconnect (e.g., 60 seconds)
PERSISTENCE_TIMEOUT_SECONDS = 60 

# Global structures for anonymous chat
user_aliases = {} # Maps Session ID (sid) to the user's alias
current_anon_id = 0
alias_lock = threading.Lock()

# New structure for persistence: Stores disconnected aliases with their cutoff time
# Format: {'Anon-User-1': datetime_object_of_expiry}
temporary_sessions = {}

# New structure to track who is typing: stores a set of user aliases
typing_users = set()
typing_lock = threading.Lock()

# Global structure for chat history (with thread safety)
# Format: [{'alias': 'name', 'msg': 'text', 'timestamp': 1678886400.0}, ...]
chat_history = []
history_lock = threading.Lock()

# --- CENSORSHIP LOGIC ---
# List of words to censor (Abridged for brevity, use your full list)
BAD_WORDS = ['hate', 'harass', 'slur', 'offensive', 'fuck', 'shit', 'ass', 'bastard', 'bitch', 'cock', 'dick', 'whore', 'sex'] 

def censor_message(msg):
    """Censors bad words in the message using regular expressions for word boundaries."""
    censored_msg = msg
    for word in BAD_WORDS:
        # \b ensures word boundaries; re.IGNORECASE makes it case-insensitive
        pattern = r'\b' + re.escape(word) + r'\b'

        # Create a replacement string of asterisks matching the word length
        replacement = '*' * len(word)

        # Replace all occurrences
        censored_msg = re.sub(pattern, replacement, censored_msg, flags=re.IGNORECASE)

    return censored_msg

# --- Broadcast Functions ---

def broadcast_user_count():
    """Calculates current user count and broadcasts it to all clients."""
    # 💡 When emitting outside a request context (like in a background thread or helper),
    # you often need to use app.app_context(). Here, since it's called from socket handlers, 
    # it *should* work, but wrapping it is safer for external calls.
    # For simplicity, we rely on the SocketIO context here, which is usually sufficient.
    with alias_lock:
        count = len(user_aliases)
    socketio.emit('user_count', {'count': count})
    print(f"[STATUS] Broadcasting user count: {count}")

def broadcast_typists():
    """Broadcasts the current list of users who are typing."""
    with typing_lock:
        typists_list = list(typing_users)
    socketio.emit('typists', {'typists': typists_list})

# --- Identity Persistence Helper ---

def get_alias_or_reconnect(sid, provided_alias=None):
    """
    Tries to reuse an alias if the provided alias is still within the persistence timeout.
    Otherwise, generates a new alias.
    """
    global current_anon_id

    # 1. Check if the client provided a valid, recently-used alias
    if provided_alias and provided_alias in temporary_sessions:
        expiry_time = temporary_sessions[provided_alias]
        
        # Check if the alias is still reserved (not expired)
        if datetime.now() < expiry_time:
            # Reconnect successful! Remove from temp sessions and reuse.
            print(f"[RECONNECT] {provided_alias} reused alias (SID: {sid}).")
            del temporary_sessions[provided_alias]
            user_aliases[sid] = provided_alias
            return provided_alias, True

    # 2. If no valid reconnect, generate a new anonymous ID
    current_anon_id += 1
    new_alias = f"Anon-User-{current_anon_id}"
    user_aliases[sid] = new_alias
    print(f"[NEW CONNECTION] {new_alias} assigned (SID: {sid}).")
    return new_alias, False

# --- Background Purge Logic ---
def purge_messages_loop():
    """Runs a continuous loop in a background thread to purge old messages."""
    global chat_history

    # 💡 Running background threads that involve emitting outside of a request 
    # context is a common cause of context errors. Eventlet/SocketIO greenlets 
    # usually manage this, but using app.app_context() is robust if you 
    # were doing other Flask operations. Since this only modifies history 
    # and doesn't emit, it's fine as is.

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
            
            # CRITICAL: Sleep to prevent high CPU usage
            time.sleep(PURGE_INTERVAL_SECONDS)

        except Exception as e:
            print(f"[PURGE ERROR] An exception occurred in the purge loop: {e}")
            time.sleep(PURGE_INTERVAL_SECONDS) 
purge_thread = threading.Thread(target=purge_messages_loop, daemon=True)
purge_thread.start()
    
# --- ROUTES (Serving the UI) ---
@app.route('/')
def index():
    """Renders the main chatroom HTML page."""
    return render_template('index.html')

# --- SOCKET.IO EVENT HANDLERS (Real-time Communication) ---

@socketio.on('connect')
def handle_connect(auth):
    """Assigns an alias, potentially reusing one, and sends history."""
    sid = request.sid
    
    # Get the alias the client might have stored locally
    provided_alias = auth.get('alias') if auth else None
    
    with alias_lock:
        alias, is_reconnect = get_alias_or_reconnect(sid, provided_alias)
    
    # 1. Broadcast the updated user count and current typists
    broadcast_user_count()
    broadcast_typists() 

    # 2. Send Alias back to the user only (Client will store this token)
    emit('set_alias', {'alias': alias})

    # 3. Send entire current history to the newly connected client only
    with history_lock:
        history_to_send = [
            {'alias': msg['alias'], 'msg': msg['msg']} 
            for msg in chat_history
        ]
        emit('history', {'messages': history_to_send})

    # 4. Broadcast join message to everyone else
    if is_reconnect:
        join_msg = f'{alias} reconnected.'
    else:
        join_msg = f'{alias} has joined the chat.'
        
    emit('message', {'alias': 'SERVER', 'msg': join_msg}, 
              broadcast=True, include_self=False)

@socketio.on('disconnect')
def handle_disconnect():
    """Removes the alias, reserves it for a short time, and broadcasts disconnect."""
    sid = request.sid

    with alias_lock:
        alias = user_aliases.pop(sid, 'Unknown User')
        
        # Reserve the alias for the persistence timeout
        if alias != 'Unknown User':
            expiry_time = datetime.now() + timedelta(seconds=PERSISTENCE_TIMEOUT_SECONDS)
            temporary_sessions[alias] = expiry_time
            print(f"[DISCONNECT] {alias} reserved until {expiry_time.strftime('%H:%M:%S')}")

    # Remove user from typing list if they disconnect while typing
    with typing_lock:
        if alias in typing_users:
            typing_users.remove(alias)
            broadcast_typists() 

    # Broadcast the updated user count 
    broadcast_user_count()

    # Broadcast leave message to everyone else
    emit('message', {'alias': 'SERVER', 'msg': f'{alias} has temporarily disconnected (timeout: {PERSISTENCE_TIMEOUT_SECONDS}s).'}, 
              broadcast=True, include_self=False)

# --- SOCKET.IO EVENT HANDLERS (Real-time Communication) ---
@socketio.on('send_message')
def handle_send_message(data):
    """Receives, censors, stores, and broadcasts a message to all clients."""
    
    sid = request.sid
    alias = user_aliases.get(sid, 'Unknown Anon')
    msg = data.get('msg')
    
    # Ensure user is marked as NOT typing after sending a message
    with typing_lock:
        if alias in typing_users:
            typing_users.remove(alias)
            broadcast_typists()

    if msg and msg.strip():
        # --- CENSORSHIP APPLIED HERE ---
        censored_msg = censor_message(msg)

        # 1. Store the CENSORED message with a timestamp
        message_data = {
            'alias': alias, 
            'msg': censored_msg, 
            'timestamp': time.time() # Store current Unix timestamp
        }
        with history_lock:
            chat_history.append(message_data)

        # 2. Broadcast the CENSORED message immediately
        # NOTE: Clients will ignore the echo if the alias matches their own (see index.html)
        emit('message', {'alias': alias, 'msg': censored_msg}, broadcast=True)
    else:
        print(f"[DEBUG] Message was empty or whitespace from {alias}.")

# --- Typing Status Events ---
@socketio.on('is_typing')
def handle_is_typing():
    """Adds the user to the typing list and broadcasts the update."""
    sid = request.sid
    alias = user_aliases.get(sid, 'Unknown Anon')
    with typing_lock:
        if alias not in typing_users:
            typing_users.add(alias)
            broadcast_typists()

@socketio.on('not_typing')
def handle_not_typing():
    """Removes the user from the typing list and broadcasts the update."""
    sid = request.sid
    alias = user_aliases.get(sid, 'Unknown Anon')
    with typing_lock:
        if alias in typing_users:
            typing_users.remove(alias)
            broadcast_typists()

