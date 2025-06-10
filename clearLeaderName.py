import sqlite3
def clear_leaderboard():
    db_path = "plugins/questions.db"
    try:
        with sqlite3.connect(db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM leaderboard")
            conn.commit()
            # logger.info("Leaderboard cleared successfully.")
    except Exception as e:
        pass
        # logger.error(f"Error clearing leaderboard: {str(e)}")

clear_leaderboard()