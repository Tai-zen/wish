# app.py
from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit
import threading
import time
import re # Essential for powerful word filtering
import os
from datetime import datetime

# --- Flask & Socket.IO Setup ---
app = Flask(__name__)
# WARNING: Change 'your_secret_key' to a long, random value in production
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'a-secure-random-string') 
socketio = SocketIO(app)

# --- Global Configuration & Data Structures ---
# Retention time: 2 hours (7200 seconds)
HISTORY_RETENTION_SECONDS = 2 * 60 * 60 
# Purge check interval: Check every 5 minutes (300 seconds)
PURGE_INTERVAL_SECONDS = 5 * 60 

# Global structures for anonymous chat
user_aliases = {} # Maps Session ID (sid) to the user's alias
current_anon_id = 0
alias_lock = threading.Lock()

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
    with alias_lock:
        count = len(user_aliases)
    socketio.emit('user_count', {'count': count})
    print(f"[STATUS] Broadcasting user count: {count}")

def broadcast_typists():
    """Broadcasts the current list of users who are typing."""
    with typing_lock:
        typists_list = list(typing_users)
    socketio.emit('typists', {'typists': typists_list})
    # print(f"[STATUS] Broadcasting typists: {typists_list}") # Commented for less noise

# --- Background Purge Logic ---
def purge_messages_loop():
    """Runs a continuous loop in a background thread to purge old messages."""
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
            
            # CRITICAL: Sleep to prevent high CPU usage
            time.sleep(PURGE_INTERVAL_SECONDS)

        except Exception as e:
            print(f"[PURGE ERROR] An exception occurred in the purge loop: {e}")
            time.sleep(PURGE_INTERVAL_SECONDS) 
# --- Background Thread Initialization (Runs once when Gunicorn loads the app) ---
purge_thread = threading.Thread(target=purge_messages_loop, daemon=True)
purge_thread.start()
# --- ROUTES (Serving the UI) ---
@app.route('/')
def index():
    """Renders the main chatroom HTML page."""
    return render_template('index.html')

# --- SOCKET.IO EVENT HANDLERS (Real-time Communication) ---

@socketio.on('connect')
def handle_connect():
    """Assigns an anonymous alias and sends history upon a new client connection."""
    global current_anon_id
    sid = request.sid

    with alias_lock:
        current_anon_id += 1
        alias = f"Anon-User-{current_anon_id}"
        user_aliases[sid] = alias

        print(f"[NEW CONNECTION] {alias} connected (SID: {sid}).")
    
    # 1. Broadcast the updated user count and current typists
    broadcast_user_count()
    broadcast_typists() 

    # 2. Send Alias back to the user only
    emit('set_alias', {'alias': alias})

    # 3. Send entire current history to the newly connected client only
    with history_lock:
        history_to_send = [
            {'alias': msg['alias'], 'msg': msg['msg']} 
            for msg in chat_history
        ]
        emit('history', {'messages': history_to_send})

    # 4. Broadcast join message to everyone else
    emit('message', {'alias': 'SERVER', 'msg': f'{alias} has joined the chat.'}, 
              broadcast=True, include_self=False)

@socketio.on('disconnect')
def handle_disconnect():
    """Removes the alias, removes from typing list, and broadcasts disconnect."""
    sid = request.sid

    with alias_lock:
        alias = user_aliases.pop(sid, 'Unknown User')
    
    # Remove user from typing list if they disconnect while typing
    with typing_lock:
        if alias in typing_users:
            typing_users.remove(alias)
            broadcast_typists() # Update typist list if user was typing

    # Broadcast the updated user count 
    broadcast_user_count()

    print(f"[DISCONNECTED] {alias} disconnected (SID: {sid}).")

    # Broadcast leave message to everyone else
    emit('message', {'alias': 'SERVER', 'msg': f'{alias} has left the chat.'}, 
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

        # 🛑 DEBUG LINE 2: Log the processed message
        print(f"[{alias}]: Original: '{msg}' | Censored: '{censored_msg}'")
        
        # 1. Store the CENSORED message with a timestamp
        message_data = {
            'alias': alias, 
            'msg': censored_msg, 
            'timestamp': time.time() # Store current Unix timestamp
        }
        with history_lock:
            chat_history.append(message_data)

        # 2. Broadcast the CENSORED message immediately
        emit('message', {'alias': alias, 'msg': censored_msg}, broadcast=True)
    else:
        # 🛑 DEBUG LINE 3: Log if the message was empty/not processed
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


    # Run the SocketIO application

    socketio.run(app, host='0.0.0.0', port=5050, debug=True)

