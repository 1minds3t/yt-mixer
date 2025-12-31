from yt_mixer.routes import app, manager

# This hook ensures the session manager starts when the app runs
if __name__ == "__main__":
    # Start the maintenance thread manually if not already started by import
    if not manager.cleaner_thread.is_alive():
        manager.cleaner_thread.start()
        
    app.run(host="0.0.0.0", port=5052, debug=True, use_reloader=False)