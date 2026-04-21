"""Hydra Authentication and Session Management."""
import sqlite3
import os
import time
import jwt
import base64
from cryptography.fernet import Fernet
from passlib.context import CryptContext
from typing import Optional, Dict, Tuple

# Configuration
SECRET_KEY = os.environ.get("HYDRA_JWT_SECRET", os.urandom(32).hex())
JWT_ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7  # 7 days

# Symmetric encryption key for API secrets
# Must be 32 url-safe base64-encoded bytes. Default generated for local dev if not set.
ENCRYPTION_KEY = os.environ.get("HYDRA_ENCRYPTION_KEY", base64.urlsafe_b64encode(os.urandom(32)).decode())
fernet = Fernet(ENCRYPTION_KEY.encode())

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

DB_PATH = "hydra_users.db"

def init_db():
    """Initialize the SQLite database for user credentials."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT DEFAULT 'user',
            created_at REAL
        )
    ''')
    
    c.execute('''
        CREATE TABLE IF NOT EXISTS api_keys (
            user_id INTEGER NOT NULL,
            exchange TEXT NOT NULL,
            api_key TEXT NOT NULL,
            api_secret_encrypted TEXT NOT NULL,
            created_at REAL,
            PRIMARY KEY (user_id, exchange),
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    ''')
    
    # Create default admin if no users exist
    c.execute("SELECT COUNT(*) FROM users")
    if c.fetchone()[0] == 0:
        admin_hash = pwd_context.hash("admin") # Default password 'admin'
        c.execute("INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, ?, ?)",
                  ("admin", admin_hash, "admin", time.time()))
    conn.commit()
    conn.close()

def create_user(username: str, password: str, role: str = "user") -> bool:
    """Create a new user. Returns True if successful, False if exists."""
    password_hash = pwd_context.hash(password)
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, ?, ?)",
                  (username, password_hash, role, time.time()))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()

def authenticate_user(username: str, password: str) -> Optional[Dict]:
    """Verify credentials and return user info if valid."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, username, password_hash, role FROM users WHERE username = ?", (username,))
    user = c.fetchone()
    conn.close()
    
    if not user:
        return None
        
    user_id, u_name, p_hash, role = user
    if pwd_context.verify(password, p_hash):
        return {"id": user_id, "username": u_name, "role": role}
    return None

def create_access_token(data: dict) -> str:
    """Generate a JWT token for the session."""
    to_encode = data.copy()
    expire = time.time() + (ACCESS_TOKEN_EXPIRE_MINUTES * 60)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=JWT_ALGORITHM)
    return encoded_jwt

def verify_token(token: str) -> Optional[Dict]:
    """Verify a JWT token and return payload."""
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[JWT_ALGORITHM])
        return payload
    except jwt.PyJWTError:
        return None

def save_api_keys(user_id: int, exchange: str, api_key: str, api_secret: str) -> bool:
    """Securely encrypt and save API keys for a user/exchange."""
    encrypted_secret = fernet.encrypt(api_secret.encode()).decode()
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("""
            INSERT INTO api_keys (user_id, exchange, api_key, api_secret_encrypted, created_at) 
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id, exchange) DO UPDATE SET 
            api_key = excluded.api_key,
            api_secret_encrypted = excluded.api_secret_encrypted,
            created_at = excluded.created_at
        """, (user_id, exchange, api_key, encrypted_secret, time.time()))
        conn.commit()
        return True
    except Exception as e:
        import logging
        logging.error(f"Failed to save API keys: {e}")
        return False
    finally:
        conn.close()

def get_api_keys(user_id: int, exchange: str) -> Optional[Dict[str, str]]:
    """Retrieve and decrypt API keys for a user/exchange."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT api_key, api_secret_encrypted FROM api_keys WHERE user_id = ? AND exchange = ?", (user_id, exchange))
    row = c.fetchone()
    conn.close()
    
    if not row:
        return None
        
    api_key, encrypted_secret = row
    try:
        decrypted_secret = fernet.decrypt(encrypted_secret.encode()).decode()
        return {"api_key": api_key, "api_secret": decrypted_secret}
    except Exception as e:
        import logging
        logging.error(f"Failed to decrypt API secret for user {user_id}: {e}")
        return None

def get_api_keys_by_username(username: str, exchange: str) -> Optional[Dict[str, str]]:
    """Retrieve API keys using username instead of user_id."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id FROM users WHERE username = ?", (username,))
    row = c.fetchone()
    conn.close()
    if not row:
        return None
    return get_api_keys(row[0], exchange)

# Initialize db on module load
init_db()
