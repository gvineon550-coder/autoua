import re
from pathlib import Path

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36"
OPT_LINE = f"#EXTVLCOPT:http-user-agent={UA}\n"
URL_PATTERN = re.compile(r'^https?://')


def process_file(file_path: Path):
    with open(file_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    new_lines = []
    inserted = 0

    for line in lines:
        stripped = line.strip()
        if URL_PATTERN.match(stripped):
            if not new_lines or not new_lines[-1].startswith('#EXTVLCOPT:http-user-agent='):
                new_lines.append(OPT_LINE)
                inserted += 1
        new_lines.append(line)

    if inserted:
        with open(file_path, 'w', encoding='utf-8') as f:
            f.writelines(new_lines)
        print(f"✅ {file_path} — добавлено {inserted}")
    else:
        print(f"⏭️ {file_path} — уже актуально")


def main():
    for ext in ('*.m3u', '*.m3u8'):
        for file_path in Path('.').rglob(ext):
            process_file(file_path)


if __name__ == '__main__':
    main()
