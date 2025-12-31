"""
Port Finder - Concurrent-safe port allocation for Flask/Werkzeug apps

Simplified version from omnipkg.utils.flask_port_finder
"""
import socket
import threading
from contextlib import closing
from typing import Set

# Global port reservation system (thread-safe)
_port_lock = threading.Lock()
_reserved_ports: Set[int] = set()


def is_port_free(port: int) -> bool:
    """
    Check if a port is actually free.
    """
    try:
        with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", port))
            return True
    except OSError:
        return False


def reserve_port(port: int) -> bool:
    """
    Reserve a port to prevent concurrent allocation race conditions.
    Returns True if successfully reserved, False if already reserved.
    """
    with _port_lock:
        if port in _reserved_ports:
            return False
        _reserved_ports.add(port)
        return True


def release_port(port: int):
    """Release a reserved port."""
    with _port_lock:
        _reserved_ports.discard(port)


def find_free_port(start_port: int = 5000, max_attempts: int = 100, reserve: bool = True) -> int:
    """
    Find an available port with concurrent safety.
    
    Args:
        start_port: Port to start searching from
        max_attempts: Maximum number of ports to try
        reserve: Whether to reserve the port (prevents race conditions)
    
    Returns:
        An available port number
    
    Raises:
        RuntimeError: If no free port found in range
    """
    for port in range(start_port, start_port + max_attempts):
        # Check if already reserved
        with _port_lock:
            if port in _reserved_ports:
                continue
        
        # Check if actually free
        if not is_port_free(port):
            continue
        
        # Reserve if requested
        if reserve:
            if not reserve_port(port):
                continue
        
        return port
    
    raise RuntimeError(
        f"Could not find free port in range {start_port}-{start_port + max_attempts}"
    )


def get_available_port(preferred_port: int = None, start_range: int = 5000) -> int:
    """
    Get an available port, preferring the specified port if free.
    
    Args:
        preferred_port: Port to try first (if provided)
        start_range: Where to start searching if preferred port is taken
    
    Returns:
        An available port number
    """
    if preferred_port and is_port_free(preferred_port):
        if reserve_port(preferred_port):
            return preferred_port
    
    return find_free_port(start_port=start_range, reserve=True)