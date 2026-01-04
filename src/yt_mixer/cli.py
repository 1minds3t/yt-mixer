#!/usr/bin/env python3
"""
YT Mixer CLI - Command-line interface for managing the audio mixer
"""
import sys
import os
import argparse
import subprocess
import shutil
from pathlib import Path

# Import config but NOT routes (which creates manager instance)
from .config import config, DATA_DIR, CHUNK_DIR, AUDIO_DIR

def cmd_config(args):
    """Manage configuration"""
    # If no action specified, show help
    if not (args.list or args.set or args.get):
        import argparse
        parser = argparse.ArgumentParser(prog='yt-mixer config')
        parser.add_argument('--list', action='store_true', help='List all config values')
        parser.add_argument('--get', metavar='KEY', help='Get a config value')
        parser.add_argument('--set', metavar='KEY=VALUE', help='Set a config value')
        parser.print_help()
        return 1
    
    if args.list:
        print("Current Configuration:")
        print("-" * 40)
        for key, value in config.settings.items():
            print(f"{key}: {value}")
        return
    
    if args.set:
        key, value = args.set.split('=', 1)
        # Try to convert to appropriate type
        try:
            if value.lower() in ('true', 'false'):
                value = value.lower() == 'true'
            elif value.replace('.', '').isdigit():
                value = float(value) if '.' in value else int(value)
        except:
            pass
        
        if config.set(key, value):
            print(f"✓ Set {key} = {value}")
        else:
            print(f"✗ Failed to save config")
            return 1
    
    if args.get:
        value = config.get(args.get)
        if value is not None:
            print(f"{args.get} = {value}")
        else:
            print(f"Key '{args.get}' not found")
            return 1

def cmd_sessions(args):
    """Manage sessions"""
    # If no action specified, just list
    if not args.clean:
        sessions = list(CHUNK_DIR.glob('*'))
        
        if not sessions:
            print("No active sessions found")
            return
        
        print(f"Active Sessions ({len(sessions)}):")
        print("-" * 60)
        
        for session_dir in sessions:
            if session_dir.is_dir():
                chunks = list(session_dir.glob('*.mp3'))
                size_mb = sum(f.stat().st_size for f in chunks) / (1024 * 1024)
                print(f"  {session_dir.name}: {len(chunks)} chunks ({size_mb:.1f} MB)")
        return
    
    # Clean action
    sessions = list(CHUNK_DIR.glob('*'))
    
    if not sessions:
        print("No active sessions found")
        return
    
    print(f"Active Sessions ({len(sessions)}):")
    print("-" * 60)
    
    for session_dir in sessions:
        if session_dir.is_dir():
            chunks = list(session_dir.glob('*.mp3'))
            size_mb = sum(f.stat().st_size for f in chunks) / (1024 * 1024)
            print(f"  {session_dir.name}: {len(chunks)} chunks ({size_mb:.1f} MB)")
    
    if args.clean:
        confirm = input("\nDelete all sessions? [y/N]: ")
        if confirm.lower() == 'y':
            for session_dir in sessions:
                if session_dir.is_dir():
                    shutil.rmtree(session_dir)
                    print(f"✓ Deleted {session_dir.name}")
            # Also clean audio cache
            audio_sessions = list(AUDIO_DIR.glob('*'))
            for audio_dir in audio_sessions:
                if audio_dir.is_dir():
                    shutil.rmtree(audio_dir)
            print("✓ Cleaned all session data")

