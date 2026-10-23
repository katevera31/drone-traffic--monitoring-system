import sqlite3

DB_NAME = "database.db"

def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    # 1. Users Table (Admin & User roles)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            role TEXT CHECK(role IN ('admin', 'user')) DEFAULT 'user'
        )
    ''')

    # 2. Telemetry History Logs Table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS telemetry_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            device_id TEXT NOT NULL,
            model TEXT,
            latitude REAL NOT NULL,
            longitude REAL NOT NULL,
            altitude REAL,
            speed REAL,
            heading REAL,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # 3. Geofences Table (Restricted Areas)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS geofences (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            coordinates_json TEXT NOT NULL,
            max_altitude REAL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # 4. Blacklist Table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS blacklist (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT UNIQUE NOT NULL,
            reason TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # Default Accounts
    cursor.execute("INSERT OR IGNORE INTO users (username, password, role) VALUES ('admin', 'admin123', 'admin')")
    cursor.execute("INSERT OR IGNORE INTO users (username, password, role) VALUES ('user', 'user123', 'user')")

    conn.commit()
    conn.close()
    print("[+] Database Initialized Successfully!")

if __name__ == "__main__":
    init_db()