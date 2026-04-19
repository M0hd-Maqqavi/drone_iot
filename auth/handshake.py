# auth/handshake.py
"""
4-Step Mutual Authentication Handshake

Terminology:
    λi      = pre-shared secret (PRESHARED_SECRET from config)
    μkey    = session key (generated fresh every session)
    ηserver = server nonce (random, used once, prevents replay attacks)
    ηclient = client nonce (random, used once, prevents replay attacks)
    ψ       = λi XOR μkey  (hides session key inside pre-shared secret)
"""

import os
import json
import logging
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad
from config import PRESHARED_SECRET, DEVICE_ID, MAX_AUTH_FAILURES

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# LOW-LEVEL CRYPTO HELPERS
# ─────────────────────────────────────────────

def generate_nonce() -> bytes:
    """
    Generate a 16-byte cryptographically random nonce.
    'Nonce' = Number used ONCE. Every session gets a fresh one.
    This is what prevents replay attacks — an attacker can't reuse
    a captured packet because the nonce will be different next time.
    os.urandom() uses the OS's secure random number generator.
    """
    return os.urandom(16)


def generate_session_key() -> bytes:
    """
    Generate a fresh 16-byte (128-bit) session key (μkey).
    This is different from the pre-shared secret — it's temporary,
    valid for one session only. Even if one session is compromised,
    past and future sessions remain secure (forward secrecy).
    """
    return os.urandom(16)


def xor_bytes(a: bytes, b: bytes) -> bytes:
    """
    XOR two byte strings together, byte by byte.
    Key property: a XOR b XOR b = a  (XOR is its own inverse)
    
    Used for:
        ψ = λi XOR μkey   (hide session key inside pre-shared secret)
        μkey = ψ XOR λi   (recover session key on the other side)
    
    Both inputs must be the same length (both 16 bytes here).
    """
    return bytes(x ^ y for x, y in zip(a, b))


def aes_encrypt(key: bytes, data: bytes) -> bytes:
    """
    Encrypt data using AES-128-CBC.
    
    AES-128: Block cipher, 128-bit key, 128-bit blocks.
    CBC mode: Each block is XORed with previous ciphertext block
              before encryption — adds extra security.
    
    IV (Initialization Vector): Random 16 bytes prepended to output.
    The receiver needs the IV to decrypt — it's not secret, just random.
    We prepend it to the ciphertext so the receiver always has it.
    
    pad(): AES requires input to be a multiple of 16 bytes.
           pad() adds PKCS7 padding to make it fit.
    
    Returns: IV (16 bytes) + ciphertext
    """
    iv = os.urandom(16)
    cipher = AES.new(key, AES.MODE_CBC, iv)
    ciphertext = cipher.encrypt(pad(data, AES.block_size))
    return iv + ciphertext  # prepend IV so receiver can decrypt


def aes_decrypt(key: bytes, data: bytes) -> bytes:
    """
    Decrypt AES-128-CBC encrypted data.
    
    First 16 bytes = IV (extracted, not secret)
    Remaining bytes = actual ciphertext
    
    unpad(): Removes the PKCS7 padding that was added during encryption.
    
    Returns: original plaintext bytes
    """
    iv = data[:16]           # extract IV from first 16 bytes
    ciphertext = data[16:]   # rest is the actual encrypted data
    cipher = AES.new(key, AES.MODE_CBC, iv)
    return unpad(cipher.decrypt(ciphertext), AES.block_size)


# ─────────────────────────────────────────────
# SERVER SIDE
# ─────────────────────────────────────────────

