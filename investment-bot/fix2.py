with open("bot.py", "r") as f:
    content = f.read()

old = """        rows = conn.execute(\"\"\"
            SELECT id, symbol, direction, reason, current_qty, target_qty,
                   delta_qty, price, delta_value, executed, ts
            FROM decision_log
            WHERE date(ts) = ?
            ORDER BY direction, symbol
        \"\"\", (last_date,)).fetchall()"""

new = """        rows = conn.execute(\"\"\"
            SELECT id, symbol, direction, reason, current_qty, target_qty,
                   delta_qty, price, delta_value, executed, ts
            FROM decision_log
            WHERE date(ts) = ?
              AND id IN (
                SELECT MAX(id) FROM decision_log
                WHERE date(ts) = ?
                GROUP BY symbol
              )
            ORDER BY direction, symbol
        \"\"\", (last_date, last_date)).fetchall()"""

content = content.replace(old, new)
with open("bot.py", "w") as f:
    f.write(content)
print("done")
