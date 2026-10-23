import os
import time
import sqlite3
import threading
import json
from datetime import datetime, timedelta
import pandas as pd
from flask import Flask, render_template, request, redirect, url_for, jsonify, send_file
from flask_socketio import SocketIO, emit
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user

from database import init_db, DB_NAME
import telemetry.wifi_sniffer as wifi_module
import telemetry.network as network_module

app = Flask(__name__)
app.config['SECRET_KEY'] = 'dms_secret_key_2026'

socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

init_db()

class User(UserMixin):
    def __init__(self, id, username, role):
        self.id = id
        self.username = username
        self.role = role

@login_manager.user_loader
def load_user(user_id):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT id, username, role FROM users WHERE id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    if row:
        return User(id=row[0], username=row[1], role=row[2])
    return None

last_logged_time = {}

# Background thread to purge old records
def auto_purge_old_logs():
    while True:
        try:
            conn = sqlite3.connect(DB_NAME)
            cursor = conn.cursor()
            cutoff_date = (datetime.now() - timedelta(days=90)).strftime('%Y-%m-%d %H:%M:%S')
            cursor.execute("DELETE FROM telemetry_logs WHERE timestamp < ?", (cutoff_date,))
            deleted_count = cursor.rowcount
            conn.commit()
            conn.close()
            if deleted_count > 0:
                print(f"[CLEANUP] Purged {deleted_count} old telemetry logs")
        except Exception as e:
            print(f"[!] Purge Error: {e}")
        time.sleep(86400)
        
try:
    import rid_probe
    rec = rid_probe.Recorder("captures", show=False)
    wifi_module.CAPTURE_HOOK = rec.hook
except Exception as e:
    print("[!] recorder not started:", e)

# Hardware status monitor
def hardware_health_monitor():
    while True:
        try:
            curr = time.time()
            
            # Wi-Fi sniffer status
            wifi_state = "waiting"
            if (curr - wifi_module.last_wifi_recv_time) < 10:
                wifi_state = "receiving"
            else:
                wifi_state = "waiting"

            status_payload = {
                "wifi_sniffer": wifi_state
            }
            socketio.emit('hardware_status_update', status_payload)

            # MQTT network RID status
            if network_module.mqtt_connected:
                if (curr - network_module.last_mqtt_recv_time) < 10:
                    socketio.emit('mqtt_status', {'status': 'receiving'})
                else:
                    socketio.emit('mqtt_status', {'status': 'waiting'})
            else:
                socketio.emit('mqtt_status', {'status': 'disconnected'})

            socketio.sleep(0)
        except Exception as e:
            print(f"[!] Health Monitor Error: {e}")
        time.sleep(2)

