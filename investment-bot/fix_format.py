import re

with open("bot.py", "r") as f:
    content = f.read()

# 移除所有 HTML tag
replacements = [
    ('<b>', ''),
    ('</b>', ''),
    ('&lt;', '<'),
    ('&gt;', '>'),
]
for old, new in replacements:
    content = content.replace(old, new)

with open("bot.py", "w") as f:
    f.write(content)
print("done")