class AuthServer:
    """
    Runs on the gateway/server side.
    Handles incoming authentication requests from clients.
    
    State machine per device:
        IDLE → CHALLENGED → AUTHENTICATED
    
    Tracks failed attempts per device and blacklists after MAX_AUTH_FAILURES.
    """

    def __init__(self):
        # Stores active session state per device_id
        # Format: { device_id: { "mu_key": ..., "eta_server": ..., "state": ... } }
        self.sessions = {}

        # Tracks failed attempts per device
        # Format: { device_id: int }
        self.failed_attempts = {}

        # Blacklisted device IDs — rejected immediately
        self.blacklist = set()

    def _is_blacklisted(self, device_id: str) -> bool:
        return device_id in self.blacklist

    def _record_failure(self, device_id: str):
        """Increment failure count. Blacklist if over the limit."""
        self.failed_attempts[device_id] = self.failed_attempts.get(device_id, 0) + 1
        if self.failed_attempts[device_id] >= MAX_AUTH_FAILURES:
            self.blacklist.add(device_id)
            logger.warning("Device %s blacklisted after %d failures", device_id, MAX_AUTH_FAILURES)

    # ── STEP 1 HANDLER ──────────────────────────────────────────────
    def step1_receive_initiation(self, payload: bytes) -> dict:
        """
        Step 1: Server receives Device ID from client (plaintext).
        
        Server:
        1. Checks if device is blacklisted
        2. Checks if device_id is known (has a pre-shared secret)
        3. If valid, returns success so client can proceed to step 2

        In a real system, each device would have its OWN unique λi.
        For this project, all devices share the same PRESHARED_SECRET
        from config — simpler but demonstrates the concept correctly.
        
        Returns dict with "status": "ok" or "error"
        """
        try:
            data = json.loads(payload.decode())
            device_id = data.get("device_id")

            if not device_id:
                return {"status": "error", "reason": "missing device_id"}

            if self._is_blacklisted(device_id):
                logger.warning("Rejected blacklisted device: %s", device_id)
                return {"status": "error", "reason": "blacklisted"}

            # In a real system: look up device_id in a database to get its λi
            # For this project: we accept any device_id and use the shared secret
            logger.info("Step 1: Received initiation from device: %s", device_id)

            # Store a pending session for this device
            self.sessions[device_id] = {"state": "INITIATED"}

            return {"status": "ok", "device_id": device_id}

        except Exception as e:
            logger.error("Step 1 error: %s", e)
            return {"status": "error", "reason": str(e)}

    # ── STEP 2 HANDLER ──────────────────────────────────────────────
    def step2_send_challenge(self, device_id: str) -> bytes:
        """
        Step 2: Server generates and sends challenge to client.
        
        Server:
        1. Generates ηserver (random nonce — prevents replay attacks)
        2. Generates μkey (fresh session key for this session)
        3. Computes ψ = λi XOR μkey  (hides μkey inside λi)
        4. Encrypts (ψ + ηserver) using AES{λi}
        5. Sends encrypted challenge to client
        
        Why XOR first? Hides μkey so even if AES were broken,
        attacker still can't get μkey without knowing λi.
        
        Returns: encrypted challenge bytes
        """
        # Generate fresh nonce and session key for this session
        eta_server = generate_nonce()    # ηserver
        mu_key = generate_session_key()  # μkey

        # ψ = λi XOR μkey
        psi = xor_bytes(PRESHARED_SECRET, mu_key)

        # Store in session state — needed to verify step 3
        self.sessions[device_id].update({
            "eta_server": eta_server,
            "mu_key": mu_key,
            "state": "CHALLENGED"
        })

        # Plaintext to encrypt: ψ (16 bytes) + ηserver (16 bytes) = 32 bytes
        plaintext = psi + eta_server

        # Encrypt with λi (pre-shared secret)
        # AES{λi, (ψ | ηserver)}
        encrypted_challenge = aes_encrypt(PRESHARED_SECRET, plaintext)

        logger.info("Step 2: Sent challenge to device: %s", device_id)
        return encrypted_challenge

    # ── STEP 3 HANDLER ──────────────────────────────────────────────
    def step3_verify_client(self, device_id: str, payload: bytes) -> dict:
        """
        Step 3: Server receives and verifies client's response.
        
        Client sent: AES{μkey, (ηserver XOR λi | ηclient)}
        
        Server:
        1. Decrypts using μkey (proves client successfully got μkey from step 2)
        2. Extracts Y = ηserver XOR λi, recovers ηserver, verifies it matches
        3. Stores ηclient for use in step 4
        
        If decryption works and ηserver matches → client is authenticated.
        Only a real client with correct λi could have computed μkey from step 2.
        
        Returns dict with "status": "ok" or "error"
        """
        try:
            session = self.sessions.get(device_id)
            if not session or session["state"] != "CHALLENGED":
                return {"status": "error", "reason": "invalid session state"}

            mu_key = session["mu_key"]
            eta_server = session["eta_server"]

            # Decrypt client response using μkey
            # AES{μkey, (Y | ηclient)} where Y = ηserver XOR λi
            plaintext = aes_decrypt(mu_key, payload)

            # Extract Y (first 16 bytes) and ηclient (next 16 bytes)
            Y = plaintext[:16]
            eta_client = plaintext[16:32]

            # Recover ηserver from Y: Y XOR λi = (ηserver XOR λi) XOR λi = ηserver
            recovered_eta_server = xor_bytes(Y, PRESHARED_SECRET)

            # Verify: recovered ηserver must match what we sent in step 2
            if recovered_eta_server != eta_server:
                self._record_failure(device_id)
                logger.warning("Step 3: ηserver mismatch for device %s", device_id)
                return {"status": "error", "reason": "nonce mismatch"}

            # Client is verified — store ηclient for step 4
            session.update({
                "eta_client": eta_client,
                "state": "CLIENT_VERIFIED"
            })

            logger.info("Step 3: Client verified successfully: %s", device_id)
            return {"status": "ok"}

        except Exception as e:
            self._record_failure(device_id)
            logger.error("Step 3 error: %s", e)
            return {"status": "error", "reason": str(e)}

    # ── STEP 4 HANDLER ──────────────────────────────────────────────
    def step4_send_response(self, device_id: str) -> bytes:
        """
        Step 4: Server proves its own identity back to the client.
        
        Server:
        1. Takes ηclient (received in step 3)
        2. Encrypts (ηclient + μkey) using AES{λi}
        3. Sends to client
        
        Client will decrypt with λi, find its own ηclient inside —
        only a real server with λi could have produced this.
        
        After this step: MUTUAL authentication is complete.
        Both sides have verified each other. μkey is the shared session key.
        
        Returns: encrypted response bytes
        """
        session = self.sessions[device_id]
        eta_client = session["eta_client"]
        mu_key = session["mu_key"]

        # Encrypt (ηclient + μkey) with λi
        # AES{λi, (ηclient | μkey)}
        plaintext = eta_client + mu_key
        encrypted_response = aes_encrypt(PRESHARED_SECRET, plaintext)

        # Mark session as fully authenticated
        session["state"] = "AUTHENTICATED"
        logger.info("Step 4: Mutual authentication complete for device: %s", device_id)

        return encrypted_response

    def get_session_key(self, device_id: str) -> bytes | None:
        """
        Returns the shared session key μkey for an authenticated device.
        Used by the CoAP server to encrypt/decrypt subsequent messages.
        Returns None if device is not authenticated.
        """
        session = self.sessions.get(device_id)
        if session and session.get("state") == "AUTHENTICATED":
            return session["mu_key"]
        return None

    def is_authenticated(self, device_id: str) -> bool:
        session = self.sessions.get(device_id)
        return session is not None and session.get("state") == "AUTHENTICATED"