# Telemetry processor
def process_telemetry(data):
    try:
        dev_id = data.get('device_id')
        if not dev_id:
            print(f"[!] Warning: Missing device_id in data: {data}")
            return

        current_time = time.time()

        # Database logging (throttle to 1 update per 60 seconds per device)
        if dev_id not in last_logged_time or (current_time - last_logged_time[dev_id]) >= 60:
            try:
                conn = sqlite3.connect(DB_NAME, timeout=10)
                cursor = conn.cursor()
                local_time_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

                cursor.execute('''
                    INSERT INTO telemetry_logs (source, device_id, model, latitude, longitude, altitude, speed, heading, timestamp)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    data.get('source', 'Network RID'),
                    dev_id,
                    data.get('model', 'Unknown'),
                    data.get('latitude', 0.0),
                    data.get('longitude', 0.0),
                    data.get('altitude', 0),
                    data.get('speed', 0),
                    data.get('heading', 0),
                    local_time_str
                ))
                conn.commit()
                last_logged_time[dev_id] = current_time
                print(f"[DB LOGGED] Telemetry saved for {dev_id} at {local_time_str}")
            except Exception as db_err:
                print(f"[!] DB Insert Error: {db_err}")
            finally:
                conn.close()

        # Blacklist check
        try:
            conn = sqlite3.connect(DB_NAME, timeout=10)
            cursor = conn.cursor()
            cursor.execute("SELECT device_id FROM blacklist WHERE device_id = ?", (dev_id,))
            blacklisted = cursor.fetchone() is not None
        except Exception as e:
            print(f"[!] Blacklist Check Error: {e}")
            blacklisted = False
        finally:
            conn.close()

        data['blacklisted'] = blacklisted

        # Live map update via WebSocket
        socketio.emit('telemetry_update', data)
        socketio.sleep(0)
    except Exception as e:
        print(f"[!] Processing Error: {e}")

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute("SELECT id, username, password, role FROM users WHERE username = ?", (username,))
        row = cursor.fetchone()
        conn.close()

        if row and row[2] == password:
            user = User(id=row[0], username=row[1], role=row[3])
            login_user(user)
            return redirect(url_for('dashboard'))
        else:
            return render_template('login.html', error="Invalid username or password")
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

@app.route('/')
@login_required
def dashboard():
    return render_template('dashboard.html', user=current_user)

@app.route('/history')
@login_required
def history():
    return render_template('history.html', user=current_user)

@app.route('/playback')
@login_required
def playback():
    return render_template('playback.html', user=current_user)

@app.route('/api/history_data')
@login_required
def get_history_data():
    drone_id = request.args.get('drone_id', '').strip()
    start_time = request.args.get('start_time', '').strip()
    end_time = request.args.get('end_time', '').strip()

    conn = sqlite3.connect(DB_NAME)
    query = "SELECT * FROM telemetry_logs WHERE 1=1"
    params = []

    if drone_id:
        query += " AND (device_id LIKE ? OR model LIKE ?)"
        params.extend([f"%{drone_id}%", f"%{drone_id}%"])

    if start_time:
        formatted_start = start_time.replace('T', ' ')
        if len(formatted_start) == 16:
            formatted_start += ":00"
        query += " AND timestamp >= ?"
        params.append(formatted_start)

    if end_time:
        formatted_end = end_time.replace('T', ' ')
        if len(formatted_end) == 16:
            formatted_end += ":59"
        query += " AND timestamp <= ?"
        params.append(formatted_end)

    query += " ORDER BY timestamp DESC"
    df = pd.read_sql_query(query, conn, params=params)
    conn.close()
    return jsonify(df.to_dict(orient='records'))

@app.route('/api/export_excel')
@login_required
def export_excel():
    conn = sqlite3.connect(DB_NAME)
    df = pd.read_sql_query("SELECT * FROM telemetry_logs ORDER BY timestamp DESC", conn)
    conn.close()
    file_path = "telemetry_export.xlsx"
    df.to_excel(file_path, index=False)
    return send_file(file_path, as_attachment=True, download_name="DMS_Telemetry_History.xlsx")

@app.route('/api/blacklist', methods=['GET', 'POST', 'DELETE'])
@login_required
def manage_blacklist():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    if request.method == 'GET':
        cursor.execute("SELECT * FROM blacklist")
        rows = cursor.fetchall()
        conn.close()
        return jsonify([{"id": r[0], "device_id": r[1], "reason": r[2]} for r in rows])
    elif request.method == 'POST':
        if current_user.role != 'admin':
            conn.close()
            return jsonify({"error": "Unauthorized"}), 403
        data = request.json
        cursor.execute("INSERT OR REPLACE INTO blacklist (device_id, reason) VALUES (?, ?)", (data['device_id'], data.get('reason', '')))
        conn.commit()
        conn.close()
        return jsonify({"success": True})
    elif request.method == 'DELETE':
        if current_user.role != 'admin':
            conn.close()
            return jsonify({"error": "Unauthorized"}), 403
        device_id = request.args.get('device_id')
        cursor.execute("DELETE FROM blacklist WHERE device_id = ?", (device_id,))
        conn.commit()
        conn.close()
        return jsonify({"success": True})

@app.route('/api/users', methods=['GET', 'POST', 'DELETE'])
@login_required
def manage_users():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    if request.method == 'GET':
        cursor.execute("SELECT id, username, role FROM users")
        rows = cursor.fetchall()
        conn.close()
        return jsonify([{"id": r[0], "username": r[1], "role": r[2]} for r in rows])
    elif request.method == 'POST':
        if current_user.role != 'admin':
            conn.close()
            return jsonify({"error": "Unauthorized"}), 403
        data = request.json
        username = data.get('username')
        password = data.get('password')
        role = data.get('role', 'user')
        try:
            cursor.execute("INSERT INTO users (username, password, role) VALUES (?, ?, ?)", (username, password, role))
            conn.commit()
            conn.close()
            return jsonify({"success": True})
        except sqlite3.IntegrityError:
            conn.close()
            return jsonify({"error": "Username already exists"}), 400
    elif request.method == 'DELETE':
        if current_user.role != 'admin':
            conn.close()
            return jsonify({"error": "Unauthorized"}), 403
        username = request.args.get('username')
        if username == 'admin':
            conn.close()
            return jsonify({"error": "Cannot delete primary admin"}), 400
        cursor.execute("DELETE FROM users WHERE username = ?", (username,))
        conn.commit()
        conn.close()
        return jsonify({"success": True})

if __name__ == '__main__':
    wifi_thread = threading.Thread(target=wifi_module.start_wifi_sniffer, args=(process_telemetry,), daemon=True)
    purge_thread = threading.Thread(target=auto_purge_old_logs, daemon=True)
    health_thread = threading.Thread(target=hardware_health_monitor, daemon=True)

    wifi_thread.start()
    purge_thread.start()
    health_thread.start()

    network_module.start_mqtt_client(socketio, process_telemetry)

    print("[*] Launching Multi-Source Drone Monitoring Server on http://127.0.0.1:5000")
    socketio.run(app, host='0.0.0.0', port=5000, debug=False)