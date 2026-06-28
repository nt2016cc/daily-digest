import os
os.chdir('/tmp/daily-digest-check')
lines = open('digest.py').readlines()

new_main_start = [
    'def main():\n',
    '    preview = "--preview" in sys.argv\n',
    '\n',
    '    # --- Expiration check ---\n',
    '    expire_dt = datetime.strptime(EXPIRE_DATE, "%Y-%m-%d").replace(tzinfo=timezone.utc)\n',
    '    if datetime.now(timezone.utc) > expire_dt and not preview:\n',
    '        print(f"Task expired on {EXPIRE_DATE} — frozen. Use --preview to still test.")\n',
    '        return\n',
    '\n',
]

start = None
end = None
for i, line in enumerate(lines):
    if line.strip() == 'def main():':
        start = i
    elif start is not None and 'cutoff = datetime' in line:
        end = i
        break

if start is not None and end is not None:
    new_lines = lines[:start] + new_main_start + lines[end:]
    open('digest.py', 'w').writelines(new_lines)
    print('OK')
else:
    print(f'start={start}, end={end}')