def cmd_service(args):
    """Manage systemd service"""
    # If no action specified, show help
    if not any([args.status, args.start, args.stop, args.restart, 
                args.enable, args.disable, args.install, args.logs]):
        import argparse
        parser = argparse.ArgumentParser(prog='yt-mixer service')
        parser.add_argument('--install', action='store_true', help='Install systemd service')
        parser.add_argument('--status', action='store_true', help='Show service status')
        parser.add_argument('--start', action='store_true', help='Start service')
        parser.add_argument('--stop', action='store_true', help='Stop service')
        parser.add_argument('--restart', action='store_true', help='Restart service')
        parser.add_argument('--enable', action='store_true', help='Enable service on boot')
        parser.add_argument('--disable', action='store_true', help='Disable service on boot')
        parser.add_argument('--logs', action='store_true', help='Follow service logs')
        parser.print_help()
        return 1
    
    service_name = "yt-mixer.service"
    
    if args.status:
        subprocess.run(["systemctl", "--user", "status", service_name])
    elif args.start:
        subprocess.run(["systemctl", "--user", "start", service_name])
        print("✓ Service started")
    elif args.stop:
        subprocess.run(["systemctl", "--user", "stop", service_name])
        print("✓ Service stopped")
    elif args.restart:
        subprocess.run(["systemctl", "--user", "restart", service_name])
        print("✓ Service restarted")
    elif args.enable:
        subprocess.run(["systemctl", "--user", "enable", service_name])
        print("✓ Service enabled (will start on boot)")
    elif args.disable:
        subprocess.run(["systemctl", "--user", "disable", service_name])
        print("✓ Service disabled")
    elif args.install:
        install_systemd_service()
    elif args.logs:
        # Get the actual log file path from config
        from .session_manager import manager
        log_file = manager.log_file
        
        if not log_file.exists():
            print(f"Log file not found: {log_file}")
            return 1
        
        print(f"Following logs from: {log_file}")
        
        try:
            process = subprocess.Popen(
                ['tail', '-f', '-n', '50', str(log_file)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            
            for line in iter(process.stdout.readline, ''):
                sys.stdout.write(line)
                sys.stdout.flush()
                
        except KeyboardInterrupt:
            if 'process' in locals():
                process.terminate()
            print("\nStopped following logs.")
            sys.exit(0)

def install_systemd_service():
    """Install systemd user service"""
    import sys
    from pathlib import Path
    
    # Get the Python executable path
    python_path = sys.executable
    
    # Get the package location
    import yt_mixer
    pkg_path = Path(yt_mixer.__file__).parent.parent.parent
    
    service_content = f"""[Unit]
Description=YT Mixer - Audio Playlist Mixer
After=network-online.target

[Service]
Type=simple
WorkingDirectory={pkg_path}
ExecStart={python_path} -m yt_mixer.routes
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal
Environment="YT_MIXER_HOST={config.get('host')}"
Environment="YT_MIXER_PORT={config.get('port')}"
Environment="YT_MIXER_DATA_DIR={DATA_DIR}"

[Install]
WantedBy=default.target
"""
    
    service_dir = Path.home() / ".config" / "systemd" / "user"
    service_dir.mkdir(parents=True, exist_ok=True)
    service_file = service_dir / "yt-mixer.service"
    
    with open(service_file, 'w') as f:
        f.write(service_content)
    
    print(f"✓ Service file created: {service_file}")
    
    # Also create the timer for yt-dlp updates
    timer_content = """[Unit]
Description=Update yt-dlp every 3 hours

[Timer]
OnBootSec=5min
OnUnitActiveSec=3h

[Install]
WantedBy=timers.target
"""
    
    update_service_content = f"""[Unit]
Description=Update yt-dlp

[Service]
Type=oneshot
ExecStart={python_path} -m pip install --no-cache-dir --upgrade yt-dlp
"""
    
    timer_file = service_dir / "yt-dlp-update.timer"
    update_file = service_dir / "yt-dlp-update.service"
    
    with open(timer_file, 'w') as f:
        f.write(timer_content)
    with open(update_file, 'w') as f:
        f.write(update_service_content)
    
    print(f"✓ Update timer created: {timer_file}")
    
    # Reload systemd
    subprocess.run(["systemctl", "--user", "daemon-reload"])
    
    print("\nTo start the service:")
    print(f"  systemctl --user start yt-mixer")
    print(f"  systemctl --user enable yt-mixer  # Start on boot")
    print(f"\nTo enable auto-updates:")
    print(f"  systemctl --user enable --now yt-dlp-update.timer")

def cmd_stop(args):
    """Stop the background server"""
    pid_file = DATA_DIR / "yt-mixer.pid"
    
    if not pid_file.exists():
        print("✗ YT Mixer is not running (no PID file found)")
        return 1
    
    try:
        with open(pid_file, 'r') as f:
            pid = int(f.read().strip())
        
        # Try to kill the process
        import os
        import signal
        import time
        
        print(f"Stopping YT Mixer (PID: {pid})...")
        
        try:
            # Send SIGTERM first (graceful)
            os.kill(pid, signal.SIGTERM)
            
            # Wait up to 10 seconds for graceful shutdown
            for _ in range(10):
                time.sleep(1)
                try:
                    os.kill(pid, 0)  # Check if still alive
                except OSError:
                    break
            else:
                # Still alive, force kill
                print("Process didn't stop gracefully, forcing...")
                os.kill(pid, signal.SIGKILL)
            
            print("✓ YT Mixer stopped")
            
        except OSError as e:
            if e.errno == 3:  # No such process
                print("✓ Process already stopped")
            else:
                raise
        
        # Remove PID file
        pid_file.unlink()
        
    except Exception as e:
        print(f"✗ Error stopping YT Mixer: {e}")
        return 1

def cmd_status_daemon(args):
    """Check if the background server is running"""
    pid_file = DATA_DIR / "yt-mixer.pid"
    
    if not pid_file.exists():
        print("Status: Not running")
        return
    
    try:
        with open(pid_file, 'r') as f:
            pid = int(f.read().strip())
        
        import os
        try:
            os.kill(pid, 0)  # Check if process exists
            print(f"Status: Running (PID: {pid})")
            print(f"Config: http://{config.get('host')}:{config.get('port')}")
            print(f"Logs: {DATA_DIR}/yt-mixer.log")
        except OSError:
            print(f"Status: Stale PID file (process {pid} not found)")
            pid_file.unlink()
    except Exception as e:
        print(f"✗ Error checking status: {e}")

def cmd_update(args):
    """Update yt-dlp"""
    print("Updating yt-dlp...")
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--no-cache-dir", "--upgrade", "yt-dlp"],
        capture_output=True,
        text=True
    )
    
    if result.returncode == 0:
        print("✓ yt-dlp updated successfully")
    else:
        print(f"✗ Update failed: {result.stderr}")
        return 1

def cmd_logs(args):
    """View or follow logs"""
    log_file = DATA_DIR / "yt-mixer.log"
    
    if not log_file.exists():
        print("No log file found")
        return 1
    
    if args.follow:
        # Robustly follow logs using tail -f subprocess
        print(f"=== Following logs from {log_file} (Ctrl+C to stop) ===")
        try:
            # Popen allows us to read the output line by line
            process = subprocess.Popen(
                ['tail', '-f', '-n', '50', str(log_file)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            # Read from stdout in a blocking manner, which is what we want
            for line in iter(process.stdout.readline, ''):
                sys.stdout.write(line)
                sys.stdout.flush()

        except KeyboardInterrupt:
            print("\nStopped following logs.")
        except Exception as e:
            print(f"\nError following logs: {e}")
        finally:
            if 'process' in locals() and process.poll() is None:
                process.terminate() # Ensure tail process is killed
    else:
        # Show last N lines (this part was fine)
        lines = args.lines or 50
        subprocess.run(['tail', f'-n{lines}', str(log_file)])

def cmd_serve(args):
    """Start the web server directly"""
    import os
    import signal
    from .port_finder import get_available_port, release_port
    
    # Get preferred port from args or config
    preferred_port = args.port or config.get('port', 5052)
    host = args.host or config.get('host', '0.0.0.0')
    
    # Find actually available port
    try:
        port = get_available_port(preferred_port=preferred_port, start_range=5000)
        if port != preferred_port:
            print(f"⚠️  Port {preferred_port} in use, using {port} instead")
            # Update config with working port
            config.set('port', port)
    except RuntimeError as e:
        print(f"✗ Could not find available port: {e}")
        return 1
    
    # Daemonize if requested
    if args.daemon:
        pid_file = DATA_DIR / "yt-mixer.pid"
        log_file = DATA_DIR / "yt-mixer.log"
        
        # Check if already running
        if pid_file.exists():
            try:
                with open(pid_file, 'r') as f:
                    old_pid = int(f.read().strip())
                    # Check if process is still alive
                    os.kill(old_pid, 0)
                    print(f"✗ YT Mixer already running (PID: {old_pid})")
                    print(f"  Stop it with: yt-mixer stop")
                    return 1
            except (OSError, ValueError):
                # Process doesn't exist, remove stale pid file
                pid_file.unlink()
        
        # Fork to background
        try:
            pid = os.fork()
        except OSError as e:
            print(f"✗ Fork failed: {e}")
            return 1
            
        if pid > 0:
            # Parent process - wait a moment to see if child survives
            import time
            time.sleep(1)
            
            # Check if child is still alive
            try:
                os.kill(pid, 0)
                # Child is alive
                with open(pid_file, 'w') as f:
                    f.write(str(pid))
                print(f"✓ YT Mixer started in background (PID: {pid})")
                print(f"  Access at: http://{host}:{port}")
                print(f"  Logs: {log_file}")
                print(f"  Stop with: yt-mixer stop")
                return 0
            except OSError:
                print(f"✗ Child process died immediately, check logs: {log_file}")
                return 1
        
        # Child process continues here
        # Create new session
        os.setsid()
        
        # Second fork to prevent zombie
        try:
            pid = os.fork()
        except OSError as e:
            sys.exit(1)
            
        if pid > 0:
            # First child exits
            sys.exit(0)
        
        # Second child - this is the daemon
        # Change working directory
        os.chdir('/')
        
        # Redirect stdin to /dev/null
        sys.stdin.close()
        sys.stdin = open('/dev/null', 'r')
        
        # Redirect stdout/stderr to log file
        log_fd = open(str(log_file), 'a')
        sys.stdout = log_fd
        sys.stderr = log_fd
        
        # Configure logging to use the log file
        import logging
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            handlers=[logging.StreamHandler(log_fd)]
        )
        
        print(f"=== YT Mixer Daemon Starting ===")
        print(f"PID: {os.getpid()}")
        print(f"Host: {host}:{port}")
        print(f"Data: {DATA_DIR}")
        
    else:
        print(f"Starting YT Mixer on http://{host}:{port}")
        print(f"Data directory: {DATA_DIR}")
        print("Press Ctrl+C to stop")
        
        # Configure logging for foreground
        import logging
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )
    
    # Import here to avoid circular imports and ensure logging is configured
    # This import will create the manager instance
    from .routes import app, manager
    
    # Start maintenance thread only once
    manager.start_maintenance()
    
    # Setup signal handlers
    def signal_handler(sig, frame):
        if not args.daemon:
            print("\nShutting down gracefully...")
        else:
            print("Received shutdown signal, stopping...")
        manager.shutdown()
        release_port(port)  # Release the reserved port
        import os
        os._exit(0)  # Force exit
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    try:
        app.run(host=host, port=port, debug=args.debug, use_reloader=False, threaded=True)
    except Exception as e:
        print(f"Error starting server: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

def main():
    parser = argparse.ArgumentParser(
        prog='yt-mixer',
        description='YouTube Audio Mixer with server-side processing'
    )
    
    subparsers = parser.add_subparsers(dest='command', help='Commands')
    
    # Config command
    config_parser = subparsers.add_parser('config', help='Manage configuration')
    config_parser.add_argument('--list', action='store_true', help='List all config values')
    config_parser.add_argument('--get', metavar='KEY', help='Get a config value')
    config_parser.add_argument('--set', metavar='KEY=VALUE', help='Set a config value')
    config_parser.set_defaults(func=cmd_config)
    
    # Sessions command
    sessions_parser = subparsers.add_parser('sessions', help='Manage sessions')
    sessions_parser.add_argument('--clean', action='store_true', help='Delete all sessions')
    sessions_parser.set_defaults(func=cmd_sessions)
    
    # Service command
    service_parser = subparsers.add_parser('service', help='Manage systemd service')
    service_parser.add_argument('--install', action='store_true', help='Install systemd service')
    service_parser.add_argument('--status', action='store_true', help='Show service status')
    service_parser.add_argument('--start', action='store_true', help='Start service')
    service_parser.add_argument('--stop', action='store_true', help='Stop service')
    service_parser.add_argument('--restart', action='store_true', help='Restart service')
    service_parser.add_argument('--enable', action='store_true', help='Enable service on boot')
    service_parser.add_argument('--disable', action='store_true', help='Disable service on boot')
    service_parser.add_argument('--logs', action='store_true', help='Follow service logs')
    service_parser.set_defaults(func=cmd_service)
    
    # Update command
    update_parser = subparsers.add_parser('update', help='Update yt-dlp')
    update_parser.set_defaults(func=cmd_update)
    
    # Serve command
    serve_parser = subparsers.add_parser('serve', help='Start the web server')
    serve_parser.add_argument('--host', help='Host to bind to')
    serve_parser.add_argument('--port', type=int, help='Port to bind to')
    serve_parser.add_argument('--debug', action='store_true', help='Enable debug mode')
    serve_parser.add_argument('-d', '--daemon', action='store_true', help='Run in background')
    serve_parser.set_defaults(func=cmd_serve)
    
    # Stop command
    stop_parser = subparsers.add_parser('stop', help='Stop the background server')
    stop_parser.set_defaults(func=cmd_stop)
    
    # Status command
    status_parser = subparsers.add_parser('status', help='Check server status')
    status_parser.set_defaults(func=cmd_status_daemon)
    
    # Logs command
    logs_parser = subparsers.add_parser('logs', help='View server logs')
    logs_parser.add_argument('-f', '--follow', action='store_true', help='Follow log output')
    logs_parser.add_argument('-n', '--lines', type=int, help='Number of lines to show (default: 50)')
    logs_parser.set_defaults(func=cmd_logs)
    
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        return 1
    
    return args.func(args) or 0

if __name__ == '__main__':
    sys.exit(main())