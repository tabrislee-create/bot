import re

with open("bot.py", "r") as f:
    content = f.read()

old = """        rows = conn.execute(\"\"\"
            SELECT id, symbol, direction, reason, current_qty, target_qty,
                   delta_qty, price, delta_value, executed, ts
            FROM decision_log
            WHERE date(ts) = date('now', '+8 hours')
            ORDER BY direction, symbol
        \"\"\").fetchall()
        if not rows:
            last = conn.execute(
                \"SELECT date(ts) FROM decision_log ORDER BY ts DESC LIMIT 1\"
            ).fetchone()
            if not last:
                await update.message.reply_text(\"📭 decision_log 尚無資料\")
                return
            last_date = last[0]
            rows = conn.execute(\"\"\"
                SELECT id, symbol, direction, reason, current_qty, target_qty,
                       delta_qty, price, delta_value, executed, ts
                FROM decision_log
                WHERE date(ts) = ?
                ORDER BY direction, symbol
            \"\"\", (last_date,)).fetchall()
            date_label = last_date
        else:
            date_label = datetime.now().strftime(\"%Y-%m-%d\")"""

new = """        last = conn.execute(
                \"SELECT date(ts) FROM decision_log ORDER BY ts DESC LIMIT 1\"
            ).fetchone()
        if not last:
            await update.message.reply_text(\"📭 decision_log 尚無資料\")
            return
        last_date = last[0]
        date_label = last_date
        rows = conn.execute(\"\"\"
            SELECT id, symbol, direction, reason, current_qty, target_qty,
                   delta_qty, price, delta_value, executed, ts
            FROM decision_log
            WHERE date(ts) = ?
            ORDER BY direction, symbol
        \"\"\", (last_date,)).fetchall()"""

content = content.replace(old, new)
with open("bot.py", "w") as f:
    f.write(content)
print("done")