# ─────────────────────────────────────────────
# CLIENT SIDE
# ─────────────────────────────────────────────

class AuthClient:
    """
    Runs on the client side.
    Performs the 4-step handshake with the server.
    After completion, holds the shared session key μkey
    which is used to encrypt all subsequent CoAP messages.
    """

    def __init__(self):
        self.mu_key = None        # shared session key — set after auth
        self.eta_client = None    # client nonce — generated in step 3
        self.authenticated = False

    # ── STEP 1 ───────────────────────────────────────────────────────
    def step1_initiate(self) -> bytes:
        """
        Step 1: Client sends its Device ID to the server (plaintext).
        This just identifies who the client is.
        Knowing the Device ID alone is not enough — attacker still
        can't pass step 3 without knowing λi.
        
        Returns: JSON payload bytes to send via CoAP POST
        """
        payload = json.dumps({"device_id": DEVICE_ID}).encode()
        logger.info("Step 1: Sending initiation with device_id: %s", DEVICE_ID)
        return payload

    # ── STEP 2 ───────────────────────────────────────────────────────
    def step2_process_challenge(self, encrypted_challenge: bytes) -> bool:
        """
        Step 2: Client receives and decrypts server's challenge.
        
        Client:
        1. Decrypts using λi → recovers ψ and ηserver
        2. Recovers μkey = ψ XOR λi  (reverses the XOR from step 2)
        3. Stores both μkey and ηserver for use in step 3
        
        If decryption succeeds → server knows λi → server is who it claims.
        
        Returns: True if successful, False if decryption failed
        """
        try:
            # Decrypt server challenge using λi
            plaintext = aes_decrypt(PRESHARED_SECRET, encrypted_challenge)

            # Extract ψ (first 16 bytes) and ηserver (next 16 bytes)
            psi = plaintext[:16]
            eta_server = plaintext[16:32]

            # Recover μkey: ψ XOR λi = (λi XOR μkey) XOR λi = μkey
            self.mu_key = xor_bytes(psi, PRESHARED_SECRET)
            self.eta_server = eta_server

            logger.info("Step 2: Challenge decrypted, μkey recovered")
            return True

        except Exception as e:
            logger.error("Step 2 failed: %s", e)
            return False

    # ── STEP 3 ───────────────────────────────────────────────────────
    def step3_respond_and_challenge(self) -> bytes:
        """
        Step 3: Client proves its identity AND challenges the server.
        
        Client:
        1. Computes Y = ηserver XOR λi
        2. Generates fresh ηclient nonce
        3. Encrypts (Y | ηclient) using μkey
        4. Sends to server
        
        Server will:
        - Decrypt with μkey (only works if client got μkey right in step 2)
        - Recover ηserver from Y (only works if client knows λi)
        - Use ηclient in step 4 to prove its own identity back
        
        Returns: encrypted response bytes to send via CoAP POST
        """
        # Y = ηserver XOR λi
        Y = xor_bytes(self.eta_server, PRESHARED_SECRET)

        # Generate fresh client nonce
        self.eta_client = generate_nonce()

        # Encrypt (Y | ηclient) with μkey
        plaintext = Y + self.eta_client
        encrypted_response = aes_encrypt(self.mu_key, plaintext)

        logger.info("Step 3: Sent client response and challenge")
        return encrypted_response

    # ── STEP 4 ───────────────────────────────────────────────────────
    def step4_verify_server(self, encrypted_response: bytes) -> bool:
        """
        Step 4: Client verifies server's response.
        
        Client:
        1. Decrypts using λi → recovers ηclient and μkey
        2. Checks that ηclient matches what it sent in step 3
        3. Checks that μkey matches what it recovered in step 2
        
        If both match → server knows λi AND μkey → server is authenticated.
        MUTUAL AUTHENTICATION COMPLETE.
        
        Returns: True if server verified, False otherwise
        """
        try:
            # Decrypt with λi
            plaintext = aes_decrypt(PRESHARED_SECRET, encrypted_response)

            # Extract ηclient (first 16 bytes) and μkey (next 16 bytes)
            recovered_eta_client = plaintext[:16]
            recovered_mu_key = plaintext[16:32]

            # Verify ηclient matches what we sent in step 3
            if recovered_eta_client != self.eta_client:
                logger.error("Step 4: ηclient mismatch — server not verified")
                return False

            # Verify μkey matches what we recovered in step 2
            if recovered_mu_key != self.mu_key:
                logger.error("Step 4: μkey mismatch — server not verified")
                return False

            self.authenticated = True
            logger.info("Step 4: Server verified. Mutual authentication complete.")
            return True

        except Exception as e:
            logger.error("Step 4 failed: %s", e)
            return False